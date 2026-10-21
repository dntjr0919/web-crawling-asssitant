"""결과 형식과 JSON 저장. 확인하지 못한 값은 None으로 둔다."""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# 사이트 실행 상태 코드와 화면 표시용 이름
STATE_LABELS = {
    "idle": "대기",
    "queued": "실행 대기열",
    "running": "실행 중",
    "needs_user": "사용자 확인 필요",
    "done": "완료",
    "failed": "실패",
    "cancelling": "중지 처리 중",
    "cancelled": "취소됨",
}

# 아직 구현하지 않은 처리. 완료로 표시하지 않기 위해 명시적으로 기록한다.
NOT_IMPLEMENTED_PROCESSING = {
    "body": "not_requested",
    "keywords": "not_implemented",
    "summary": "not_implemented",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ResultItem:
    query: str
    source: str              # 사이트 ID (naver, google)
    source_name: str         # 화면 표시 이름
    title: str
    search_link: str         # 검색 결과 화면에서 찾은 링크
    original_link: str | None = None   # 원문으로 판단할 수 있는 경우만
    portal_link: str | None = None     # 포털 내부 기사 링크가 따로 보이는 경우
    description: str | None = None     # 검색 결과 설명. 본문 요약이 아님
    date_text: str | None = None       # 검색 결과에 보인 날짜 문구 그대로
    press: str | None = None
    collected_at: str = field(default_factory=now_iso)
    extraction_method: str = ""
    status: str = "collected"
    error: str | None = None
    body_text: str | None = None
    body_status: str = "not_requested"
    body_error: str | None = None
    body_final_url: str | None = None
    body_method: str | None = None
    body_collected_at: str | None = None
    processing: dict[str, str] = field(
        default_factory=lambda: dict(NOT_IMPLEMENTED_PROCESSING)
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SiteRunResult:
    site_id: str
    site_name: str
    state: str = "idle"
    message: str = ""
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    final_url: str | None = None
    items: list[ResultItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["items"] = [item.to_dict() for item in self.items]
        data["item_count"] = len(self.items)
        return data


def default_result_path(results_dir: Path, query: str) -> Path:
    """같은 이름이 있으면 번호를 붙여 기존 결과를 덮어쓰지 않는다."""
    safe = "".join(c if c.isalnum() else "_" for c in query)[:30].strip("_") or "query"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = results_dir / f"{stamp}_{safe}.json"
    n = 2
    while candidate.exists():
        candidate = results_dir / f"{stamp}_{safe}_{n}.json"
        n += 1
    return candidate


def save_job_json(job: dict[str, Any], path: Path, overwrite: bool = False) -> Path:
    """임시 파일에 쓴 뒤 교체한다. overwrite=False면 기존 파일을 보호한다."""
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"이미 존재하는 파일입니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


CSV_HEADERS = [
    "출처",
    "검색어",
    "제목",
    "언론사",
    "날짜 원문",
    "검색 결과 링크",
    "원문 링크",
    "포털 기사 링크",
    "수집 시각",
    "추출 방식",
    "본문 상태",
    "본문 추출 방식",
    "본문 수집 시각",
    "본문 오류",
    "본문 내용",
    "검색 결과 설명",
]


def default_csv_path(results_dir: Path, query: str) -> Path:
    """같은 이름이 있으면 번호를 붙여 기존 CSV 결과를 덮어쓰지 않는다."""
    safe = "".join(c if c.isalnum() else "_" for c in query)[:30].strip("_") or "query"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = results_dir / f"{stamp}_{safe}.csv"
    n = 2
    while candidate.exists():
        candidate = results_dir / f"{stamp}_{safe}_{n}.csv"
        n += 1
    return candidate


def save_job_csv(job: dict[str, Any], path: Path, overwrite: bool = False) -> Path:
    """수집된 기사 목록을 CSV 파일로 저장한다. Excel 호환을 위해 utf-8-sig 인코딩을 사용한다."""
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"이미 존재하는 파일입니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        for site in job.get("sites", []):
            for item in site.get("items", []):
                writer.writerow([
                    item.get("source_name") or "",
                    item.get("query") or "",
                    item.get("title") or "",
                    item.get("press") or "",
                    item.get("date_text") or "",
                    item.get("search_link") or "",
                    item.get("original_link") or "",
                    item.get("portal_link") or "",
                    item.get("collected_at") or "",
                    item.get("extraction_method") or "",
                    item.get("body_status") or "",
                    item.get("body_method") or "",
                    item.get("body_collected_at") or "",
                    item.get("body_error") or "",
                    item.get("body_text") or "",
                    item.get("description") or "",
                ])
    os.replace(tmp, path)
    return path

