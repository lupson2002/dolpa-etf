"""
main.py - 돌파etf 마스터 데몬 (24/7)
- 일일 배치: 16:00 KST에 유니버스 스코어링 → 화이트리스트 갱신 + 전일 종가 돌파 리포트
- 실시간 스캔: 장시간 중 5분 폴링 → 돌파 감지 → Telegram 알림
- 한국 장 (09:00-15:30 KST) + 미국 장 (09:30-16:00 ET, DST 자동)
"""
import logging
import sys
import time
import signal
import argparse
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta

from config.settings import (
    KST, ET, DAILY_BATCH_KST, SCAN_INTERVAL_SEC, KR_SCAN_TIMES,
    LOG_DIR, LOG_LEVEL, LOG_BACKUP_COUNT,
)
from core.universe import build_universe
from core.scanner import (
    daily_batch, realtime_scan_once, is_market_open, load_region_whitelist,
    fetch_us_close, fetch_kr_close,
)
from core.state import (
    save_universe, load_universe,
    load_baseline, save_baseline, is_first_scan_today,
)
from core.notifier import (
    send_telegram, send_telegram_photo,
    format_daily_summary, format_breakout_list,
    format_theme_section, format_word_frequency, format_co_occurrence,
)
from core.emailer import send_theme_email, send_whitelist_email
from core.indicators import is_breakout
from core.themes import theme_counts, word_frequency, co_occurrence, build_wordcloud

# === 로깅 설정 (자정 회전) ===
LOG_DIR.mkdir(parents=True, exist_ok=True)
_root_logger = logging.getLogger()
_root_logger.setLevel(LOG_LEVEL)
# TimedRotatingFileHandler — 자정마다 회전(etf_watcher.log → etf_watcher.log.YYYY-MM-DD).
# 24/7 데몬이 날짜를 넘겨도 전날 파일에 고정되지 않음.
_fh = TimedRotatingFileHandler(
    LOG_DIR / "etf_watcher.log", when="midnight",
    backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s | %(message)s"))
_root_logger.addHandler(_fh)
_root_logger.addHandler(_sh)

logger = logging.getLogger("main")


RUNNING = True


def _shutdown(signum, frame):
    global RUNNING
    logger.info("종료 신호 수신 (%d) - 정리 중...", signum)
    RUNNING = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def refresh_universe():
    """유니버스 재구축 + 저장."""
    logger.info("=== 유니버스 재구축 ===")
    u = build_universe()
    save_universe(u)
    return u


def run_daily_batch(universe):
    """일일 배치 스코어링 + 화이트리스트 요약 + 전일 종가 돌파 리포트."""
    logger.info("=== 일일 배치 시작 ===")
    try:
        whitelists = daily_batch(universe)
        # 1) 화이트리스트 요약 (상위 10건) + 테마/단어 분석 Telegram 발송
        #    US 분석 먼저 → KR 순 (Telegram 메시지/워드클라우드 사진 순서)
        theme_reports: dict = {}  # 이메일용: {region: {text, wordcloud}}
        for region in ("US", "KR"):
            wl = whitelists.get(region, [])
            if wl:
                names = [x.get("name") or x["symbol"] for x in wl]
                msg = format_daily_summary(wl, region)
                theme_text = ""
                try:
                    theme_text = (
                        format_theme_section(region, theme_counts(region, names), len(wl))
                        + format_word_frequency(region, word_frequency(region, names))
                        + format_co_occurrence(region, co_occurrence(region, names))
                    )
                    msg += theme_text
                except Exception as e:
                    logger.warning("[%s] 테마 분석 실패: %s", region, e)
                send_telegram(msg)
                # 워드클라우드 사진 별도 발송
                wc_path = None
                try:
                    wc_path = build_wordcloud(region, names)
                    if wc_path:
                        send_telegram_photo(wc_path, caption=f"☁️ {region} 화이트리스트 단어 클라우드")
                except Exception as e:
                    logger.warning("[%s] 워드클라우드 발송 실패: %s", region, e)
                theme_reports[region] = {"text": theme_text, "wordcloud": wc_path}
        # 2) 화이트리스트 Excel 이메일 발송 (KR/US 시트 → yunjin.mike.choi@gmail.com)
        logger.info("화이트리스트 이메일 발송 중...")
        if not send_whitelist_email(whitelists):
            logger.warning("화이트리스트 이메일 발송 실패/스킵 — 일일 배치는 계속 진행")
        # 2-1) 테마 분석(단어/연관성/워드클라우드) 이메일 발송 — Telegram 전용 → 이메일 추가
        logger.info("테마 분석 이메일 발송 중...")
        if not send_theme_email(theme_reports):
            logger.warning("테마 분석 이메일 발송 실패/스킵 — 일일 배치는 계속 진행")
        # 3) 전일 종가 기준 돌파 종목 리포트 자동 발송
        logger.info("배치 후 돌파 리포트 자동 생성 중...")
        _breakout_report()
        logger.info("=== 일일 배치 완료 ===")
    except Exception as e:
        logger.exception("일일 배치 실패: %s", e)
        send_telegram(f"❌ 일일 배치 실패: {e}")


def next_batch_time() -> datetime:
    """다음 16:00 KST 배치 시간 계산.

    현재 시각이 오늘 16:00 이전이면 오늘 16:00, 이미 지났으면 내일 16:00 반환.
    """
    now = datetime.now(KST)
    batch_today = now.replace(hour=DAILY_BATCH_KST[0], minute=DAILY_BATCH_KST[1],
                              second=0, microsecond=0)
    if now >= batch_today:
        return batch_today + timedelta(days=1)
    return batch_today


def _breakout_report():
    """
    현재 화이트리스트 기준 최신 종가(어제/오늘 일봉) 기준 돌파 종목 리포트.
    각 region별 돌파 종목 리스트를 Telegram으로 전송.
    """
    logger.info("=== 돌파 종목 리포트 생성 ===")
    for region in ("KR", "US"):
        wl = load_region_whitelist(region)
        if not wl:
            logger.info("[%s] 화이트리스트 없음 - 스킵", region)
            continue
        broke_list = []
        for etf in wl:
            symbol = etf["symbol"]
            name = etf.get("name", "")
            try:
                close = fetch_us_close(symbol) if region == "US" else fetch_kr_close(symbol)
                brk = is_breakout(close)
                if brk is None or not brk["breakout"]:
                    continue
                broke_list.append({
                    "symbol": symbol,
                    "name": name,
                    "type": brk["type"],
                    "price": brk["price"],
                    "bb_upper": brk["bb_upper"],
                    "donchian_high": brk["donchian_high"],
                    "score": etf.get("score", 0.0),
                    "momentum": etf.get("momentum", 0.0),
                    "bw40": etf.get("bw40", 0.0),
                })
            except Exception as e:
                logger.warning("[%s] %s 리포트 확인 실패: %s", region, symbol, e)
        msg = format_breakout_list(region, broke_list)
        if send_telegram(msg):
            logger.info("[%s] 돌파 리포트 전송 완료 - 돌파 %d건/%d건",
                        region, len(broke_list), len(wl))


def _next_kr_scan(now_kst: datetime) -> datetime:
    """오늘 남은 KR_SCAN_TIMES 중 now 이후 첫 시각, 없으면 내일 09:15 KST."""
    today = [now_kst.replace(hour=h, minute=m, second=0, microsecond=0)
             for h, m in KR_SCAN_TIMES]
    future = [t for t in today if t > now_kst]
    if future:
        return future[0]
    return (now_kst + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)


def _scan_kr_with_baseline(wl: list) -> None:
    """KR 스캔 + 당일 첫(9:15) baseline 대비 신규 표시.

    첫 스캔(당일 baseline 미존재) → 돌파 set을 baseline 저장(신규 표시 X).
    이후 스캔 → baseline 대비 신규 돌파 is_new=True, baseline은 9:15 고정 유지.
    """
    if is_first_scan_today("KR"):
        sent, symbols = realtime_scan_once("KR", wl, baseline_symbols=None)
        save_baseline(symbols, "KR")
        logger.info("[KR] 당일 첫 스캔 - baseline %d종목 저장, 알림 %d건", len(symbols), sent)
    else:
        baseline = load_baseline("KR") or {"symbols": []}
        sent, symbols = realtime_scan_once("KR", wl, baseline_symbols=baseline.get("symbols", []))
        logger.info("[KR] 스캔 완료 - 알림 %d건 (baseline 9:15 대비)", sent)


def main_loop():
    """메인 루프: 배치 대기 + 실시간 스캔."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-only", action="store_true",
                        help="일일 배치 1회 실행 후 종료")
    parser.add_argument("--scan-only", action="store_true",
                        help="실시간 스캔만 실행 (화이트리스트 파일 사용)")
    parser.add_argument("--refresh-universe", action="store_true",
                        help="유니버스 재구축 후 종료")
    parser.add_argument("--breakout-report", action="store_true",
                        help="현재 화이트리스트 기준 돌파 종목 리포트 Telegram 전송")
    parser.add_argument("--theme-only", action="store_true",
                        help="현재 화이트리스트 테마/단어 분석만 터미널 출력")
    args = parser.parse_args()

    # 초기 유니버스 로드 — 시작 시 1회 구축. 데몬 중 자동 갱신 안 함
    # (재구축 = --refresh-universe 옵션 또는 재시작).
    universe = load_universe()
    if (not universe.get("US") and not universe.get("KR")) or args.refresh_universe:
        universe = refresh_universe()

    if args.refresh_universe:
        return

    if args.batch_only:
        run_daily_batch(universe)
        return

    if args.scan_only:
        for region in ("KR", "US"):
            wl = load_region_whitelist(region)
            if wl:
                sent, _ = realtime_scan_once(region, wl)
                logger.info("[%s] 1회 스캔 완료 - 알림 %d건", region, sent)
        return

    if args.breakout_report:
        _breakout_report()
        return

    if args.theme_only:
        for region in ("US", "KR"):
            wl = load_region_whitelist(region)
            if not wl:
                logger.info("[%s] 화이트리스트 없음 - 스킵", region)
                continue
            names = [x.get("name") or x["symbol"] for x in wl]
            print(f"===== {region} ({len(wl)}개) =====")
            print(format_theme_section(region, theme_counts(region, names), len(wl)))
            print(format_word_frequency(region, word_frequency(region, names)))
            print(format_co_occurrence(region, co_occurrence(region, names)))
        return

    # === 24/7 데몬 루프 ===
    logger.info("=== 돌파etf 마스터 데몬 시작 ===")
    logger.info("KST=%s ET=%s", datetime.now(KST), datetime.now(ET))
    logger.info("=== 최종 점수 계산 공식 ===")
    logger.info("모멘텀 = (((ret_12m + ret_6m)/2) - ret_3m + ret_1m)")
    logger.info("BW40   = 4 * std_40 / sma_40  (40일 볼린저밴드 대역폭)")
    logger.info("최종점수 = 모멘텀 / BW40  (저변동성 가중: BW 작을수록 점수 증가)")
    logger.info("돌파조건: 현재가 > BB40 상단(SMA40+2*STD40)  OR  현재가 > Donchian 40일 고점(오늘 제외)")
    logger.info("KR 고정 스캔: %s KST (영업일)", KR_SCAN_TIMES)
    logger.info("================================")
    next_batch = next_batch_time()
    now = datetime.now(KST)
    next_kr_scan = _next_kr_scan(now)
    next_us_scan = now  # US: 즉시 첫 스캔(개장 시)
    logger.info("다음 배치: %s | 다음 KR 스캔: %s",
                next_batch.isoformat(), next_kr_scan.isoformat())

    while RUNNING:
        now = datetime.now(KST)
        # 일일 배치 트리거 (16:00 KST). universe 는 시작 시 구축한 것 재사용.
        if now >= next_batch:
            logger.info("배치 시간 도달: %s", now.isoformat())
            run_daily_batch(universe)
            next_batch = next_batch_time()
            logger.info("다음 배치 예정: %s", next_batch.isoformat())

        # KR 고정 시간 스캔 (09:15/12:15/14:30 KST) — 영업일만(is_market_open 으로 휴일/주말 스킵)
        if now >= next_kr_scan:
            if is_market_open("KR"):
                wl = load_region_whitelist("KR")
                if wl:
                    try:
                        _scan_kr_with_baseline(wl)
                    except Exception as e:
                        logger.exception("[KR] 스캔 루프 오류: %s", e)
            next_kr_scan = _next_kr_scan(now)
            logger.info("다음 KR 스캔 예정: %s", next_kr_scan.isoformat())

        # US 3시간 스캔 (현행 유지)
        if now >= next_us_scan:
            if is_market_open("US"):
                wl = load_region_whitelist("US")
                if wl:
                    try:
                        sent, _ = realtime_scan_once("US", wl)
                        logger.info("[US] 스캔 완료 - 알림 %d건", sent)
                    except Exception as e:
                        logger.exception("[US] 스캔 루프 오류: %s", e)
            next_us_scan = now + timedelta(seconds=SCAN_INTERVAL_SEC)

        # 60초 스케줄 폴링 대기
        for _ in range(60):
            if not RUNNING:
                break
            time.sleep(1)

    logger.info("데몬 종료.")


if __name__ == "__main__":
    main_loop()