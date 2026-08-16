"""
core/universe.py - ETF 유니버스 구축
- 미국: yfinance 공식 스크리너 (ETFQuery + yf.screen), 자산규모 desc, 상위 N개 페이징
- 한국: FinanceDataReader ETF/KR 리스트 + 정규식 필터 (단기자금/레버리지/인버스 등 제외)
"""
import re
import logging
from typing import List, Dict
import yfinance as yf
import FinanceDataReader as fdr

from config.settings import (
    US_ETF_TOP_N, SCREENER_PAGE_SIZE, KR_ETF_EXCLUDE_REGEX, US_ETF_EXCLUDE_REGEX,
    YF_MAX_RETRY, yf_sleep,
)

logger = logging.getLogger(__name__)


_US_EXCLUDE_RE = re.compile(US_ETF_EXCLUDE_REGEX, re.IGNORECASE)


def fetch_us_etf_universe(top_n: int = US_ETF_TOP_N) -> List[Dict]:
    """
    미국 상장 ETF 중 자산규모(fundnetassets) 내림차순 상위 top_n개 반환.
    yfinance 공식 스크리너 사용 (비공식 크롤링 X).
    페이징: size=250, offset 누적.

    Returns:
        [{"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust"}, ...]
    """
    query = yf.ETFQuery("gt", ["fundnetassets", 0])
    collected: List[Dict] = []
    offset = 0
    page_size = min(SCREENER_PAGE_SIZE, top_n)

    while len(collected) < top_n:
        attempt = 0
        result = None
        last_err = None
        while attempt < YF_MAX_RETRY and result is None:
            try:
                result = yf.screen(
                    query,
                    offset=offset,
                    size=page_size,
                    sortField="fundnetassets",
                    sortAsc=False,
                )
            except Exception as e:
                last_err = e
                attempt += 1
                logger.warning("yf.screen retry %d (offset=%d): %s", attempt, offset, e)
                yf_sleep()
        if result is None:
            logger.error("yf.screen 최종 실패 offset=%d: %s", offset, last_err)
            break

        quotes = result.get("quotes") or []
        if not quotes:
            logger.info("yf.screen 더 이상 결과 없음 (offset=%d)", offset)
            break

        for q in quotes:
            symbol = q.get("symbol") or ""
            name = q.get("shortName") or q.get("longName") or ""
            exchange = q.get("exchange", "")
            # 미국 상장 ETF만: symbol에 점(.)이 있으면 비미국(.VN/.L/.TO/.PA 등)
            if not symbol or "." in symbol:
                continue
            if exchange in ("KSC", "KOE", "JPX", "VSE", "LSE", "PAR", "TOR", "GER", "AMS", "BRU"):
                continue
            # 레버리지/인버스 제외 (Daily Bull/Bear, 2X/3X, Direxion Daily, ProShares Ultra/Short)
            if _US_EXCLUDE_RE.search(name) or _US_EXCLUDE_RE.search(symbol):
                continue
            collected.append({"symbol": symbol, "name": name, "exchange": exchange})
            if len(collected) >= top_n:
                break

        total = result.get("total")
        logger.info("US ETF 유니버스 %d/%d 수집 (offset=%d, total=%s)",
                    len(collected), top_n, offset, total)
        if len(quotes) < page_size:
            break
        offset += page_size
        yf_sleep()

    logger.info("US ETF 유니버스 최종 %d건", len(collected))
    return collected[:top_n]


def fetch_kr_etf_universe() -> List[Dict]:
    """
    한국 ETF 유니버스 (FinanceDataReader 기반).
    단기자금/머니마켓/CD/KOFR/SOFR/파킹/전단채/레버리지/인버스 정규식 제외.

    Returns:
        [{"symbol": "069500", "name": "KODEX 200"}, ...]
    """
    try:
        df = fdr.StockListing("ETF/KR")
    except Exception as e:
        logger.error("FinanceDataReader ETF/KR 조회 실패: %s", e)
        return []

    if df is None or df.empty:
        logger.warning("ETF/KR 빈 응답")
        return []

    # 컬럼 정규화 (Symbol/Name 또는 code/name)
    col_sym = "Symbol" if "Symbol" in df.columns else ("code" if "code" in df.columns else None)
    col_name = "Name" if "Name" in df.columns else ("name" if "name" in df.columns else None)
    if not col_sym or not col_name:
        logger.error("ETF/KR 컬럼 확인 불가: %s", df.columns.tolist())
        return []

    exclude_pattern = re.compile(KR_ETF_EXCLUDE_REGEX)
    out: List[Dict] = []
    for _, row in df.iterrows():
        symbol = str(row[col_sym]).strip()
        name = str(row[col_name]).strip()
        if not symbol or not name:
            continue
        if exclude_pattern.search(name):
            continue
        out.append({"symbol": symbol, "name": name, "exchange": "KSC"})

    logger.info("KR ETF 유니버스 최종 %d건 (필터 전 %d건)",
                len(out), len(df))
    return out


def build_universe() -> Dict[str, List[Dict]]:
    """한/미 ETF 유니버스 동시 구축. 딕셔너리 반환."""
    logger.info("=== 유니버스 구축 시작 ===")
    us = fetch_us_etf_universe()
    kr = fetch_kr_etf_universe()
    logger.info("유니버스 완성: US=%d, KR=%d", len(us), len(kr))
    return {"US": us, "KR": kr}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    u = build_universe()
    print(f"US: {len(u['US'])}개, 예시:", u["US"][:3])
    print(f"KR: {len(u['KR'])}개, 예시:", u["KR"][:3])