"""수집 기사 결과의 원문 URL 기준 중복 제거 및 URL 정규화."""
from __future__ import annotations

import re
from typing import Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import ResultItem

# 분석/트래킹용 쿼리 파라미터 (중복 판정 시 무시)
TRACKING_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "_ga",
    "_gl",
    "ref",
    "ref_src",
    "sns",
}


def normalize_url(url: str | None) -> str | None:
    """비교를 위한 표준 URL 문자열을 반환한다.
    
    - 프로토콜(http/https) 및 호스트 소문자화
    - 표준 포트(:80, :443) 제거
    - 끝 슬래시(/) 정리
    - 트래킹 쿼리 파라미터 제거 및 파라미터 알파벳순 정렬
    - fragment(#...) 제거
    """
    if not url:
        return None
    trimmed = url.strip()
    if not trimmed:
        return None
    try:
        parsed = urlparse(trimmed)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            return None
        netloc = parsed.netloc.lower()
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        elif netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]

        path = parsed.path or "/"
        path = re.sub(r"/+", "/", path)
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]

        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered_pairs = [
            (k, v) for k, v in query_pairs if k.lower() not in TRACKING_QUERY_PARAMS
        ]
        filtered_pairs.sort(key=lambda pair: pair[0])
        new_query = urlencode(filtered_pairs)

        return urlunparse((scheme, netloc, path, parsed.params, new_query, ""))
    except Exception:
        return trimmed


def get_dedup_key(item: ResultItem) -> str:
    """중복 제거를 위한 기사의 고유 식별 키를 반환한다.
    
    1순위: original_link (원문 링크가 확인된 경우)
    2순위: portal_link (포털 자체 기사 링크)
    3순위: search_link (검색 결과 링크)
    """
    if item.original_link:
        norm = normalize_url(item.original_link)
        if norm:
            return f"orig:{norm}"
    if item.portal_link:
        norm = normalize_url(item.portal_link)
        if norm:
            return f"portal:{norm}"
    norm = normalize_url(item.search_link)
    if norm:
        return f"search:{norm}"
    return f"raw:{item.search_link}"


def deduplicate_items(
    items: Sequence[ResultItem],
    seen_keys: set[str] | None = None,
) -> tuple[list[ResultItem], int]:
    """기사 목록에서 중복된 항목을 제거하고 (유일한 기사 목록, 제거된 중복 수)를 반환한다.
    
    seen_keys 집합을 전달하면 이전 사이트/작업에서 이미 확인된 키를 유지하며 중복을 누적 차단한다.
    """
    if seen_keys is None:
        seen_keys = set()
    unique_items: list[ResultItem] = []
    duplicate_count = 0

    for item in items:
        key = get_dedup_key(item)
        if key in seen_keys:
            duplicate_count += 1
            continue
        seen_keys.add(key)
        unique_items.append(item)

    return unique_items, duplicate_count

