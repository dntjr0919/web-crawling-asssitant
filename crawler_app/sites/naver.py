"""네이버 포털 검색 모듈.

근거와 검증 상태:
- 홈 URL, 검색 입력칸 #query, search.naver.com 이동: 네이버 홈페이지 HTML에서 #query 확인,
  사용자 VS Code에서 검색 실행 검증 완료(open_naver.py).
- 뉴스 탭·결과 영역·결과 항목 선택자: 미검증. 화면 구조가 바뀌면 이 파일 상단 상수만 고친다.
- 접근 제한 판정 문구: 미검증 휴리스틱.
- 정렬·기간 조건은 적용하지 않는다(사이트 기본 정렬). 최신순이라고 표시하지 않는다.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..models import ResultItem
from . import common

if TYPE_CHECKING:
    from ..runner import SiteRunContext

SITE_ID = "naver"
DISPLAY_NAME = "네이버"
PROFILE_NAME = "naver"
SUPPORTED_SEARCH_TYPES = ("news", "web", "blog")

HOME_URL = "https://www.naver.com/"
SEARCH_INPUTS = [lambda p: p.locator("#query")]
SEARCH_URL = re.compile(r"^https://search\.naver\.com/")

# --- 이하 사용자 검증 대기 선택자 ---
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
    "#main_pack a[href]",
]
BLOCK_URLS = [re.compile(r"captcha", re.I), re.compile(r"nid\.naver\.com/")]
BLOCK_SELECTORS = ["iframe[src*='captcha']", "#captcha", "img[src*='captcha']"]
BLOCK_TEXTS = ["자동입력 방지", "자동 입력 방지", "보안 확인", "비정상적인 접근"]
PORTAL_DOMAINS = ["naver.com", "naver.net"]

NAVER_PERIOD_MAP = {
    "1d": {"pd": "4", "nso": "so:r,p:1d"},
    "1w": {"pd": "1", "nso": "so:r,p:1w"},
    "1m": {"pd": "2", "nso": "so:r,p:1m"},
    "1y": {"pd": "3", "nso": "so:r,p:1y"},
}

# 1) 기존에 알려진 목록 구조(ul.list_news) → 2) 실패 시 링크 휴리스틱
EXTRACT_JS = r"""
({max, searchType}) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const abs = (h) => { try { return new URL(h, location.href).href; } catch (e) { return null; } };
  const out = [];
  const seen = new Set();
  const push = (o) => {
    if (!o.href || !/^https?:/.test(o.href) || seen.has(o.href) || out.length >= max) return;
    seen.add(o.href);
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
        title: clean(a.textContent),
        href: abs(a.getAttribute('href')),
        portal_href: null,
        press: author ? clean(author.textContent) : null,
        date_text: date ? clean(date.textContent) : null,
        description: dsc ? clean(dsc.textContent) : null,
      });
    }
  } else {
    for (const li of document.querySelectorAll('ul.list_news > li')) {
      const a = li.querySelector('a.news_tit');
      if (!a) continue;
      const infos = [...li.querySelectorAll('.info_group .info')].map((e) => clean(e.textContent));
      const portal = li.querySelector('.info_group a.info[href*="news.naver.com"]');
      const press = li.querySelector('.info_group .press');
      const dsc = li.querySelector('.news_dsc, .dsc_txt_wrap');
      push({
        method: 'naver_list_news',
        title: clean(a.getAttribute('title') || a.textContent),
        href: abs(a.getAttribute('href')),
        portal_href: portal ? abs(portal.getAttribute('href')) : null,
        press: press ? clean(press.textContent) : null,
        date_text: infos.find((t) => /전$|\d{4}\.\d{1,2}\.\d{1,2}/.test(t)) || null,
        description: dsc ? clean(dsc.textContent) : null,
      });
    }
  }
  if (out.length) return out;
  const root = document.querySelector('#main_pack') || document.body;
  for (const a of root.querySelectorAll('a[href]')) {
    const href = abs(a.getAttribute('href'));
    if (!href) continue;
    let parsed;
    try { parsed = new URL(href); } catch (e) { continue; }
    const host = parsed.hostname.toLowerCase();
    if (searchType !== 'blog') {
      if (host === 'naver.com' || host.endsWith('.naver.com') ||
          host === 'naver.net' || host.endsWith('.naver.net')) continue;
    }
    if (!parsed.pathname || parsed.pathname === '/') continue;
    const title = clean(a.textContent);
    if (title.length < 10 || title.length > 200) continue;
    push({ method: 'naver_link_heuristic', title, href, portal_href: null,
           press: null, date_text: null, description: null });
  }
  return out;
}
"""


async def _detect(page: Any) -> str | None:
    return await common.detect_block(page, BLOCK_URLS, BLOCK_SELECTORS, BLOCK_TEXTS)


async def run(page: Any, query: str, ctx: "SiteRunContext") -> list[ResultItem]:
    search_type = getattr(ctx, "search_type", "news")
    await common.goto_home(page, HOME_URL, ctx)
    await ctx.ensure_accessible(page, _detect)
    await common.submit_search(page, SEARCH_INPUTS, query, ctx)
    await common.wait_url(page, SEARCH_URL, ctx, "검색 결과")
    await ctx.ensure_accessible(page, _detect)
    if search_type == "news":
        await common.open_tab(page, NEWS_TAB, NEWS_URL, ctx, "뉴스")
    elif search_type == "blog":
        await common.open_tab(page, BLOG_TAB, BLOG_URL, ctx, "블로그")
    else:
        ctx.checkpoint("통합 웹 검색 화면 유지")

    if getattr(ctx, "period", "all") in NAVER_PERIOD_MAP:
        period_cfg = NAVER_PERIOD_MAP[ctx.period]
        ctx.checkpoint(f"기간 조건 적용({ctx.period})")
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
        parsed = urlparse(page.url)
        query_dict = dict(parse_qsl(parsed.query))
        query_dict["pd"] = period_cfg["pd"]
        query_dict["nso"] = period_cfg["nso"]
        new_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query_dict), parsed.fragment))
        await page.goto(new_url, wait_until="domcontentloaded", timeout=ctx.navigation_timeout_ms)
        await ctx.ensure_accessible(page, _detect)
    await common.wait_for_results(page, RESULT_READY, _detect, ctx)

    ctx.checkpoint("결과 추출")
    raw = await page.evaluate(EXTRACT_JS, {"max": ctx.max_results, "searchType": search_type}) or []
    ctx.final_url = page.url
    items: list[ResultItem] = []
    for row in raw[: ctx.max_results]:
        title = common.clean_text(row.get("title"))
        href = row.get("href")
        if not title or not href:
            continue
        is_portal = common.host_matches(href, PORTAL_DOMAINS)
        if search_type == "blog" and "blog.naver.com" in href:
            orig = href
        elif is_portal:
            orig = None
        else:
            orig = href

        items.append(ResultItem(
            query=query,
            source=SITE_ID,
            source_name=DISPLAY_NAME,
            title=title,
            search_link=href,
            original_link=orig,
            portal_link=row.get("portal_href") or (
                href if common.host_matches(href, ["news.naver.com"]) else None),
            description=common.clean_text(row.get("description")),
            date_text=common.clean_text(row.get("date_text")),
            press=common.clean_text(row.get("press")),
            extraction_method=row.get("method", ""),
        ))
    if not items:
        raise common.SiteError("결과 항목을 추출하지 못했습니다(선택자 사용자 검증 대기)")
    if items[0].extraction_method == "naver_link_heuristic":
        ctx.warning("목록 구조를 찾지 못해 링크 휴리스틱으로 추출했습니다. 제목·링크만 신뢰하고 결과를 확인하세요.")
    return items