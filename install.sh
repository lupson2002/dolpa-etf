#!/usr/bin/env bash
# 돌파etf systemd 설치 스크립트
# 사용: sudo bash install.sh
set -e

SRC="/home/mikey/돌파etf/etf_watcher.service"
DST="/etc/systemd/system/etf_watcher.service"

if [ "$EUID" -ne 0 ]; then
  echo "sudo 권한 필요: sudo bash install.sh"
  exit 1
fi

echo "1. 서비스 파일 복사: $SRC -> $DST"
cp "$SRC" "$DST"

echo "2. systemd 데몬 리로드"
systemctl daemon-reload

echo "3. 서비스 활성화 (부팅 시 자동 시작)"
systemctl enable etf_watcher.service

echo "4. 서비스 시작"
systemctl start etf_watcher.service

echo ""
echo "=== 설치 완료 ==="
echo "상태: systemctl status etf_watcher.service"
echo "로그: journalctl -u etf_watcher -f  또는 /home/mikey/돌파etf/logs/systemd.log"
echo "중지: sudo systemctl stop etf_watcher"
echo "재시작: sudo systemctl restart etf_watcher"