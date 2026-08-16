"""
core/scanner.py - 일일 배치 모멘텀 스코어링 + 실시간 돌파 스캔
- daily_batch: 유니버스 전체 대상 모멘텀/지표 계산 → 화이트리스트 저장
- realtime_scan: 화이트리스트 대상 5분 폴링 → BB40/Donchian 돌파 감지 → Telegram 알림
"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional
import yfinance as yf
import FinanceDataReader as fdr
import pandas as pd

import concurrent.futures
from config.settings import (
    KST, ET, KR_MARKET_OPEN, KR_MARKET_CLOSE,
    US_MARKET_OPEN_ET, US_MARKET_CLOSE_ET,
    NYSE_HOLIDAYS, KRX_HOLIDAYS, REALTIME_FRESHNESS_SEC,
    SCAN_INTERVAL_SEC, YF_MAX_RETRY, MIN_BW40, STATE_DIR, yf_sleep,
    MIN_KR_DAILY_TRADING_VALUE_KRW, MIN_US_DAILY_TRADING_VALUE_USD,
    BATCH_CONCURRENCY_WORKERS_US, BATCH_CONCURRENCY_WORKERS_KR,
)
from .indicators import final_score, is_breakout_realtime
from .state import select_whitelist, is_alert_recent, record_alert, utcnow_iso
from .notifier import send_telegram, format_breakout_list

logger = logging.getLogger(__name__)


def fetch_us_close(symbol: str) -> pd.DataFrame:
    """yfinance에서 미국 ETF 일봉 (1d, 2y) - 종가 및 거래량 포함."""
    attempt = 0
    while attempt < YF_MAX_RETRY:
        try:
            df = yf.download(symbol, period="2y", interval="1d",
                             progress=False, auto_adjust=True, threads=False)
            if df is None or df.empty:
                return pd.DataFrame()
            return df
        except Exception as e:
            attempt += 1
            logger.warning("yf.download 재시도 %d %s: %s", attempt, symbol, e)
            yf_sleep()
    return pd.DataFrame()


def _last_bar_fresh(df: pd.DataFrame) -> bool:
    """5분봉 마지막 바의 신선도(REALTIME_FRESHNESS_SEC 이내) 검사."""
    if df is None or df.empty:
        return False
    try:
        last_ts = df.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        now = datetime.now(last_ts.tzinfo)
        age_sec = (now - last_ts).total_seconds()
        return age_sec <= REALTIME_FRESHNESS_SEC
    except Exception as e:
        logger.warning("신선도 검사 실패: %s", e)
        return False


def fetch_us_realtime_price(symbol: str) -> tuple[float, Optional[float]]:
    """
    미국 ETF 실시간 가격 및 거래량 - yfinance 5분봉 마지막 close & volume.
    반환: (price, volume)
    """
    attempt = 0
    while attempt < YF_MAX_RETRY:
        try:
            df = yf.download(symbol, period="5d", interval="5m",
                             progress=False, auto_adjust=True, threads=False)
            if df is None or df.empty:
                return float("nan"), None
            if not _last_bar_fresh(df):
                logger.info("[US] %s 5분봉 신선도 미달 - stale 간주", symbol)
                return float("nan"), None
            close = df["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = close.dropna()
            if len(close) == 0:
                return float("nan"), None
            
            # 당일 누적 거래량 합산
            vol = None
            if "Volume" in df.columns:
                v_col = df["Volume"].iloc[:, 0] if isinstance(df["Volume"], pd.DataFrame) else df["Volume"]
                # 당일 날짜의 5분봉 거래량 합
                today_date = close.index[-1].date()
                today_vols = v_col[v_col.index.date == today_date].dropna()
                vol = float(today_vols.sum()) if not today_vols.empty else None

            return float(close.iloc[-1]), vol
        except Exception as e:
            attempt += 1
            logger.warning("yf 5m 재시도 %d %s: %s", attempt, symbol, e)
            yf_sleep()
    return float("nan"), None


def fetch_kr_close(symbol: str) -> pd.DataFrame:
    """FinanceDataReader 한국 ETF 일봉 (종가 및 거래량 포함)."""
    try:
        df = fdr.DataReader(symbol, start="20240101")
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning("fdr.DataReader 실패 %s: %s", symbol, e)
        return pd.DataFrame()


def fetch_kr_realtime_price(symbol: str) -> tuple[float, Optional[float]]:
    """
    한국 ETF 실시간 가격 및 거래량 - yfinance Ticker 현재가 및 거래량.
    반환: (price, volume)
    """
    base = symbol[:-3] if symbol.endswith((".KS", ".KQ")) else symbol
    for suffix in (".KS", ".KQ"):
        ym = base + suffix
        try:
            tk = yf.Ticker(ym)
            price = tk.fast_info.last_price
            vol = tk.fast_info.last_volume
            if price is not None and not pd.isna(price) and float(price) > 0:
                cur_vol = float(vol) if vol is not None and not pd.isna(vol) else None
                return float(price), cur_vol
        except Exception as e:
            logger.warning("yf Ticker KR 현재가 %s 실패: %s", ym, e)
            yf_sleep()
    return float("nan"), None


def _score_single_etf(region: str, etf: Dict) -> Optional[Dict]:
    """단일 ETF 지표 계산 및 유동성/변동성 필터링 (멀티스레드 워커 함수)."""
    symbol = etf["symbol"]
    name = etf.get("name", "")
    try:
        if region == "US":
            data = fetch_us_close(symbol)
        else:
            data = fetch_kr_close(symbol)

        snap = final_score(data)
        if snap is None:
            return None

        # 1. BW40 임계값 미만 (저변동 과대평가 방지) 제외
        if snap["bw40"] < MIN_BW40:
            return None

        # 2. 20일 평균 거래대금 바닥 필터 (유동성 부족 슬리피지 방지)
        if region == "KR" and snap["avg_trading_value_20d"] < MIN_KR_DAILY_TRADING_VALUE_KRW:
            return None
        if region == "US" and snap["avg_trading_value_20d"] < MIN_US_DAILY_TRADING_VALUE_USD:
            return None

        return {
            "symbol": symbol,
            "name": name,
            "region": region,
            "momentum": snap["momentum"],
            "bw40": snap["bw40"],
            "bb_upper": snap["bb_upper"],
            "donchian_high": snap["donchian_high"],
            "score": snap["score"],
            "last_close": snap["last_close"],
            "avg_volume_20d": snap["avg_volume_20d"],
            "avg_trading_value_20d": snap["avg_trading_value_20d"],
        }
    except Exception as e:
        logger.warning("[%s] %s 스코어링 실패: %s", region, symbol, e)
        return None


def daily_batch(universe: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
    """
    유니버스 전체 대상 모멘텀/지표 계산 → 상위 25% 화이트리스트 저장 (멀티스레드 병렬 가속).
    반환: {"US": [...], "KR": [...]}
    """
    scored_by_region = {"US": [], "KR": []}

    for region, items in universe.items():
        workers = BATCH_CONCURRENCY_WORKERS_US if region == "US" else BATCH_CONCURRENCY_WORKERS_KR
        logger.info("[%s] 병렬 배치 스코어링 시작 (총 %d건, 워커 %d개)", region, len(items), workers)
        
        results = []
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_etf = {executor.submit(_score_single_etf, region, etf): etf for etf in items}
            for future in concurrent.futures.as_completed(future_to_etf):
                completed += 1
                res = future.result()
                if res is not None:
                    results.append(res)
                if completed % 50 == 0 or completed == len(items):
                    logger.info("[%s] %d/%d 완료 (유효 %d건)", region, completed, len(items), len(results))

        # 랭킹 점수 내림차순 정렬
        scored_by_region[region] = sorted(results, key=lambda x: x["score"], reverse=True)
        logger.info("[%s] 병렬 스코어링 완료: 유효 %d/%d (유동성/변동성 필터 통과)", region, len(results), len(items))

    # 화이트리스트 선택 및 region별 저장
    whitelists = {}
    for region, scored in scored_by_region.items():
        # 어제 화이트리스트(저장 전 현재 파일) 로드 → 신규 종목 판정.
        prev_symbols = {x["symbol"] for x in load_region_whitelist(region)}
        wl = select_whitelist(scored)
        for x in wl:
            x["is_new"] = x["symbol"] not in prev_symbols
        whitelists[region] = wl
        # 저장은 is_new 제외한 클린본(상태파일 정결 유지). 반환은 is_new 포함본.
        save_region_whitelist(
            region, [{k: v for k, v in x.items() if k != "is_new"} for x in wl], scored
        )
        new_cnt = sum(1 for x in wl if x.get("is_new"))
        logger.info("[%s] 화이트리스트 %d건 (신규 %d건, 스코어링 유효 %d건)",
                    region, len(wl), new_cnt, len(scored))

    return whitelists


def save_region_whitelist(region: str, whitelist: List[Dict], scored: List[Dict]) -> None:
    """region별 화이트리스트 저장 (data/state/whitelist_<region>.json)."""
    path = STATE_DIR / f"whitelist_{region}.json"
    payload = {
        "updated_at": utcnow_iso(),
        "region": region,
        "count": len(whitelist),
        "scored_count": len(scored),
        "data": whitelist,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[%s] 화이트리스트 저장 완료: %s", region, path.name)


def load_region_whitelist(region: str) -> List[Dict]:
    path = STATE_DIR / f"whitelist_{region}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("data", [])
    except Exception as e:
        logger.warning("화이트리스트 로드 실패 %s: %s", path, e)
        return []


def _fetch_daily_and_price(region: str, symbol: str) -> tuple:
    """region별 일봉(지표용) + 실시간 가격 및 거래량 페치. (daily, cur_price, cur_vol) 반환."""
    if region == "US":
        p, v = fetch_us_realtime_price(symbol)
        return fetch_us_close(symbol), p, v
    p, v = fetch_kr_realtime_price(symbol)
    return fetch_kr_close(symbol), p, v


def realtime_scan_once(region: str, whitelist: List[Dict],
                       baseline_symbols: List[str] | None = None) -> tuple:
    """
    화이트리스트 전체 스캔 후 돌파 종목 리스트를 1건의 Telegram 메시지로 통합 전송.
    - US: yfinance 일봉(지표) + 5분봉 마지막 close(실시간 가격) & 누적 거래량
    - KR: FDR 일봉(지표) + yfinance 5분봉(.KS/.KQ) 실시간 가격 & 누적 거래량
    - 쿨다운(60분) 중인 종목은 리스트에서 제외
    - 거래량 폭발(RVOL >= 1.5x) 여부 자동 판정
    - baseline_symbols(KR): 당일 첫 스캔 돌파 set. 이 set에 없는 돌파 종목 = 신규(is_new)
    - 반환: (전송 메시지 수, 돌파 종목 symbol 리스트)
    """
    broke_list: List[Dict] = []
    for etf in whitelist:
        symbol = etf["symbol"]
        name = etf.get("name", "")
        try:
            daily, cur_price, cur_vol = _fetch_daily_and_price(region, symbol)
            if daily is None or len(daily) == 0:
                yf_sleep(); continue
            if pd.isna(cur_price) or cur_price <= 0:
                yf_sleep(); continue
            brk = is_breakout_realtime(daily, cur_price, cur_vol)
            if brk is None or not brk["breakout"]:
                yf_sleep(); continue
            # 쿨다운 확인 (60분) - 중복 알림 방지
            if is_alert_recent(symbol, region, cooldown_minutes=60):
                logger.info("[%s] %s 돌파감지 but 쿨다운 중 - 스킵", region, symbol)
                yf_sleep(); continue
            # 신규 판정: baseline 있고 해당 symbol 없으면 신규(KR만). US/첫스캔은 False.
            is_new = bool(baseline_symbols is not None and symbol not in baseline_symbols)
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
                "volume_surge": brk.get("volume_surge", False),
                "rvol": brk.get("rvol", 1.0),
                "is_new": is_new,
            })
        except Exception as e:
            logger.warning("[%s] %s 스캔 실패: %s", region, symbol, e)
        yf_sleep()

    # 전체 스캔 후 통합 메시지 1건 전송
    sent = 0
    if broke_list:
        msg = format_breakout_list(region, broke_list)
        if send_telegram(msg):
            sent = 1
            # 알림 이력 기록 (쿨다운용)
            for x in broke_list:
                record_alert(x["symbol"], region, {
                    "type": x["type"], "price": x["price"],
                })
            new_cnt = sum(1 for x in broke_list if x.get("is_new"))
            surge_cnt = sum(1 for x in broke_list if x.get("volume_surge"))
            logger.info("[%s] 통합 돌파 리포트 전송 - 돌파 %d건/%d건 (신규 %d건, 거래량폭발 %d건)",
                        region, len(broke_list), len(whitelist), new_cnt, surge_cnt)
    else:
        logger.info("[%s] 스캔 완료 - 돌파 0건/%d건", region, len(whitelist))
    return sent, [x["symbol"] for x in broke_list]



def is_market_open(region: str) -> bool:
    """현재 시장 개장 여부 (시간대 + 휴일 + 주말 체크)."""
    if region == "KR":
        now = datetime.now(KST)
        holidays = KRX_HOLIDAYS
        open_h, open_m = KR_MARKET_OPEN
        close_h, close_m = KR_MARKET_CLOSE
    elif region == "US":
        now = datetime.now(ET)
        holidays = NYSE_HOLIDAYS
        open_h, open_m = US_MARKET_OPEN_ET
        close_h, close_m = US_MARKET_CLOSE_ET
    else:
        return False
    if now.weekday() >= 5:  # 토/일 폐장
        return False
    if now.date() in holidays:
        return False
    t = now.hour * 60 + now.minute
    return open_h * 60 + open_m <= t <= close_h * 60 + close_m