"""
core/state.py - 상태 관리 (유니버스/화이트리스트/돌파 이력)
JSON 기반 영속화 - data/state/*.json
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict

from config.settings import STATE_DIR, WHITELIST_TOP_PCT, KST

logger = logging.getLogger(__name__)

UNIVERSE_FILE = STATE_DIR / "universe.json"
ALERT_HISTORY_FILE = STATE_DIR / "alert_history.json"


def _baseline_path(region: str) -> Path:
    return STATE_DIR / f"daily_baseline_{region}.json"


def load_baseline(region: str = "KR") -> dict | None:
    """당일 첫 스캔 돌파 종목 baseline. {date, symbols} 없으면 None."""
    p = _load(_baseline_path(region))
    return p if p else None


def save_baseline(symbols: list, region: str = "KR") -> None:
    """당일 첫 스캔 돌파 symbol set 저장(신규 표시 baseline)."""
    today = datetime.now(KST).date().isoformat()
    _save(_baseline_path(region), {"date": today, "symbols": list(symbols)})


def is_first_scan_today(region: str = "KR") -> bool:
    """당일 첫 스캔 여부 — baseline 미존재 or 날짜 다름(자정 리셋)."""
    p = load_baseline(region)
    if not p:
        return True
    today = datetime.now(KST).date().isoformat()
    return p.get("date") != today


def utcnow_iso() -> str:
    """UTC 현재시각 ISO 문자열 (Z 접미사). scanner 모듈에서 공용 사용."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("상태 파일 로드 실패 %s: %s", path, e)
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_universe(universe: Dict[str, List[Dict]]) -> None:
    payload = {
        "updated_at": utcnow_iso(),
        "count": {k: len(v) for k, v in universe.items()},
        "data": universe,
    }
    _save(UNIVERSE_FILE, payload)
    logger.info("유니버스 저장: %s", payload["count"])


def load_universe() -> Dict[str, List[Dict]]:
    p = _load(UNIVERSE_FILE)
    return p.get("data", {"US": [], "KR": []})


def select_whitelist(scored: List[Dict], top_pct: float = WHITELIST_TOP_PCT) -> List[Dict]:
    """
    score 내림차순 정렬 후 상위 top_pct% 선택.
    scored 항목: {symbol, name, region, score, momentum, bw40, last_close, ...}
    """
    valid = [x for x in scored if x.get("score") is not None]
    valid.sort(key=lambda x: x["score"], reverse=True)
    n = max(1, int(len(valid) * top_pct))
    return valid[:n]


def is_alert_recent(symbol: str, region: str, cooldown_minutes: int = 60) -> bool:
    """
    동일 종목의 최근 알림이 cooldown 내 있으면 True (중복 알림 방지).
    """
    hist = _load(ALERT_HISTORY_FILE)
    key = f"{region}:{symbol}"
    entries = hist.get(key, [])
    if not entries:
        return False
    last = entries[-1]
    ts = last.get("ts")
    if not ts:
        return False
    try:
        last_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    now = datetime.now(last_dt.tzinfo) if last_dt.tzinfo else datetime.now(timezone.utc)
    elapsed_min = (now - last_dt).total_seconds() / 60.0
    return elapsed_min < cooldown_minutes


def record_alert(symbol: str, region: str, info: dict) -> None:
    hist = _load(ALERT_HISTORY_FILE)
    key = f"{region}:{symbol}"
    entries = hist.get(key, [])
    entries.append({
        "ts": utcnow_iso(),
        **info,
    })
    # 최근 50개만 유지
    hist[key] = entries[-50:]
    _save(ALERT_HISTORY_FILE, hist)