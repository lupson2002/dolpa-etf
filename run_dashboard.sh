#!/usr/bin/env bash
# run_dashboard.sh - 돌파etf Streamlit 대시보드 실행기 (포트 8502)
cd "$(dirname "$0")"

PORT=8502
echo "🚀 [돌파etf] Streamlit 대시보드를 시작합니다 (포트: $PORT)..."
echo "🌐 브라우저 접속 주소: http://localhost:$PORT"

streamlit run app.py --server.port $PORT --server.headless true
