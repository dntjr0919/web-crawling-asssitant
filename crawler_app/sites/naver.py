"""네이버 포털 검색 모듈.

지원 기능:
- 뉴스 / 웹 전체 / 블로그 탭 이동 및 결과 추출
- 기간 필터(전체/1일/1주/1개월/1년) 및 정렬 조건(관련도순/최신순) 적용
- 1페이지 초과 시 스크롤 및 페이지네이션(Next Page / start 파라미터) 추가 수집
- 전용 프로필 기반 실제 로그인 상태 판정 및 수동 로그인 대기·재개 지원
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..models import ResultItem
from . import common

if TYPE_CHECKING:
    from ..runner import SiteRunContext

SITE_ID = "naver"
DISPLAY_NAME = "네이버"
PROFILE_NAME = "naver"
SUPPORTED_SEARCH_TYPES = ("news", "web", "blog")
REQUIRES_LOGIN = False

HOME_URL = "https://www.naver.com/"
LOGIN_URL = "https://nid.naver.com/nidlogin.login"
SEARCH_INPUTS = [lambda p: p.locator("#query")]
SEARCH_URL = re.compile(r"^https://search\.naver\.com/")

NEWS_TAB = [
    lambda p: p.locator('a[href*="where=news"]'),
    lambda p: p.locator('a[href*="ssc=tab.news"]'),
    lambda p: p.get_by_role("tab", name="뉴스", exact=True),
    lambda p: p.get_by_role("link", name="뉴스", exact=True),
]
NEWS_URL = re.compile(r"[?&](where=news|ssc=tab\.news)")

BLOG_TAB = [
    lambda p: p.locator('a[href*="ssc=tab.blog"]'),
    lambda p: p.locator('a[href*="where=blog"]'),
    lambda p: p.get_by_role("tab", name="블로그", exact=True),
    lambda p: p.get_by_role("link", name="블로그", exact=True),
]
BLOG_URL = re.compile(r"[?&](where=blog|ssc=tab\.blog)")

RESULT_READY = [
    "ul.list_news",
    "ul.lst_view",
    "ul.lst_total",
    "a.news_tit",
    "a.title_link",
    "a.link_tit",
    "#main_pack a[href]",
]
NEXT_PAGE_SELECTORS = [
    'a.btn_next:not([aria-disabled="true"])',
    '.sc_page_inner a.btn_next:not([aria-disabled="true"])',
]
MAX_PAGES = 5

BLOCK_URLS = [re.compile(r"captcha", re.I), re.compile(r"nid\.naver\.com/")]
BLOCK_SELECTORS = ["iframe[src*='captcha']", "#captcha", "img[src*='captcha']"]
BLOCK_TEXTS = ["자동입력 방지", "자동 입력 방지", "보안 확인", "비정상적인 접근"]
PORTAL_DOMAINS = ["naver.com", "naver.net"]

NAVER_PERIOD_MAP = {
    "1d": {"pd": "4", "p": "1d"},
    "1w": {"pd": "1", "p": "1w"},
    "1m": {"pd": "2", "p": "1m"},
    "1y": {"pd": "3", "p": "1y"},
}

# 검색 유형별(blog / news / web) 구조화 추출 → 실패 시 정제된 링크 휴리스틱
EXTRACT_JS = r"""
({max, searchType}) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const cleanTitleText = (raw) => {
    let t = clean(raw);
    // 보조 문구('새 창 열림', '새창열림') 및 앞뒤 불필요 기호 제거
    t = t.replace(/(?:\s*새\s*창\s*열림|\s*새창열림|\s*새\s*창에서\s*열기)+$/gi, '').trim();
    // 제목 끝에 붙은 URL 브레드크럼(예: 2026.09.23.smart.science.go.kr›board) 제거
    t = t.replace(/\d{4}\.\d{1,2}\.\d{1,2}\.\s*[a-z0-9.-]+\.[a-z]{2,}(?:\s*›[^\s]+)*$/i, '').trim();
    t = t.replace(/\s+[a-z0-9.-]+\.(?:co\.kr|go\.kr|or\.kr|kr|com|net|org)(?:\s*›[^\s]+)*$/i, '').trim();
    return t;
  };
  const extractAnchorTitle = (a) => {
    if (!a) return '';
    const titleAttr = a.getAttribute('title');
    if (titleAttr && cleanTitleText(titleAttr).length >= 2) {
      return cleanTitleText(titleAttr);
    }
    const clone = a.cloneNode(true);
    for (const noise of clone.querySelectorAll(
      '.blind, .ico_ad, .spnew, .url, .txt_url, .source_box, .info_group, .sub_txt, .sub_time, script, style, svg, button'
    )) {
      noise.remove();
    }
    return cleanTitleText(clone.textContent);
  };
  const abs = (h) => { try { return new URL(h, location.href).href; } catch (e) { return null; } };
  const isExcludedUrl = (href, searchType) => {
    if (!href || !/^https?:/i.test(href)) return true;
    let parsed;
    try { parsed = new URL(href); } catch (e) { return true; }
    const host = parsed.hostname.toLowerCase();
    const path = (parsed.pathname || '').toLowerCase();
    // 지도 저작권·광고·도움말·정책·로그인 링크 제외
    const blockedHosts = [
      'openstreetmap.org', 'mapbox.com', 'here.com',
      'pstatic.net', 'navercorp.com', 'help.naver.com',
      'policy.naver.com', 'nid.naver.com', 'adcr.naver.com', 'ader.naver.com',
      'search.naver.com', 'terms.naver.com',
    ];
    if (blockedHosts.some(d => host === d || host.endsWith('.' + d))) return true;
    if (/^\/(copyright|terms|privacy|policy|help|login|signin)(\/|$)/i.test(path)) return true;
    if (searchType === 'news') {
      if (host === 'naver.com' || host.endsWith('.naver.com') ||
          host === 'naver.net' || host.endsWith('.naver.net')) return true;
    } else {
      // 블로그·카페의 개별 글이 아닌 블로그 홈/카페 홈(/id) 프로필 링크는 제외
      if (['blog.naver.com', 'm.blog.naver.com', 'cafe.naver.com', 'm.cafe.naver.com'].includes(host)) {
        const segs = path.split('/').filter(Boolean);
        if (segs.length < 2 && !/postview|articleread/i.test(path)) return true;
      }
      if (searchType === 'web') {
        if ((host === 'naver.com' || host.endsWith('.naver.com') || host.endsWith('.naver.net')) &&
            !['blog.naver.com', 'm.blog.naver.com', 'post.naver.com', 'n.news.naver.com', 'news.naver.com', 'cafe.naver.com'].includes(host)) {
          return true;
        }
      }
    }
    if (!path || path === '/') return true;
    return false;
  };
  const DATE_RE = /\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?|\d+\s*(초|분|시간|일|주|개월|년)\s*전/;
  const out = [];
  const byHref = new Map();
  const push = (o) => {
    if (!o.href || isExcludedUrl(o.href, searchType)) return;
    if (!o.title || o.title.startsWith('/') || o.title.length < 2) return;
    if (byHref.has(o.href)) {
      const prev = byHref.get(o.href);
      if (!prev.description && o.description && o.description !== prev.title) {
        prev.description = o.description;
      }
      if (!prev.press && o.press) prev.press = o.press;
      if (!prev.date_text && o.date_text) prev.date_text = o.date_text;
      return;
    }
    if (out.length >= max) return;
    byHref.set(o.href, o);
    out.push(o);
  };

  if (searchType === 'blog') {
    for (const li of document.querySelectorAll('ul.lst_view > li, ul.lst_total > li, li.bx')) {
      const a = li.querySelector('a.title_link, a.api_txt_lines.total_tit, a.api_txt_lines');
      if (!a) continue;
      const author = li.querySelector('.user_info .name, .sub_txt, .user_name');
      const dsc = li.querySelector('.dsc_link, .api_txt_lines.dsc_txt, .total_dsc');
      const date = li.querySelector('.sub_time, .sub_txt');
      push({
        method: 'naver_blog_list',
        title: extractAnchorTitle(a),
        href: abs(a.getAttribute('href')),
        portal_href: null,
        press: author ? clean(author.textContent) : null,
        date_text: date ? clean(date.textContent) : null,
        description: dsc ? clean(dsc.textContent) : null,
      });
    }
  } else if (searchType === 'news') {
    for (const li of document.querySelectorAll('ul.list_news > li, div.news_wrap, li.bx')) {
      const a = li.querySelector('a.news_tit, a[data-heatmap-target=".tit"]');
      if (!a) continue;
      const infos = [...li.querySelectorAll('.info_group .info, .sds-comps-profile-info-subtext')].map((e) => clean(e.textContent));
      const portal = li.querySelector('.info_group a.info[href*="news.naver.com"], a[href*="n.news.naver.com"]');
      const press = li.querySelector('.info_group .press, .sds-comps-profile-info-title-text');
      const dsc = li.querySelector('.news_dsc, .dsc_txt_wrap, .sds-comps-text-type-body1');
      push({
        method: 'naver_list_news',
        title: extractAnchorTitle(a),
        href: abs(a.getAttribute('href')),
        portal_href: portal ? abs(portal.getAttribute('href')) : null,
        press: press ? clean(press.textContent) : null,
        date_text: infos.find((t) => DATE_RE.test(t)) || null,
        description: dsc ? clean(dsc.textContent) : null,
      });
    }
  } else {
    // searchType === 'web' (통합 웹 검색 블록: 웹문서, VIEW, 지식스니펫 등)
    const containers = document.querySelectorAll(
      '#main_pack div.total_wrap, #main_pack div.view_wrap, #main_pack li.bx, #main_pack ul.lst_total > li'
    );
    for (const box of containers) {
      if (box.closest('.ad_section, .sp_ntotal_ad, [data-ad]')) continue;
      const a = box.querySelector(
        'a.link_tit, a.title_link, a.total_tit, a.api_txt_lines.total_tit, a.news_tit, a.txt_title'
      );
      if (!a) continue;
      const href = abs(a.getAttribute('href'));
      if (!href || isExcludedUrl(href, searchType)) continue;
      const title = extractAnchorTitle(a);
      if (!title || title.length < 2) continue;
      const dscEl = box.querySelector(
        '.total_dsc, .dsc_txt, .api_txt_lines.dsc_txt, .dsc_link, .news_dsc, .api_txt_lines'
      );
      let dsc = dscEl && dscEl !== a ? clean(dscEl.textContent) : null;
      if (dsc && dsc === title) dsc = null;
      const pressEl = box.querySelector(
        '.source_box .name, .user_info .name, .info_group .press, .sub_txt.sub_name, .txt_name'
      );
      let press = pressEl ? cleanTitleText(pressEl.textContent) : null;
      const dateEl = box.querySelector('.sub_time, .sub_date, .txt_inline, .info_group .info');
      let dateText = null;
      if (dateEl) {
        const m = clean(dateEl.textContent).match(DATE_RE);
        if (m) dateText = m[0];
      }
      if (!dateText) {
        const m = clean(box.textContent).match(DATE_RE);
        if (m) dateText = m[0];
      }
      push({
        method: 'naver_web_list',
        title,
        href,
        portal_href: null,
        press,
        date_text: dateText,
        description: dsc,
      });
    }
  }

  if (out.length) return out;

  // 폴백 및 SDS 컴포넌트 구조 추출 (동일 href의 두 번째 긴 앵커를 설명으로 병합하고 상위 카드에서 출처·날짜 추출)
  const root = document.querySelector('#main_pack') || document.body;
  for (const a of root.querySelectorAll('a[href]')) {
    if (a.closest('nav, header, footer, .api_ly_map, .map_area, .ad_section, .sc_page')) continue;
    const href = abs(a.getAttribute('href'));
    if (!href || isExcludedUrl(href, searchType)) continue;
    const text = extractAnchorTitle(a);
    if (text.length < 6) continue;
    if (byHref.has(href)) {
      const existing = byHref.get(href);
      if (!existing.description && text !== existing.title && text.length >= 20) {
        existing.description = text;
        if (existing.method === 'naver_link_heuristic') {
          existing.method = searchType === 'blog' ? 'naver_blog_list' : (searchType === 'news' ? 'naver_list_news' : 'naver_web_list');
        }
      }
      continue;
    }
    if (text.length > 200) continue;
    const card = a.closest('.desktop_mode, li.bx, div.total_wrap, div.view_wrap, li, article, div.api_subject_bx');
    let dsc = null;
    let dateText = null;
    let press = null;
    let method = 'naver_link_heuristic';
    if (card) {
      const dscEl = card.querySelector('.total_dsc, .dsc_txt, .dsc_link, .news_dsc, .sds-comps-text-type-body1, p');
      if (dscEl && dscEl !== a && !a.contains(dscEl)) {
        const cand = cleanTitleText(dscEl.textContent);
        if (cand && cand !== text && cand.length >= 10) dsc = cand;
      }
      const pressEl = card.querySelector('.sds-comps-profile-info-title-text, .source_box .name, .user_info .name, .info_group .press, .sub_name');
      if (pressEl && pressEl !== a) press = cleanTitleText(pressEl.textContent);
      const dateEls = card.querySelectorAll('.sds-comps-profile-info-subtext, .sub_time, .sub_date, .txt_inline, .info_group .info');
      for (const de of dateEls) {
        const m = clean(de.textContent).match(DATE_RE);
        if (m) { dateText = m[0]; break; }
      }
      if (dsc || press || dateText) {
        method = searchType === 'blog' ? 'naver_blog_list' : (searchType === 'news' ? 'naver_list_news' : 'naver_web_list');
      }
    }
    push({
      method,
      title: text,
      href,
      portal_href: null,
      press,
      date_text: dateText,
      description: dsc,
    });
  }
  return out;
}
"""


async def check_login(page: Any) -> bool:
    """네이버 실제 로그인 여부를 쿠키(NID_AUT, NID_SES) 및 화면 요소로 확인한다."""
    if "nid.naver.com/nidlogin" in (page.url or ""):
        return False
    if await common.has_auth_cookies(page, ("NID_AUT", "NID_SES"), ("naver.com",)):
        return True
    try:
        for sel in (
            '[class*="MyView-module__my_info"]',
            '#account .my_info',
            'a[href*="nid.naver.com/user2"]',
            '.gnb_my_namebox',
        ):
            if await page.locator(sel).count() > 0:
                return True
    except Exception:
        pass
    return False


async def _detect(page: Any) -> str | None:
    return await common.detect_block(page, BLOCK_URLS, BLOCK_SELECTORS, BLOCK_TEXTS)


def _build_filter_url(current_url: str, period: str, sort: str) -> str | None:
    """기간(period)과 정렬(sort: relevance|date) 조건이 반영된 네이버 검색 URL을 생성한다."""
    has_period = period in NAVER_PERIOD_MAP
    has_sort = sort == "date"
    if not has_period and not has_sort:
        return None
    parsed = urlparse(current_url)
    query_dict = dict(parse_qsl(parsed.query))
    so_code = "dd" if has_sort else "r"
    query_dict["sort"] = "1" if has_sort else "0"
    if has_period:
        p_cfg = NAVER_PERIOD_MAP[period]
        query_dict["pd"] = p_cfg["pd"]
        p_code = p_cfg["p"]
    else:
        p_code = "all"
    query_dict["nso"] = f"so:{so_code},p:{p_code}"
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlencode(query_dict), parsed.fragment,
    ))


def _build_next_page_url(current_url: str, page_index: int) -> str:
    """네이버 검색의 다음 페이지(start=11, 21, ...) URL을 생성한다."""
    parsed = urlparse(current_url)
    query_dict = dict(parse_qsl(parsed.query))
    query_dict["start"] = str(page_index * 10 + 1)
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlencode(query_dict), parsed.fragment,
    ))


async def _advance_page(page: Any, page_index: int, ctx: "SiteRunContext") -> bool:
    """무한 스크롤 또는 다음 페이지 이동을 시도한다. 이동 성공 가능성을 반환한다."""
    try:
        prev_height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
        await page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
        await asyncio.sleep(0.7)
        new_height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
        if new_height > prev_height:
            return True
    except Exception:
        pass

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
    await common.wait_url(page, SEARCH_URL, ctx, "검색 결과")
    await ctx.ensure_accessible(page, _detect)

    if search_type == "news":
        await common.open_tab(page, NEWS_TAB, NEWS_URL, ctx, "뉴스")
    elif search_type == "blog":
        await common.open_tab(page, BLOG_TAB, BLOG_URL, ctx, "블로그")
    else:
        ctx.checkpoint("통합 웹 검색 화면 유지")

    filter_url = _build_filter_url(page.url, period, sort)
    if filter_url:
        ctx.checkpoint(f"검색 조건 적용(기간={period}, 정렬={sort})")
        await page.goto(filter_url, wait_until="domcontentloaded", timeout=ctx.navigation_timeout_ms)
        await ctx.ensure_accessible(page, _detect)

    await common.wait_for_results(page, RESULT_READY, _detect, ctx)

    collected_raw: list[dict[str, Any]] = []
    seen_hrefs: set[str] = set()
    pages_visited = 0

    for page_idx in range(1, MAX_PAGES + 1):
        ctx.checkpoint(f"결과 추출 ({page_idx}페이지)")
        batch = await page.evaluate(
            EXTRACT_JS, {"max": ctx.max_results, "searchType": search_type}
        ) or []
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
        is_portal = common.host_matches(href, PORTAL_DOMAINS)
        if search_type in ("blog", "web") and common.host_matches(
            href, ["blog.naver.com", "m.blog.naver.com", "post.naver.com", "n.news.naver.com", "news.naver.com"]
        ):
            orig = href
        elif is_portal:
            orig = None
        else:
            orig = href

        domain = common.host_of(orig or href)
        press_val = common.clean_text(row.get("press")) or (domain if search_type != "news" else None)

        items.append(ResultItem(
            query=query,
            source=SITE_ID,
            source_name=DISPLAY_NAME,
            title=title,
            search_link=href,
            original_link=orig,
            portal_link=row.get("portal_href") or (
                href if common.host_matches(href, ["news.naver.com", "n.news.naver.com"]) else None),
            description=common.clean_text(row.get("description")),
            date_text=common.clean_text(row.get("date_text")),
            press=press_val,
            domain=domain or None,
            search_type=search_type,
            extraction_method=row.get("method", ""),
        ))
    if not items:
        raise common.SiteError("결과 항목을 추출하지 못했습니다(선택자 사용자 검증 대기)")
    if items[0].extraction_method == "naver_link_heuristic":
        ctx.warning("목록 구조를 찾지 못해 링크 휴리스틱으로 추출했습니다. 제목·링크만 신뢰하고 결과를 확인하세요.")
    return items
