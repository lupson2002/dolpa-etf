# 돌파etf — 한/미 통합 ETF 저변동 모멘텀 돌파 감시 시스템

24/7 데몬으로 한국·미국 ETF 유니버스를 모니터링하여 BB40 상단 돌파 / Donchian 40일 고점 돌파를 감지하고 Telegram으로 알림을 전송합니다.

## 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│  일일 배치 (16:00 KST)                                  │
│  유니버스 구축 → 모멘텀/지표 계산 → 상위 25% 화이트리스트 │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│  실시간 스캔 (5분 폴링, 장시간만)                        │
│  화이트리스트 대상 BB40/Donchian 돌파 감지 → Telegram   │
└─────────────────────────────────────────────────────────┘
```

### 데이터 소스 (공식 API만 사용)
- **미국 ETF**: `yfinance` 공식 스크리너 (`yf.ETFQuery` + `yf.screen`)
  - 자산규모(`fundnetassets`) 내림차순 상위 400개 (변수화)
  - 페이징: `size=250`, `offset` 누적
- **한국 ETF**: `FinanceDataReader.StockListing("ETF/KR")`
  - 정규식 필터: 단기자금/머니마켓/CD/KOFR/SOFR/파킹/전단채/레버리지/인버스 제외

### 지표 & 필터링
- **모멘텀 스코어** = `(((ret_12m + ret_6m)/2) - ret_3m + ret_1m)` (퍼센트)
- **BW40** (볼린저밴드 대역폭) = `4 * std_40 / sma_40`
- **최종 랭킹 점수** = `momentum / BW40` (저변동성 가중)
- **BB40 상단** = `SMA40 + 2 * STD40`
- **Donchian 40일 고점** = 오늘 제외 직전 40일 최고가
- **유동성 바닥 필터 (Liquidity Floor)**: 20일 평균 일일 거래대금 기준 미달 종목(KR 1억원 미만, US $3M 미만) 자동 배제
- **배치 병렬 가속 (Concurrency)**: `ThreadPoolExecutor` 멀티스레드로 1,300+개 ETF 스코어링 소요시간을 20분 ➔ 3분대로 단축

### 돌파 감지 및 거래량 폭발 조건
- 현재가가 BB40 상단 **또는** Donchian 40일 고점(오늘 제외)을 상향 돌파 시 Telegram 실시간 알림. (동일 종목 60분 쿨다운)
- **거래량 폭발 확인 (Volume Surge Confirmation)**: 당일 실시간 거래량이 20일 평균 거래량의 150% 이상($\text{RVOL} \ge 1.5\times$)일 경우 `🔥 [RVOL 1.8x]` 강조 배지 부착.


## 디렉토리 구조

```
돌파etf/
├── config/
│   ├── __init__.py
│   └── settings.py         # 전역 설정
├── core/
│   ├── __init__.py
│   ├── universe.py         # ETF 유니버스 구축
│   ├── indicators.py       # 모멘텀/BB/Donchian 지표
│   ├── scanner.py          # 배치 + 실시간 스캔
│   ├── state.py            # JSON 상태 관리
│   └── notifier.py         # Telegram 알림
├── data/
│   └── state/              # 유니버스/화이트리스트/알림이력 JSON
├── logs/                   # 날짜별 로그
├── main.py                 # 마스터 데몬
├── etf_watcher.service     # systemd 유닛
├── install.sh              # systemd 설치 스크립트
├── requirements.txt
├── .env.example
└── .gitignore
```

## 설치

### 1. 의존성
```bash
cd /home/mikey/돌파etf
pip install -r requirements.txt
```

### 2. 환경변수
```bash
cp .env.example .env
# .env 편집:
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...
```

Telegram 봇 토큰은 [@BotFather](https://t.me/BotFather)에서 생성. chat_id는 봇에 메시지 보낸 후 `https://api.telegram.org/bot<TOKEN>/getUpdates`로 확인.

### 3. 초기 유니버스 구축
```bash
python3 main.py --refresh-universe
```
- US 400 + KR ~900 ETF 수집 → `data/state/universe.json` 저장

### 4. 일일 배치 1회 테스트
```bash
python3 main.py --batch-only
```
- 전 유니버스 스코어링 (약 15-25분 소요) → `data/state/whitelist_US.json`, `whitelist_KR.json` 생성

### 5. 실시간 스캔 1회 테스트
```bash
python3 main.py --scan-only
```
- 화이트리스트 기반 1회 스캔 + 알림 전송

### 6. systemd 데몬 등록 (24/7)
```bash
sudo bash install.sh
systemctl status etf_watcher.service
journalctl -u etf_watcher -f
```

## 시장 시간
- **한국**: 09:00-15:30 KST (Asia/Seoul)
- **미국**: 09:30-16:00 ET (America/New_York, DST 자동 반영)
- **일일 배치**: 16:00 KST 매일 (한/미 동시 스코어링)

## 실행 모드 요약

| 명령 | 동작 |
|------|------|
| `python3 main.py` | 24/7 데몬 (배치 + 실시간 스캔) |
| `python3 main.py --refresh-universe` | 유니버스 재구축 후 종료 |
| `python3 main.py --batch-only` | 일일 배치 1회 실행 후 종료 |
| `python3 main.py --scan-only` | 실시간 스캔 1회 실행 후 종료 |

## 주의사항
- yfinance 호출 사이 랜덤 sleep (0.3-1.0s) + 최대 3회 재시도로 블록 방지
- Telegram 자격증명 없으면 알림 스킵 (로그만 기록)
- 동일 종목 60분 쿨다운으로 알림 스팸 방지
- 비공식 웹 스크래핑 사용하지 않음 (yfinance 공식 스크리너 + FinanceDataReader 공식 API만)