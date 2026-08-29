#!/usr/bin/env bash
# 開發期的服務啟停（無 systemd 時使用）
#
#   sudo bash scripts/dev_services.sh start|stop|status
#
# WSL 未啟用 systemd 時，重開 WSL 後 PostgreSQL 與 Redis 不會自動啟動。
# 這支腳本是過渡方案；正式的開機自啟見 Scope 8 任務 51（systemd unit）。

set -uo pipefail

PG_VERSION=16
ACTION="${1:-status}"

if [ "$(id -u)" -ne 0 ] && [ "$ACTION" != "status" ]; then
  echo "start/stop 需要 root：sudo bash $0 $ACTION" >&2
  exit 1
fi

case "$ACTION" in
  start)
    service postgresql start 2>/dev/null || pg_ctlcluster "$PG_VERSION" main start 2>/dev/null
    service redis-server start 2>/dev/null
    sleep 1
    ;;
  stop)
    service redis-server stop 2>/dev/null
    service postgresql stop 2>/dev/null || pg_ctlcluster "$PG_VERSION" main stop 2>/dev/null
    ;;
  status) ;;
  *)
    echo "用法： $0 start|stop|status" >&2
    exit 1
    ;;
esac

echo "--- 服務狀態 ---"
if pg_isready -q 2>/dev/null; then
  echo "  PostgreSQL: 就緒"
else
  echo "  PostgreSQL: 未回應"
fi
if [ "$(redis-cli ping 2>/dev/null)" = "PONG" ]; then
  echo "  Redis:      就緒"
else
  echo "  Redis:      未回應"
fi
