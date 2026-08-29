#!/usr/bin/env bash
# 新聞事件持續追蹤系統 — 以 conda 安裝 PostgreSQL + pgvector（ADR-0010）
#
#   bash scripts/setup_db_conda.sh
#
# 不需要 sudo、不碰 apt / dpkg。伺服器裝在獨立的 conda 環境 newstrack-db，
# 與應用環境 leo3.10 分離，避免為了裝 PG server 而讓 conda 重解 torch 等相依。
#
# 資料目錄置於 WSL2 原生 ext4（$HOME 下），符合 ADR-0006 第 6 點——
# 絕不可放在 /mnt/d，那裡經 9P 協定存取，效能差一到兩個數量級。

set -euo pipefail

DB_ENV=newstrack-db
PGDATA="${PGDATA:-$HOME/newstrack/pgdata}"
PGPORT="${PGPORT:-5432}"
DB_NAME=newstrack
DB_USER=newstrack
DB_PASSWORD="${DB_PASSWORD:-newstrack_dev}"

# shellcheck disable=SC1091
source "$HOME/anaconda3/etc/profile.d/conda.sh"

banner() { echo; echo "=============== $* ==============="; }

# ---------------------------------------------------------------- 1. 建立環境
banner "1/5 建立 conda 環境 $DB_ENV"
if conda env list | awk '{print $1}' | grep -qx "$DB_ENV"; then
  echo "    已存在，略過建立"
else
  conda create -y -n "$DB_ENV" -c conda-forge postgresql pgvector redis-server
fi
conda activate "$DB_ENV"
echo "    postgres: $(command -v postgres)"
postgres --version

# ---------------------------------------------------------------- 2. 初始化資料目錄
banner "2/5 初始化資料目錄"
echo "    位置： $PGDATA"
case "$PGDATA" in
  /mnt/*)
    echo "    ❌ 資料目錄不得位於 /mnt（Windows 檔案系統，9P 效能災難）" >&2
    exit 1 ;;
esac

if [ -f "$PGDATA/PG_VERSION" ]; then
  echo "    已初始化（PG_VERSION $(cat "$PGDATA/PG_VERSION")），略過"
else
  mkdir -p "$PGDATA"
  chmod 700 "$PGDATA"
  pwfile="$(mktemp)"
  printf '%s' "$DB_PASSWORD" > "$pwfile"
  initdb -D "$PGDATA" -U "$DB_USER" --auth-local=trust --auth-host=scram-sha-256 \
    --pwfile="$pwfile" --encoding=UTF8 --locale=C
  rm -f "$pwfile"
fi

# ---------------------------------------------------------------- 3. 啟動
banner "3/5 啟動 PostgreSQL"
if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  echo "    已在執行中"
else
  pg_ctl -D "$PGDATA" -o "-p $PGPORT" -l "$PGDATA/server.log" start
fi

for _ in $(seq 1 30); do
  pg_isready -p "$PGPORT" -q && break
  sleep 1
done
if ! pg_isready -p "$PGPORT" -q; then
  echo "    ❌ 啟動失敗，最後 20 行日誌：" >&2
  tail -20 "$PGDATA/server.log" >&2
  exit 1
fi
echo "    就緒（port $PGPORT）"

# ---------------------------------------------------------------- 4. 建立資料庫
banner "4/5 建立資料庫與 extension"
if psql -p "$PGPORT" -U "$DB_USER" -d postgres -tAc \
     "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  echo "    資料庫 $DB_NAME 已存在"
else
  createdb -p "$PGPORT" -U "$DB_USER" -O "$DB_USER" "$DB_NAME"
  echo "    已建立 $DB_NAME"
fi

psql -p "$PGPORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

# ---------------------------------------------------------------- 5. 驗證
banner "5/5 驗證"
psql -p "$PGPORT" -U "$DB_USER" -d "$DB_NAME" -tA <<'SQL'
SELECT '  PostgreSQL ' || current_setting('server_version');
SELECT '  pgvector ' || extversion FROM pg_extension WHERE extname = 'vector';
SQL

# 實際建一張帶向量與 HNSW 索引的表，確認 pgvector 真的可用
psql -p "$PGPORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE TABLE IF NOT EXISTS _smoke_vec (id serial PRIMARY KEY, v halfvec(3));
CREATE INDEX IF NOT EXISTS _smoke_vec_hnsw ON _smoke_vec
  USING hnsw (v halfvec_cosine_ops);
INSERT INTO _smoke_vec (v) VALUES ('[1,2,3]'), ('[4,5,6]');
SQL
echo -n "  halfvec + HNSW 煙霧測試： "
psql -p "$PGPORT" -U "$DB_USER" -d "$DB_NAME" -tAc \
  "SELECT count(*) || ' 列，最近鄰 id=' ||
   (SELECT id FROM _smoke_vec ORDER BY v <=> '[1,2,3]' LIMIT 1) FROM _smoke_vec;"
psql -p "$PGPORT" -U "$DB_USER" -d "$DB_NAME" -q -c "DROP TABLE _smoke_vec;"

# ---------------------------------------------------------------- 6. Redis
banner "6/6 啟動 Redis（Celery broker）"
REDIS_DIR="$HOME/newstrack/redis"
mkdir -p "$REDIS_DIR"
if redis-cli ping >/dev/null 2>&1; then
  echo "    已在執行中"
else
  redis-server --daemonize yes --dir "$REDIS_DIR" \
    --logfile "$REDIS_DIR/redis.log" --save 900 1
  sleep 2
fi
echo -n "    ping： "; redis-cli ping 2>/dev/null || echo "無回應"

cat <<MSG

============================================
✅ 完成

  連線字串： postgresql://${DB_USER}:${DB_PASSWORD}@localhost:${PGPORT}/${DB_NAME}
  資料目錄： ${PGDATA}
  伺服器日誌： ${PGDATA}/server.log

啟停（需先 conda activate ${DB_ENV}）：
  pg_ctl -D ${PGDATA} start
  pg_ctl -D ${PGDATA} stop
  pg_ctl -D ${PGDATA} status

或用： bash scripts/dev_services.sh start|stop|status
============================================
MSG
