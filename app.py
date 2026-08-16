"""
app.py - 돌파etf 통합 인터랙티브 웹 대시보드 (Streamlit)
한/미 ETF 저변동 모멘텀 랭킹, 인터랙티브 캔들 차트, 테마 분석, 변동성 역가중 계산기
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# 프로젝트 루트 경로 등록
_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from config.settings import (
    KST, ET, STATE_DIR,
    BB_PERIOD, BB_STD, DONCHIAN_PERIOD,
    MIN_KR_DAILY_TRADING_VALUE_KRW, MIN_US_DAILY_TRADING_VALUE_USD,
)
from core.indicators import (
    compute_risk_parity_weights, bollinger_bands, donchian_high,
    _extract_close_and_volume, compute_liquidity_stats
)
from core.themes import theme_counts, word_frequency, build_wordcloud
from core.scanner import fetch_kr_close, fetch_us_close, is_market_open

# ── 페이지 기본 설정 ──
st.set_page_config(
    page_title="돌파etf — 한/미 통합 ETF 모멘텀 감시 대시보드",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 커스텀 CSS 스타일링 ──
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif;
    }
    
    .metric-card {
        background: #1e222d;
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 18px 20px;
        margin-bottom: 12px;
    }
    .metric-label {
        font-size: 0.85rem;
        color: #8b949e;
        font-weight: 500;
        margin-bottom: 4px;
    }
    .metric-value {
        font-size: 1.6rem;
        font-weight: 700;
        color: #f0f6fc;
    }
    .metric-sub {
        font-size: 0.8rem;
        color: #58a6ff;
        margin-top: 4px;
    }
    
    .badge-kr {
        background-color: #1f6feb;
        color: white;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    .badge-us {
        background-color: #238636;
        color: white;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    .badge-surge {
        background-color: #d29922;
        color: black;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 700;
    }
    
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        padding: 10px 20px;
        border-radius: 8px 8px 0 0;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ── 데이터 로드 함수 ──
@st.cache_data(ttl=60)
def load_whitelist(region: str) -> dict:
    path = STATE_DIR / f"whitelist_{region}.json"
    if not path.exists():
        return {"updated_at": "-", "count": 0, "scored_count": 0, "data": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        st.error(f"화이트리스트 로드 실패: {e}")
        return {"updated_at": "-", "count": 0, "scored_count": 0, "data": []}


@st.cache_data(ttl=30)
def load_alerts() -> list[dict]:
    path = STATE_DIR / "alert_history.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        alerts = []
        for sym_reg, info in data.items():
            parts = sym_reg.split(":")
            sym = parts[0]
            reg = parts[1] if len(parts) > 1 else "KR"
            alerts.append({
                "symbol": sym,
                "region": reg,
                "timestamp": info.get("timestamp", ""),
                "type": info.get("type", "돌파"),
                "price": info.get("price", 0.0),
            })
        return sorted(alerts, key=lambda x: x["timestamp"], reverse=True)
    except Exception:
        return []


# ── 사이드바 ──
with st.sidebar:
    st.title("🚀 돌파etf 감시 센터")
    st.caption("한/미 통합 저변동 모멘텀 돌파 감시")
    st.divider()

    # 시장 개장 상태
    now_kst = datetime.now(KST)
    now_et = datetime.now(ET)
    kr_open = is_market_open("KR")
    us_open = is_market_open("US")

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        st.markdown(f"**🇰🇷 한국장**")
        if kr_open:
            st.success("🟢 개장 중 (09:00~15:30)")
        else:
            st.info("⚪ 장마감 / 휴장")
        st.caption(f"{now_kst.strftime('%H:%M:%S KST')}")
    with col_s2:
        st.markdown(f"**🇺🇸 미국장**")
        if us_open:
            st.success("🟢 개장 중 (09:30~16:00)")
        else:
            st.info("⚪ 장마감 / 휴장")
        st.caption(f"{now_et.strftime('%H:%M:%S ET')}")

    st.divider()
    st.markdown("### ⚙️ 전략 파라미터")
    st.markdown(f"""
    - **모멘텀**: 12M + 6M - 3M + 1M 가중합
    - **볼린저밴드**: {BB_PERIOD}일 / {BB_STD}σ (BW40)
    - **돈치안 채널**: {DONCHIAN_PERIOD}일 최고가 (당일 제외)
    - **선별 점수**: `모멘텀 / BW40` (저변동 가중)
    - **유동성 바닥 필터**:
      - KR: 20일 평균 거래대금 ≥ {MIN_KR_DAILY_TRADING_VALUE_KRW // 100_000_000}억원
      - US: 20일 평균 거래대금 ≥ ${MIN_US_DAILY_TRADING_VALUE_USD // 1_000_000}M
    - **거래량 폭발 확인**: RVOL ≥ 1.5x
    """)
    st.divider()
    if st.button("⚡ 실시간 온디맨드 스코어링 구동", use_container_width=True):
        with st.spinner("한/미 ETF 유니버스 실시간 스코어링 및 필터링 수행 중 (약 1~2분)..."):
            from core.universe import load_universe, build_universe
            from core.scanner import daily_batch
            u = load_universe()
            if not u or not u.get("KR") or not u.get("US"):
                u = build_universe()
            daily_batch(u)
            st.cache_data.clear()
            st.success("✅ 실시간 스코어링 및 화이트리스트 갱신 완료!")
            st.rerun()

    if st.button("🔄 캐시 새로고침", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── 메인 헤더 & 상태 카드 ──
st.title("🎯 한/미 ETF 저변동 모멘텀 돌파 감시 대시보드")
st.markdown("유니버스 1,300+개 중 엄선된 상위 25% 화이트리스트 및 실시간 돌파 시그널을 제공합니다.")


wl_kr = load_whitelist("KR")
wl_us = load_whitelist("US")
alerts = load_alerts()

# 상단 메트릭 카드 4개
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">🇰🇷 한국 화이트리스트</div>
        <div class="metric-value">{wl_kr.get('count', 0)} <span style="font-size: 0.9rem; color:#8b949e;">/ {wl_kr.get('scored_count', 0)}개</span></div>
        <div class="metric-sub">갱신: {wl_kr.get('updated_at', '-')[:16].replace('T', ' ')} UTC</div>
    </div>
    """, unsafe_allow_html=True)
with c2:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">🇺🇸 미국 화이트리스트</div>
        <div class="metric-value">{wl_us.get('count', 0)} <span style="font-size: 0.9rem; color:#8b949e;">/ {wl_us.get('scored_count', 0)}개</span></div>
        <div class="metric-sub">갱신: {wl_us.get('updated_at', '-')[:16].replace('T', ' ')} UTC</div>
    </div>
    """, unsafe_allow_html=True)
with c3:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">🚨 누적 돌파 감지</div>
        <div class="metric-value">{len(alerts)} <span style="font-size: 0.9rem; color:#8b949e;">건</span></div>
        <div class="metric-sub">최근 감지: {alerts[0]['timestamp'][:16].replace('T', ' ') if alerts else '-'}</div>
    </div>
    """, unsafe_allow_html=True)
with c4:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">⚖️ 포트폴리오 사이징</div>
        <div class="metric-value">Risk Parity</div>
        <div class="metric-sub">1/BW40 변동성 역가중 배분</div>
    </div>
    """, unsafe_allow_html=True)

st.write("")

# ── 탭 메뉴 ──
tabs = st.tabs([
    "📋 화이트리스트 랭킹",
    "📈 인터랙티브 차트 룸",
    "🧭 테마 분석 & 워드클라우드",
    "🚨 돌파 감시 로그",
    "⚖️ 포트폴리오 비중 계산기"
])

# ── TAB 1: 화이트리스트 랭킹 ──
with tabs[0]:
    reg_tab1, reg_tab2 = st.tabs(["🇰🇷 한국 ETF (KR)", "🇺🇸 미국 ETF (US)"])

    for r_tab, r_code, r_data, r_cur in [(reg_tab1, "KR", wl_kr, "원"), (reg_tab2, "US", wl_us, "$")]:
        with r_tab:
            items = r_data.get("data", [])
            if not items:
                st.warning(f"{r_code} 화이트리스트 데이터가 없습니다. 배치 실행이 필요합니다.")
                continue

            # Risk Parity 가중치 부여 (상위 10개)
            top10_weighted = compute_risk_parity_weights(items, max_items=10)
            weight_map = {x["symbol"]: x.get("weight_pct", 0.0) for x in top10_weighted}

            df = pd.DataFrame(items)
            df["순위"] = range(1, len(df) + 1)
            df["권장비중(%)"] = df["symbol"].map(lambda s: f"{weight_map[s]:.1f}%" if s in weight_map else "-")
            df["모멘텀(%)"] = df["momentum"].round(1)
            df["변동성대역(BW40)"] = (df["bw40"] * 100).round(2).astype(str) + "%"
            df["랭킹점수"] = df["score"].round(1)
            df["현재가/종가"] = df["last_close"].map(lambda p: f"{p:,.0f}{r_cur}" if r_code == "KR" else f"${p:,.2f}")
            df["BB40상단"] = df["bb_upper"].map(lambda p: f"{p:,.0f}" if r_code == "KR" else f"${p:,.2f}")
            df["Donchian40"] = df["donchian_high"].map(lambda p: f"{p:,.0f}" if r_code == "KR" else f"${p:,.2f}")

            # 필터 및 검색
            c_f1, c_f2, c_f3 = st.columns([2, 1, 1])
            with c_f1:
                search = st.text_input(f"🔍 종목명 / 티커 검색 ({r_code})", key=f"search_{r_code}")
            with c_f2:
                sort_col = st.selectbox("정렬 기준", ["랭킹점수", "모멘텀(%)", "순위"], key=f"sort_{r_code}")
            with c_f3:
                top_limit = st.slider("표시 개수", 10, len(df), min(30, len(df)), key=f"limit_{r_code}")

            filtered_df = df
            if search:
                filtered_df = filtered_df[
                    filtered_df["name"].str.contains(search, case=False, na=False) |
                    filtered_df["symbol"].str.contains(search, case=False, na=False)
                ]

            if sort_col in filtered_df.columns:
                filtered_df = filtered_df.sort_values(
                    by=sort_col,
                    ascending=(sort_col == "순위")
                )

            view_cols = ["순위", "symbol", "name", "랭킹점수", "모멘텀(%)", "변동성대역(BW40)", "권장비중(%)", "현재가/종가", "BB40상단", "Donchian40"]
            st.dataframe(
                filtered_df[view_cols].head(top_limit),
                use_container_width=True,
                hide_index=True,
            )

            # CSV / Excel 다운로드
            c_d1, c_d2 = st.columns(2)
            with c_d1:
                csv_bytes = filtered_df[view_cols].to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                st.download_button(
                    label=f"📥 {r_code} 화이트리스트 CSV 다운로드",
                    data=csv_bytes,
                    file_name=f"whitelist_{r_code}_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                    use_container_width=True
                )


# ── TAB 2: 인터랙티브 차트 룸 ──
with tabs[1]:
    st.subheader("📈 실시간 기술적 지표 & 돌파 채널 인터랙티브 차트")
    c_c1, c_c2 = st.columns([1, 3])
    
    with c_c1:
        chart_region = st.radio("시장 선택", ["KR", "US"], horizontal=True)
        cur_wl = wl_kr if chart_region == "KR" else wl_us
        wl_items = cur_wl.get("data", [])
        
        options = {f"{x['name']} ({x['symbol']})": x['symbol'] for x in wl_items}
        custom_input = st.text_input("또는 직접 티커 입력 (예: 069500 / SPY)")
        
        selected_symbol = ""
        if custom_input:
            selected_symbol = custom_input.strip()
        elif options:
            sel_label = st.selectbox("화이트리스트 종목 선택", list(options.keys()))
            selected_symbol = options[sel_label]
        
        lookback_days = st.slider("조회 기간 (일)", 60, 400, 200)

    with c_c2:
        if selected_symbol:
            with st.spinner(f"{selected_symbol} 일봉 및 지표 계산 중..."):
                try:
                    if chart_region == "US":
                        df_raw = fetch_us_close(selected_symbol)
                    else:
                        df_raw = fetch_kr_close(selected_symbol)
                    
                    if df_raw is not None and not df_raw.empty:
                        close_s, vol_s = _extract_close_and_volume(df_raw)
                        
                        # 40일 지표 산출
                        sma40 = close_s.rolling(BB_PERIOD).mean()
                        std40 = close_s.rolling(BB_PERIOD).std()
                        bb_upper = sma40 + BB_STD * std40
                        bb_lower = sma40 - BB_STD * std40
                        
                        # 돈치안 40일선
                        dh40 = close_s.shift(1).rolling(DONCHIAN_PERIOD).max()
                        
                        # 차트용 슬라이싱
                        chart_df = pd.DataFrame({
                            "Close": close_s,
                            "SMA40": sma40,
                            "BBUpper": bb_upper,
                            "BBLower": bb_lower,
                            "DonchianHigh": dh40,
                        })
                        if vol_s is not None:
                            chart_df["Volume"] = vol_s
                            chart_df["VolSMA20"] = vol_s.rolling(20).mean()
                            chart_df["RVOL"] = chart_df["Volume"] / chart_df["VolSMA20"]

                        chart_df = chart_df.dropna().iloc[-lookback_days:]

                        # Plotly 서브플롯 생성
                        fig = make_subplots(
                            rows=2, cols=1,
                            shared_xaxes=True,
                            vertical_spacing=0.04,
                            row_heights=[0.75, 0.25],
                            subplot_titles=[f"<b>{selected_symbol}</b> BB40 & Donchian40 채널", "거래량 & 20일 평균"]
                        )

                        # 1. 가격 및 채널 라인
                        fig.add_trace(go.Scatter(
                            x=chart_df.index, y=chart_df["BBUpper"],
                            mode="lines", line=dict(color="rgba(235, 87, 87, 0.8)", width=1.5, dash="dash"),
                            name="BB40 상단 (+2σ)"
                        ), row=1, col=1)

                        fig.add_trace(go.Scatter(
                            x=chart_df.index, y=chart_df["BBLower"],
                            mode="lines", line=dict(color="rgba(46, 204, 113, 0.5)", width=1.5, dash="dash"),
                            fill="tonexty", fillcolor="rgba(255, 255, 255, 0.03)",
                            name="BB40 하단 (-2σ)"
                        ), row=1, col=1)

                        fig.add_trace(go.Scatter(
                            x=chart_df.index, y=chart_df["DonchianHigh"],
                            mode="lines", line=dict(color="#f1c40f", width=2),
                            name="Donchian 40일 최고가"
                        ), row=1, col=1)

                        fig.add_trace(go.Scatter(
                            x=chart_df.index, y=chart_df["SMA40"],
                            mode="lines", line=dict(color="#58a6ff", width=1.5),
                            name="SMA 40 (중심선)"
                        ), row=1, col=1)

                        fig.add_trace(go.Scatter(
                            x=chart_df.index, y=chart_df["Close"],
                            mode="lines", line=dict(color="#ffffff", width=2.2),
                            name="종가 (Close)"
                        ), row=1, col=1)

                        # 2. 거래량 바
                        if "Volume" in chart_df.columns:
                            colors = ["#238636" if r >= 1.5 else "#1f6feb" for r in chart_df.get("RVOL", [1.0] * len(chart_df))]
                            fig.add_trace(go.Bar(
                                x=chart_df.index, y=chart_df["Volume"],
                                marker_color=colors,
                                name="거래량"
                            ), row=2, col=1)

                            fig.add_trace(go.Scatter(
                                x=chart_df.index, y=chart_df["VolSMA20"],
                                mode="lines", line=dict(color="#f39c12", width=1.5),
                                name="20일 평균 거래량"
                            ), row=2, col=1)

                        fig.update_layout(
                            height=580,
                            template="plotly_dark",
                            margin=dict(l=20, r=20, t=40, b=20),
                            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                            hovermode="x unified"
                        )
                        st.plotly_chart(fig, use_container_width=True)

                        # 현재가 돌파 분석 요약
                        last_row = chart_df.iloc[-1]
                        cur_p = last_row["Close"]
                        bb_u = last_row["BBUpper"]
                        dc_h = last_row["DonchianHigh"]
                        is_bb = cur_p > bb_u
                        is_dc = cur_p > dc_h
                        rvol_val = last_row.get("RVOL", 1.0)
                        
                        st.markdown(f"""
                        **📊 실시간 지표 진단 ({selected_symbol}):**
                        - 현재가: `{'${:,.2f}'.format(cur_p) if chart_region == 'US' else '{:,.0f}원'.format(cur_p)}`
                        - BB40 상단: `{'${:,.2f}'.format(bb_u) if chart_region == 'US' else '{:,.0f}원'.format(bb_u)}` ({'🚨 돌파 달성' if is_bb else '미돌파'})
                        - Donchian 40: `{'${:,.2f}'.format(dc_h) if chart_region == 'US' else '{:,.0f}원'.format(dc_h)}` ({'🚨 돌파 달성' if is_dc else '미돌파'})
                        - 상대 거래량(RVOL): `{rvol_val:.2f}x` ({'🔥 거래량 폭발' if rvol_val >= 1.5 else '정상 거래량'})
                        """)
                    else:
                        st.error(f"{selected_symbol} 데이터 페치 실패")
                except Exception as e:
                    st.error(f"차트 렌더링 오류: {e}")


# ── TAB 3: 테마 분석 & 워드클라우드 ──
with tabs[2]:
    st.subheader("🧭 화이트리스트 테마 구성 및 핵심 키워드 분석")
    t_c1, t_c2 = st.columns(2)

    for col, region, wl in [(t_c1, "KR", wl_kr), (t_c2, "US", wl_us)]:
        with col:
            st.markdown(f"### {'🇰🇷 한국 화이트리스트 테마' if region == 'KR' else '🇺🇸 미국 화이트리스트 테마'}")
            items = wl.get("data", [])
            if items:
                names = [x.get("name") or x["symbol"] for x in items]
                t_counts = theme_counts(region, names)
                w_freq = word_frequency(region, names)

                if t_counts:
                    df_theme = pd.DataFrame(t_counts, columns=["테마", "종목수"])
                    fig_pie = px.pie(
                        df_theme, names="테마", values="종목수",
                        hole=0.45,
                        title=f"{region} 테마 점유율 ({len(items)}개 ETF)",
                        color_discrete_sequence=px.colors.qualitative.Plotly
                    )
                    fig_pie.update_layout(template="plotly_dark", height=340, margin=dict(l=10, r=10, t=40, b=10))
                    st.plotly_chart(fig_pie, use_container_width=True)

                if w_freq:
                    df_words = pd.DataFrame(w_freq[:10], columns=["키워드", "빈도"])
                    fig_bar = px.bar(
                        df_words, x="빈도", y="키워드",
                        orientation="h",
                        title=f"{region} 핵심 단어 빈도 Top 10",
                        color="빈도",
                        color_continuous_scale="Viridis"
                    )
                    fig_bar.update_layout(
                        template="plotly_dark",
                        height=300,
                        yaxis=dict(autorange="reversed"),
                        margin=dict(l=10, r=10, t=40, b=10)
                    )
                    st.plotly_chart(fig_bar, use_container_width=True)


# ── TAB 4: 돌파 감시 로그 ──
with tabs[3]:
    st.subheader("🚨 실시간 돌파 감시 및 발송 이력")
    if alerts:
        df_alerts = pd.DataFrame(alerts)
        df_alerts["시장"] = df_alerts["region"].map(lambda r: "🇰🇷 KR" if r == "KR" else "🇺🇸 US")
        df_alerts["일시 (UTC)"] = df_alerts["timestamp"].map(lambda t: t[:19].replace("T", " "))
        df_alerts["돌파 가격"] = df_alerts["price"].map(lambda p: f"{p:,.2f}")
        df_alerts["돌파 유형"] = df_alerts["type"]
        
        st.dataframe(
            df_alerts[["일시 (UTC)", "시장", "symbol", "돌파 유형", "돌파 가격"]],
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("최근 24시간 동안 감지된 돌파 신호가 없습니다.")


# ── TAB 5: 변동성 역가중(Risk Parity) 포트폴리오 계산기 ──
with tabs[4]:
    st.subheader("⚖️ Risk Parity (변동성 역가중) 자산배분 계산기")
    st.markdown("변동성(BW40)이 낮은 저변동 고모멘텀 ETF에 더 많은 비중을 부여하여 위험을 균등화합니다.")

    p_c1, p_c2 = st.columns([1, 2])
    with p_c1:
        sim_region = st.selectbox("시장 선택", ["KR", "US"], key="sim_reg")
        sim_cur = "원" if sim_region == "KR" else "$"
        default_cap = 50_000_000 if sim_region == "KR" else 50_000
        capital = st.number_input(f"총 투자 자본금 ({sim_cur})", value=default_cap, step=1_000_000 if sim_region == "KR" else 1_000)
        top_n = st.slider("포트폴리오 편입 종목 수", 3, 20, 10)

    with p_c2:
        sim_wl = wl_kr if sim_region == "KR" else wl_us
        sim_items = sim_wl.get("data", [])
        if sim_items:
            weighted = compute_risk_parity_weights(sim_items, max_items=top_n)
            sim_df = pd.DataFrame(weighted)
            sim_df["배분비중"] = sim_df["weight_pct"].map(lambda w: f"{w:.1f}%")
            sim_df["목표투자금"] = sim_df["weight_pct"].map(lambda w: f"{int(capital * w / 100):,}{sim_cur}" if sim_region == "KR" else f"${capital * w / 100:,.2f}")
            sim_df["예상매수수량"] = sim_df.apply(
                lambda r: f"{int((capital * r['weight_pct'] / 100) / r['last_close']):,}주" if r["last_close"] > 0 else "-", axis=1
            )
            sim_df["변동성(BW40)"] = (sim_df["bw40"] * 100).round(2).astype(str) + "%"

            fig_p = px.pie(
                sim_df, names="name", values="weight_pct",
                hole=0.4,
                title=f"Risk Parity 최적 배분 비중 (총 자본 {capital:,}{sim_cur})",
                color_discrete_sequence=px.colors.qualitative.Bold
            )
            fig_p.update_layout(template="plotly_dark", height=320, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig_p, use_container_width=True)

            view_sim_cols = ["symbol", "name", "score", "변동성(BW40)", "배분비중", "목표투자금", "예상매수수량"]
            st.dataframe(sim_df[view_sim_cols], use_container_width=True, hide_index=True)
        else:
            st.warning("화이트리스트 데이터가 없습니다.")

# ── 하단 푸터 ──
st.divider()
st.caption(f"돌파etf 마스터 데몬 v2.0 | 실행 시각: {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')} | 24/7 백그라운드 감시 활성화")
