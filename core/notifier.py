"""
core/notifier.py - Telegram 봇 알림
HTTP API 호출 (requests). 토큰/채팅ID는 .env에서 로드.
"""
import logging
import requests
from typing import Optional

from config.settings import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HTTP_TIMEOUT,
)

logger = logging.getLogger(__name__)


def send_telegram(text: str, parse_mode: Optional[str] = "HTML") -> bool:
    """Telegram 봇 API로 메시지 전송.

    4096자 제한 → 라인 경계에서 분할(HTML 태그 중단 방지) 후 순차 전송.
    모든 청크 송부 성공 시 True, 하나라도 실패/예외 시 False.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram 자격증명 없음 - 전송 스킵")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _chunk_on_lines(text, 4000)
    ok = True
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                continue
            logger.error("Telegram 전송 실패 %d: %s", r.status_code, r.text[:200])
            ok = False
        except Exception as e:
            logger.error("Telegram 예외: %s", e)
            ok = False
    if ok:
        logger.info("Telegram 전송 성공 (%d자, %d청크)", len(text), len(chunks))
    return ok


def _chunk_on_lines(text: str, max_len: int) -> list:
    """HTML 태그가 중간에 잘리지 않도록 라인 경계에서 분할.

    단일 라인이 max_len 초과 시 강제 분할. UTF-8 바이트 길이 기준.
    """
    if len(text.encode("utf-8")) <= max_len:
        return [text]
    chunks: list = []
    current = ""
    for line in text.split("\n"):
        test = (current + "\n" + line) if current else line
        if len(test.encode("utf-8")) <= max_len:
            current = test
        else:
            if current:
                chunks.append(current)
            while len(line.encode("utf-8")) > max_len:
                cut = max_len
                while cut > 0 and len(line[:cut].encode("utf-8")) > max_len:
                    cut -= 1
                chunks.append(line[:cut])
                line = line[cut:]
            current = line
    if current:
        chunks.append(current)
    return chunks


def send_telegram_photo(photo_path: str, caption: str = "") -> bool:
    """Telegram 봇 API로 사진 전송 (워드클라우드 등)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram 자격증명 없음 - 사진 전송 스킵")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            r = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=HTTP_TIMEOUT * 3,
            )
        if r.status_code == 200:
            logger.info("Telegram 사진 전송 성공: %s", photo_path)
            return True
        logger.error("Telegram 사진 전송 실패 %d: %s", r.status_code, r.text[:200])
        return False
    except Exception as e:
        logger.error("Telegram 사진 예외: %s", e)
        return False


def _bar_chart(counts: list[tuple[str, int]], max_blocks: int = 20) -> list[str]:
    """(라벨, 개수) 목록 → 최대치 기준 상대 블록 막대 라인. 개수 내림차순 가정."""
    if not counts:
        return []
    max_cnt = max(c for _, c in counts)
    lines = []
    for label, cnt in counts:
        blocks = max(1, round(cnt / max_cnt * max_blocks)) if max_cnt else 0
        lines.append(f"  {label:<16} {'█' * blocks} {cnt}개")
    return lines


def format_theme_section(region: str, theme_counts: list, total: int) -> str:
    """테마 점유율 섹션 (ETF를 최우선 테마로 분류)."""
    flag = _region_flag(region)
    lines = [f"\n🧭 <b>화이트리스트 테마 구성</b> {flag} ({total}개)"]
    lines += _bar_chart(theme_counts[:8])
    return "\n".join(lines)


def format_word_frequency(region: str, words: list, top_n: int = 10) -> str:
    """단어 빈도 순위 섹션."""
    flag = _region_flag(region)
    lines = [f"\n🔤 <b>핵심 단어 빈도</b> {flag}"]
    lines += _bar_chart(words[:top_n])
    return "\n".join(lines)


def format_co_occurrence(region: str, pairs: list, top_n: int = 8) -> str:
    """단어 연관성(공기출) 섹션."""
    flag = _region_flag(region)
    lines = [f"\n🔗 <b>단어 연관성 (함께 등장)</b> {flag}"]
    for a, b, cnt in pairs[:top_n]:
        lines.append(f"  {a} ↔ {b} ({cnt}회)")
    return "\n".join(lines)


def _region_flag(region: str) -> str:
    """region → 국기 이모지. format_breakout_list/format_daily_summary 공용(중복 제거)."""
    return "🇺🇸" if region == "US" else "🇰🇷"


def format_breakout_list(region: str, broke_list: list) -> str:
    """돌파 종목 리스트 통합 메시지. ETF 이름 + 거래량 폭발(🔥 RVOL) 배지 표시.
    broke_list: [{name, symbol, volume_surge, rvol, ...}, ...]
    """
    flag = _region_flag(region)
    if not broke_list:
        return f"📊 <b>실시간 돌파 리포트</b> {flag}\n돌파 종목 없음"
    head = f"📊 <b>실시간 돌파 리포트</b> {flag} - {len(broke_list)}건\n"
    # score 내림차순 정렬
    broke_list = sorted(broke_list, key=lambda x: x.get("score", 0.0), reverse=True)
    new_cnt = sum(1 for x in broke_list if x.get("is_new"))
    surge_cnt = sum(1 for x in broke_list if x.get("volume_surge"))
    if new_cnt:
        head += f"🆕 신규 돌파 {new_cnt}건 (당일 첫 감시 대비)\n"
    if surge_cnt:
        head += f"🔥 거래량 폭발(RVOL ≥ 1.5x) {surge_cnt}건 감지!\n"
    lines = []
    for i, x in enumerate(broke_list[:30], 1):
        sym_part = f" ({x['symbol']})" if region == "US" else ""
        new_mark = " 🆕" if x.get("is_new") else ""
        surge_mark = f" 🔥<b>[RVOL {x['rvol']:.1f}x]</b>" if x.get("volume_surge") else ""
        lines.append(f"{i}. {x['name']}{sym_part}{surge_mark}{new_mark}")
    return head + "\n".join(lines)


def format_daily_summary(whitelist: list, region: str) -> str:
    """일일 배치 요약 (화이트리스트 상위 10개) + 변동성 역가중(Risk Parity) 권장 비중 표기.

    어제 대비 신규 진입 종목(is_new) 을 제일 상단에 배치하고 🆕 표시.
    신규·기존 각각 점수 내림차순 유지(whitelist 가 이미 점수 정렬이므로 순서 보존).
    """
    from .indicators import compute_risk_parity_weights

    flag = _region_flag(region)
    new_cnt = sum(1 for x in whitelist if x.get("is_new"))
    head = f"📋 <b>일일 모멘텀 화이트리스트</b> {flag} ({len(whitelist)}건, 유동성 필터 통과)\n"
    if new_cnt:
        head += f"🆕 신규 {new_cnt}건 (어제 대비)\n"

    # 상위 10개에 대해 변동성 역가중치(Risk Parity) 계산
    top10_weighted = compute_risk_parity_weights(whitelist, max_items=10)
    # 맵으로 변환
    weight_map = {x["symbol"]: x.get("weight_pct", 10.0) for x in top10_weighted}

    # 신규 상단 → 기존 순. 둘 다 점수 내림차순(입력 순서 보존).
    ordered = [x for x in whitelist if x.get("is_new")] + [x for x in whitelist if not x.get("is_new")]
    lines = []
    for i, x in enumerate(ordered[:10], 1):
        sym_part = f" ({x['symbol']})" if region == "US" else ""
        new_mark = " 🆕" if x.get("is_new") else ""
        weight = weight_map.get(x["symbol"])
        weight_str = f" | ⚖️<b>{weight:.1f}%</b>" if weight is not None else ""
        lines.append(
            f"{i}. <b>{x['name']}</b>{sym_part} "
            f"점수:{x['score']:.1f} 모멘텀:{x['momentum']:.1f} BW:{x['bw40']:.1f}{weight_str}{new_mark}"
        )
    return head + "\n".join(lines)