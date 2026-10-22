"""결과 형식과 JSON/CSV 저장. 확인하지 못한 값은 None으로 둔다."""
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

# 후속 처리 기본 상태 (요청 전에는 not_requested로 기록)
DEFAULT_PROCESSING = {
    "body": "not_requested",
    "keywords": "not_requested",
    "summary": "not_requested",
}
# 하위 호환성을 위한 별칭
NOT_IMPLEMENTED_PROCESSING = DEFAULT_PROCESSING


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
    description: str | None = None     # 검색 결과 설명. 본문이나 본문 요약이 아님
    date_text: str | None = None       # 검색 결과에 표시된 게시일/작성일 원문
    press: str | None = None           # 뉴스: 언론사 / 웹·블로그: 사이트명·작성자
    domain: str | None = None          # 원문 또는 검색 링크의 도메인 호스트
    search_type: str = "news"          # news | web | blog
    collected_at: str = field(default_factory=now_iso)
    extraction_method: str = ""
    status: str = "collected"
    error: str | None = None
    body_text: str | None = None
    body_status: str = "not_requested"  # not_requested | pending | processing | extracted | unsupported | blocked | failed | skipped | cancelled
    body_error: str | None = None
    body_final_url: str | None = None
    body_method: str | None = None
    body_collected_at: str | None = None
    keywords: list[str] = field(default_factory=list)
    keywords_source: str = "none"      # body | title_description | title_only | none
    summary: str | None = None
    summary_source: str = "none"       # body | none
    processing: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_PROCESSING)
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
    logged_in: bool | None = None
    pages_collected: int = 0
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
    "검색 유형",
    "검색어",
    "제목",
    "언론사/사이트명",
    "도메인",
    "게시일 원문",
    "검색 결과 링크",
    "원문 링크",
    "포털 기사 링크",
    "수집 시각",
    "추출 방식",
    "본문 상태",
    "본문 추출 방식",
    "본문 수집 시각",
    "본문 오류",
    "핵심 키워드",
    "키워드 입력 출처",
    "본문 요약",
    "요약 입력 출처",
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
    """수집된 목록을 CSV 파일로 저장한다. Excel 호환을 위해 utf-8-sig 인코딩을 사용한다."""
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"이미 존재하는 파일입니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    job_search_type = job.get("search_type") or "news"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        for site in job.get("sites", []):
            for item in site.get("items", []):
                keywords_val = item.get("keywords") or []
                keywords_str = ", ".join(keywords_val) if isinstance(keywords_val, list) else str(keywords_val)
                writer.writerow([
                    item.get("source_name") or "",
                    item.get("search_type") or job_search_type,
                    item.get("query") or "",
                    item.get("title") or "",
                    item.get("press") or "",
                    item.get("domain") or "",
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
                    keywords_str,
                    item.get("keywords_source") or "",
                    item.get("summary") or "",
                    item.get("summary_source") or "",
                    item.get("body_text") or "",
                    item.get("description") or "",
                ])
    os.replace(tmp, path)
    return path
