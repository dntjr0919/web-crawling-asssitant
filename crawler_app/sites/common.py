"""사이트 모듈이 함께 쓰는 Playwright 동작과 실행 규약.

사이트 모듈 규약 (sites/naver.py, sites/google.py):
- SITE_ID, DISPLAY_NAME, PROFILE_NAME, SUPPORTED_SEARCH_TYPES, REQUIRES_LOGIN, LOGIN_URL 상수
- async def check_login(page) -> bool
- async def run(page, query, ctx) -> list[ResultItem]
- 사이트 화면 구조에 의존하는 선택자는 각 사이트 모듈 상단 상수에 모은다.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

if TYPE_CHECKING:
    from ..runner import SiteRunContext

logger = logging.getLogger(__name__)

LocatorFactory = Callable[[Any], Any]          # page -> Locator
Detector = Callable[[Any], Awaitable[str | None]]
LoginChecker = Callable[[Any], Awaitable[bool]]


class SiteError(RuntimeError):
    """사이트 동작 실패. 메시지는 사용자에게 그대로 보인다."""


class SiteBlocked(SiteError):
    """접근 제한·추가 인증·CAPTCHA·로그인 미완료가 계속 보여 중지한 경우."""


def host_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def host_matches(url: str | None, domains: Sequence[str]) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in domains)


async def has_auth_cookies(page: Any, cookie_names: Sequence[str],
                           domain_keywords: Sequence[str]) -> bool:
    """컨텍스트 쿠키 중 지정된 도메인의 인증 쿠키가 모두 존재하는지 확인한다."""
    try:
        cookies = await page.context.cookies()
    except Exception:
        return False
    found: set[str] = set()
    for c in cookies:
        domain = (c.get("domain") or "").lower()
        name = c.get("name") or ""
        value = c.get("value") or ""
        if value and any(dk in domain for dk in domain_keywords):
            found.add(name)
    return all(req in found for req in cookie_names)


async def ensure_login(
    page: Any,
    home_url: str,
    login_url: str | None,
    check_fn: LoginChecker,
    ctx: "SiteRunContext",
    site_requires_login: bool = False,
) -> bool:
    """실제 로그인 여부를 판정하고, 로그인이 필요한 경우 사용자 수동 로그인 후 재확인한다.

    프로필 폴더가 존재한다는 이유만으로 로그인 성공으로 간주하지 않고 check_fn으로 검증한다.
    """
    ctx.checkpoint("로그인 상태 확인")
    is_logged = await check_fn(page)
    ctx.logged_in = is_logged
    must_login = site_requires_login or getattr(ctx, "require_login", False)

    if is_logged:
        ctx.info("로그인 상태 판정: 로그인됨 (전용 프로필 세션 확인)")
        return True

    if not must_login:
        ctx.info("로그인 상태 판정: 비로그인 (공개 검색 모드로 계속 진행)")
        return False

    ctx.warning("로그인이 필요한 작업이지만 현재 비로그인 상태입니다.")
    if login_url and not page.url.startswith(login_url):
        try:
            await page.goto(login_url, wait_until="domcontentloaded",
                            timeout=ctx.navigation_timeout_ms)
        except PlaywrightError as exc:
            ctx.warning(f"로그인 페이지 이동 중 경고: {exc}")

    rounds = 0
    max_rounds = getattr(ctx, "_max_rounds", 3)
    while not is_logged:
        rounds += 1
        if rounds > max_rounds:
            raise SiteBlocked(f"사용자 확인 {max_rounds}회 후에도 로그인 상태가 확인되지 않아 중지했습니다")
        await ctx.request_user(
            "로그인이 필요합니다. 열린 브라우저에서 직접 로그인한 뒤 앱의 '계속' 버튼을 누르세요."
        )
        # 로그인 후 홈 화면으로 이동해 상태를 다시 검증한다.
        if not host_matches(page.url, [host_of(home_url)]):
            try:
                await page.goto(home_url, wait_until="domcontentloaded",
                                timeout=ctx.navigation_timeout_ms)
            except PlaywrightError:
                pass
        is_logged = await check_fn(page)
        ctx.logged_in = is_logged

    ctx.info("수동 로그인 확인 완료 - 자동화를 재개합니다")
    if not page.url.startswith(home_url):
        await goto_home(page, home_url, ctx)
    return True


async def goto_home(page: Any, url: str, ctx: "SiteRunContext") -> None:
    ctx.checkpoint("포털 열기")
    response = await page.goto(url, wait_until="domcontentloaded")
    status = response.status if response else None
    ctx.info(f"포털 접속: {page.url} (HTTP {status if status is not None else '응답 없음'})")
    if status is not None and status >= 400:
        raise SiteError(f"포털 응답 오류: HTTP {status}")


async def first_visible(page: Any, candidates: Sequence[LocatorFactory],
                        timeout_ms: int) -> Any | None:
    """후보 선택자를 순서대로 시도해 처음 보이는 요소를 돌려준다."""
    per = max(1_000, timeout_ms // max(1, len(candidates)))
    for factory in candidates:
        locator = factory(page).first
        try:
            await locator.wait_for(state="visible", timeout=per)
            return locator
        except PlaywrightTimeoutError:
            continue
    return None


async def submit_search(page: Any, input_candidates: Sequence[LocatorFactory],
                        query: str, ctx: "SiteRunContext") -> None:
    ctx.checkpoint("검색어 입력")
    box = await first_visible(page, input_candidates, ctx.action_timeout_ms)
    if box is None:
        raise SiteError("검색 입력칸을 찾지 못했습니다(선택자 확인 필요)")
    await box.fill(query)
    ctx.checkpoint("검색 실행")
    await box.press("Enter")


async def wait_url(page: Any, pattern: re.Pattern[str], ctx: "SiteRunContext",
                   what: str) -> None:
    try:
        await page.wait_for_url(pattern, wait_until="domcontentloaded",
                                timeout=ctx.navigation_timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise SiteError(f"{what} 화면으로 이동하지 않았습니다. 현재 URL: {page.url}") from exc


async def open_tab(page: Any, tab_candidates: Sequence[LocatorFactory],
                   url_pattern: re.Pattern[str], ctx: "SiteRunContext",
                   tab_name: str) -> None:
    """검색 결과의 탭(예: 뉴스)으로 이동한다.

    찾지 못하면 사용자에게 직접 탭을 열도록 요청하고, '계속' 후 URL을 다시 확인한다.
    """
    ctx.checkpoint(f"{tab_name} 탭 이동")
    if url_pattern.search(page.url):
        return
    tab = await first_visible(page, tab_candidates, ctx.action_timeout_ms)
    if tab is not None:
        await tab.click()
        try:
            await page.wait_for_url(url_pattern, wait_until="domcontentloaded",
                                    timeout=ctx.navigation_timeout_ms)
            return
        except PlaywrightTimeoutError:
            ctx.warning(f"{tab_name} 탭 클릭 후 URL이 바뀌지 않았습니다: {page.url}")
    await ctx.request_user(
        f"{tab_name} 탭을 자동으로 열지 못했습니다. 브라우저에서 직접 {tab_name} 탭을 연 뒤 '계속'을 누르세요."
    )
    if not url_pattern.search(page.url):
        raise SiteError(f"{tab_name} 탭 화면을 확인하지 못했습니다. 현재 URL: {page.url}")


async def detect_block(page: Any, url_patterns: Sequence[re.Pattern[str]],
                       selectors: Sequence[str], text_markers: Sequence[str],
                       short_page_chars: int = 5_000) -> str | None:
    """접근 제한·인증 화면으로 보이면 이유 문자열, 아니면 None."""
    url = page.url
    for pattern in url_patterns:
        if pattern.search(url):
            return f"접근 제한 또는 인증 화면으로 보이는 URL: {url}"
    for selector in selectors:
        try:
            if await page.locator(selector).count() > 0:
                return f"인증 요소 발견: {selector}"
        except PlaywrightError:
            continue
    if text_markers:
        try:
            body = await page.locator("body").inner_text(timeout=3_000)
        except PlaywrightError:
            return None
        if len(body) <= short_page_chars:
            lowered = body.lower()
            for marker in text_markers:
                if marker.lower() in lowered:
                    return f"접근 제한 또는 인증 문구 발견: '{marker}'"
    return None


async def wait_for_results(page: Any, ready_selectors: Sequence[str],
                           detector: Detector, ctx: "SiteRunContext") -> None:
    """결과 영역이 나타날 때까지 조건 기반으로 기다린다.

    나타나지 않으면 제한 화면인지 확인하고, 제한이면 사용자 확인 후 한 번 더 기다린다.
    """
    selector = ", ".join(ready_selectors)
    for attempt in range(2):
        ctx.checkpoint("결과 대기")
        try:
            await page.wait_for_selector(selector, state="attached",
                                         timeout=ctx.result_wait_timeout_ms)
            return
        except PlaywrightTimeoutError:
            reason = await detector(page)
            if reason and attempt == 0:
                await ctx.ensure_accessible(page, detector, first_reason=reason)
                continue
            if reason:
                raise SiteBlocked(reason)
            raise SiteError(
                "결과 영역을 찾지 못했습니다. 결과가 없거나 화면 구조가 바뀌었을 수 있습니다"
                f"(선택자 사용자 검증 대기). 현재 URL: {page.url}"
            )


def clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None
