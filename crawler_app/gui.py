"""PySide6 화면. 브라우저 작업은 Runner(별도 스레드)에 요청만 하고 직접 실행하지 않는다."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .config import AppConfig
from .models import STATE_LABELS, default_result_path, save_job_json
from .runner import Runner
from .sites import SITES

logger = logging.getLogger(__name__)

RESULT_COLUMNS = ["출처", "제목", "언론사", "날짜 원문", "추출 방식", "본문 상태"]
BODY_LABELS = {
    "not_requested": "요청 안 함", "pending": "미처리", "processing": "처리 중",
    "extracted": "후보 추출", "failed": "실패", "blocked": "접근 제한",
    "skipped": "건너뜀", "cancelled": "취소됨",
}


class LogBridge(QObject):
    message = Signal(str)


class QtLogHandler(logging.Handler):
    """어느 스레드의 로그든 시그널로 GUI에 전달한다."""

    def __init__(self, bridge: LogBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._bridge.message.emit(self.format(record))
        except Exception:
            self.handleError(record)


def _cell(text: Any) -> QTableWidgetItem:
    item = QTableWidgetItem("" if text is None else str(text))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, runner: Runner, log_bridge: LogBridge) -> None:
        super().__init__()
        self.config = config
        self.runner = runner
        self._items: list[dict[str, Any]] = []
        self._last_job: dict[str, Any] | None = None
        self._busy = False
        self._waiting = False
        self._site_rows: dict[str, int] = {}
        self._closing = False

        self.setWindowTitle("웹 검색 자동화 도우미 (MVP)")
        self.resize(1200, 800)
        self._build_ui()

        q = Qt.ConnectionType.QueuedConnection
        runner.site_state.connect(self._on_site_state, q)
        runner.site_finished.connect(self._on_site_finished, q)
        runner.job_finished.connect(self._on_job_finished, q)
        runner.busy_changed.connect(self._on_busy_changed, q)
        runner.waiting_changed.connect(self._on_waiting_changed, q)
        log_bridge.message.connect(self._append_log, q)
        self._update_buttons()

    # ---------- 화면 구성 ----------
    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        top = QHBoxLayout()
        top.addWidget(QLabel("검색어"))
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("검색할 주제를 입력하세요")
        self.query_edit.returnPressed.connect(self._on_run)
        top.addWidget(self.query_edit, 1)
        self.site_checks: dict[str, QCheckBox] = {}
        for site_id, module in SITES.items():
            box = QCheckBox(module.DISPLAY_NAME)
            box.setChecked(self.config.enabled_sites.get(site_id, True))
            self.site_checks[site_id] = box
            top.addWidget(box)
        self.run_btn = QPushButton("실행")
        self.stop_btn = QPushButton("중지")
        self.continue_btn = QPushButton("계속")
        self.continue_btn.setToolTip("브라우저에서 직접 확인·조작한 뒤 누르면 화면 상태를 다시 확인하고 재개합니다")
        self.save_btn = QPushButton("JSON 저장")
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)
        self.continue_btn.clicked.connect(self._on_continue)
        self.save_btn.clicked.connect(self._on_save)
        for btn in (self.run_btn, self.stop_btn, self.continue_btn, self.save_btn):
            top.addWidget(btn)
        layout.addLayout(top)

        self.body_check = QCheckBox("본문 추출 (공개 원문만, 한 건씩)")
        self.body_check.setChecked(False)
        self.body_check.setToolTip(
            "체크하지 않으면 기존 검색 결과만 수집합니다. 언론사별 접근 조건을 확인하세요. "
            "제한·유료벽은 우회하지 않으며 본문 완전성은 직접 확인해야 합니다."
        )
        layout.addWidget(self.body_check)

        info = QLabel(
            f"사이트별 최대 {self.config.max_results_per_site}건 · 동시 실행 {self.config.max_concurrency} · "
            "정렬·기간 미지정(사이트 기본) · 본문은 후보 추출, 키워드·요약은 미구현"
        )
        info.setStyleSheet("color: gray;")
        layout.addWidget(info)

        self.site_table = QTableWidget(len(SITES), 3)
        self.site_table.setHorizontalHeaderLabels(["사이트", "상태", "메시지"])
        self.site_table.verticalHeader().setVisible(False)
        self.site_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.site_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, (site_id, module) in enumerate(SITES.items()):
            self._site_rows[site_id] = row
            self.site_table.setItem(row, 0, _cell(module.DISPLAY_NAME))
            self.site_table.setItem(row, 1, _cell(STATE_LABELS["idle"]))
            self.site_table.setItem(row, 2, _cell(""))
        self.site_table.setMaximumHeight(36 + 30 * len(SITES))
        layout.addWidget(self.site_table)

        splitter = QSplitter(Qt.Orientation.Vertical)
        middle = QSplitter(Qt.Orientation.Horizontal)
        self.result_table = QTableWidget(0, len(RESULT_COLUMNS))
        self.result_table.setHorizontalHeaderLabels(RESULT_COLUMNS)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.result_table.itemSelectionChanged.connect(self._on_result_selected)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("결과를 선택하면 상세 내용이 표시됩니다")
        middle.addWidget(self.result_table)
        middle.addWidget(self.detail)
        middle.setSizes([750, 450])
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        splitter.addWidget(middle)
        splitter.addWidget(self.log_view)
        splitter.setSizes([520, 200])
        layout.addWidget(splitter, 1)

        self.setCentralWidget(root)
        self.statusBar().showMessage("준비")

    def _update_buttons(self) -> None:
        self.run_btn.setEnabled(not self._busy)
        self.stop_btn.setEnabled(self._busy)
        self.continue_btn.setEnabled(self._busy and self._waiting)
        self.save_btn.setEnabled(not self._busy and self._last_job is not None)
        self.query_edit.setEnabled(not self._busy)
        self.body_check.setEnabled(not self._busy)
        for box in self.site_checks.values():
            box.setEnabled(not self._busy)

    # ---------- 사용자 조작 ----------
    @Slot()
    def _on_run(self) -> None:
        if self._busy:
            return
        site_ids = [sid for sid, box in self.site_checks.items() if box.isChecked()]
        query = self.query_edit.text()
        ok, message = self.runner.submit_job(query, site_ids, self.body_check.isChecked())
        if not ok:
            QMessageBox.information(self, "실행할 수 없음", message)
            return
        self._items.clear()
        self._last_job = None
        self.result_table.setRowCount(0)
        self.detail.clear()
        for site_id, row in self._site_rows.items():
            state = "queued" if site_id in site_ids else "idle"
            self.site_table.setItem(row, 1, _cell(STATE_LABELS[state]))
            self.site_table.setItem(row, 2, _cell("" if site_id in site_ids else "선택 안 함"))
        self.statusBar().showMessage("실행 중")

    @Slot()
    def _on_stop(self) -> None:
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("중지 요청됨 - 실행부가 브라우저를 정리하는 중입니다")
        self.runner.request_cancel()

    @Slot()
    def _on_continue(self) -> None:
        self.continue_btn.setEnabled(False)
        self.runner.request_continue()

    @Slot()
    def _on_save(self) -> None:
        if self._last_job is None:
            return
        default = default_result_path(self.config.results_dir, self._last_job.get("query", ""))
        default.parent.mkdir(parents=True, exist_ok=True)
        # 기존 파일 선택 시 대화상자가 덮어쓰기 확인을 묻는다.
        path, _ = QFileDialog.getSaveFileName(self, "결과 JSON 저장", str(default), "JSON (*.json)")
        if not path:
            return
        try:
            saved = save_job_json(self._last_job, Path(path), overwrite=True)
        except OSError as exc:
            QMessageBox.warning(self, "저장 실패", str(exc))
            logger.error("결과 저장 실패: %s", exc)
            return
        logger.info("결과 저장: %s", saved)
        self.statusBar().showMessage(f"저장됨: {saved}")

    # ---------- 실행부 시그널 ----------
    @Slot(str, str, str)
    def _on_site_state(self, site_id: str, state: str, message: str) -> None:
        row = self._site_rows.get(site_id)
        if row is None:
            return
        self.site_table.setItem(row, 1, _cell(STATE_LABELS.get(state, state)))
        self.site_table.setItem(row, 2, _cell(message))

    @Slot(object)
    def _on_site_finished(self, site: dict[str, Any]) -> None:
        for item in site.get("items", []):
            self._items.append(item)
            row = self.result_table.rowCount()
            self.result_table.insertRow(row)
            values = [item.get("source_name"), item.get("title"), item.get("press"),
                      item.get("date_text"), item.get("extraction_method"),
                      BODY_LABELS.get(item.get("body_status"), item.get("body_status"))]
            for col, value in enumerate(values):
                self.result_table.setItem(row, col, _cell(value))

    @Slot(object)
    def _on_job_finished(self, job: dict[str, Any]) -> None:
        self._last_job = job
        state = job.get("state")
        labels = {"done": "완료", "partial": "일부 완료", "failed": "실패", "cancelled": "중지됨"}
        self.statusBar().showMessage(f"작업 종료: {labels.get(state, state)} · 결과 {len(self._items)}건")

    @Slot(bool)
    def _on_busy_changed(self, busy: bool) -> None:
        self._busy = busy
        if not busy:
            self._waiting = False
        self._update_buttons()

    @Slot(object)
    def _on_waiting_changed(self, site_ids: list[str]) -> None:
        self._waiting = bool(site_ids)
        self._update_buttons()
        if site_ids:
            names = ", ".join(SITES[s].DISPLAY_NAME for s in site_ids if s in SITES)
            self.statusBar().showMessage(f"사용자 확인 필요: {names} - 브라우저에서 확인 후 '계속'")

    @Slot(str)
    def _append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    @Slot()
    def _on_result_selected(self) -> None:
        rows = self.result_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._items):
            return
        item = self._items[rows[0].row()]
        none = "(확인 못 함)"
        lines = [
            f"제목: {item.get('title')}",
            f"검색 출처: {item.get('source_name')}",
            f"검색어: {item.get('query')}",
            f"언론사: {item.get('press') or none}",
            f"날짜 원문(검색 결과 표시): {item.get('date_text') or none}",
            f"검색 결과 링크: {item.get('search_link')}",
            f"원문 링크: {item.get('original_link') or none}",
            f"포털 기사 링크: {item.get('portal_link') or none}",
            f"수집 시각: {item.get('collected_at')}",
            f"추출 방식: {item.get('extraction_method')}",
            f"수집 상태: {item.get('status')}" + (f" / 오류: {item['error']}" if item.get("error") else ""),
            "",
            "검색 결과 설명 (본문 요약 아님):",
            item.get("description") or none,
            "",
            f"본문 상태: {BODY_LABELS.get(item.get('body_status'), item.get('body_status'))}",
            f"본문 오류: {item.get('body_error') or '(없음)'}",
            f"본문 접속 후 URL: {item.get('body_final_url') or none}",
            f"본문 추출 방식: {item.get('body_method') or none}",
            f"본문 수집 시각: {item.get('body_collected_at') or none}",
            "기사 본문 후보 (요약 아님, 전문 완전성 미보장):",
            item.get("body_text") or "(본문 없음)",
            "",
            "후속 처리 상태: " + ", ".join(f"{k}={v}" for k, v in (item.get("processing") or {}).items()),
        ]
        self.detail.setPlainText("\n".join(lines))

    # ---------- 종료 ----------
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 이름 규칙)
        if self._closing:
            event.accept()
            return
        if self.runner.is_busy():
            answer = QMessageBox.question(
                self, "종료 확인",
                "실행 중인 작업이 있습니다. 중지하고 브라우저를 정리한 뒤 종료할까요?\n"
                f"정리에 최대 {self.config.shutdown_timeout_sec}초 정도 걸릴 수 있습니다.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._closing = True
        self.statusBar().showMessage("종료 중 - 브라우저와 실행부를 정리합니다")
        self.runner.shutdown()
        event.accept()
