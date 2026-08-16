"""
core/indicators.py - 기술지표 계산
- 모멘텀 스코어: (((ret_12m + ret_6m)/2) - ret_3m + ret_1m)  [퍼센트 단위]
- 볼린저밴드 (40일, 2σ): BW40 = 4*std_40/sma_40, 상단 = SMA40 + 2*STD40
- 돈치안 40일 고점 (오늘 제외)
- 최종 랭킹 점수: momentum / BW40 (저변동성 가중)
"""
import logging
from typing import Optional
import numpy as np
import pandas as pd

from config.settings import (
    MOM_PERIOD_12M, MOM_PERIOD_6M, MOM_PERIOD_3M, MOM_PERIOD_1M,
    BB_PERIOD, BB_STD, DONCHIAN_PERIOD, VOLUME_SURGE_RATIO,
)

logger = logging.getLogger(__name__)


def _extract_close_and_volume(data) -> tuple[pd.Series, Optional[pd.Series]]:
    """
    DataFrame(멀티인덱스 또는 단일 컬럼) 또는 Series에서 종가(Close)와 거래량(Volume)을 분리 추출.
    반환: (close_series, volume_series)
    """
    if isinstance(data, pd.Series):
        return pd.Series(data.values, index=data.index).astype(float), None

    if not isinstance(data, pd.DataFrame) or data.empty:
        return pd.Series(dtype=float), None

    close_s = None
    vol_s = None

    # 1. MultiIndex columns: ('Close', 'SPY') or ('Volume', 'SPY')
    if isinstance(data.columns, pd.MultiIndex):
        for lvl0 in ("Close", "Adj Close", "close"):
            for col in data.columns:
                if col[0] == lvl0:
                    close_s = data[col]
                    break
            if close_s is not None:
                break
        for lvl0 in ("Volume", "volume"):
            for col in data.columns:
                if col[0] == lvl0:
                    vol_s = data[col]
                    break
            if vol_s is not None:
                break
    else:
        # 2. Standard columns
        for c in ("Close", "Adj Close", "close"):
            if c in data.columns:
                close_s = data[c]
                break
        for c in ("Volume", "volume", "vol"):
            if c in data.columns:
                vol_s = data[c]
                break

    if close_s is None:
        close_s = data.iloc[:, 0]

    close_series = pd.Series(close_s.values, index=data.index).astype(float)
    volume_series = pd.Series(vol_s.values, index=data.index).astype(float) if vol_s is not None else None

    return close_series, volume_series


def _to_1d_series(data) -> pd.Series:
    """단일 종가 시리즈 추출 (하위 호환용)."""
    close, _ = _extract_close_and_volume(data)
    return close


def compute_liquidity_stats(data, period: int = 20) -> dict:
    """20일 평균 일일 거래량 및 평균 일일 거래대금(Close * Volume) 산출."""
    close, vol = _extract_close_and_volume(data)
    if vol is None or len(vol.dropna()) < period:
        return {"avg_volume_20d": 0.0, "avg_trading_value_20d": 0.0}

    valid = close.notna() & vol.notna()
    c_clean = close[valid]
    v_clean = vol[valid]
    if len(v_clean) < period:
        return {"avg_volume_20d": 0.0, "avg_trading_value_20d": 0.0}

    avg_vol = float(v_clean.iloc[-period:].mean())
    dollar_vol = c_clean * v_clean
    avg_dollar_vol = float(dollar_vol.iloc[-period:].mean())

    return {
        "avg_volume_20d": avg_vol if not np.isnan(avg_vol) else 0.0,
        "avg_trading_value_20d": avg_dollar_vol if not np.isnan(avg_dollar_vol) else 0.0,
    }


def _pct_return(series: pd.Series, period: int) -> Optional[float]:
    """period 영업일 수익률(%) 반환. 데이터 부족 시 None."""

    if series is None:
        return None
    try:
        s = _to_1d_series(series).dropna()
        if len(s) < period + 1:
            return None
        old = s.iloc[-(period + 1)]
        new = s.iloc[-1]
        if old <= 0 or np.isnan(old) or np.isnan(new):
            return None
        return (new / old - 1.0) * 100.0
    except Exception as e:
        logger.warning("수익률 계산 실패 period=%d: %s", period, e)
        return None


def momentum_score(close: pd.Series) -> Optional[float]:
    """
    모멘텀 스코어 = ((12M + 6M)/2) - 3M + 1M  (퍼센트 단위 가중합)
    각 수익률 r* 은 _pct_return 이 이미 *100 한 % 단위이므로 별도 *100 없음.
    입력 close: 종가 시리즈 (영업일 기준)
    """
    r12 = _pct_return(close, MOM_PERIOD_12M)
    r6 = _pct_return(close, MOM_PERIOD_6M)
    r3 = _pct_return(close, MOM_PERIOD_3M)
    r1 = _pct_return(close, MOM_PERIOD_1M)
    if r12 is None or r6 is None or r3 is None or r1 is None:
        return None
    # 퍼센트 단위 그대로 (이미 *100), 가중합
    score = ((r12 + r6) / 2.0) - r3 + r1
    return float(score)


def bollinger_bands(close: pd.Series, period: int = BB_PERIOD,
                    std_mult: float = BB_STD) -> dict:
    """
    볼린저밴드 (SMA 기반).
    반환: {sma, std, upper, lower, bandwidth}
    bandwidth = 4 * std / sma  (정규화 변동성 척도)
    """
    s = _to_1d_series(close).dropna()
    if len(s) < period:
        return {}
    sma = s.rolling(window=period).mean().iloc[-1]
    std = s.rolling(window=period).std().iloc[-1]
    if pd.isna(sma) or pd.isna(std) or sma <= 0:
        return {}
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    bw = (4.0 * std) / sma
    return {"sma": float(sma), "std": float(std),
            "upper": float(upper), "lower": float(lower),
            "bandwidth": float(bw)}


def donchian_high(close: pd.Series, period: int = DONCHIAN_PERIOD,
                  exclude_last: bool = True) -> Optional[float]:
    """돈치안 channel 고점.

    exclude_last=True (일봉/EOD용): 마지막 바(=오늘) 제외, 직전 period일 고점.
    exclude_last=False (실시간용): 마지막 period일(=어제까지 포함) 고점 —
      daily_close 의 마지막 바가 어제이므로 어제를 '과거'에 포함해야 정확.
    """
    s = _to_1d_series(close).dropna()
    if exclude_last:
        if len(s) < period + 1:
            return None
        window = s.iloc[-(period + 1):-1]
    else:
        if len(s) < period:
            return None
        window = s.iloc[-period:]
    if len(window) < period:
        return None
    return float(window.max())


def final_score(data) -> Optional[dict]:
    """
    최종 랭킹 점수: momentum / BW40
    저변동성 모멘텀: BW40 작을수록 점수 가중치 증가.
    반환: {momentum, bw40, bb_upper, donchian_high, score, last_close, avg_volume_20d, avg_trading_value_20d}
    """
    close, vol = _extract_close_and_volume(data)
    mom = momentum_score(close)
    bb = bollinger_bands(close)
    dh = donchian_high(close)
    if mom is None or not bb or dh is None:
        return None
    bw = bb["bandwidth"]
    if bw <= 0:
        return None
    score = mom / bw
    last = float(_to_1d_series(close).dropna().iloc[-1])
    
    liq = compute_liquidity_stats(data, period=20)
    
    return {
        "momentum": mom,
        "bw40": bw,
        "bb_upper": bb["upper"],
        "bb_sma": bb["sma"],
        "donchian_high": dh,
        "score": float(score),
        "last_close": last,
        "avg_volume_20d": liq["avg_volume_20d"],
        "avg_trading_value_20d": liq["avg_trading_value_20d"],
    }


def compute_risk_parity_weights(items: list[dict], max_items: int = 10) -> list[dict]:
    """
    변동성 역가중(Risk Parity) 포트폴리오 비중(%) 산출.
    w_i = (1 / bw40_i) / sum(1 / bw40_k)
    각 item에 'weight_pct' (0.0~100.0) 추가하여 반환.
    """
    if not items:
        return []
    
    target_items = items[:max_items]
    inv_vols = []
    for x in target_items:
        bw = x.get("bw40", 0.0)
        inv_vols.append(1.0 / bw if bw > 0 else 0.0)
    
    sum_inv = sum(inv_vols)
    res = []
    for i, x in enumerate(target_items):
        item_copy = dict(x)
        w = (inv_vols[i] / sum_inv * 100.0) if sum_inv > 0 else (100.0 / len(target_items))
        item_copy["weight_pct"] = round(float(w), 1)
        res.append(item_copy)
    return res



def _classify_breakout(broke_bb: bool, broke_dc: bool) -> str:
    """BB40/Donchian40 돌파 타입 분류. 미돌파 시 빈 문자열.

    is_breakout·is_breakout_realtime 공용(중복 분류 로직 제거).
    """
    if broke_bb and broke_dc:
        return "BB40+Donchian"
    if broke_bb:
        return "BB40"
    if broke_dc:
        return "Donchian40"
    return ""


def is_breakout(data) -> Optional[dict]:
    """
    일봉 종가 기준 돌파 검사 (마지막 일봉 종가 vs BB40상단/Donchian40고점) + 거래량 폭발(RVOL) 검사.
    반환: {breakout: bool, type: str, price, bb_upper, donchian_high, volume_surge: bool, rvol: float}
    """
    snap = final_score(data)
    if snap is None:
        return None
    price = snap["last_close"]
    bb_upper = snap["bb_upper"]
    dh = snap["donchian_high"]
    broke_bb = price > bb_upper
    broke_dc = price > dh
    
    # 거래량 폭발 검사 (당일 거래량 vs 20일 평균 거래량)
    _, vol = _extract_close_and_volume(data)
    volume_surge = False
    rvol = 1.0
    if vol is not None and len(vol.dropna()) >= 20 and snap["avg_volume_20d"] > 0:
        last_vol = float(vol.dropna().iloc[-1])
        rvol = last_vol / snap["avg_volume_20d"]
        volume_surge = bool(rvol >= VOLUME_SURGE_RATIO)

    return {
        "breakout": bool(broke_bb or broke_dc),
        "type": _classify_breakout(broke_bb, broke_dc),
        "price": price,
        "bb_upper": bb_upper,
        "donchian_high": dh,
        "volume_surge": volume_surge,
        "rvol": float(rvol),
        "avg_volume_20d": snap["avg_volume_20d"],
        "avg_trading_value_20d": snap["avg_trading_value_20d"],
    }


def is_breakout_realtime(daily_data, current_price: float, current_volume: Optional[float] = None) -> Optional[dict]:
    """
    실시간 가격(current_price) vs 일봉 기반 BB40상단/Donchian40고점 돌파 검사 + 거래량 폭발(RVOL) 검사.
    daily_data: 2y 일봉 (DataFrame 또는 Series)
    current_price: 5분봉 마지막 close 등 실시간 가격
    current_volume: 장중 누적 거래량 (있는 경우)
    """
    if current_price is None or pd.isna(current_price) or current_price <= 0:
        return None
    close, vol = _extract_close_and_volume(daily_data)
    bb = bollinger_bands(close)
    dh = donchian_high(close, exclude_last=False)  # 실시간: 어제 포함 40일
    if not bb or dh is None:
        return None
    bb_upper = bb["upper"]
    broke_bb = current_price > bb_upper
    broke_dc = current_price > dh
    
    # 거래량 폭발 검사
    liq = compute_liquidity_stats(daily_data, period=20)
    avg_vol = liq["avg_volume_20d"]
    volume_surge = False
    rvol = 1.0
    if current_volume is not None and not pd.isna(current_volume) and current_volume > 0 and avg_vol > 0:
        rvol = current_volume / avg_vol
        volume_surge = bool(rvol >= VOLUME_SURGE_RATIO)
    
    return {
        "breakout": bool(broke_bb or broke_dc),
        "type": _classify_breakout(broke_bb, broke_dc),
        "price": float(current_price),
        "bb_upper": float(bb_upper),
        "donchian_high": float(dh),
        "volume_surge": volume_surge,
        "rvol": float(rvol),
        "avg_volume_20d": avg_vol,
        "avg_trading_value_20d": liq["avg_trading_value_20d"],
    }


if __name__ == "__main__":
    # 간이 테스트용 더미 데이터
    np.random.seed(1)
    dummy = pd.DataFrame({
        "close": np.cumsum(np.random.randn(300)) + 100.0,
        "volume": np.random.randint(10000, 500000, size=300)
    })
    print("final_score:", final_score(dummy))
    print("is_breakout:", is_breakout(dummy))