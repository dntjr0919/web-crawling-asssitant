"""구글 포털 검색 모듈(Google 검색의 '뉴스' 탭 기준).

근거와 검증 상태:
- 홈 URL 외 모든 선택자(검색 입력칸, 뉴스 탭, 결과 카드)는 사용자 실제 화면 검증 전이다.
  화면 구조가 바뀌면 이 파일 상단 상수만 고친다.
- Google 서비스 약관은 robots.txt를 위반하는 자동화 접근을 금지하며, google.com/robots.txt는
  /search를 Disallow 한다(PROJECT_STATUS.md 제약 참고).
- /sorry/ 화면·reCAPTCHA 등이 보이면 사용자 확인 상태로 바꾸고, 자동 우회는 하지 않는다.
- 정렬·기간 조건은 적용하지 않는다(사이트 기본 정렬).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..models import ResultItem
from . import common

if TYPE_CHECKING:
    from ..runner import SiteRunContext

SITE_ID = "google"
DISPLAY_NAME = "구글"
PROFILE_NAME = "google"
SUPPORTED_SEARCH_TYPES = ("news",)

HOME_URL = "https://www.google.com/"

# --- 이하 사용자 검증 대기 선택자 ---
SEARCH_INPUTS = [
    lambda p: p.locator('textarea[name="q"]'),
    lambda p: p.locator('input[name="q"]'),
]
SEARCH_URL = re.compile(r"^https://www\.google\.[^/]+/search\?")
NEWS_TAB = [
    lambda p: p.locator('a[href*="tbm=nws"]'),
    lambda p: p.locator('a[href*="udm=12"]'),
    lambda p: p.get_by_role("link", name="뉴스", exact=True),
    lambda p: p.get_by_role("link", name="News", exact=True),
]
NEWS_URL = re.compile(r"[?&](tbm=nws|udm=12)(&|$)")
RESULT_READY = [
    "#rso div.SoaBEf",
    "#rso a[href] [role='heading']",
    "#search a[href] h3",
]
BLOCK_URLS = [re.compile(r"/sorry/"), re.compile(r"^https://consent\.google\.")]
BLOCK_SELECTORS = ["iframe[src*='recaptcha']", "#captcha-form", "form#captcha-form"]
BLOCK_TEXTS = ["unusual traffic", "비정상적인 트래픽", "로봇이 아닙니다", "not a robot"]
PORTAL_DOMAINS = ["google.com", "google.co.kr", "googleusercontent.com", "gstatic.com"]

# 1) 뉴스 카드 구조(div.SoaBEf) → 2) 실패 시 제목 요소를 가진 링크 휴리스틱
EXTRACT_JS = r"""
(max) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const abs = (h) => { try { return new URL(h, location.href).href; } catch (e) { return null; } };
  const out = [];
  const seen = new Set();
  const push = (o) => {
    if (!o.href || !/^https?:/.test(o.href) || seen.has(o.href) || out.length >= max) return;
    seen.add(o.href);
    out.push(o);
  };
  for (const card of document.querySelectorAll('#rso div.SoaBEf')) {
    const a = card.querySelector('a[href]');
    const h = card.querySelector('[role="heading"]');
    if (!a || !h) continue;
    const press = card.querySelector('.MgUUmf');
    const date = card.querySelector('.OSrXXb, .LfVVr');
    const dsc = card.querySelector('.GI74Re');
    push({
      method: 'google_news_card',
      title: clean(h.textContent),
      href: abs(a.getAttribute('href')),
      press: press ? clean(press.textContent) : null,
      date_text: date ? clean(date.textContent) : null,
      description: dsc ? clean(dsc.textContent) : null,
    });
  }
  if (out.length) return out;
  const root = document.querySelector('#rso') || document.querySelector('#search') || document.body;
  for (const a of root.querySelectorAll('a[href]')) {
    const h = a.querySelector('[role="heading"], h3');
    if (!h) continue;
    push({ method: 'google_heading_heuristic', title: clean(h.textContent),
           href: abs(a.getAttribute('href')), press: null, date_text: null, description: null });
  }
  return out;
}
"""


def resolve_original(href: str) -> str | None:
    """구글 내부 이동 링크(/url?q=)면 실제 주소를 꺼낸다. 구글 도메인이면 None."""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(href)
    if common.host_matches(href, PORTAL_DOMAINS) and parsed.path == "/url":
        qs = parse_qs(parsed.query)
        target = (qs.get("q") or qs.get("url") or [None])[0]
        if target and target.startswith(("http://", "https://")) and not common.host_matches(target, PORTAL_DOMAINS):
            return target
        return None
    if common.host_matches(href, PORTAL_DOMAINS):
        return None
    return href


async def _detect(page: Any) -> str | None:
    return await common.detect_block(page, BLOCK_URLS, BLOCK_SELECTORS, BLOCK_TEXTS)


async def run(page: Any, query: str, ctx: "SiteRunContext") -> list[ResultItem]:
    await common.goto_home(page, HOME_URL, ctx)
    await ctx.ensure_accessible(page, _detect)
    await common.submit_search(page, SEARCH_INPUTS, query, ctx)
    try:
        await common.wait_url(page, SEARCH_URL, ctx, "검색 결과")
    except common.SiteError:
        # 제한 화면(/sorry/)으로 이동한 경우 사용자 확인 후 다시 판단한다.
        await ctx.ensure_accessible(page, _detect)
        if not SEARCH_URL.search(page.url):
            raise
    await ctx.ensure_accessible(page, _detect)
    await common.open_tab(page, NEWS_TAB, NEWS_URL, ctx, "뉴스")
    await ctx.ensure_accessible(page, _detect)
    await common.wait_for_results(page, RESULT_READY, _detect, ctx)

    ctx.checkpoint("결과 추출")
    raw = await page.evaluate(EXTRACT_JS, ctx.max_results)
    ctx.final_url = page.url
    items: list[ResultItem] = []
    for row in raw[: ctx.max_results]:
        title = common.clean_text(row.get("title"))
        href = row.get("href")
        if not title or not href:
            continue
        items.append(ResultItem(
            query=query,
            source=SITE_ID,
            source_name=DISPLAY_NAME,
            title=title,
            search_link=href,
            original_link=resolve_original(href),
            description=common.clean_text(row.get("description")),
            date_text=common.clean_text(row.get("date_text")),
            press=common.clean_text(row.get("press")),
            extraction_method=row.get("method", ""),
        ))
    if not items:
        raise common.SiteError("결과 항목을 추출하지 못했습니다(선택자 사용자 검증 대기)")
    if items[0].extraction_method == "google_heading_heuristic":
        ctx.warning("뉴스 카드 구조를 찾지 못해 제목 링크 휴리스틱으로 추출했습니다. 결과를 확인하세요.")
    return items
