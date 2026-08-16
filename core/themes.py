"""
core/themes.py - 화이트리스트 ETF 이름에서 핵심 단어/테마 추출 & 집계

ETF 이름은 전략 테마를 담는 고정 패턴이라, 형태소 분석 대신
"브랜드 제거 → 테마 사전 매칭" 으로 추출한다 (KR 띄어쓰기 문제 해결).

기능:
- theme_counts: ETF를 최우선 매칭 테마 1개로 분류 → 점유율 (옵션2)
- word_frequency: 단어/키워드 빈도 순위 (옵션1)
- co_occurrence: 같은 이름 안에서 함께 나온 테마 단어쌍 (옵션4)
- build_wordcloud: 단어 빈도 기반 워드클라우드 PNG (옵션3)
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)

# systemd ProtectSystem=strict 에서 /tmp 쓰기 불가 — 워드클라우드 PNG 도
# emailer 와 동일하게 쓰기 가능한 프로젝트 data/tmp 에 저장.
_DATA_TMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tmp"
)
os.makedirs(_DATA_TMP, exist_ok=True)

# ── 브랜드/발행사 불용어 ──
# KR ETF 브랜드 접두어 (이름 시작 부분에서 제거)
KR_BRANDS = (
    "TIGER", "KODEX", "ACE", "SOL", "RISE", "PLUS", "TIME", "KIWOOM",
    "HANARO", "TIMEFOLIOS", "KoAct", "KBSTAR", "ARIRANG", "KOSEF",
    "BNK", "WOORI", "1Q", "마이다스", "유진", "다올", "키움",
    "신한", "한국투자", "삼성", "미래에셋", "NH", "FOCUS", "iSelect",
)

# US ETF 발행사/일반 불용어 (공백 분리 단어 기준)
US_STOPWORDS = {
    "etf", "fund", "the", "and", "trust", "index", "indexes", "funds",
    "vanguard", "ishares", "schwab", "dimensional", "avantis", "invesco",
    "jpmorgan", "first", "trust", "global", "x", "proshares", "wisdomtree",
    "fidelity", "american", "century", "hartford", "pimco", "blackrock",
    "baird", "cambria", "columbia", "franklin", "goldman", "sachs",
    "principal", "putnam", "russell", "vanek", "victory", "virtus",
    "xtrackers", "u.s.", "u.s", "us", "msci", "ftse", "crsp", "russell",
    "s&p", "sp", "select", "sector", "spdr", "street", "state",
    "et", "cap", "inc", "co", "corp", "ltd", "funds",
}

# ── 테마 사전 (라벨, [키워드]). 우선순위 = 리스트 순서 ──
# KR: substring 매칭 (브랜드 제거 후). US: 소문자 단어 토큰 부분일치.
THEMES = {
    "KR": [
        ("커버드콜", ["커버드콜", "커버드"]),
        ("배당", ["배당"]),
        ("반도체", ["반도체", "HBM"]),
        ("TDF/자산배분", ["TDF", "자산배분"]),
        ("원자재/에너지", ["원유", "에너지", "자원", "천연가스", "우라늄", "전력", "수소"]),
        ("채권/금리", ["채권", "금리", "KOFR", "국채", "회사채", "통안"]),
        ("밸류체인", ["밸류체인", "체인"]),
        ("글로벌/지수", ["글로벌", "미국", "나스닥", "다우존스", "유로스탁스", "유럽",
                        "중국", "일본", "인도", "베트남", "라틴", "브라질", "미국S&P"]),
        ("섹터테마", ["은행", "헬스케어", "바이오", "로봇", "2차전지", "전기차",
                     "방산", "조선", "원전", "부동산", "리츠", "엔터", "게임",
                     "커머스", "소프트웨어", "클라우드", "메타버스", "AI"]),
        ("액티브", ["액티브"]),
        ("주주가치", ["주주가치", "라이프자산"]),
    ],
    "US": [
        ("Dividend", ["dividend", "income", "yield", "value", "fundamental"]),
        ("Growth", ["growth", "momentum", "quality"]),
        ("SmallCap", ["small cap", "small-cap", "smallcap"]),
        ("MidCap", ["mid cap", "mid-cap"]),
        ("LargeCap", ["large cap", "large-cap", "marketwide", "equity"]),
        ("International", ["international", "ex-usa", "developed", "emerging",
                           "global", "world", "foreign"]),
        ("Sector", ["semiconductor", "technology", "tech", "energy", "financial",
                    "healthcare", "real estate", "reit", "consumer", "industrial",
                    "material", "utility", "defense", "uranium", "nuclear",
                    "software", "cloud", "artificial intelligence", "ai", "robotics"]),
        ("Bond", ["bond", "treasury", "aggregate", "corporate", "muni",
                  "short-term", "long-term", "credit"]),
        ("Country", ["canada", "china", "japan", "europe", "uk", "india",
                     "brazil", "australia", "korea", "taiwan"]),
    ],
}

# 워드클라우드 한글 폰트 경로 (NanumGothic — fc-list 로 확인)
WORDCLOUD_FONT = "/home/mikey/.local/share/fonts/NanumGothic-Bold.ttf"


def strip_brand(region: str, name: str) -> str:
    """ETF 이름에서 브랜드/발행사 접두어 제거."""
    n = str(name).strip()
    if region == "KR":
        for b in KR_BRANDS:
            if n.startswith(b):
                n = n[len(b):]
                break
    return n


def _name_tokens(region: str, name: str) -> list[str]:
    """테마 키워드 매칭용 토큰화."""
    n = strip_brand(region, name)
    if region == "KR":
        return [n]  # substring 매칭은 원본 문자열 사용
    return n.lower().split()


def match_themes(region: str, name: str) -> list[str]:
    """이름에 매칭되는 모든 테마 라벨 (빈도/연관성용). 우선순위 유지."""
    n = strip_brand(region, name)
    tokens = _name_tokens(region, name)
    matched = []
    for label, keywords in THEMES[region]:
        if region == "KR":
            if any(k.lower() in n for k in keywords):
                matched.append(label)
        else:
            if any(k in " ".join(tokens) for k in keywords):
                matched.append(label)
    return matched


def primary_theme(region: str, name: str) -> Optional[str]:
    """최우선 매칭 테마 1개 (점유율용). 없으면 None."""
    m = match_themes(region, name)
    return m[0] if m else None


def theme_counts(region: str, names: list[str]) -> list[tuple[str, int]]:
    """ETF를 최우선 테마로 분류 → (테마, 개수) 내림차순."""
    counter = Counter(primary_theme(region, n) for n in names)
    counter.pop(None, None)
    return sorted(counter.items(), key=lambda x: -x[1])


def word_frequency(region: str, names: list[str]) -> list[tuple[str, int]]:
    """단어/키워드 빈도 순위 (상위 단어).

    US: 공백 분리 단어 - 불용어. KR: 테마 사전 키워드 substring 카운트.
    """
    counter: Counter = Counter()
    for name in names:
        n = strip_brand(region, name)
        if region == "KR":
            for label, keywords in THEMES["KR"]:
                for k in keywords:
                    if k.lower() in n:
                        counter[k] += 1
                        break  # 라벨당 1회만
        else:
            for tok in n.lower().split():
                tok = tok.strip(".,()")
                if tok and tok not in US_STOPWORDS and len(tok) > 1:
                    counter[tok] += 1
    return sorted(counter.items(), key=lambda x: -x[1])


def co_occurrence(region: str, names: list[str]) -> list[tuple[str, str, int]]:
    """같은 ETF 이름 안에서 함께 나온 테마 쌍 빈도 (연관성)."""
    counter: Counter = Counter()
    for name in names:
        m = match_themes(region, name)
        for i in range(len(m)):
            for j in range(i + 1, len(m)):
                a, b = m[i], m[j]
                counter[tuple(sorted((a, b)))] += 1
    return [(a, b, c) for (a, b), c in sorted(counter.items(), key=lambda x: -x[1])]


def build_wordcloud(region: str, names: list[str], out_path: Optional[str] = None) -> Optional[str]:
    """워드클라우드 PNG 생성 (단어 빈도 기반). 생성 경로 반환, 실패 시 None."""
    try:
        from wordcloud import WordCloud
    except ImportError:
        logger.warning("wordcloud 미설치 — 워드클라우드 스킵")
        return None

    freq = dict(word_frequency(region, names))
    if not freq:
        logger.warning("[%s] 워드클라우드용 단어 없음", region)
        return None

    try:
        wc = WordCloud(
            font_path=WORDCLOUD_FONT, width=900, height=450,
            background_color="white", max_words=80,
            colormap="viridis", random_state=42,
        ).generate_from_frequencies(freq)

        path = out_path or os.path.join(_DATA_TMP, f"wordcloud_{region}.png")
        wc.to_file(path)
        logger.info("[%s] 워드클라우드 생성: %s (%d단어)", region, path, len(freq))
        return path
    except Exception as e:
        logger.warning("[%s] 워드클라우드 생성 실패: %s", region, e)
        return None
