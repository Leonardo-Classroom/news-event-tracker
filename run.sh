#!/usr/bin/env bash
# 新聞事件持續追蹤系統 — 啟動／停止／查看狀態
#
#   ./run.sh          啟動全部（資料庫、Redis、Celery、Django）
#   ./run.sh stop     停止全部
#   ./run.sh status   查看狀態
#   ./run.sh web      只啟動 Django（前景，可看即時日誌）
#
# 服務未以 systemd 管理（ADR-0010）：資料庫與 Redis 裝在 conda 環境
# newstrack-db，WSL 亦尚未啟用 systemd。正式的開機自啟見 Scope 8 任務 51。

set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-5861}"
APP_ENV=leo3.10
DB_ENV=newstrack-db
PGDATA="${PGDATA:-$HOME/newstrack/pgdata}"
REDIS_DIR="$HOME/newstrack/redis"
LOGDIR="${LOGDIR:-$HOME/newstrack/logs}"
PIDFILE="$LOGDIR/django.pid"

mkdir -p "$LOGDIR" "$REDIS_DIR"
source "$HOME/anaconda3/etc/profile.d/conda.sh"

# ---------------------------------------------------------------- 工具

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m•\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }

pg_up()    { conda activate "$DB_ENV" 2>/dev/null; pg_isready -q 2>/dev/null; }
redis_up() { [ "$(redis-cli ping 2>/dev/null)" = "PONG" ]; }
web_up()   { curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/admin/login/"; }

# 刻意不使用 pgrep／pkill：本機曾有程序卡在 D 狀態，
# 使任何掃描 /proc 的工具永久阻塞（見 ADR-0010）。改以 PID 檔管理。
web_pid() { [ -f "$PIDFILE" ] && cat "$PIDFILE" 2>/dev/null; }

# ---------------------------------------------------------------- 啟動

start_infra() {
  conda activate "$DB_ENV"
  if pg_isready -q 2>/dev/null; then
    ok "PostgreSQL 已在執行"
  else
    pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" start >/dev/null 2>&1
    sleep 2
    pg_isready -q 2>/dev/null && ok "PostgreSQL 已啟動" || bad "PostgreSQL 啟動失敗"
  fi

  if redis_up; then
    ok "Redis 已在執行"
  else
    redis-server --daemonize yes --dir "$REDIS_DIR" \
      --logfile "$REDIS_DIR/redis.log" --save 900 1 >/dev/null 2>&1
    sleep 1
    redis_up && ok "Redis 已啟動" || bad "Redis 啟動失敗"
  fi
}

start_workers() {
  conda activate "$APP_ENV"
  # Celery 自身的 inspect 需要 broker，若已有 worker 會回應
  if timeout 8 celery -A config inspect ping >/dev/null 2>&1; then
    ok "Celery worker 已在執行"
    return
  fi
  # 雙佇列（ADR-0007）：fetch 高併發輕量 HTTP；browser 低併發 Playwright，
  # --max-tasks-per-child 讓子程序定期重生以避免瀏覽器累積劣化
  celery -A config worker -Q fetch -n fetch@%h --concurrency=8 \
    --loglevel=INFO --logfile="$LOGDIR/worker-fetch.log" --detach >/dev/null 2>&1
  celery -A config worker -Q browser -n browser@%h --concurrency=2 \
    --max-tasks-per-child=20 \
    --loglevel=INFO --logfile="$LOGDIR/worker-browser.log" --detach >/dev/null 2>&1
  celery -A config beat --loglevel=INFO \
    --logfile="$LOGDIR/beat.log" --pidfile="$LOGDIR/beat.pid" --detach >/dev/null 2>&1
  sleep 4
  ok "Celery worker 與 beat 已啟動（fetch ×8、browser ×2）"
}

start_web() {
  conda activate "$APP_ENV"
  if web_up; then
    ok "Django 已在執行（port $PORT）"
    return
  fi
  nohup python manage.py runserver "0.0.0.0:$PORT" \
    > "$LOGDIR/django.log" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 20); do
    sleep 1
    web_up && break
  done
  if web_up; then
    ok "Django 已啟動"
  else
    bad "Django 啟動失敗，見 $LOGDIR/django.log"
    tail -5 "$LOGDIR/django.log"
  fi
}

# ---------------------------------------------------------------- 停止

stop_all() {
  conda activate "$APP_ENV"

  local pid; pid="$(web_pid)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    # runserver 的 autoreload 會再開一個子程序，需終止整個程序群組
    kill -TERM -- "-$(ps -o pgid= "$pid" 2>/dev/null | tr -d ' ')" 2>/dev/null \
      || kill -TERM "$pid" 2>/dev/null
    sleep 2
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    ok "Django 已停止"
  else
    warn "Django 未在執行"
  fi
  rm -f "$PIDFILE"

  if timeout 10 celery -A config control shutdown >/dev/null 2>&1; then
    ok "Celery worker 已停止"
  else
    warn "Celery worker 未回應（可能已停止）"
  fi
  [ -f "$LOGDIR/beat.pid" ] && kill "$(cat "$LOGDIR/beat.pid")" 2>/dev/null \
    && rm -f "$LOGDIR/beat.pid" && ok "Celery beat 已停止"

  # 資料庫與 Redis 刻意不停：它們持有 238,304 篇語料與 36k 向量，
  # 反覆啟停沒有好處，且背景的向量化作業可能仍在寫入。
  warn "PostgreSQL 與 Redis 保持執行（如需停止：./run.sh stop-db）"
}

stop_db() {
  conda activate "$DB_ENV"
  redis-cli shutdown nosave 2>/dev/null && ok "Redis 已停止" || warn "Redis 未在執行"
  pg_ctl -D "$PGDATA" stop >/dev/null 2>&1 && ok "PostgreSQL 已停止" \
    || warn "PostgreSQL 未在執行"
}

# ---------------------------------------------------------------- 狀態

show_status() {
  echo "── 服務 ──"
  pg_up    && ok "PostgreSQL" || bad "PostgreSQL"
  redis_up && ok "Redis"      || bad "Redis"
  conda activate "$APP_ENV"
  timeout 8 celery -A config inspect ping >/dev/null 2>&1 \
    && ok "Celery worker" || bad "Celery worker"
  web_up && ok "Django　http://localhost:$PORT/admin/" || bad "Django"

  echo
  echo "── 資料 ──"
  python - <<'PY' 2>/dev/null || echo "  （無法連線資料庫）"
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings"); django.setup()
from apps.ingest.models import Document, Source
from apps.events.models import Event
from apps.llm.budget import remaining_usd, spent_usd

docs = Document.objects.count()
print(f"  文件 {docs:,}"
      f"　相關 {Document.objects.relevant().count():,}"
      f"　已向量化 {Document.objects.exclude(embedding=None).count():,}")
print(f"  來源 {Source.objects.filter(enabled=True).count()}"
      f"　事件 {Event.objects.count()}")
print(f"  LLM 已花費 US${spent_usd():.4f}　剩餘 US${remaining_usd():.4f}")
PY
}

# ---------------------------------------------------------------- 進入點

case "${1:-start}" in
  start)
    echo "啟動中…"
    start_infra
    start_workers
    start_web
    echo
    echo "  後台： http://localhost:$PORT/admin/"
    echo "  日誌： $LOGDIR"
    ;;
  stop)     stop_all ;;
  stop-db)  stop_db ;;
  status)   show_status ;;
  web)
    # 前景執行，可直接看到即時日誌與 traceback
    conda activate "$APP_ENV"
    exec python manage.py runserver "0.0.0.0:$PORT"
    ;;
  restart)  stop_all; sleep 2; "$0" start ;;
  *)
    echo "用法： $0 {start|stop|restart|status|web|stop-db}"
    exit 1
    ;;
esac
