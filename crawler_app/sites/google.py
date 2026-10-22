"""구글 포털 검색 모듈.

지원 기능:
- 뉴스 / 웹 전체 / 블로그 검색 및 결과 추출
- 기간 필터(전체/1일/1주/1개월/1년) 및 정렬 조건(관련도순/최신순) 적용
- 1페이지 초과 시 페이지네이션(#pnnext 또는 start 파라미터) 추가 수집
- 전용 프로필 기반 실제 로그인 상태 판정 및 수동 로그인/CAPTCHA 대기·재개 지원
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from ..models import ResultItem
from . import common

if TYPE_CHECKING:
    from ..runner import SiteRunContext

SITE_ID = "google"
DISPLAY_NAME = "구글"
PROFILE_NAME = "google"
SUPPORTED_SEARCH_TYPES = ("news", "web", "blog")
REQUIRES_LOGIN = False

HOME_URL = "https://www.google.com/"
LOGIN_URL = "https://accounts.google.com/ServiceLogin"

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
    "#rso div.MjjYud",
    "#rso div.g",
    "#rso a[href] [role='heading']",
    "#search a[href] h3",
]
NEXT_PAGE_SELECTORS = [
    "a#pnnext",
    'a[aria-label="다음"]',
    'a[aria-label="Next"]',
]
MAX_PAGES = 5

BLOCK_URLS = [re.compile(r"/sorry/"), re.compile(r"^https://consent\.google\.")]
BLOCK_SELECTORS = ["iframe[src*='recaptcha']", "#captcha-form", "form#captcha-form"]
BLOCK_TEXTS = ["unusual traffic", "비정상적인 트래픽", "로봇이 아닙니다", "not a robot"]
PORTAL_DOMAINS = ["google.com", "google.co.kr", "googleusercontent.com", "gstatic.com"]

GOOGLE_PERIOD_MAP = {
    "1d": "qdr:d",
    "1w": "qdr:w",
    "1m": "qdr:m",
    "1y": "qdr:y",
}

# 1) 뉴스 카드(div.SoaBEf) → 2) 일반 웹 카드(div.MjjYud / div.g) → 3) 제목 링크 휴리스틱
EXTRACT_JS = r"""
(max) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const cleanTitle = (s) => clean(s).replace(/(?:\s*새\s*창\s*열림|\s*새창열림|\s*새\s*창에서\s*열기)+$/gi, '').trim();
  const abs = (h) => { try { return new URL(h, location.href).href; } catch (e) { return null; } };
  const isExcludedUrl = (href) => {
    if (!href || !/^https?:/i.test(href)) return true;
    let parsed;
    try { parsed = new URL(href); } catch (e) { return true; }
    const host = parsed.hostname.toLowerCase();
    const path = (parsed.pathname || '').toLowerCase();
    if (['openstreetmap.org', 'support.google.com', 'policies.google.com', 'accounts.google.com', 'maps.google.com'].some(
      d => host === d || host.endsWith('.' + d)
    )) return true;
    if ((host.endsWith('.google.com') || host.endsWith('.google.co.kr') || host === 'google.com') &&
        path !== '/goto' && path !== '/url') {
      return true;
    }
    if (/^\/(copyright|terms|privacy|policy|preferences|advanced_search)(\/|$)/i.test(path)) return true;
    return false;
  };
  const out = [];
  const seen = new Set();
  const push = (o) => {
    if (!o.href || isExcludedUrl(o.href) || seen.has(o.href) || out.length >= max) return;
    if (!o.title || o.title.startsWith('/') || o.title.length < 2) return;
    seen.add(o.href);
    out.push(o);
  };

  // 1) 구글 뉴스 전용 카드 (div.SoaBEf)
  for (const card of document.querySelectorAll('#rso div.SoaBEf')) {
    const a = card.querySelector('a[href]');
    const h = card.querySelector('[role="heading"], h3');
    if (!a || !h) continue;
    const press = card.querySelector('.MgUUmf, .VuuXrf');
    const date = card.querySelector('.OSrXXb, .LfVVr, .LEwnzc');
    const dsc = card.querySelector('.GI74Re, .VwiC3b');
    push({
      method: 'google_news_card',
      title: cleanTitle(h.textContent),
      href: abs(a.getAttribute('href')),
      press: press ? clean(press.textContent) : null,
      date_text: date ? clean(date.textContent) : null,
      description: dsc ? clean(dsc.textContent) : null,
    });
  }
  if (out.length) return out;

  // 2) 구글 일반 웹 검색 결과 및 제목 링크 추출 (카드 단위 메타데이터 포함)
  const root = document.querySelector('#rso') || document.querySelector('#search') || document.body;
  for (const a of root.querySelectorAll('a[href]')) {
    const h = a.querySelector('[role="heading"], h3');
    if (!h) continue;
    const href = abs(a.getAttribute('href'));
    if (!href || isExcludedUrl(href)) continue;
    const card = a.closest('div.MjjYud, div.g, div.tF2Cxc, div.SoaBEf, div[data-hveid]');
    let press = null;
    let dateText = null;
    let dsc = null;
    let method = 'google_heading_heuristic';
    if (card) {
      const pressEl = card.querySelector('.VuuXrf, .MgUUmf');
      if (pressEl) press = clean(pressEl.textContent);
      const dateEl = card.querySelector('.LEwnzc, .OSrXXb, .LfVVr, .YrbPuc');
      if (dateEl) dateText = clean(dateEl.textContent).replace(/[—·-]\s*$/, '').trim();
      const dscEl = card.querySelector('.VwiC3b, .GI74Re, [data-sncf="1"], .ITZIwc');
      if (dscEl) {
        let rawDsc = clean(dscEl.textContent);
        // 스니펫 앞머리에 날짜(예: "2026. 10. 15. — ")가 포함된 경우 분리
        const dm = rawDsc.match(/^(\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?|\d+\s*(?:시간|일|주|개월|년)\s*전)\s*[—·-]\s*(.+)$/);
        if (dm) {
          if (!dateText) dateText = dm[1].trim();
          rawDsc = dm[2].trim();
        }
        dsc = rawDsc || null;
      }
      if (press || dsc || dateText) {
        method = 'google_web_card';
      }
    }
    push({
      method,
      title: cleanTitle(h.textContent),
      href,
      press,
      date_text: dateText,
      description: dsc,
    });
  }
  return out;
}
"""


async def check_login(page: Any) -> bool:
    """구글 실제 로그인 여부를 쿠키(SID, SSID, HSID) 및 계정 화면 요소로 확인한다."""
    url = page.url or ""
    if "accounts.google.com/ServiceLogin" in url or "accounts.google.com/v3/signin" in url:
        return False
    if await common.has_auth_cookies(page, ("SID", "HSID"), ("google.com", "google.co.kr")):
        return True
    try:
        for sel in (
            'a[aria-label*="Google 계정"]',
            'a[aria-label*="Google Account"]',
            'a[href*="accounts.google.com/SignOutOptions"]',
        ):
            if await page.locator(sel).count() > 0:
                return True
    except Exception:
        pass
    return False


def resolve_original(href: str) -> str | None:
    """구글 내부 이동 링크(/url?q=, /goto?url=)면 실제 주소 또는 리다이렉트 게이트웨이 주소를 반환한다.
    구글 자체 서비스 도메인(검색, 계정 등)이면 None."""
    parsed = urlparse(href)
    is_google = common.host_matches(href, PORTAL_DOMAINS)
    if is_google and parsed.path == "/goto" and "url=" in parsed.query:
        return href
    if is_google and parsed.path == "/url":
        qs = parse_qs(parsed.query)
        target = (qs.get("q") or qs.get("url") or [None])[0]
        if target and target.startswith(("http://", "https://")) and not common.host_matches(target, PORTAL_DOMAINS):
            return target
        return None
    if is_google:
        return None
    return href


def _build_filter_url(current_url: str, period: str, sort: str) -> str | None:
    """기간(period)과 정렬(sort: relevance|date) 조건이 반영된 구글 검색 URL을 생성한다."""
    tbs_parts: list[str] = []
    if period in GOOGLE_PERIOD_MAP:
        tbs_parts.append(GOOGLE_PERIOD_MAP[period])
    if sort == "date":
        tbs_parts.append("sbd:1")
    if not tbs_parts:
        return None
    parsed = urlparse(current_url)
    query_dict = dict(parse_qsl(parsed.query))
    query_dict["tbs"] = ",".join(tbs_parts)
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlencode(query_dict), parsed.fragment,
    ))


def _build_next_page_url(current_url: str, page_index: int) -> str:
    """구글 검색의 다음 페이지(start=10, 20, ...) URL을 생성한다."""
    parsed = urlparse(current_url)
    query_dict = dict(parse_qsl(parsed.query))
    query_dict["start"] = str(page_index * 10)
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlencode(query_dict), parsed.fragment,
    ))


async def _advance_page(page: Any, page_index: int, ctx: "SiteRunContext") -> bool:
    """구글 검색 결과의 다음 페이지로 이동한다."""
    for sel in NEXT_PAGE_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=ctx.action_timeout_ms)
                await page.wait_for_load_state("domcontentloaded", timeout=ctx.navigation_timeout_ms)
                return True
        except Exception:
            continue

    next_url = _build_next_page_url(page.url, page_index)
    if next_url == page.url:
        return False
    try:
        response = await page.goto(next_url, wait_until="domcontentloaded", timeout=ctx.navigation_timeout_ms)
        return response is not None and response.status < 400
    except Exception:
        return False


async def _detect(page: Any) -> str | None:
    return await common.detect_block(page, BLOCK_URLS, BLOCK_SELECTORS, BLOCK_TEXTS)


async def run(page: Any, query: str, ctx: "SiteRunContext") -> list[ResultItem]:
    search_type = getattr(ctx, "search_type", "news")
    period = getattr(ctx, "period", "all")
    sort = getattr(ctx, "sort", "relevance")

    await common.goto_home(page, HOME_URL, ctx)
    await ctx.ensure_accessible(page, _detect)
    await common.ensure_login(
        page, HOME_URL, LOGIN_URL, check_login, ctx, site_requires_login=REQUIRES_LOGIN
    )
    await common.submit_search(page, SEARCH_INPUTS, query, ctx)
    try:
        await common.wait_url(page, SEARCH_URL, ctx, "검색 결과")
    except common.SiteError:
        await ctx.ensure_accessible(page, _detect)
        if not SEARCH_URL.search(page.url):
            raise
    await ctx.ensure_accessible(page, _detect)

    if search_type == "news":
        await common.open_tab(page, NEWS_TAB, NEWS_URL, ctx, "뉴스")
    else:
        ctx.checkpoint("일반 웹 검색 화면 유지")

    filter_url = _build_filter_url(page.url, period, sort)
    if filter_url:
        ctx.checkpoint(f"검색 조건 적용(기간={period}, 정렬={sort})")
        await page.goto(filter_url, wait_until="domcontentloaded", timeout=ctx.navigation_timeout_ms)
        await ctx.ensure_accessible(page, _detect)

    await ctx.ensure_accessible(page, _detect)
    await common.wait_for_results(page, RESULT_READY, _detect, ctx)

    collected_raw: list[dict[str, Any]] = []
    seen_hrefs: set[str] = set()
    pages_visited = 0

    for page_idx in range(1, MAX_PAGES + 1):
        ctx.checkpoint(f"결과 추출 ({page_idx}페이지)")
        batch = await page.evaluate(EXTRACT_JS, ctx.max_results) or []
        pages_visited = page_idx
        ctx.pages_collected = pages_visited
        ctx.final_url = page.url

        added_in_batch = 0
        for row in batch:
            href = row.get("href")
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            collected_raw.append(row)
            added_in_batch += 1
            if len(collected_raw) >= ctx.max_results:
                break

        if len(collected_raw) >= ctx.max_results:
            break
        if page_idx >= MAX_PAGES or added_in_batch == 0:
            break

        ctx.checkpoint(f"추가 결과 페이지 이동 ({page_idx + 1}페이지)")
        advanced = await _advance_page(page, page_idx, ctx)
        if not advanced:
            break
        await ctx.ensure_accessible(page, _detect)
        try:
            await common.wait_for_results(page, RESULT_READY, _detect, ctx)
        except common.SiteError:
            break

    items: list[ResultItem] = []
    for row in collected_raw[: ctx.max_results]:
        title = common.clean_text(row.get("title"))
        href = row.get("href")
        if not title or not href:
            continue
        orig = resolve_original(href)
        domain = common.host_of(orig) if orig and not common.host_matches(orig, PORTAL_DOMAINS) else None
        items.append(ResultItem(
            query=query,
            source=SITE_ID,
            source_name=DISPLAY_NAME,
            title=title,
            search_link=href,
            original_link=orig,
            description=common.clean_text(row.get("description")),
            date_text=common.clean_text(row.get("date_text")),
            press=common.clean_text(row.get("press")) or domain,
            domain=domain,
            search_type=search_type,
            extraction_method=row.get("method", ""),
        ))
    if not items:
        raise common.SiteError("결과 항목을 추출하지 못했습니다(선택자 사용자 검증 대기)")
    if items[0].extraction_method == "google_heading_heuristic":
        ctx.warning("카드 메타데이터를 일부 찾지 못해 제목 링크 위주로 추출했습니다. 결과를 확인하세요.")
    return items
