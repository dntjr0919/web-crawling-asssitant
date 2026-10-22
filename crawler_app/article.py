"""공개 기사·블로그·일반 웹페이지 주요 콘텐츠 추출. 검색 결과 설명·숨겨진 데이터는 사용하지 않는다.

원칙:
- 검색 결과 설명(description)을 본문(body_text)으로 대체하지 않는다.
- 뉴스/블로그 전용 선택자(1단계) → 일반 웹 주요 콘텐츠 영역·밀도 기반 추출(2단계) → 사진 기사 설명(3단계) 순으로 시도한다.
- 메뉴·네비게이션·광고·관련 글·공유 버튼·푸터 등 노이즈 요소를 본문에서 제외한다.
- 결과 상태를 extracted(추출됨) / unsupported(미지원 유형) / blocked(접근 제한) / failed(추출 실패) / skipped(링크 없음)로 구분하며,
  원인이 불명확할 때는 억지로 단정하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

MIN_BODY_CHARS = 200
MIN_WEB_BODY_CHARS = 120
MIN_PHOTO_CAPTION_CHARS = 20
BODY_WAIT_MS = 8_000

# 1단계: 주요 포털·언론사·블로그 본문 컨테이너 선택자
BODY_SELECTORS = (
    "#dic_area",                    # 네이버 뉴스
    "#newsct_article",              # 네이버 뉴스 모바일/개편 뷰
    "[itemprop='articleBody']",     # 표준 Schema.org 적용 언론사 다수
    "#articleBodyContents",         # 구형 네이버/언론사 뷰
    "#articleBody",                 # 경향, 한경 등 다수 언론사
    "#article_body",                # 중앙, 동아 등 다수 언론사
    ".article_body",                # 조선, 매일경제 등
    ".article-body",                # 한겨레 등
    ".story-news",                  # 연합뉴스
    "#articleViewCon",              # 뉴시스, NDSoft 계열 인터넷 신문사
    ".article-view-content-div",    # NDSoft 계열 언론사
    ".article-txt",                 # 서울신문 등
    "#news_body_area",              # KBS, SBS 등 방송사
    ".news_cnt_detail_wrap",        # YTN, 방송 뉴스
    ".article_view",                # 다음/언론사 뷰어
    ".view_cont",                   # 일반 언론사/기관 상세 뷰
    ".se-main-container",           # 네이버 블로그 스마트에디터 ONE
    "#postViewArea",                # 네이버 블로그 구형 에디터
    "article",                      # HTML5 시맨틱 태그
)

# 2단계: 일반 웹페이지(기관 게시판, 관광 안내, 위키/가이드, 커뮤니티 등) 주요 콘텐츠 컨테이너 선택자
WEB_CONTENT_SELECTORS = (
    ".board_view",
    ".bbs_view",
    ".view_body",
    ".view_con",
    ".view_content",
    ".post-content",
    ".entry-content",
    "#sub_content",
    "#contents",
    ".contents_body",
    "#content",
    "main",
    "[role='main']",
)

# 3단계: 일반 본문이 없을 때 확인하는 사진·포토 기사 전용 설명 선택자
PHOTO_CAPTION_SELECTORS = (
    "#articleBodyContent .m-auto",
    ".end_photo_org .img_desc",
    ".photo-view .caption",
    ".photo_article .desc",
    ".comp_photo .txt_caption",
    ".article_photo .caption",
    "#articleBody .caption",
    ".view_photo .caption",
    "figure figcaption",
    ".img_desc",
    ".txt_caption",
    ".photo_txt",
)

# 동영상·오디오·SNS 피드·지도·바이너리 파일 등 텍스트 본문 추출 미지원 URL 패턴
UNSUPPORTED_HOST_PATTERNS = (
    re.compile(r"(^|\.)youtube\.com$", re.I),
    re.compile(r"(^|\.)youtu\.be$", re.I),
    re.compile(r"(^|\.)instagram\.com$", re.I),
    re.compile(r"(^|\.)tiktok\.com$", re.I),
    re.compile(r"(^|\.)tv\.naver\.com$", re.I),
    re.compile(r"(^|\.)map\.naver\.com$", re.I),
    re.compile(r"(^|\.)map\.kakao\.com$", re.I),
    re.compile(r"(^|\.)openstreetmap\.org$", re.I),
)
UNSUPPORTED_EXT_RE = re.compile(r"\.(pdf|zip|rar|7z|hwp|hwpx|doc|docx|xls|xlsx|ppt|pptx|mp4|mp3|avi|mov)(\?|$)", re.I)

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

# 본문 텍스트 후처리 시 제거할 광고·UI 상투 문구 패턴
NOISE_LINE_PATTERNS = (
    re.compile(r"한국로또\s*큰일났다|1등\s*당첨자\s*찾아가보니", re.I),
    re.compile(r"죽을\s*것\s*같은\s*허리통증|新기술로\s*단\s*\d+분", re.I),
    re.compile(r"^(?:본문|메뉴|주메뉴|하단|콘텐츠)\s*바로가기$"),
    re.compile(r"^(?:글자크기|폰트크기)\s*(?:조절|확대|축소|변경)$"),
    re.compile(r"^(?:인쇄|프린트|공유|공유하기|스크랩|URL\s*복사|링크\s*복사|목록|목록으로|목록보기|위로|TOP)$", re.I),
    re.compile(r"^(?:이전글|다음글|윗글|아랫글)\s*[:：]?"),
    re.compile(r"^(?:관련\s*기사|관련\s*글|추천\s*기사|많이\s*본\s*뉴스|이\s*시각\s*주요\s*뉴스|함께\s*보면\s*좋은\s*글)$"),
    re.compile(r"^무단\s*전재\s*(?:및|·|-)?\s*재배포\s*금지"),
    re.compile(r"^저작권자\s*(?:\(c\)|©)", re.I),
)

BODY_JS = r"""
({selectors, minChars, webSelectors, minWebChars, photoSelectors, minPhotoChars}) => {
  const NOISE_TAG = new Set([
    'NAV', 'HEADER', 'FOOTER', 'ASIDE', 'FORM', 'SCRIPT', 'STYLE', 'NOSCRIPT',
    'IFRAME', 'SVG', 'BUTTON', 'SELECT', 'OPTION', 'CANVAS', ' DIALOG'
  ]);
  const NOISE_ROLE = new Set(['navigation', 'banner', 'contentinfo', 'complementary', 'search', 'menu', 'menubar', 'dialog']);
  const NOISE_ATTR_RE = /(^|[-_\s])(nav|gnb|lnb|snb|menu|breadcrumb|location|pagination|paging|comment|reply|related|recommend|popular|ranking|ad|ads|advert|adwrap|banner|sponsor|share|sns|social|toolbar|util|footer|copyright|byline|reporter|tag|hashtag|modal|popup|layer|sidebar|widget|blind|sr-only|screen-reader)([-_\s]|$)/i;

  const visible = el => {
    if (!el || !el.getClientRects || !el.getClientRects().length) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 5 && rect.height > 5;
  };

  const isNoiseElement = (el, rootEl) => {
    if (!el || el === rootEl) return false;
    if (NOISE_TAG.has(el.tagName)) return true;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (NOISE_ROLE.has(role)) return true;
    if (el.getAttribute('aria-hidden') === 'true') return true;
    const idCls = `${el.id || ''} ${typeof el.className === 'string' ? el.className : ''}`;
    if (NOISE_ATTR_RE.test(idCls)) return true;
    // 링크가 밀집된 목록(관련 글, 추천 기사, 사이트맵, 메뉴 블록)은 제외
    if (el.tagName === 'UL' || el.tagName === 'OL' || el.tagName === 'DIV') {
      const links = el.querySelectorAll('a[href]');
      if (links.length >= 3) {
        const totalTxt = (el.innerText || '').replace(/\s+/g, '');
        let linkTxtLen = 0;
        for (const a of links) linkTxtLen += (a.innerText || '').replace(/\s+/g, '').length;
        if (totalTxt.length > 0 && totalTxt.length < 600 && (linkTxtLen / totalTxt.length) > 0.65) {
          return true;
        }
      }
    }
    return false;
  };

  const cleanLines = text => {
    if (!text) return '';
    return text
      .replace(/[\u200b\u200c\u200d\ufeff]/g, '')
      .replace(/\r/g, '')
      .split('\n')
      .map(s => s.replace(/[\t\u00a0 ]+/g, ' ').trim())
      .filter(s => {
        if (!s) return false;
        if (/^(?:새\s*창\s*열림|새창열림|광고|AD|Sponsored)$/i.test(s)) return false;
        return true;
      })
      .join('\n');
  };

  // 컨테이너 내부에서 메뉴·광고·관련글 하위 요소를 제외한 순수 텍스트만 추출
  const extractCleanText = (rootEl, paragraphOnly = false) => {
    if (!visible(rootEl)) return '';
    if (paragraphOnly) {
      const ps = [...rootEl.querySelectorAll('p')].filter(p => {
        if (!visible(p)) return false;
        let cur = p;
        while (cur && cur !== rootEl) {
          if (isNoiseElement(cur, rootEl)) return false;
          cur = cur.parentElement;
        }
        return true;
      });
      const pText = ps.map(p => cleanLines(p.innerText)).filter(Boolean).join('\n\n');
      if (pText.length >= minChars) return pText;
    }

    // DOM을 직접 변경하지 않고 TreeWalker로 가시 텍스트 노드를 수집하되 노이즈 서브트리는 건너뜀
    const lines = [];
    const BLOCK_TAGS = new Set(['P', 'DIV', 'LI', 'DT', 'DD', 'TR', 'H1', 'H2', 'H3', 'H4', 'BLOCKQUOTE', 'SECTION', 'ARTICLE', 'BR', 'HR']);
    let currentBuf = [];
    const flush = () => {
      if (currentBuf.length) {
        const joined = currentBuf.join(' ').replace(/\s+/g, ' ').trim();
        if (joined) lines.push(joined);
        currentBuf = [];
      }
    };

    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        const val = (node.nodeValue || '').replace(/[\u200b\u200c\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
        if (val) currentBuf.push(val);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      if (el !== rootEl && (!visible(el) || isNoiseElement(el, rootEl))) return;
      if (el.tagName === 'BR') {
        flush();
        return;
      }
      const isBlock = BLOCK_TAGS.has(el.tagName);
      if (isBlock) flush();
      for (const child of el.childNodes) {
        walk(child);
      }
      if (isBlock) flush();
    };
    walk(rootEl);
    flush();
    return cleanLines(lines.join('\n'));
  };

  const linkDensity = (el) => {
    const total = (el.innerText || '').replace(/\s+/g, '').length;
    if (!total) return 1;
    let linkLen = 0;
    for (const a of el.querySelectorAll('a[href]')) {
      linkLen += (a.innerText || '').replace(/\s+/g, '').length;
    }
    return linkLen / total;
  };

  // 1) 명시적 기사/블로그 선택자 탐색
  const findBySelectorList = (selectorList, minLen, methodPrefix = '', allowMultiplePickBest = false) => {
    for (const selector of selectorList || []) {
      const rawMatches = [];
      for (const el of document.querySelectorAll(selector)) {
        if (!visible(el)) continue;
        const text = extractCleanText(el, selector === 'article');
        if (text.length >= minLen && linkDensity(el) <= 0.55) {
          rawMatches.push({el, text, method: `${methodPrefix}${selector}`});
        }
      }
      const matches = rawMatches.filter(
        m => !rawMatches.some(other => other.el !== m.el && m.el.contains(other.el))
      );
      const unique = [...new Map(matches.map(m => [m.text, {text: m.text, method: m.method, el: m.el}])).values()];
      if (unique.length === 1) return {text: unique[0].text, method: unique[0].method};
      if (unique.length > 1) {
        if (methodPrefix === 'photo:') {
          return {
            text: unique.map(u => u.text).join('\n\n'),
            method: `${methodPrefix}${selector}`,
          };
        }
        if (allowMultiplePickBest) {
          // 일반 웹 컨테이너가 여러 개 잡힌 경우 가장 본문 밀도가 높은 요소를 선택
          unique.sort((a, b) => (b.text.length * (1 - linkDensity(b.el))) - (a.text.length * (1 - linkDensity(a.el))));
          return {text: unique[0].text, method: unique[0].method};
        }
        return {ambiguous: true, method: `${methodPrefix}${selector}`};
      }
    }
    return null;
  };

  const stage1 = findBySelectorList(selectors, minChars);
  if (stage1 && !stage1.ambiguous) return stage1;

  // 2) 일반 웹 주요 콘텐츠 선택자 탐색
  const stage2 = findBySelectorList(webSelectors, minWebChars, 'web:', true);
  if (stage2 && !stage2.ambiguous) return stage2;

  // 3) 일반 웹페이지 밀도 기반 주요 콘텐츠 블록 탐색 (관광지 안내, 기관 게시판, 커스텀 레이아웃 대응)
  const candidates = [];
  const blockNodes = document.querySelectorAll('main, article, section, div, td');
  for (const el of blockNodes) {
    if (el === document.body || el === document.documentElement) continue;
    if (!visible(el) || isNoiseElement(el, document.body)) continue;
    // 너무 많은 상위 래퍼를 피하기 위해 하위 블록이 지나치게 큰 루트 컨테이너는 건너뜀
    const rect = el.getBoundingClientRect();
    if (rect.width < 200 || rect.height < 60) continue;
    const rawLen = (el.innerText || '').trim().length;
    if (rawLen < minWebChars || rawLen > 60000) continue;
    const ld = linkDensity(el);
    if (ld > 0.45) continue;
    const text = extractCleanText(el, false);
    if (text.length < minWebChars) continue;
    // 문장 부호나 줄바꿈이 있는 실질 콘텐츠 가산점
    const punctCount = (text.match(/[.?!다요죠음함됨임](\s|$)/g) || []).length;
    const lineCount = text.split('\n').length;
    if (punctCount < 1 && lineCount < 3) continue;

    const idCls = `${el.tagName} ${el.id || ''} ${typeof el.className === 'string' ? el.className : ''}`.toLowerCase();
    let bonus = 1.0;
    if (/(content|article|view|detail|board|bbs|post|body|main|guide|info|story|txt|desc)/.test(idCls)) bonus += 0.45;
    // 자식 요소 중에 이미 더 구체적인 동일 텍스트 컨테이너가 있으면 자식에게 양보하기 위해 깊이 반영
    const score = text.length * (1 - ld) * bonus + punctCount * 25;
    candidates.push({el, text, score});
  }
  if (candidates.length > 0) {
    // 부모 요소가 자식 요소와 텍스트 길이가 거의 같다면(90% 이상) 더 안쪽(자식) 요소를 우선
    const filtered = candidates.filter(c => !candidates.some(
      other => other.el !== c.el && c.el.contains(other.el) && other.text.length >= c.text.length * 0.85
    ));
    const pool = filtered.length > 0 ? filtered : candidates;
    pool.sort((a, b) => b.score - a.score);
    const best = pool[0];
    if (best && best.text.length >= minWebChars) {
      const tag = best.el.tagName.toLowerCase();
      return {text: best.text, method: `web:density(${tag})`};
    }
  }

  // 4) 사진 기사 캡션 탐색
  const stagePhoto = findBySelectorList(photoSelectors, minPhotoChars, 'photo:');
  if (stagePhoto) return stagePhoto;

  if (stage1 && stage1.ambiguous) return stage1;
  return null;
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


def unsupported_url_reason(url: str | None) -> str | None:
    """동영상·SNS·지도·첨부파일 등 일반 웹 문서 본문 추출 미지원 URL이면 사유를 반환한다."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if UNSUPPORTED_EXT_RE.search(path):
        return f"문서/미디어 첨부파일 형식 미지원 ({path.rsplit('.', 1)[-1]})"
    for pat in UNSUPPORTED_HOST_PATTERNS:
        if pat.search(host):
            return f"동영상·SNS·지도 서비스 본문 자동 추출 미지원 ({host})"
    if path in ("/copyright", "/terms", "/privacy"):
        return f"저작권·약관 안내 페이지 제외 ({host}{path})"
    return None


def clean_extracted_body(text: str | None) -> str | None:
    """추출된 본문에서 남은 인라인 광고 문구·UI 상투어·제로폭 공백을 정제한다."""
    if not text:
        return None
    cleaned_lines: list[str] = []
    for raw_line in text.replace("\r", "").split("\n"):
        line = re.sub(r"[\u200b\u200c\u200d\ufeff]+", "", raw_line)
        line = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if not line:
            continue
        if any(pat.search(line) for pat in NOISE_LINE_PATTERNS):
            continue
        # 끝에 남은 '새 창 열림' 제거
        line = re.sub(r"(?:\s*새\s*창\s*열림|\s*새창열림)+$", "", line).strip()
        if not line:
            continue
        cleaned_lines.append(line)
    result = "\n".join(cleaned_lines).strip()
    return result or None


async def restriction_reason(page: Any) -> str | None:
    if re.search(r"/sorry/|captcha|/challenge(?:/|$)", page.url, re.I):
        return "접근 제한·CAPTCHA로 보이는 URL"
    for selector in BLOCK_SELECTORS:
        matches = page.locator(selector)
        for index in range(min(await matches.count(), 5)):
            if await matches.nth(index).is_visible():
                return f"표시된 인증·구독 제한 요소: {selector}"
    text = await page.locator("body").inner_text(timeout=3_000)
    if len(text) <= 5_000:
        lowered = text.lower()
        for phrase in BLOCK_PHRASES:
            if phrase in lowered:
                return f"접근 제한·인증·구독 안내: {phrase}"
    return None


async def _resolve_target_frame(page: Any) -> Any:
    """네이버 블로그 등 본문이 iframe(#mainFrame, #cafe_main) 내부에 있는 경우 해당 프레임을 반환한다."""
    for sel in ("iframe#mainFrame", "iframe#cafe_main"):
        try:
            iframe_loc = page.locator(sel)
            if await iframe_loc.count() > 0:
                handle = await iframe_loc.first.element_handle(timeout=2_000)
                if handle is not None:
                    frame = await handle.content_frame()
                    if frame is not None:
                        await frame.wait_for_load_state("domcontentloaded", timeout=5_000)
                        return frame
        except Exception:
            continue
    return page


async def extract_article(page: Any, url: str, navigation_timeout_ms: int) -> BodyExtraction:
    """이미 열린 실행부 소유 페이지를 사용한다. 다른 항목의 실패와 독립적이다."""
    if not valid_article_url(url):
        return BodyExtraction("skipped", error="유효한 원문 HTTP(S) 링크 없음")

    pre_unsupported = unsupported_url_reason(url)
    if pre_unsupported:
        return BodyExtraction("unsupported", final_url=url, error=pre_unsupported)

    try:
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=navigation_timeout_ms
        )
        final_url = page.url
        post_unsupported = unsupported_url_reason(final_url)
        if post_unsupported:
            return BodyExtraction("unsupported", final_url=final_url, error=post_unsupported)

        if response is None:
            return BodyExtraction("failed", final_url=final_url, error="HTTP 응답을 확인하지 못함 (원인 미확정)")

        content_type = (response.headers.get("content-type") or "").lower()
        if content_type and not any(t in content_type for t in ("text/html", "application/xhtml")):
            return BodyExtraction(
                "unsupported", final_url=final_url, error=f"HTML 문서가 아닌 응답 형식 ({content_type.split(';')[0]})"
            )

        if response.status in (401, 403, 429):
            return BodyExtraction(
                "blocked", final_url=final_url, error=f"접근 제한 응답 (HTTP {response.status})"
            )
        if response.status >= 400:
            return BodyExtraction(
                "failed", final_url=final_url, error=f"페이지 오류 응답 (HTTP {response.status})"
            )

        reason = await restriction_reason(page)
        if reason:
            return BodyExtraction("blocked", final_url=final_url, error=reason)

        args = {
            "selectors": list(BODY_SELECTORS),
            "minChars": MIN_BODY_CHARS,
            "webSelectors": list(WEB_CONTENT_SELECTORS),
            "minWebChars": MIN_WEB_BODY_CHARS,
            "photoSelectors": list(PHOTO_CAPTION_SELECTORS),
            "minPhotoChars": MIN_PHOTO_CAPTION_CHARS,
        }
        target = await _resolve_target_frame(page)
        try:
            handle = await target.wait_for_function(BODY_JS, arg=args, timeout=BODY_WAIT_MS)
        except PlaywrightTimeoutError:
            reason = await restriction_reason(page)
            if reason:
                return BodyExtraction("blocked", final_url=page.url, error=reason)
            return BodyExtraction(
                "failed",
                final_url=page.url,
                error="본문 영역을 특정하지 못함 (비정형 레이아웃·본문 분량 부족 또는 동적 렌더링 미완료 가능성)",
            )
        try:
            body = await handle.json_value()
        finally:
            await handle.dispose()

        reason = await restriction_reason(page)
        if reason:
            return BodyExtraction("blocked", final_url=page.url, error=reason)
        if body.get("ambiguous"):
            return BodyExtraction(
                "failed", final_url=page.url, method=body.get("method"),
                error="서로 다른 본문 후보 영역이 여러 개여서 자동 확정하지 않음",
            )
        cleaned = clean_extracted_body(body.get("text"))
        if not cleaned or len(cleaned) < MIN_PHOTO_CAPTION_CHARS:
            return BodyExtraction(
                "failed", final_url=page.url, method=body.get("method"),
                error="정제 후 유효 본문 분량이 부족하여 제외함",
            )
        return BodyExtraction(
            "extracted", text=cleaned, final_url=page.url, method=body["method"]
        )
    except Exception as exc:
        return BodyExtraction(
            "failed", final_url=page.url, error=f"페이지 처리 중 오류 ({type(exc).__name__}: {exc})"
        )
