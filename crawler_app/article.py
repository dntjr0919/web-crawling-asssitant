"""공개 기사 본문 후보 추출. 검색 결과 설명·숨겨진 데이터는 사용하지 않는다.

실제 언론사별 선택자 검증은 사용자 실행 대기 상태다. 추출 성공은 보이는
본문 후보를 찾았다는 뜻이며 기사 전문의 완전성이나 정확성을 보장하지 않는다.
유료벽·접근 제한·인증 화면은 실패로 기록하며 자동 재시도/우회하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

MIN_BODY_CHARS = 200
MIN_PHOTO_CAPTION_CHARS = 20
BODY_WAIT_MS = 8_000

# 휴리스틱 후보. 사이트별 실제 동작 검증 전이며 body/main 전체를 대체 본문으로 쓰지 않는다.
BODY_SELECTORS = ("#dic_area", "[itemprop='articleBody']", "article")
# 일반 기사 본문(200자 이상)이 없을 때만 확인하는 사진 기사 전용 설명 선택자.
PHOTO_CAPTION_SELECTORS = ("#articleBodyContent .m-auto",)
BLOCK_SELECTORS = (
    "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
    "iframe[src*='captcha']", "#captcha-form", "#captcha",
    "[data-testid='paywall']", "[data-paywall='true']", ".paywall",
)
BLOCK_PHRASES = (
    "비정상적인 트래픽", "비정상적인 접근", "자동입력 방지", "자동 입력 방지",
    "unusual traffic", "verify you are human", "checking your browser",
    "이 기사는 유료", "구독 후 이용", "로그인 후 이용", "subscribe to continue reading",
)

# article 요소는 본문 외 요소가 섞일 수 있어 문단만 사용한다.
# innerText 및 표시 여부를 사용하며 숨겨진 본문·JSON-LD·유료 데이터는 읽지 않는다.
BODY_JS = r"""
({selectors, minChars, photoSelectors, minPhotoChars}) => {
  const visible = el => {
    if (!el || !el.getClientRects().length) return false;
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden';
  };
  const clean = text => (text || '').replace(/\r/g, '')
    .split('\n').map(s => s.replace(/[ \t]+/g, ' ').trim())
    .filter(Boolean).join('\n');
  const findCandidate = (selectorList, minLen, methodPrefix = '') => {
    for (const selector of selectorList || []) {
      const rawMatches = [];
      for (const el of document.querySelectorAll(selector)) {
        if (!visible(el)) continue;
        let text;
        if (selector === 'article') {
          text = [...el.querySelectorAll('p')].filter(p =>
            visible(p) && !p.closest('nav, header, footer, aside, figure, form')
          ).map(p => clean(p.innerText)).filter(Boolean).join('\n\n');
        } else {
          text = clean(el.innerText);
        }
        if (text.length >= minLen) {
          rawMatches.push({el, text, method: `${methodPrefix}${selector}`});
        }
      }
      // 상위 래퍼 div와 하위 실제 텍스트 div가 함께 잡힐 때는 가장 안쪽 요소만 남긴다.
      const matches = rawMatches.filter(
        m => !rawMatches.some(other => other.el !== m.el && m.el.contains(other.el))
      );
      // 서로 다른 본문 후보가 여러 개면 임의로 합치거나 가장 긴 것을 정답으로 간주하지 않는다.
      const unique = [...new Map(matches.map(m => [m.text, {text: m.text, method: m.method}])).values()];
      if (unique.length === 1) return unique[0];
      if (unique.length > 1) return {ambiguous: true, method: `${methodPrefix}${selector}`};
    }
    return null;
  };
  return (
    findCandidate(selectors, minChars) ||
    findCandidate(photoSelectors, minPhotoChars, 'photo:')
  );
}
"""


@dataclass
class BodyExtraction:
    status: str
    text: str | None = None
    final_url: str | None = None
    method: str | None = None
    error: str | None = None


def valid_article_url(url: str | None) -> bool:
    try:
        parsed = urlparse(url or "")
        return parsed.scheme in ("https", "http") and bool(parsed.hostname)
    except ValueError:
        return False


async def restriction_reason(page: Any) -> str | None:
    if re.search(r"/sorry/|captcha|/challenge(?:/|$)", page.url, re.I):
        return "접근 제한·CAPTCHA로 보이는 URL"
    for selector in BLOCK_SELECTORS:
        matches = page.locator(selector)
        for index in range(min(await matches.count(), 5)):
            if await matches.nth(index).is_visible():
                return f"표시된 인증·구독 제한 요소: {selector}"
    # 짧은 안내 화면의 문구만 검사한다. 기사 내 관련 보도로 인한 오탐을 줄이기 위함이다.
    text = await page.locator("body").inner_text(timeout=3_000)
    if len(text) <= 5_000:
        lowered = text.lower()
        for phrase in BLOCK_PHRASES:
            if phrase in lowered:
                return f"접근 제한·인증·구독 안내: {phrase}"
    return None


async def extract_article(page: Any, url: str, navigation_timeout_ms: int) -> BodyExtraction:
    """이미 열린 실행부 소유 페이지를 사용한다. 다른 기사의 실패와 독립적이다."""
    if not valid_article_url(url):
        return BodyExtraction("skipped", error="유효한 원문 HTTP(S) 링크 없음")
    try:
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=navigation_timeout_ms
        )
        if response is None:
            return BodyExtraction("failed", final_url=page.url, error="HTTP 응답 확인 불가")
        if response.status >= 400:
            return BodyExtraction(
                "failed", final_url=page.url, error=f"HTTP {response.status}; 재시도하지 않음"
            )
        reason = await restriction_reason(page)
        if reason:
            return BodyExtraction("blocked", final_url=page.url, error=reason)
        args = {
            "selectors": list(BODY_SELECTORS),
            "minChars": MIN_BODY_CHARS,
            "photoSelectors": list(PHOTO_CAPTION_SELECTORS),
            "minPhotoChars": MIN_PHOTO_CAPTION_CHARS,
        }
        try:
            handle = await page.wait_for_function(BODY_JS, arg=args, timeout=BODY_WAIT_MS)
        except PlaywrightTimeoutError:
            reason = await restriction_reason(page)
            return BodyExtraction(
                "blocked" if reason else "failed", final_url=page.url,
                error=reason or "지원 본문(200자 이상) 또는 사진 설명(20자 이상) 영역을 찾지 못함",
            )
        try:
            body = await handle.json_value()
        finally:
            await handle.dispose()
        # 본문 대기 중 나타난 제한 화면도 재확인한다.
        reason = await restriction_reason(page)
        if reason:
            return BodyExtraction("blocked", final_url=page.url, error=reason)
        if body.get("ambiguous"):
            return BodyExtraction(
                "failed", final_url=page.url, method=body.get("method"),
                error="본문 후보가 여러 개여서 자동 선택하지 않음",
            )
        return BodyExtraction(
            "extracted", text=body["text"], final_url=page.url, method=body["method"]
        )
    except Exception as exc:
        # 사용자가 중지했는지는 호출자가 직후 checkpoint에서 다시 판단한다.
        return BodyExtraction(
            "failed", final_url=page.url, error=f"{type(exc).__name__}: {exc}"
        )