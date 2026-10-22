"""로컬 규칙·통계 기반 핵심 키워드 추출 및 추출 요약(Extractive Summarization) 모듈.

외부 LLM API나 서버를 사용하지 않고 순수 Python으로 동작한다.
- 핵심 키워드 추출: 한글/영문 토큰화, 조사·어미·불용어 제거, 제목 가중치 및 빈도(TF) 점수 계산
- 본문 요약: 문장 분리, 상투적 문구(기자명·저작권·광고·목차·관련 글 링크 등) 제거, 키워드 포함도·제목 유사도·위치 가중치 기반 핵심 문장 추출
"""
from __future__ import annotations

import math
import re
from collections import Counter

KOREAN_SUFFIXES = (
    "에서는", "으로는", "에게는", "한테는", "로부터", "에서부터", "이라고", "라면서", "이라며",
    "했다고", "한다며", "밝혔다", "전했다", "말했다", "보인다", "나타났다", "알려졌다",
    "대해서는", "위해서는", "통해서는", "관해서는",
    "에서", "으로", "에게", "한테", "부터", "까지", "보다", "처럼", "만큼", "대로",
    "이나", "거나", "든지", "라도", "마저", "조차", "밖에", "이라", "라고", "하며",
    "하고", "하는", "되는", "있는", "없는", "같은", "위한", "대한", "통한", "관한",
    "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만",
)

STOPWORDS = {
    "기자", "뉴스", "사진", "제공", "무단", "전재", "무단전재", "재배포", "금지", "특파원",
    "앵커", "리포트", "인터뷰", "자료", "화면", "출처", "저작권자", "배포", "구독", "좋아요",
    "오늘", "어제", "내일", "이번", "지난", "올해", "내년", "작년", "최근", "현재", "당시",
    "지금", "이후", "이전", "오전", "오후", "시간", "하루", "이날", "해당", "관련", "통해",
    "대해", "위해", "경우", "때문", "정도", "다시", "모두", "함께", "가장", "매우", "특히",
    "또한", "그리고", "하지만", "그러나", "따라서", "한편", "다만", "즈음", "가운데", "따르면",
    "대비", "기준", "수준", "예정", "계획", "진행", "결과", "모습", "부분", "내용", "사실",
    "문제", "상황", "입장", "설명", "발표", "확인", "제시", "포함", "시작", "사용", "이용",
    "이상", "이하", "약", "등", "것", "수", "점", "바", "중", "뒤", "전", "후", "안", "밖",
    "새창열림", "열림", "바로가기", "목록", "클릭", "옵션", "상품", "예약",
    "the", "and", "for", "that", "this", "with", "from", "have", "has", "had", "are",
    "was", "were", "will", "would", "could", "should", "about", "into", "more", "other",
}

# 요약 시 제외할 상투적 문구·광고·목차·관련글 카드 패턴
BOILERPLATE_PATTERNS = (
    re.compile(r"무단\s*전재\s*(및|·|-)?\s*재배포\s*금지"),
    re.compile(r"저작권자\s*\(c\)|copyright\s*©?", re.I),
    re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    re.compile(r"^[가-힣]{2,4}\s*(기자|특파원|논설위원)\s*$"),
    re.compile(r"^\[[^\]]*(기자|뉴스|사진|특파원)[^\]]*\]$"),
    re.compile(r"한국로또\s*큰일났다|1등\s*당첨자\s*찾아가보니|죽을\s*것\s*같은\s*허리통증|新기술로\s*단\s*\d+분"),
    re.compile(r"©\s*글\s*[·ㆍ]\s*사진|credit\s*:\s*@", re.I),
    re.compile(r"^(?:목차|오늘의\s*콘텐츠\s*미리보기|관련\s*글|함께\s*보면\s*좋은\s*글|이전글|다음글)"),
    re.compile(r"\|\s*[^|]+\|\s*[^|]+(?:제공|옵션|출발|할인|투어)"),
    re.compile(r"\.\.\.$|…$"),
)

TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9+#.-]{1,}|[0-9]+(?:년|월|일|원|달러|명|건|개|%)")


def _clean_invisible(text: str) -> str:
    return re.sub(r"[\u200b\u200c\u200d\ufeff]+", "", text or "")


def _strip_suffix(word: str) -> str:
    if not re.match(r"^[가-힣]+$", word):
        return word.lower()
    for suffix in KOREAN_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            return word[: -len(suffix)]
    return word


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    text = _clean_invisible(text)
    tokens: list[str] = []
    for raw in TOKEN_RE.findall(text):
        stem = _strip_suffix(raw)
        if len(stem) < 2:
            continue
        if stem.lower() in STOPWORDS or raw.lower() in STOPWORDS:
            continue
        if re.match(r"^\d+(년|월|일|명|건|개)$", stem):
            continue
        tokens.append(stem)
    return tokens


def extract_keywords(
    text: str | None,
    title: str | None = None,
    top_k: int = 5,
) -> list[str]:
    """본문(또는 설명)과 제목에서 중요도가 높은 핵심 키워드를 최대 top_k개 반환한다."""
    combined_body = _clean_invisible(text or "").strip()
    clean_title = _clean_invisible(title or "").strip()
    if not combined_body and not clean_title:
        return []

    body_tokens = tokenize(combined_body)
    title_tokens = tokenize(clean_title)
    if not body_tokens and not title_tokens:
        return []

    lead_tokens = set(tokenize(combined_body[:300]))
    title_set = set(title_tokens)

    counts = Counter(body_tokens)
    for t in title_tokens:
        counts[t] += 1

    scores: dict[str, float] = {}
    first_pos: dict[str, int] = {}
    all_seq = title_tokens + body_tokens
    for idx, tok in enumerate(all_seq):
        if tok not in first_pos:
            first_pos[tok] = idx

    for tok, freq in counts.items():
        score = float(freq)
        if tok in title_set:
            score = score * 1.8 + 1.5
        if tok in lead_tokens:
            score += 0.8
        if len(tok) >= 3:
            score += 0.2
        scores[tok] = score

    ranked = sorted(
        scores.keys(),
        key=lambda k: (-scores[k], first_pos.get(k, 999999)),
    )
    return ranked[: max(1, top_k)]


def split_sentences(text: str) -> list[str]:
    """본문 텍스트를 문장 단위로 분리하고 광고·목차·상투적 문구를 정리한다."""
    if not text:
        return []
    normalized = re.sub(r"\r\n?", "\n", _clean_invisible(text))
    raw_lines = normalized.split("\n")
    sentences: list[str] = []

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        if any(p.search(line) for p in BOILERPLATE_PATTERNS):
            continue
        parts = re.split(r"(?<=[다요죠음함됨임\.!\?])\s+(?=[가-힣A-Z0-9\"'\(\[])", line)
        for part in parts:
            s = re.sub(r"\s+", " ", part).strip()
            if not s:
                continue
            s = re.sub(r"^\[[^\]]{2,30}\]\s*", "", s)
            s = re.sub(r"^\([^\)]{2,30}\)\s*[=/-]\s*", "", s)
            s = re.sub(r"^[가-힣]{2,4}\s*(기자|특파원)\s*=\s*", "", s).strip()
            if len(s) < 18:
                continue
            if any(p.search(s) for p in BOILERPLATE_PATTERNS):
                continue
            # 서술어/문장 종결 형태가 전혀 없는 단순 나열형 제목·메뉴 줄 제외
            if not re.search(r"(다|요|죠|음|함|됨|임|니다|어요|아요|세요|했다|한다|된다|있다|없다|이다|같다|[.!?])$", s):
                if len(s) < 35:
                    continue
            if s not in sentences:
                sentences.append(s)
    return sentences


def summarize_text(
    text: str | None,
    title: str | None = None,
    max_sentences: int = 3,
) -> str | None:
    """본문에서 핵심 문장을 추출하여 원문 순서대로 결합한 요약문을 반환한다."""
    if not text or not text.strip():
        return None

    sentences = split_sentences(text)
    if not sentences:
        cleaned = " ".join(_clean_invisible(text).split()).strip()
        if any(p.search(cleaned) for p in BOILERPLATE_PATTERNS):
            return None
        return cleaned if 15 <= len(cleaned) <= 300 else None

    if len(sentences) <= max_sentences:
        return " ".join(sentences)

    keywords = extract_keywords(text, title=title, top_k=8)
    keyword_weights = {kw: (len(keywords) - idx) for idx, kw in enumerate(keywords)}
    title_tokens = set(tokenize(title or ""))

    scored: list[tuple[float, int, str]] = []
    total = len(sentences)
    for idx, sent in enumerate(sentences):
        sent_tokens = tokenize(sent)
        if not sent_tokens:
            continue
        unique_tokens = set(sent_tokens)

        kw_score = sum(keyword_weights.get(t, 0) for t in unique_tokens)
        title_overlap = len(unique_tokens & title_tokens) * 2.0

        if idx == 0:
            pos_score = 3.0
        elif idx == 1:
            pos_score = 1.8
        else:
            pos_score = max(0.2, 1.2 * (1.0 - idx / total))

        char_len = len(sent)
        if char_len < 25:
            len_factor = 0.6
        elif char_len > 220:
            len_factor = 0.75
        else:
            len_factor = 1.0 + math.log10(max(1, len(sent_tokens))) * 0.2

        final_score = (kw_score + title_overlap + pos_score) * len_factor
        scored.append((final_score, idx, sent))

    if not scored:
        return " ".join(sentences[:max_sentences])

    top_selected = sorted(scored, key=lambda item: -item[0])[: max(1, max_sentences)]
    top_selected.sort(key=lambda item: item[1])
    return " ".join(item[2] for item in top_selected)
