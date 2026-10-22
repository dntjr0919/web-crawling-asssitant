"""PySide6 화면. 브라우저 작업은 Runner(별도 스레드)에 요청만 하고 직접 실행하지 않는다."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QEasingCurve, QObject, QSize, Qt, QUrl, QVariantAnimation, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from .config import AppConfig
from .models import (
    STATE_LABELS, default_csv_path, default_result_path, save_job_csv, save_job_json,
)
from .runner import Runner
from .selector import recommend_for_query
from .sites import SITES
from .sites.common import host_of

logger = logging.getLogger(__name__)

RESULT_COLUMNS = ["출처", "제목", "언론사/사이트", "게시일", "핵심 키워드", "본문 상태", "링크 열기"]
BODY_LABELS = {
    "not_requested": "요청 안 함",
    "pending": "미처리",
    "processing": "처리 중",
    "extracted": "추출 완료",
    "Extracted": "추출 완료",
    "extracted_from_body": "본문 기준 추출",
    "extracted_from_snippet": "제목·검색설명 기준 추출",
    "unsupported": "미지원 유형",
    "failed": "추출 실패",
    "blocked": "접근 제한",
    "skipped": "건너뜀",
    "cancelled": "취소됨",
    "empty": "추출 결과 없음",
}
KEYWORD_SOURCE_LABELS = {
    "body": "본문 기준",
    "title_description": "제목 + 검색 설명 기준 (본문 아님)",
    "title_only": "제목 기준 (본문·검색 설명 없음)",
    "none": "없음",
}
SUMMARY_SOURCE_LABELS = {
    "body": "본문 기준 추출 요약",
    "none": "생성 안 됨 (본문 없음)",
}


def _create_layout_preset_icon(active_panels: set[int], size: int = 20) -> QIcon:
    """4개 패널(1:상단, 2:중앙좌, 3:중앙우, 4:하단)의 활성화 상태를 사각형으로 시각화한 아이콘을 생성한다."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    active_fill = QColor("#2563EB")
    inactive_fill = QColor("#E5E7EB")
    border_pen = QPen(QColor("#4B5563"), 1)
    painter.setPen(border_pen)

    rects = {
        1: (1, 1, 17, 4),    # 1번: 최상단 패널
        2: (1, 6, 9, 7),     # 2번: 중앙 좌측 패널
        3: (11, 6, 7, 7),    # 3번: 중앙 우측 패널
        4: (1, 14, 17, 4),   # 4번: 최하단 패널
    }
    for pid, (x, y, w, h) in rects.items():
        painter.setBrush(active_fill if pid in active_panels else inactive_fill)
        painter.drawRect(x, y, w, h)

    painter.end()
    return QIcon(pix)


def _create_panel_action_icon(mode: str, size: int = 16) -> QIcon:
    """패널 개별 제어용 작은 사각형 아이콘(full: 전체, default: 기본 4분할, hide: 숨기기)을 생성한다."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    pen = QPen(QColor("#374151"), 1)
    painter.setPen(pen)

    if mode == "full":
        # 단일 패널 전체 채움 사각형
        painter.setBrush(QColor("#2563EB"))
        painter.drawRect(2, 2, 11, 11)
        painter.setPen(QPen(QColor("#FFFFFF"), 1))
        painter.drawRect(4, 4, 7, 7)
    elif mode == "default":
        # 기본 4분할 사각형 모두 표시
        painter.setBrush(QColor("#10B981"))
        painter.drawRect(1, 1, 13, 3)
        painter.drawRect(1, 5, 6, 5)
        painter.drawRect(8, 5, 6, 5)
        painter.drawRect(1, 11, 13, 3)
    else:
        # 숨기기(닫기/최소화) 사각형
        painter.setBrush(QColor("#F3F4F6"))
        painter.drawRect(2, 2, 11, 11)
        painter.setPen(QPen(QColor("#DC2626"), 2))
        painter.drawLine(5, 8, 10, 8)

    painter.end()
    return QIcon(pix)


def resolve_openable_http_url(item: dict[str, Any]) -> tuple[str | None, str]:
    """항목에서 브라우저로 열 수 있는 유효한 http/https URL을 선택한다.

    우선순위:
    1) original_link (구글 게이트웨이 /goto 링크가 아닌 실제 원문 URL이거나, body_final_url이 확인된 경우 body_final_url 우선)
    2) body_final_url
    3) search_link
    반환값: (유효한 URL 또는 None, 설명 또는 오류 사유)
    """
    orig = (item.get("original_link") or "").strip()
    final_u = (item.get("body_final_url") or "").strip()
    search_u = (item.get("search_link") or "").strip()

    if orig and "google." in host_of(orig) and final_u and "google." not in host_of(final_u):
        candidates = [(final_u, "원문 URL"), (orig, "원문 연결 URL"), (search_u, "검색 결과 링크")]
    else:
        candidates = [(orig, "원문 URL"), (final_u, "원문 최종 URL"), (search_u, "검색 결과 링크")]

    for cand, label in candidates:
        if not cand:
            continue
        try:
            parsed = urlparse(cand)
        except ValueError:
            continue
        scheme = (parsed.scheme or "").lower()
        if scheme in ("http", "https") and parsed.hostname:
            return cand, label
    return None, "열 수 있는 유효한 http/https 링크가 없습니다."


def format_detail_text(item: dict[str, Any], default_search_type: str = "news") -> str:
    """검색 유형에 따라 뉴스형(news)과 일반형(web, blog 등) 상세 화면 문자열을 생성한다."""
    none = "(확인 못 함)"
    search_type = item.get("search_type") or default_search_type or "news"
    kws = item.get("keywords") or []
    kw_str = ", ".join(kws) if isinstance(kws, list) else str(kws)

    kw_src = item.get("keywords_source")
    if not kw_src:
        proc_kw = (item.get("processing") or {}).get("keywords", "")
        if proc_kw in ("extracted", "extracted_from_body"):
            kw_src = "body"
        elif proc_kw == "extracted_from_snippet":
            kw_src = "title_description" if item.get("description") else "title_only"
        else:
            kw_src = "none"
    kw_src_label = KEYWORD_SOURCE_LABELS.get(kw_src, kw_src)

    sum_text = item.get("summary")
    sum_src = item.get("summary_source") or ("body" if sum_text else "none")
    sum_src_label = SUMMARY_SOURCE_LABELS.get(sum_src, sum_src)

    body_status = item.get("body_status") or "not_requested"
    body_status_label = BODY_LABELS.get(body_status, body_status)
    body_text = item.get("body_text")
    body_error = item.get("body_error")

    proc_items = item.get("processing") or {}
    proc_str = ", ".join(f"{k}={BODY_LABELS.get(v, v)}" for k, v in proc_items.items()) or none
    collect_status = f"{item.get('status') or 'collected'}" + (
        f" / 오류: {item['error']}" if item.get("error") else ""
    )

    if search_type == "news":
        if body_text:
            body_display = body_text
        elif body_status == "not_requested":
            body_display = "(본문 추출을 선택하지 않았습니다. 검색 결과 설명은 본문이 아닙니다.)"
        else:
            body_display = f"(본문 없음 — 상태: {body_status_label}" + (
                f", 사유: {body_error})" if body_error else ")"
            )

        lines = [
            "[뉴스 상세 보기]",
            f"제목: {item.get('title') or none}",
            f"검색 출처: {item.get('source_name') or none}",
            f"검색어: {item.get('query') or none}",
            f"언론사: {item.get('press') or none}",
            f"게시일(검색 결과 표시): {item.get('date_text') or none}",
            f"수집 시각(앱 수집): {item.get('collected_at') or none}",
            f"검색 결과 링크: {item.get('search_link') or none}",
            f"원문 링크: {item.get('original_link') or none}",
            f"포털 기사 링크: {item.get('portal_link') or none}",
            f"목록 추출 방식: {item.get('extraction_method') or none}",
            f"수집 상태: {collect_status}",
            "",
            "검색 결과 설명 (검색 목록 스니펫 · 본문 요약 아님):",
            item.get("description") or "(검색 결과 설명 없음)",
            "",
            f"핵심 키워드 [입력 출처: {kw_src_label}]:",
            kw_str or "(추출된 키워드 없음)",
            "",
            f"본문 요약 [입력 출처: {sum_src_label}]:",
            sum_text or "(본문이 추출된 경우에만 생성됩니다)",
            "",
            f"본문 상태: {body_status_label}",
            f"본문 상태 사유: {body_error or '(없음)'}",
            f"본문 접속 후 URL: {item.get('body_final_url') or none}",
            f"본문 추출 방식: {item.get('body_method') or none}",
            f"본문 수집 시각: {item.get('body_collected_at') or none}",
            "기사 본문 후보 (전문 완전성 미보장):",
            body_display,
            "",
            f"후속 처리 상태: {proc_str}",
        ]
        return "\n".join(lines)

    domain = item.get("domain") or host_of(
        item.get("body_final_url") or item.get("original_link") or item.get("search_link")
    )
    site_name = item.get("press") or ""
    if site_name and domain and site_name.lower() != domain.lower():
        site_domain_display = f"{site_name} ({domain})"
    else:
        site_domain_display = site_name or domain or none

    primary_link, link_label = resolve_openable_http_url(item)
    search_link = item.get("search_link") or none

    if body_text:
        main_content_display = body_text
    elif body_status == "not_requested":
        main_content_display = "(본문 추출을 요청하지 않았습니다. 위 검색 설명은 목록 스니펫이며 페이지 본문이 아닙니다.)"
    elif body_status == "unsupported":
        main_content_display = f"(미지원 유형으로 본문을 자동 추출하지 않았습니다: {body_error or '동영상·SNS·비문서 형식'})"
    elif body_status == "blocked":
        main_content_display = f"(접근 제한·인증 화면으로 본문을 추출하지 못했습니다: {body_error or '접근 제한'})"
    else:
        main_content_display = f"(주요 내용을 추출하지 못했습니다 — 사유: {body_error or '본문 영역 미확인'})"

    type_badge = "블로그" if search_type == "blog" else "웹 문서 (일반형)"
    lines = [
        f"[{type_badge} 상세 보기]",
        f"제목: {item.get('title') or none}",
        f"사이트명 / 도메인: {site_domain_display} (검색 포털: {item.get('source_name') or none})",
        f"원문 링크({link_label}): {primary_link or none}",
    ]
    if search_link != primary_link:
        lines.append(f"검색 결과 링크: {search_link}")
    if item.get("body_final_url") and item.get("body_final_url") not in (primary_link, search_link):
        lines.append(f"접속 후 최종 URL: {item.get('body_final_url')}")

    lines.extend([
        f"게시일(검색 결과 표시): {item.get('date_text') or '(표시 없음)'}",
        f"수집 시각(앱 수집): {item.get('collected_at') or none}" + (
            f" / 본문 수집: {item.get('body_collected_at')}" if item.get("body_collected_at") else ""
        ),
        "",
        "검색 설명 (검색 결과 스니펫 · 페이지 본문 아님):",
        item.get("description") or "(검색 결과 설명 없음)",
        "",
        f"핵심 키워드 [입력 출처: {kw_src_label}]:",
        kw_str or "(추출된 키워드 없음)",
        "",
        f"요약 [입력 출처: {sum_src_label}]:",
        sum_text or "(추출된 주요 내용이 없어 요약을 생성하지 않았습니다)",
        "",
        f"주요 내용 [상태: {body_status_label}" + (
            f" · 방식: {item.get('body_method')}" if item.get("body_method") else ""
        ) + "]:",
        main_content_display,
        "",
        "처리 상태:",
        f"- 목록 수집: {collect_status} (방식: {item.get('extraction_method') or none})",
        f"- 본문 수집: {body_status_label}" + (f" ({body_error})" if body_error else ""),
        f"- 후속 처리: {proc_str}",
    ])
    return "\n".join(lines)


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
        self._last_saved_dir: Path | None = None
        self._busy = False
        self._waiting = False
        self._site_rows: dict[str, int] = {}
        self._closing = False
        self._active_detail_row: int | None = None
        self.panels: dict[int, QFrame] = {}
        self.panel_controls: dict[int, dict[str, QToolButton]] = {}
        self.preset_buttons: dict[str, QToolButton] = {}

        # 3번 상세 내용 패널 미닫이(슬라이딩) 애니메이션
        self._last_detail_width: int = 480
        self.animations_enabled: bool = True
        self._detail_anim = QVariantAnimation(self)
        self._detail_anim.setDuration(220)
        self._detail_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._detail_anim.valueChanged.connect(self._on_detail_slide_step)
        self._detail_anim.finished.connect(self._on_detail_slide_finished)

        self.setWindowTitle("웹 검색 자동화 도우미")
        self.resize(1300, 840)
        self._build_ui()

        q = Qt.ConnectionType.QueuedConnection
        runner.site_state.connect(self._on_site_state, q)
        runner.site_finished.connect(self._on_site_finished, q)
        runner.job_finished.connect(self._on_job_finished, q)
        runner.busy_changed.connect(self._on_busy_changed, q)
        runner.waiting_changed.connect(self._on_waiting_changed, q)
        log_bridge.message.connect(self._append_log, q)
        self._update_buttons()

    def _wrap_panel(self, panel_id: int, title: str, content_widget: QWidget) -> QFrame:
        """각 패널(1~4)에 제목과 우측 상단 [전체 / 기본 / 숨기기] 아이콘 버튼을 포함한 프레임을 생성한다."""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        if panel_id == 3:
            frame.setMinimumWidth(0)
            content_widget.setMinimumWidth(0)
        vbox = QVBoxLayout(frame)
        vbox.setContentsMargins(4, 3, 4, 4)
        vbox.setSpacing(3)

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        header.setSpacing(3)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: bold; font-size: 11px; color: #374151;")
        header.addWidget(title_label)
        header.addStretch()

        btn_full = QToolButton()
        btn_full.setIcon(_create_panel_action_icon("full"))
        btn_full.setIconSize(QSize(14, 14))
        btn_full.setFixedSize(20, 20)
        btn_full.setToolTip(f"{title} — 전체 화면으로 보기")
        btn_full.clicked.connect(lambda _checked=False, pid=panel_id: self.maximize_panel(pid))

        btn_default = QToolButton()
        btn_default.setIcon(_create_panel_action_icon("default"))
        btn_default.setIconSize(QSize(14, 14))
        btn_default.setFixedSize(20, 20)
        btn_default.setToolTip("기본 배치(1~4번 패널 모두 표시)로 복원")
        btn_default.clicked.connect(lambda _checked=False: self.restore_default_panels())

        btn_hide = QToolButton()
        btn_hide.setIcon(_create_panel_action_icon("hide"))
        btn_hide.setIconSize(QSize(14, 14))
        btn_hide.setFixedSize(20, 20)
        btn_hide.setToolTip(f"{title} — 숨기기")
        btn_hide.clicked.connect(lambda _checked=False, pid=panel_id: self.hide_panel(pid))

        for b in (btn_full, btn_default, btn_hide):
            header.addWidget(b)

        vbox.addLayout(header)
        vbox.addWidget(content_widget, 1)

        self.panels[panel_id] = frame
        self.panel_controls[panel_id] = {
            "full": btn_full,
            "default": btn_default,
            "hide": btn_hide,
        }
        return frame

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

        self.recommend_btn = QPushButton("사이트 자동 추천")
        self.recommend_btn.setToolTip("입력한 검색어 키워드를 분석해 적합한 사이트와 수집 대상을 자동 선택합니다")
        self.recommend_btn.clicked.connect(self._on_recommend)
        top.addWidget(self.recommend_btn)

        self.site_checks: dict[str, QCheckBox] = {}
        for site_id, module in SITES.items():
            box = QCheckBox(module.DISPLAY_NAME)
            box.setChecked(self.config.enabled_sites.get(site_id, True))
            self.site_checks[site_id] = box
            top.addWidget(box)
        self.run_btn = QPushButton("실행")
        self.stop_btn = QPushButton("중지")
        self.continue_btn = QPushButton("계속")
        self.continue_btn.setToolTip("브라우저에서 직접 로그인·확인·조작한 뒤 누르면 화면 상태를 다시 확인하고 재개합니다")
        self.save_btn = QPushButton("JSON 저장")
        self.save_csv_btn = QPushButton("CSV 저장")
        self.open_dir_btn = QPushButton("저장 폴더 열기")
        self.open_dir_btn.setToolTip("최근 결과를 내보낸 폴더(또는 기본 저장 폴더)를 Windows 탐색기로 엽니다")
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn.clicked.connect(self._on_stop)
        self.continue_btn.clicked.connect(self._on_continue)
        self.save_btn.clicked.connect(self._on_save)
        self.save_csv_btn.clicked.connect(self._on_save_csv)
        self.open_dir_btn.clicked.connect(self._on_open_results_dir)
        for btn in (
            self.run_btn, self.stop_btn, self.continue_btn,
            self.save_btn, self.save_csv_btn, self.open_dir_btn,
        ):
            top.addWidget(btn)

        # 우측 상단 패널 형태 설정 프리셋 버튼 (전체 활성화 / 1,2번 활성화 / 2,4번 활성화)
        top.addSpacing(6)
        preset_specs = [
            ("all", {1, 2, 3, 4}, "패널 전체 활성화 (1·2·3·4번 모두 표시)"),
            ("1_2", {1, 2}, "1·2번 패널 활성화 (사이트 상태 + 결과 목록)"),
            ("2_4", {2, 4}, "2·4번 패널 활성화 (결과 목록 + 실행 로그)"),
        ]
        for key, active_set, tip in preset_specs:
            pbtn = QToolButton()
            pbtn.setIcon(_create_layout_preset_icon(active_set))
            pbtn.setIconSize(QSize(20, 20))
            pbtn.setFixedSize(26, 26)
            pbtn.setToolTip(tip)
            pbtn.clicked.connect(lambda _checked=False, s=set(active_set): self.apply_panel_preset(s))
            self.preset_buttons[key] = pbtn
            top.addWidget(pbtn)

        layout.addLayout(top)

        options_layout = QHBoxLayout()
        self.body_check = QCheckBox("본문·요약 추출 (공개 원문)")
        self.body_check.setChecked(False)
        self.body_check.setToolTip(
            "체크하면 원문 주요 내용을 추출하고 본문 기준 키워드 및 3문장 추출 요약을 생성합니다."
        )
        self.dedup_check = QCheckBox("원문 중복 제거")
        self.dedup_check.setChecked(True)
        self.dedup_check.setToolTip(
            "동일한 원문 URL을 가진 항목이 여러 개 수집되면 최초 1건만 남기고 중복을 제거합니다."
        )
        self.login_check = QCheckBox("로그인 확인")
        self.login_check.setChecked(False)
        self.login_check.setToolTip(
            "체크하면 각 사이트의 전용 프로필 로그인 여부를 실제 확인하고, 비로그인 시 수동 로그인을 기다립니다."
        )
        options_layout.addWidget(self.body_check)
        options_layout.addWidget(self.dedup_check)
        options_layout.addWidget(self.login_check)

        options_layout.addSpacing(12)
        options_layout.addWidget(QLabel("수집 대상:"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("뉴스", "news")
        self.type_combo.addItem("웹 전체 (통합)", "web")
        self.type_combo.addItem("블로그", "blog")
        self.type_combo.setToolTip("수집할 검색 결과 유형 (뉴스는 뉴스형 상세 화면, 웹/블로그는 일반형 상세 화면 적용)")
        options_layout.addWidget(self.type_combo)

        options_layout.addSpacing(8)
        options_layout.addWidget(QLabel("최대 건수:"))
        self.max_results_spin = QSpinBox()
        self.max_results_spin.setRange(1, 50)
        self.max_results_spin.setValue(self.config.max_results_per_site)
        self.max_results_spin.setSuffix("건")
        self.max_results_spin.setToolTip("사이트별 최대 수집 건수 (1~50건, 1페이지 초과 시 자동 페이지네이션)")
        options_layout.addWidget(self.max_results_spin)

        options_layout.addSpacing(8)
        options_layout.addWidget(QLabel("검색 기간:"))
        self.period_combo = QComboBox()
        self.period_combo.addItem("전체 (기본)", "all")
        self.period_combo.addItem("1일 (최근 24시간)", "1d")
        self.period_combo.addItem("1주일", "1w")
        self.period_combo.addItem("1개월", "1m")
        self.period_combo.addItem("1년", "1y")
        self.period_combo.setToolTip("검색 대상 기간 필터")
        options_layout.addWidget(self.period_combo)

        options_layout.addSpacing(8)
        options_layout.addWidget(QLabel("정렬:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("관련도순 (기본)", "relevance")
        self.sort_combo.addItem("최신순", "date")
        self.sort_combo.setToolTip("검색 결과 정렬 기준")
        options_layout.addWidget(self.sort_combo)

        options_layout.addSpacing(8)
        options_layout.addWidget(QLabel("동시 실행:"))
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 2)
        self.concurrency_spin.setValue(self.config.max_concurrency)
        self.concurrency_spin.setSuffix("개")
        self.concurrency_spin.setToolTip("동시에 실행할 브라우저 창 개수 (1~2개)")
        options_layout.addWidget(self.concurrency_spin)

        options_layout.addStretch()
        layout.addLayout(options_layout)

        self.info_label = QLabel(
            "지원 기능: 사이트 자동 추천 · 뉴스/웹/블로그 맞춤형 상세 화면 · 같은 행 재클릭 시 상세 패널 토글 · "
            "우측 상단 및 패널별 레이아웃 전환(전체/기본/숨기기)"
        )
        self.info_label.setStyleSheet("color: gray;")
        layout.addWidget(self.info_label)

        # 1번 패널: 최상단 사이트 상태 테이블
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
        self.panel_1 = self._wrap_panel(1, "1. 사이트 진행 상태", self.site_table)

        # 2번 패널: 중앙 좌측 결과 목록 테이블
        self.result_table = QTableWidget(0, len(RESULT_COLUMNS))
        self.result_table.setHorizontalHeaderLabels(RESULT_COLUMNS)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.result_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.result_table.cellClicked.connect(self._on_result_cell_clicked)
        self.panel_2 = self._wrap_panel(2, "2. 수집 결과 목록", self.result_table)

        # 3번 패널: 중앙 우측 상세 내용 표시
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("결과 행을 클릭하면 상세 내용이 표시되며, 같은 행을 다시 클릭하면 패널이 닫힙니다")
        self.panel_3 = self._wrap_panel(3, "3. 상세 내용", self.detail)

        # 4번 패널: 최하단 실행 로그
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.panel_4 = self._wrap_panel(4, "4. 실행 로그", self.log_view)

        self.middle_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.middle_splitter.setChildrenCollapsible(True)
        self.middle_splitter.addWidget(self.panel_2)
        self.middle_splitter.addWidget(self.panel_3)
        self.middle_splitter.setSizes([780, 480])
        self.middle_splitter.splitterMoved.connect(self._on_middle_splitter_moved)

        self.main_splitter = QSplitter(Qt.Orientation.Vertical)
        self.main_splitter.setChildrenCollapsible(True)
        self.main_splitter.addWidget(self.panel_1)
        self.main_splitter.addWidget(self.middle_splitter)
        self.main_splitter.addWidget(self.panel_4)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self._apply_default_splitter_sizes()

        layout.addWidget(self.main_splitter, 1)

        self.setCentralWidget(root)
        self.statusBar().showMessage("준비")

    # ---------- 패널 레이아웃 및 미닫이(슬라이딩) 애니메이션 ----------
    def _on_middle_splitter_moved(self, pos: int, index: int) -> None:
        """사용자가 스플리터 구분선을 직접 드래그한 경우 상세 패널 크기를 기억한다."""
        if not self.panel_3.isHidden() and self._detail_anim.state() != QVariantAnimation.State.Running:
            sizes = self.middle_splitter.sizes()
            if len(sizes) >= 2 and sizes[1] > 100:
                self._last_detail_width = sizes[1]

    def _on_detail_slide_step(self, value: Any) -> None:
        """미닫이 애니메이션 진행 단계마다 3번 패널 크기를 부드럽게 조정한다."""
        w = int(value)
        total_width = sum(self.middle_splitter.sizes()) or self.width() or 1200
        p2_width = max(50, total_width - w)
        self.middle_splitter.setSizes([p2_width, w])

    def _on_detail_slide_finished(self) -> None:
        """미닫이 애니메이션 완료 시 표시/숨김 상태 및 상태바를 정리한다."""
        if self._detail_anim.endValue() == 0:
            self.panel_3.setVisible(False)
            self._active_detail_row = None
            self._sync_splitter_containers()
            self.statusBar().showMessage("3번 상세 내용 창 닫힘 (미닫이)")
        else:
            self.panel_3.setVisible(True)
            self._sync_splitter_containers()
            self.statusBar().showMessage("3번 상세 내용 창 열림 (미닫이)")

    def slide_panel_3(self, open_panel: bool, animated: bool | None = None) -> None:
        """3번 상세 내용 패널을 미닫이(가로 슬라이딩) 애니메이션으로 열거나 닫는다."""
        if animated is None:
            animated = self.animations_enabled

        total_width = sum(self.middle_splitter.sizes()) or self.width() or 1200
        target_width = getattr(self, "_last_detail_width", 480)
        target_width = max(260, min(target_width, total_width - 150))

        if self._detail_anim.state() == QVariantAnimation.State.Running:
            self._detail_anim.stop()

        current_width = self.middle_splitter.sizes()[1] if not self.panel_3.isHidden() else 0

        if not animated:
            if open_panel:
                self.panel_3.setVisible(True)
                self._sync_splitter_containers()
                self.middle_splitter.setSizes([max(50, total_width - target_width), target_width])
                self.statusBar().showMessage("3번 상세 내용 창 열림")
            else:
                self.panel_3.setVisible(False)
                self._active_detail_row = None
                self._sync_splitter_containers()
                self.statusBar().showMessage("3번 상세 내용 창 닫힘")
            return

        if open_panel:
            self.panel_3.setVisible(True)
            self._sync_splitter_containers()
            start_val = max(0, current_width)
            self._detail_anim.setStartValue(start_val)
            self._detail_anim.setEndValue(target_width)
            self._detail_anim.start()
        else:
            if current_width > 50:
                self._last_detail_width = current_width
            start_val = max(1, current_width)
            self._detail_anim.setStartValue(start_val)
            self._detail_anim.setEndValue(0)
            self._detail_anim.start()

    def _apply_default_splitter_sizes(self) -> None:
        self.panel_1.setMaximumHeight(16777215)
        self.main_splitter.setSizes([125, 500, 190])
        self.middle_splitter.setSizes([780, 480])

    def _sync_splitter_containers(self) -> None:
        """2번과 3번 패널이 모두 숨겨진 경우 중앙 스플리터 컨테이너도 숨겨 여백이 남지 않게 한다."""
        p2_vis = not self.panel_2.isHidden()
        p3_vis = not self.panel_3.isHidden()
        self.middle_splitter.setVisible(p2_vis or p3_vis)

    def apply_panel_preset(self, active_panel_ids: set[int]) -> None:
        """지정한 번호의 패널만 활성화하고 나머지는 숨긴다."""
        if self._detail_anim.state() == QVariantAnimation.State.Running:
            self._detail_anim.stop()
        for pid, frame in self.panels.items():
            if pid != 3:
                frame.setVisible(pid in active_panel_ids)
        if 3 in active_panel_ids:
            self.slide_panel_3(True)
        else:
            self.slide_panel_3(False)
        self._sync_splitter_containers()
        if active_panel_ids == {1, 2, 3, 4}:
            self._apply_default_splitter_sizes()
        elif active_panel_ids == {1, 2}:
            self.main_splitter.setSizes([135, 680, 0])
        elif active_panel_ids == {2, 4}:
            self.main_splitter.setSizes([0, 560, 240])
        sorted_ids = ", ".join(str(i) for i in sorted(active_panel_ids))
        self.statusBar().showMessage(f"패널 표시 변경: {sorted_ids}번 패널 활성화")

    def maximize_panel(self, panel_id: int) -> None:
        """선택한 단일 패널만 전체 영역으로 표시한다."""
        if self._detail_anim.state() == QVariantAnimation.State.Running:
            self._detail_anim.stop()
        for pid, frame in self.panels.items():
            frame.setVisible(pid == panel_id)
        if panel_id != 3:
            self._active_detail_row = None
        self._sync_splitter_containers()
        self.statusBar().showMessage(f"{panel_id}번 패널 전체 보기")

    def restore_default_panels(self) -> None:
        """1~4번 패널이 모두 함께 보이는 기본 형태로 복원한다."""
        self.apply_panel_preset({1, 2, 3, 4})
        self.statusBar().showMessage("기본 패널 배치(1·2·3·4번 전체 표시) 복원")

    def hide_panel(self, panel_id: int) -> None:
        """특정 패널을 숨긴다. 만약 마지막 남은 패널이면 안내 후 유지한다."""
        visible_ids = [pid for pid, frame in self.panels.items() if not frame.isHidden()]
        if visible_ids == [panel_id]:
            self.statusBar().showMessage("최소 1개의 패널은 표시되어야 합니다 (기본 버튼으로 복원 가능)")
            return
        if panel_id == 3:
            self.slide_panel_3(False)
            return
        self.panels[panel_id].setVisible(False)
        self._sync_splitter_containers()
        self.statusBar().showMessage(f"{panel_id}번 패널 숨김")

    def _update_buttons(self) -> None:
        self.run_btn.setEnabled(not self._busy)
        self.recommend_btn.setEnabled(not self._busy)
        self.stop_btn.setEnabled(self._busy)
        self.continue_btn.setEnabled(self._busy and self._waiting)
        self.save_btn.setEnabled(not self._busy and self._last_job is not None)
        self.save_csv_btn.setEnabled(not self._busy and self._last_job is not None)
        self.open_dir_btn.setEnabled(True)
        self.query_edit.setEnabled(not self._busy)
        self.body_check.setEnabled(not self._busy)
        self.dedup_check.setEnabled(not self._busy)
        self.login_check.setEnabled(not self._busy)
        self.max_results_spin.setEnabled(not self._busy)
        self.period_combo.setEnabled(not self._busy)
        self.sort_combo.setEnabled(not self._busy)
        self.type_combo.setEnabled(not self._busy)
        self.concurrency_spin.setEnabled(not self._busy)
        for box in self.site_checks.values():
            box.setEnabled(not self._busy)

    # ---------- 사용자 조작 ----------
    @Slot()
    def _on_recommend(self) -> None:
        if self._busy:
            return
        rec = recommend_for_query(self.query_edit.text(), self.config.enabled_sites)
        for sid, box in self.site_checks.items():
            box.setChecked(sid in rec.site_ids)
        idx = self.type_combo.findData(rec.search_type)
        if idx >= 0:
            self.type_combo.setCurrentIndex(idx)
        self.info_label.setText(f"사이트 선택 추천: {rec.reason}")
        self.statusBar().showMessage(rec.reason)
        logger.info("사이트 자동 추천 적용: %s (대상=%s, 사이트=%s)", rec.reason, rec.search_type, rec.site_ids)

    @Slot()
    def _on_run(self) -> None:
        if self._busy:
            return
        site_ids = [sid for sid, box in self.site_checks.items() if box.isChecked()]
        query = self.query_edit.text()
        ok, message = self.runner.submit_job(
            query,
            site_ids,
            self.body_check.isChecked(),
            self.dedup_check.isChecked(),
            max_results=self.max_results_spin.value(),
            period=self.period_combo.currentData(),
            search_type=self.type_combo.currentData(),
            concurrency=self.concurrency_spin.value(),
            sort=self.sort_combo.currentData(),
            require_login=self.login_check.isChecked(),
        )
        if not ok:
            QMessageBox.information(self, "실행할 수 없음", message)
            return
        self._items.clear()
        self._last_job = None
        self._active_detail_row = None
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
        base_dir = self._last_saved_dir or self.config.results_dir
        default = default_result_path(base_dir, self._last_job.get("query", ""))
        default.parent.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "결과 JSON 저장", str(default), "JSON (*.json)")
        if not path:
            return
        try:
            saved = save_job_json(self._last_job, Path(path), overwrite=True)
        except OSError as exc:
            QMessageBox.warning(self, "저장 실패", str(exc))
            logger.error("결과 저장 실패: %s", exc)
            return
        self._last_saved_dir = saved.parent.resolve()
        logger.info("결과 저장: %s", saved)
        self.statusBar().showMessage(f"저장됨: {saved}")

    @Slot()
    def _on_save_csv(self) -> None:
        if self._last_job is None:
            return
        base_dir = self._last_saved_dir or self.config.results_dir
        default = default_csv_path(base_dir, self._last_job.get("query", ""))
        default.parent.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "결과 CSV 저장", str(default), "CSV (*.csv)")
        if not path:
            return
        try:
            saved = save_job_csv(self._last_job, Path(path), overwrite=True)
        except OSError as exc:
            QMessageBox.warning(self, "저장 실패", str(exc))
            logger.error("CSV 결과 저장 실패: %s", exc)
            return
        self._last_saved_dir = saved.parent.resolve()
        logger.info("CSV 결과 저장: %s", saved)
        self.statusBar().showMessage(f"CSV 저장됨: {saved}")

    def _resolve_target_results_dir(self) -> Path:
        if self._last_saved_dir is not None:
            return self._last_saved_dir
        return self.config.results_dir.resolve()

    @Slot()
    def _on_open_results_dir(self) -> None:
        """결과 저장 폴더를 Windows 탐색기(또는 OS 기본 파일 관리자)로 연다."""
        target_dir = self._resolve_target_results_dir()
        if not target_dir.exists():
            if self._last_saved_dir is None:
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    QMessageBox.warning(
                        self, "폴더 열기 실패", f"저장 폴더를 만들거나 찾을 수 없습니다:\n{target_dir}\n({exc})"
                    )
                    self.statusBar().showMessage(f"저장 폴더 열기 실패: {target_dir}")
                    return
            else:
                QMessageBox.information(
                    self, "폴더 없음", f"최근 저장한 폴더가 존재하지 않습니다:\n{target_dir}"
                )
                self.statusBar().showMessage(f"폴더가 존재하지 않음: {target_dir}")
                return
        if not target_dir.is_dir():
            QMessageBox.warning(self, "폴더 열기 실패", f"유효한 폴더 경로가 아닙니다:\n{target_dir}")
            return
        try:
            if sys.platform == "win32" and hasattr(os, "startfile"):
                os.startfile(str(target_dir))
            else:
                ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_dir)))
                if not ok:
                    raise OSError("기본 파일 탐색기를 실행하지 못했습니다")
            logger.info("저장 폴더 열기: %s", target_dir)
            self.statusBar().showMessage(f"폴더 열기: {target_dir}")
        except Exception as exc:
            logger.error("저장 폴더 열기 실패(%s): %s", target_dir, exc)
            QMessageBox.warning(
                self, "폴더 열기 실패", f"탐색기로 폴더를 열지 못했습니다:\n{target_dir}\n({exc})"
            )
            self.statusBar().showMessage(f"저장 폴더 열기 실패: {exc}")

    def _open_item_link(self, row_index: int) -> None:
        """결과 행의 원문 URL(없으면 검색 결과 URL)을 검증한 뒤 기본 브라우저로 연다."""
        if row_index < 0 or row_index >= len(self._items):
            return
        item = self._items[row_index]
        target_url, label = resolve_openable_http_url(item)
        if not target_url:
            QMessageBox.information(self, "링크 열기 불가", label)
            self.statusBar().showMessage(f"링크 열기 불가: {label}")
            return
        try:
            qurl = QUrl(target_url)
            if not qurl.isValid() or qurl.scheme().lower() not in ("http", "https"):
                QMessageBox.information(
                    self, "링크 열기 불가", f"허용되지 않거나 잘못된 URL 형식입니다:\n{target_url}"
                )
                return
            opened = QDesktopServices.openUrl(qurl)
            if not opened:
                QMessageBox.warning(
                    self, "링크 열기 실패", f"기본 브라우저로 링크를 열지 못했습니다:\n{target_url}"
                )
                self.statusBar().showMessage("기본 브라우저로 링크 열기 실패")
                return
            logger.info("외부 브라우저로 %s 열기: %s", label, target_url)
            self.statusBar().showMessage(f"{label} 열기: {target_url}")
        except Exception as exc:
            logger.warning("링크 열기 중 오류(%s): %s", target_url, exc)
            QMessageBox.warning(self, "링크 열기 오류", f"링크를 여는 중 오류가 발생했습니다:\n{exc}")

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
            kws = item.get("keywords") or []
            kw_str = ", ".join(kws) if isinstance(kws, list) else str(kws)
            press_or_domain = item.get("press") or item.get("domain") or ""
            values = [
                item.get("source_name"),
                item.get("title"),
                press_or_domain,
                item.get("date_text"),
                kw_str,
                BODY_LABELS.get(item.get("body_status"), item.get("body_status")),
            ]
            for col, value in enumerate(values):
                self.result_table.setItem(row, col, _cell(value))

            open_btn = QPushButton("링크 열기")
            target_url, label = resolve_openable_http_url(item)
            if target_url:
                open_btn.setToolTip(f"{label} 열기: {target_url}")
            else:
                open_btn.setToolTip(label)
            open_btn.clicked.connect(lambda _checked=False, r=row: self._open_item_link(r))
            self.result_table.setCellWidget(row, 6, open_btn)

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

    def _show_detail_for_row(self, row: int) -> None:
        """지정한 행의 상세 내용을 3번 패널에 출력한다."""
        if row < 0 or row >= len(self._items):
            return
        item = self._items[row]
        default_st = (self._last_job or {}).get("search_type") or self.type_combo.currentData() or "news"
        self.detail.setPlainText(format_detail_text(item, default_search_type=default_st))
        self._active_detail_row = row

    @Slot(int, int)
    def _on_result_cell_clicked(self, row: int, column: int) -> None:
        """2번 패널의 결과 행 클릭 처리:
        - 이미 3번 패널에 표시 중인 같은 행을 다시 클릭하면 3번 패널을 미닫이(슬라이딩) 애니메이션으로 닫는다(토글).
        - 다른 행을 클릭하거나 3번 패널이 닫혀 있을 때 클릭하면 해당 행의 상세 내용을 출력하며 미닫이로 연다.
        """
        if column == 6 or row < 0 or row >= len(self._items):
            return

        is_open = not self.panel_3.isHidden() and (
            self._detail_anim.state() != QVariantAnimation.State.Running
            or self._detail_anim.endValue() != 0
        )
        if is_open and self._active_detail_row == row:
            self.slide_panel_3(False)
            return

        self._show_detail_for_row(row)
        if not is_open or self.panel_3.isHidden():
            self.slide_panel_3(True)

    @Slot()
    def _on_result_selected(self) -> None:
        """프로그램 코드(selectRow 등)로 선택이 변경된 경우 상세 내용을 표시하고 미닫이로 연다."""
        rows = self.result_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._items):
            return
        row = rows[0].row()
        self._show_detail_for_row(row)
        if self.panel_3.isHidden():
            self.slide_panel_3(True)

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
