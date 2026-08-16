"""
돌파etf - 전역 설정 (config/settings.py)
한/미 통합 ETF 저변동 모멘텀 돌파 감시 시스템
"""
import os
import time
import random
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# .env 로드 (프로젝트 루트)
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

# === 경로 ===
BASE_DIR = _ROOT
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = DATA_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# === 유니버스 ===
US_ETF_TOP_N = int(os.getenv("US_ETF_TOP_N", "400"))        # 미국 ETF 상위 N개 (자산규모 기준)
SCREENER_PAGE_SIZE = 250                                     # yfinance screen 1회 최대 size
KR_ETF_EXCLUDE_REGEX = r"(단기자금|머니마켓|머니본|CD|KOFR|SOFR|파킹|전단채|레버리지|인버스|마켓본|파생|선물|채권|단기채|회사채|KIS|KODEX\s*KOSDAQ\s*150|TIGER\s*KOSDAQ\s*150)"
US_ETF_EXCLUDE_REGEX = r"(Daily\s+\w+\s+Bull|Daily\s+\w+\s+Bear|2X|3X|Leveraged|Inverse|Direxion\s+Daily|ProShares\s+Ultra|ProShares\s+UltraPro|ProShares\s+Short)"
MIN_BW40 = 0.01  # BW40 임계값 - 이 값 미만은 저변동성 과대평가 방지용 제외

# === 모멘텀 스코어 ===
MOM_PERIOD_12M = 252   # 영업일 기준 ~12개월
MOM_PERIOD_6M = 126
MOM_PERIOD_3M = 63
MOM_PERIOD_1M = 21

# === 변동성/돌파 지표 ===
BB_PERIOD = 40          # 40일 볼린저밴드
BB_STD = 2.0             # 2표준편차
DONCHIAN_PERIOD = 40    # 40일 돈치안
WHITELIST_TOP_PCT = 0.25  # 상위 25% → 실시간 감시 화이트리스트

# === 3대 개선 포인트 설정: 유동성 바닥 / 거래량 폭발 / 병렬 배치 ===
# 1. 20일 평균 거래대금 바닥 필터 (저유동성 슬리피지 방지)
MIN_KR_DAILY_TRADING_VALUE_KRW = int(os.getenv("MIN_KR_DAILY_TRADING_VALUE_KRW", "100_000_000"))  # KR 1억원 이상
MIN_US_DAILY_TRADING_VALUE_USD = int(os.getenv("MIN_US_DAILY_TRADING_VALUE_USD", "3_000_000"))   # US $3M 이상

# 2. 돌파 거래량 폭발 확인 배수 (20일 평균 거래량 대비)
VOLUME_SURGE_RATIO = float(os.getenv("VOLUME_SURGE_RATIO", "1.5"))  # 150% (1.5배 이상 시 '거래량 폭발' 태깅)

# 3. 일일 배치 병렬 스레드 워커 수 (속도 5배 가속)
BATCH_CONCURRENCY_WORKERS_US = int(os.getenv("BATCH_CONCURRENCY_WORKERS_US", "5"))
BATCH_CONCURRENCY_WORKERS_KR = int(os.getenv("BATCH_CONCURRENCY_WORKERS_KR", "8"))

# === 실시간 스캔 ===
SCAN_INTERVAL_SEC = 10800  # US 3시간 폴링 (ETF 추세 느린 반응 → 중복 알림 방지)

# 한국장 영업일 고정 스캔 시각 (KST). 60초 스케줄러가 이 시각에 도달 시 스캔.
# 휴일/주말은 is_market_open 으로 스킵. 개장 시각(09:00)과 정렬 위해 15분 지연 시작.
KR_SCAN_TIMES = [(9, 15), (12, 15), (14, 30)]

# === 시장 시간 (KST = Asia/Seoul, ET = America/New_York, DST 자동반영) ===
KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

# 한국 장: 09:00-15:30 KST
KR_MARKET_OPEN = (9, 0)
KR_MARKET_CLOSE = (15, 30)

# 미국 장: 22:30-05:00 KST (ET 장시간 09:30-16:00 ET → KST 변환, DST 자동적용)
# ET 기준 09:30-16:00로 고정하면 zoneinfo가 DST 처리
US_MARKET_OPEN_ET = (9, 30)
US_MARKET_CLOSE_ET = (16, 0)

# === 휴일 캘린더 (NYSE / KRX) — 장 미개장일 ===
# 주의: 하드코딩(2025-2027). 매년 새해 전 갱신 필요 — 2028년 이후 미포함 시
# 해당일 휴장인데도 시장 개장으로 오판하여 휴일 스캔/배치 실행 위험.
# NYSE 휴장일 (America/New_York 기준 날짜)
NYSE_HOLIDAYS = {
    # 2025
    date(2025, 1, 1),   # New Year's Day
    date(2025, 1, 20),  # MLK Day
    date(2025, 2, 17),  # Washington's Birthday
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 26),  # Memorial Day
    date(2025, 6, 19),  # Juneteenth
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 11, 27), # Thanksgiving
    date(2025, 12, 25), # Christmas
    # 2026
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Washington's Birthday
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day observed (Jul 4 = Saturday)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
    # 2027
    date(2027, 1, 1),   # New Year's Day
    date(2027, 1, 18),  # MLK Day
    date(2027, 2, 15),  # Washington's Birthday
    date(2027, 3, 26),  # Good Friday
    date(2027, 5, 31),  # Memorial Day
    date(2027, 6, 18),  # Juneteenth (observed, Jul 4 = Sunday)
    date(2027, 7, 5),   # Independence Day observed
    date(2027, 9, 6),   # Labor Day
    date(2027, 11, 25), # Thanksgiving
    date(2027, 12, 24), # Christmas observed (Dec 25 = Saturday)
}

# KRX 휴장일 (Asia/Seoul 기준 날짜) — 2026년
KRX_HOLIDAYS = {
    date(2026, 1, 1),    # 신정
    date(2026, 2, 16),   # 설날 연휴 (임시공휴일 포함 예상)
    date(2026, 2, 17),   # 설날
    date(2026, 2, 18),   # 설날
    date(2026, 3, 1),    # 삼일절
    date(2026, 5, 1),    # 근로자의 날
    date(2026, 5, 5),    # 어린이날
    date(2026, 5, 21),   # 부처님 오신 날 (예상)
    date(2026, 6, 6),    # 현충일
    date(2026, 8, 15),   # 광복절
    date(2026, 9, 23),   # 추석 연휴 (예상)
    date(2026, 9, 24),   # 추석
    date(2026, 9, 25),   # 추석 연휴
    date(2026, 10, 3),   # 개천절
    date(2026, 10, 9),   # 한글날
    date(2026, 12, 25),  # 크리스마스
    date(2025, 1, 1), date(2025, 3, 1), date(2025, 5, 5), date(2025, 6, 6),
    date(2025, 8, 15), date(2025, 10, 3), date(2025, 10, 9), date(2025, 12, 25),
    # 2027 — 음력 휴일(설/부처님/추석)은 추정치, 공식 확정 후 갱신 필요
    date(2027, 1, 1),    # 신정
    date(2027, 2, 8),    # 설날 연휴 (음력 1/1=2/6, 2/8 공휴일 추정)
    date(2027, 3, 1),    # 삼일절
    date(2027, 5, 1),    # 근로자의 날
    date(2027, 5, 5),    # 어린이날
    date(2027, 5, 19),   # 부처님 오신 날 (추정)
    date(2027, 6, 6),    # 현충일
    date(2027, 8, 15),   # 광복절
    date(2027, 9, 14),   # 추석 연휴 (추정)
    date(2027, 9, 15),   # 추석 (음력 8/15)
    date(2027, 9, 16),   # 추석 연휴 (추정)
    date(2027, 10, 3),   # 개천절
    date(2027, 10, 9),   # 한글날
    date(2027, 12, 25),  # 크리스마스
}

# 5분봉 데이터 신선도 임계값 (초) - 마지막 5분봉이 이 시간보다 오래되면 stale
REALTIME_FRESHNESS_SEC = 30 * 60  # 30분

# === 일일 배치 스케줄 ===
# 한국 장 종료 후 16:00 KST에 모멘텀 스코어 계산 (한/미 동시 처리)
DAILY_BATCH_KST = (16, 0)

# === Telegram ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === 이메일 (Gmail SMTP) — whitelist Excel 발송 ===
# 발신자 = EMAIL_USER(기본 yunjin.mike.choi@gmail.com), 수신자 = EMAIL_TO(동일).
# Gmail 일반 비밀번호 대신 '앱 비밀번호' 16자리 사용(2차인증 필수).
# EMAIL_PASSWORD 만 .env에 입력하면 발송 동작.
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "yunjin.mike.choi@gmail.com")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_TO = os.getenv("EMAIL_TO", "yunjin.mike.choi@gmail.com")

# === 네트워크 ===
HTTP_TIMEOUT = 15
YF_SLEEP_MIN = 0.3   # yfinance 호출 사이 최소 sleep (초)
YF_SLEEP_MAX = 1.0
YF_MAX_RETRY = 3


def yf_sleep() -> None:
    """yfinance 호출 사이 랜덤 sleep (YF_SLEEP_MIN~MAX) — IP 블록 방지.

    scanner/universe 양쪽에서 공용 사용(중복 _sleep 제거).
    """
    time.sleep(random.uniform(YF_SLEEP_MIN, YF_SLEEP_MAX))

# === 로그 ===
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_ROTATE_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 14