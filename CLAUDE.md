# 돌파etf — 한/미 통합 ETF 저변동 모멘텀 돌파 감시 시스템

Stack: Python (yfinance, FinanceDataReader, pandas, numpy, requests, python-dotenv)

24/7 데몬 — 16:00 KST 일일 배치(모멘텀 스코어링 → 화이트리스트 → 돌파 리포트) +
3시간 폴링 실시간 스캔(KR/US 시장 개장 시 BB40/Donchian 돌파 감지 → Telegram).

## 아키텍처 / 워크플로

```
┌─ 24/7 데몬 (main.py main_loop, systemd: etf_watcher.service) ─┐
│  1. 시작 시 universe 로드(또는 --refresh-universe 구축)         │
│     - US: yf.ETFQuery 자산규모 상위 400 / KR: fdr ETF/KR 필터    │
│     - universe 는 데몬 중 자동 갱신 안 함(재시작/옵션으로 재구축) │
│  2. 16:00 KST 일일 배치 (daily_batch)                            │
│     - 유니버스 전체 모멘텀/BW40/돈치안 계산 → final_score        │
│     - 상위 25% 화이트리스트 저장(whitelist_<region>.json)        │
│     - 화이트리스트 요약 + 전일 종가 돌파 리포트 Telegram         │
│  3. 3시간 폴링 실시간 스캔 (realtime_scan_once)                  │
│     - KR/US 시장 개장 시 화이트리스트 5분봉 실시간가 + 일봉 지표 │
│     - BB40상단 OR Donchian40고점 돌파 → 통합 메시지 1건 전송     │
│     - 60분 쿨다운(alert_history.json) — 중복 알림 방지           │
└──────────────────────────────────────────────────────────────────┘
```

## 모듈

| 파일 | 역할 |
|------|------|
| `main.py` | 24/7 데몬 루프 + CLI(--batch-only/--scan-only/--refresh-universe/--breakout-report). 자정 회전 로깅 |
| `config/settings.py` | 전역 설정 + 휴일 캘린더(NYSE/KRX, 2025-2027 하드코딩) + `yf_sleep()` 공용 |
| `core/universe.py` | US yf.ETFQuery 자산규모 상위 N / KR fdr ETF/KR 정규식 제외 → 유니버스 구축 |
| `core/scanner.py` | daily_batch 스코어링 + 화이트리스트 저장 + 실시간 스캔 + 시장개장 판정 |
| `core/indicators.py` | 모멘텀/BW40/돈치안/final_score/돌파검사 지표 계산 |
| `core/state.py` | JSON 상태 관리(universe/whitelist/alert_history) + `utcnow_iso()` 공용 |
| `core/notifier.py` | Telegram 송부(4096자 라인 경계 분할) + 리포트 포맷 |

## 유니버스

- **US**: `yf.ETFQuery("gt", ["fundnetassets", 0])` + `yf.screen` 페이징(250/페이지), 자산규모 desc 상위 400. 레버리지/인버스 정규식 제외(`US_ETF_EXCLUDE_REGEX`).
- **KR**: `fdr.StockListing("ETF/KR")` + 정규식 제외(단기자금/머니마켓/CD/KOFR/SOFR/파킹/레버리지/인버스 등, `KR_ETF_EXCLUDE_REGEX`).
- **갱신 정책**: 시작 시 1회 구축(파일 있으면 로드). 데몬 중 자동 갱신 안 함. 재구축 = `--refresh-universe` 옵션 명시적 실행. 재시작만으로는 재구축 안 함.

## 지표 공식 (core/indicators.py)

- **수익률**: `_pct_return(period)` = `(new/old - 1)*100` (% 단위). `old = s.iloc[-(period+1)]`, `new = s.iloc[-1]`.
- **모멘텀**: `((r12 + r6)/2) - r3 + r1` (% 단위 가중합, 별도 *100 없음 — _pct_return이 이미 *100)
- **BW40** (볼린저밴드 대역폭): `4 * std_40 / sma_40`
- **BB40 상단**: `SMA40 + 2*STD40`
- **Donchian40 고점**: `s.iloc[-(period+1):-1].max()` (오늘 제외 과거 40일)
- **최종 점수**: `모멘텀 / BW40` (저변동성 가중 — BW 작을수록 점수 증가). `BW40 < MIN_BW40(0.01)` 제외.
- **돌파**: `가격 > BB40상단 OR 가격 > Donchian40고점`. 타입 분류 `_classify_breakout` → "BB40+Donchian"/"BB40"/"Donchian40"/"".

## 실시간 스캔 (core/scanner.py)

- **폴링**: `SCAN_INTERVAL_SEC=10800`(3시간). ETF 느린 추세 → 중복 알림 방지.
- **실시간 가격**: yfinance 5분봉 마지막 close. KR은 `.KS` 우선 실패 시 `.KQ` 폴백(FDR은 5분봉 미지원).
- **신선도**: 5분봉 마지막 바가 `REALTIME_FRESHNESS_SEC(30분)` 초과 시 stale → NaN.
- **통합 전송**: 화이트리스트 전체 스캔 후 돌파 종목 리스트를 1건 메시지로 통합 전송.
- **쿨다운**: 동일 종목 60분 내 재알림 방지(`alert_history.json`).

## 시장 시간 (config/settings.py)

- **KR**: 09:00-15:30 KST (Asia/Seoul). `KR_MARKET_OPEN/CLOSE`.
- **US**: 09:30-16:00 ET (America/New_York, DST 자동). `US_MARKET_OPEN_ET/CLOSE_ET`.
- **휴일**: `NYSE_HOLIDAYS`/`KRX_HOLIDAYS` 하드코딩(2025-2027). **매년 새해 전 갱신 필요** — 2028년 이후 미포함 시 휴일 오판 위험.
- 주말(토/일) 폐장.

## 상태 파일 (data/state/)

| 파일 | 용도 |
|------|------|
| `universe.json` | ETF 유니버스 (US/KR). updated_at, count, data |
| `whitelist_<region>.json` | 일일 배치 화이트리스트(상위 25%). updated_at, count, data |
| `alert_history.json` | 돌파 알림 이력(종목당 최근 50건) — 60분 쿨다운 판정 |

## 로깅

- `logs/etf_watcher.log` — `TimedRotatingFileHandler(when="midnight", backupCount=14)`. 자정 회전 → `etf_watcher.log.YYYY-MM-DD`. 24/7 데몬 날짜 자동 회전.
- `LOG_LEVEL` env(기본 INFO).

## Telegram (core/notifier.py)

- 토큰/ChatID: `.env`에서 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.
- `send_telegram`: 4096자 제한 → 라인 경계 분할(4000자, HTML 태그 중단 방지) 후 순차 전송.
- `format_daily_summary`: 일일 화이트리스트 상위 10건(점수/모멘텀/BW). **어제 대비 신규 진입 종목 제일 상단 배치 + 🆕 표시 + 헤더 건수**(`is_new` 플래그, `daily_batch`에서 어제 화이트리스트 대비 산정).
- `format_breakout_list`: 돌파 종목 리스트(상위 30건, ETF 이름만 간결).

## 이메일 (core/emailer.py) — whitelist Excel 발송

- **트리거**: 일일 배치(`run_daily_batch`)에서 whitelist 갱신 직후 자동(Telegram 요약 후).
- **구조**: `.xlsx` 1개, 시트 2개(KR/US 분리). score 내림차순 정렬, 컬럼 = 종목코드/종목명/리전/최종점수/모멘텀/BW40/BB40상단/Donchian40고점/최근종가.
- **SMTP**: Gmail `smtp.gmail.com:587` STARTTLS. 발신자=`EMAIL_USER`, 수신자=`EMAIL_TO`(둘 다 기본 `yunjin.mike.choi@gmail.com` 자기발송).
- **크레덴셜**: `EMAIL_PASSWORD` = Gmail **앱 비밀번호** 16자리(2차인증 필수, 일반 비밀번호 X). `.env`에서 로드. 미설정 시 스킵(경고 로그, 배치 흐름 유지).
- **실패 격리**: 이메일 발송 실패 시 `False` 반환, 일일 배치/Telegram 알림에는 영향 없음.

## 의존성 (requirements.txt)

- yfinance>=0.2.40 — ETFQuery 스크리너 + 미국 일봉/5분봉
- FinanceDataReader>=0.9.20 — 국내 ETF 리스트 + 일봉
- pandas>=2.0, numpy>=1.24 — 지표 계산
- requests>=2.31 — Telegram API
- python-dotenv>=1.0 — .env 로더

## 실행

```bash
# 24/7 데몬 (systemd)
sudo systemctl enable etf_watcher.service && sudo systemctl start etf_watcher

# CLI 1회성
python3 main.py --refresh-universe   # 유니버스 재구축 후 종료
python3 main.py --batch-only         # 일일 배치 1회
python3 main.py --scan-only          # 실시간 스캔 1회
python3 main.py --breakout-report    # 현재 화이트리스트 돌파 리포트 전송
```

## 리팩토링 이력

- **A1**: main.py 로깅 `FileHandler(날짜고정)` → `TimedRotatingFileHandler(midnight)` (24/7 날짜 회전).
- **A2**: notifier `send_telegram` 4096자 분할(`_chunk_on_lines`) — 긴 리포트 전송 실패 수정.
- **B1**: `_utcnow_iso` 2곳 → `state.utcnow_iso()` public, scanner import.
- **B2**: `_sleep` 2곳 → `config.settings.yf_sleep()`, scanner/universe import.
- **B3**: 돌파 type 분류 2곳 → `indicators._classify_breakout` 헬퍼.
- **C1**: universe 갱신 주석 정정(자동 갱신 안 함 명시).
- **C2**: 휴일 캘린더 매년 갱신 필요 주석.
- **C3**: momentum_score 주석 *100 표현 정리.
- **D1**: notifier region 플래그 중복 → `_region_flag()` 헬퍼(format_breakout_list/format_daily_summary 공용).
- **D2**: emailer 인라인 import(`Font`, `math`) → 모듈 top 이동. 동작 동일.
- **E1 — 일일 화이트리스트 신규 표시**: `daily_batch`가 저장 전 어제 화이트리스트(`whitelist_<region>.json`) 로드 → 당일 `wl` 각 항목에 `is_new`(symbol not in 어제 set) 마킹. `format_daily_summary`가 신규 항목을 제일 상단에 배치(신규·기존 각각 점수 내림차순 유지), 행에 🆕 표시, 헤더에 "🆕 신규 N건 (어제 대비)" 추가. 저장은 `is_new` 제외한 클린본(상태파일 정결). `is_new` 키 없는 레거시 입력은 기존 동작(전부 기존 취급) — 후방호환.

<!-- Auto-generated by IJFW from repo scan. Edit freely -- IJFW only touches the managed block below. -->

<!-- IJFW-MEMORY-START (managed -- do not edit manually) -->
<ijfw-memory>
Project memory at .ijfw/memory/. Call `ijfw_memory_prelude` for full context.
</ijfw-memory>
<!-- IJFW-MEMORY-END -->
