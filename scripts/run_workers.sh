#!/usr/bin/env bash
# 啟動 Celery worker 與 beat（開發用）
#
#   bash scripts/run_workers.sh
#
# ADR-0007 的雙佇列：兩類任務的記憶體與時間特性差異極大，
# 混在同一佇列會互相拖累。
#
#   fetch   高併發、輕量 HTTP（RSS、官方 API、內頁抓取）
#   browser 低併發、Playwright。--max-tasks-per-child 讓子程序定期重生，
#           瀏覽器隨之重建——復刻既有爬蟲「每記者重開瀏覽器」的防劣化設計。
set -euo pipefail
cd "$(dirname "$0")/.."
source "$HOME/anaconda3/etc/profile.d/conda.sh"
conda activate leo3.10

LOGDIR="${LOGDIR:-$HOME/newstrack/logs}"
mkdir -p "$LOGDIR"

celery -A config worker -Q fetch   -n fetch@%h   --concurrency=8 \
  --loglevel=INFO --logfile="$LOGDIR/worker-fetch.log" --detach
celery -A config worker -Q browser -n browser@%h --concurrency=2 \
  --max-tasks-per-child=20 \
  --loglevel=INFO --logfile="$LOGDIR/worker-browser.log" --detach
celery -A config beat --loglevel=INFO \
  --logfile="$LOGDIR/beat.log" --pidfile="$LOGDIR/beat.pid" --detach

sleep 5
echo "已啟動。日誌： $LOGDIR"
celery -A config inspect active_queues --timeout 10 2>/dev/null \
  | grep -E "^->|queue" | head -20 || echo "（worker 尚在啟動中）"
