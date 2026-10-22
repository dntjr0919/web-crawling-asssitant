"""입력 주제(검색어)에 적합한 사이트와 수집 대상을 고르는 규칙·키워드 기반 선택 로직.

설계 원칙(web-crawling-asssitant.md):
- 입력 주제에 맞는 사이트 선택
- 초기에는 설정·키워드 규칙으로 구현 (외부 LLM API 미사용)
- 사이트 검색·추출 로직과 분리
- 선택 이유를 함께 반환하여 GUI에서 사용자가 확인하고 필요시 변경할 수 있도록 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .sites import SITES

# 블로그·경험·리뷰 성격이 강한 키워드
BLOG_KEYWORDS = (
    "후기", "리뷰", "맛집", "블로그", "체험", "여행기", "숙소", "카페", "코스",
    "인테리어", "육아", "레시피", "만들기", "꿀팁", "추천", "비교", "사용기", "개봉기",
)

# 국내 시사·정책·기관·로컬 뉴스 성격이 강한 키워드
DOMESTIC_NEWS_KEYWORDS = (
    "속보", "단독", "국회", "정부", "대통령", "장관", "시청", "도청", "지자체",
    "공고", "분양", "청약", "수능", "교육청", "경찰", "검찰", "법원", "금리", "코스피", "코스닥",
)

# 해외·글로벌·영문 기술/학술 성격이 강한 키워드
GLOBAL_KEYWORDS = (
    "global", "overseas", "paper", "arxiv", "github", "release", "benchmark",
    "documentation", "tutorial", "open source", "api", "sdk", "cve", "sec",
    "해외", "외신", "글로벌", "논문", "원문",
)

# 일반 웹 문서/공식 정보 탐색 키워드
WEB_KEYWORDS = (
    "공식", "홈페이지", "사이트", "다운로드", "매뉴얼", "가이드", "문서", "스펙", "규격",
)


@dataclass
class SiteRecommendation:
    site_ids: list[str]
    search_type: str
    reason: str


def recommend_for_query(
    query: str,
    enabled_sites: dict[str, bool] | None = None,
) -> SiteRecommendation:
    """검색어(query)와 설정된 활성 사이트를 바탕으로 추천 사이트 목록, 검색 유형, 추천 이유를 반환한다."""
    available = [sid for sid in SITES.keys() if (enabled_sites or {}).get(sid, True)]
    if not available:
        available = list(SITES.keys())

    q = (query or "").strip()
    if not q:
        return SiteRecommendation(
            site_ids=list(available),
            search_type="news",
            reason="기본 설정: 활성화된 전체 사이트(뉴스) 선택",
        )

    lowered = q.lower()

    # 1. 블로그/리뷰/경험 키워드 확인
    matched_blog = [kw for kw in BLOG_KEYWORDS if kw in lowered]
    if matched_blog:
        sites = ["naver"] if "naver" in SITES else list(available)
        return SiteRecommendation(
            site_ids=sites,
            search_type="blog",
            reason=f"블로그·리뷰 키워드({', '.join(matched_blog[:2])}) 감지 → 네이버 블로그 우선 추천",
        )

    # 2. 영문 비율 또는 글로벌·해외 키워드 확인
    matched_global = [kw for kw in GLOBAL_KEYWORDS if kw in lowered]
    ascii_letters = len(re.findall(r"[A-Za-z]", q))
    hangul_letters = len(re.findall(r"[가-힣]", q))
    is_mostly_english = ascii_letters >= 4 and hangul_letters == 0

    if matched_global or is_mostly_english:
        sites = ["google"] if "google" in SITES else list(available)
        search_type = "web" if is_mostly_english or any(k in lowered for k in WEB_KEYWORDS) else "news"
        why = f"글로벌·영문 키워드({matched_global[0]})" if matched_global else "영문 검색어"
        type_label = "웹 전체" if search_type == "web" else "뉴스"
        return SiteRecommendation(
            site_ids=sites,
            search_type=search_type,
            reason=f"{why} 감지 → 구글({type_label}) 우선 추천",
        )

    # 3. 일반 웹 문서/가이드 키워드 확인
    matched_web = [kw for kw in WEB_KEYWORDS if kw in lowered]
    if matched_web:
        return SiteRecommendation(
            site_ids=list(available),
            search_type="web",
            reason=f"정보·문서 키워드({', '.join(matched_web[:2])}) 감지 → 통합 웹 검색 추천",
        )

    # 4. 국내 현안/속보 키워드 확인
    matched_domestic = [kw for kw in DOMESTIC_NEWS_KEYWORDS if kw in lowered]
    if matched_domestic:
        return SiteRecommendation(
            site_ids=list(available),
            search_type="news",
            reason=f"국내 시사·현안 키워드({', '.join(matched_domestic[:2])}) 감지 → 네이버·구글 뉴스 동시 검색 추천",
        )

    # 5. 기본값: 활성화된 전체 사이트 뉴스 검색
    return SiteRecommendation(
        site_ids=list(available),
        search_type="news",
        reason="일반 주제 → 네이버·구글 뉴스 검색 추천 (체크박스로 변경 가능)",
    )
