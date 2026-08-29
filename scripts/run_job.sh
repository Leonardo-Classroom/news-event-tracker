#!/usr/bin/env bash
# 啟動可追蹤的長時間背景作業
#
#   scripts/run_job.sh <名稱> <manage.py 指令與參數…>
#
# 例：
#   scripts/run_job.sh embed embed_documents
#   scripts/run_job.sh backfill backfill_dataset --site 聯合新聞網
#
# 寫入 PID 檔，使 stop.sh 能偵測到作業、顯示已執行時間，
# 並在使用者未明確要求時**不會誤停**——向量化等作業可能已跑數小時。
set -euo pipefail
cd "$(dirname "$0")/.."

LOGDIR="${LOGDIR:-$HOME/newstrack/logs}"
JOBS_DIR="$LOGDIR/jobs"
mkdir -p "$JOBS_DIR"

[ $# -ge 2 ] || { echo "用法： $0 <名稱> <manage.py 指令…>" >&2; exit 1; }
NAME="$1"; shift

PIDFILE="$JOBS_DIR/$NAME.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "作業 $NAME 已在執行（PID $(cat "$PIDFILE")）" >&2
  exit 1
fi

source "$HOME/anaconda3/etc/profile.d/conda.sh"
conda activate leo3.10

LOG="$LOGDIR/$NAME.log"
setsid nohup python manage.py "$@" > "$LOG" 2>&1 &
echo $! > "$PIDFILE"

echo "作業 $NAME 已啟動（PID $(cat "$PIDFILE")）"
echo "  日誌： $LOG"
echo "  停止： ./stop.sh --jobs"
