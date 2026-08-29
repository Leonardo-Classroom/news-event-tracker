#!/usr/bin/env bash
# 新聞事件持續追蹤系統 — 停止服務
#
#   ./stop.sh          停止 Django 與 Celery（資料庫與背景作業保持執行）
#   ./stop.sh --db     一併停止 PostgreSQL 與 Redis
#   ./stop.sh --jobs   一併停止長時間背景作業（向量化、回填等）
#   ./stop.sh --all    全部停止
#
# **預設不停資料庫。** 它持有 238,362 篇語料與向量，反覆啟停沒有好處。
#
# **預設不停背景作業，但會警告。** 向量化等作業可能已跑數小時，
# 誤停會浪費那些時間。腳本會偵測並顯示進度，由人決定是否中止。

set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-5861}"
APP_ENV=leo3.10
DB_ENV=newstrack-db
PGDATA="${PGDATA:-$HOME/newstrack/pgdata}"
LOGDIR="${LOGDIR:-$HOME/newstrack/logs}"
PIDFILE="$LOGDIR/django.pid"
JOBS_DIR="$LOGDIR/jobs"

STOP_DB=0
STOP_JOBS=0
for arg in "$@"; do
  case "$arg" in
    --db)   STOP_DB=1 ;;
    --jobs) STOP_JOBS=1 ;;
    --all)  STOP_DB=1; STOP_JOBS=1 ;;
    -h|--help)
      sed -n '2,13p' "$0" | sed 's/^# \?//'
      exit 0 ;;
    *) echo "未知選項：$arg（可用 --db、--jobs、--all）" >&2; exit 1 ;;
  esac
done

source "$HOME/anaconda3/etc/profile.d/conda.sh"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m•\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }

# 刻意不使用 pgrep／pkill：本機有程序卡在 D 狀態，
# 使任何掃描 /proc 的工具永久阻塞（見 ADR-0010）。
# 改以 PID 檔與 ss 依 port 定位。
kill_tree() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 1
  local pgid; pgid="$(ps -o pgid= "$pid" 2>/dev/null | tr -d ' ')"
  if [ -n "$pgid" ]; then
    kill -TERM -- "-$pgid" 2>/dev/null
  else
    kill -TERM "$pid" 2>/dev/null
  fi
  sleep 2
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
  return 0
}

# ---------------------------------------------------------------- Django

echo "── 應用服務 ──"

stopped_web=0
if [ -f "$PIDFILE" ] && kill_tree "$(cat "$PIDFILE")"; then
  ok "Django 已停止"
  stopped_web=1
fi
rm -f "$PIDFILE"

if [ "$stopped_web" -eq 0 ]; then
  # PID 檔遺失時（如手動啟動過），改以 port 定位
  pid="$(timeout 10 ss -lptn "sport = :$PORT" 2>/dev/null \
         | grep -oP 'pid=\K[0-9]+' | head -1)"
  if [ -n "$pid" ] && kill_tree "$pid"; then
    ok "Django 已停止（依 port $PORT 定位）"
  else
    warn "Django 未在執行"
  fi
fi

# ---------------------------------------------------------------- Celery

conda activate "$APP_ENV"
if timeout 15 celery -A config control shutdown >/dev/null 2>&1; then
  ok "Celery worker 已停止"
else
  warn "Celery worker 未回應（可能已停止）"
fi

if [ -f "$LOGDIR/beat.pid" ]; then
  kill "$(cat "$LOGDIR/beat.pid")" 2>/dev/null && ok "Celery beat 已停止"
  rm -f "$LOGDIR/beat.pid"
else
  warn "Celery beat 未在執行"
fi

# ---------------------------------------------------------------- 背景作業

echo
echo "── 背景作業 ──"

found_job=0
for pidfile in "$JOBS_DIR"/*.pid; do
  [ -e "$pidfile" ] || continue
  name="$(basename "$pidfile" .pid)"
  pid="$(cat "$pidfile" 2>/dev/null)"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pidfile"
    continue
  fi
  found_job=1
  elapsed="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
  if [ "$STOP_JOBS" -eq 1 ]; then
    kill_tree "$pid" && ok "$name 已停止（已執行 $elapsed）"
    rm -f "$pidfile"
  else
    warn "$name 執行中（$elapsed）——保持執行，如需停止請用 --jobs"
  fi
done
[ "$found_job" -eq 0 ] && warn "無登記的背景作業"

# 未經 run_job 啟動的作業偵測不到 PID，改以日誌活動判斷
for log in "$LOGDIR"/embed.log "$LOGDIR"/backfill.log; do
  [ -f "$log" ] || continue
  age=$(( $(date +%s) - $(stat -c %Y "$log") ))
  if [ "$age" -lt 300 ]; then
    warn "$(basename "$log" .log) 疑似仍在寫入（${age}秒前）——"\
"若為未登記的作業，需自行終止"
  fi
done

# ---------------------------------------------------------------- 資料庫

echo
echo "── 資料層 ──"
if [ "$STOP_DB" -eq 1 ]; then
  conda activate "$DB_ENV"
  redis-cli shutdown nosave 2>/dev/null && ok "Redis 已停止" || warn "Redis 未在執行"
  if pg_ctl -D "$PGDATA" stop -m fast >/dev/null 2>&1; then
    ok "PostgreSQL 已停止"
  else
    warn "PostgreSQL 未在執行"
  fi
else
  conda activate "$DB_ENV" 2>/dev/null
  pg_isready -q 2>/dev/null && warn "PostgreSQL 保持執行（如需停止：--db）" \
                            || warn "PostgreSQL 未在執行"
  [ "$(redis-cli ping 2>/dev/null)" = "PONG" ] \
    && warn "Redis 保持執行（如需停止：--db）" || warn "Redis 未在執行"
fi
