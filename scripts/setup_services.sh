#!/usr/bin/env bash
# 新聞事件持續追蹤系統 — 系統服務安裝（ADR-0008）
#
#   sudo bash scripts/setup_services.sh
#
# 安裝 PostgreSQL 16 + pgvector 與 Redis，並建立專案資料庫。
# 資料目錄採套件預設的 /var/lib/postgresql，位於 WSL2 原生 ext4——
# 符合 ADR-0006 第 6 點「絕對不可置於 /mnt/d」的不可妥協要求。
#
# systemd 為選用：
#   有 systemd → 一併設定開機自啟（systemctl enable）
#   無 systemd → 以 sysvinit 的 service 指令啟動，開機自啟留待 Scope 8 任務 51
#                （開發期用 scripts/dev_services.sh 手動啟停即可）

set -euo pipefail

PG_VERSION=16
DB_NAME=newstrack
DB_USER=newstrack
# 密碼可用環境變數覆寫：DB_PASSWORD=xxx sudo -E bash scripts/setup_services.sh
DB_PASSWORD="${DB_PASSWORD:-newstrack_dev}"

if [ "$(id -u)" -ne 0 ]; then
  echo "請以 root 執行：sudo bash $0" >&2
  exit 1
fi

# 以 /run/systemd/system 是否存在判斷 systemd 是否為 init。
# 刻意不用 pidof / pgrep——這台機器上掃描程序表會卡住。
if [ -d /run/systemd/system ]; then
  HAS_SYSTEMD=1
  echo "==> 偵測到 systemd，將一併設定開機自啟"
else
  HAS_SYSTEMD=0
  echo "==> 未偵測到 systemd，使用 sysvinit 啟動（開機自啟留待 Scope 8）"
fi

# 避免 apt 在設定檔衝突等情況彈出互動提示而永久卡住
export DEBIAN_FRONTEND=noninteractive

# Ubuntu 的 unattended-upgrades 可能正持有 dpkg 鎖。
# apt 2.0+ 支援 DPkg::Lock::Timeout，讓 apt 等待而非立即失敗——
# 比自行輪詢 /proc 乾淨，也避免誤刪鎖檔破壞系統。
APT_OPTS=(-o DPkg::Lock::Timeout=900)

start_service() {
  local name="$1"
  if [ "$HAS_SYSTEMD" -eq 1 ]; then
    systemctl enable --now "$name"
  else
    service "$name" start || true
  fi
}

echo "==> 1/5 安裝 PGDG apt repository"
# 系統上可能存在與本專案無關的失效 repository（例如網址錯誤的第三方來源）。
# 那不該中止安裝，所以容忍 apt-get update 的非零結束；
# 真正重要的是後續 install 步驟能否取得套件，那裡失敗會明確中止。
apt-get "${APT_OPTS[@]}" update || echo "⚠️  部分 repository 更新失敗（見上方 E: 開頭訊息），略過並繼續"
apt-get "${APT_OPTS[@]}" install -y curl ca-certificates gnupg lsb-release
install -d /usr/share/postgresql-common/pgdg
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get "${APT_OPTS[@]}" update || echo "⚠️  部分 repository 更新失敗，略過並繼續"

# 確認 PGDG 本身確實可用——這個失敗必須中止
if ! apt-cache policy "postgresql-${PG_VERSION}" 2>/dev/null | grep -q "apt.postgresql.org"; then
  echo "❌ PGDG repository 未生效，無法取得 postgresql-${PG_VERSION}。" >&2
  echo "   請檢查網路連線與 /etc/apt/sources.list.d/pgdg.list" >&2
  exit 1
fi

echo "==> 2/5 安裝 PostgreSQL ${PG_VERSION} + pgvector"
apt-get "${APT_OPTS[@]}" install -y \
  "postgresql-${PG_VERSION}" \
  "postgresql-client-${PG_VERSION}" \
  "postgresql-${PG_VERSION}-pgvector"

echo "==> 3/5 安裝 Redis"
apt-get "${APT_OPTS[@]}" install -y redis-server

echo "==> 4/5 啟動服務"
start_service postgresql
start_service redis-server
start_service cron

# 無 systemd 時 apt 不會自動起 cluster，補一次
if [ "$HAS_SYSTEMD" -eq 0 ]; then
  pg_ctlcluster "${PG_VERSION}" main start 2>/dev/null || true
fi

# 等待 PostgreSQL 就緒
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q 2>/dev/null; then break; fi
  sleep 1
done
if ! sudo -u postgres pg_isready -q 2>/dev/null; then
  echo "❌ PostgreSQL 未能啟動。檢查： pg_lsclusters 與 /var/log/postgresql/" >&2
  exit 1
fi

echo "==> 5/5 建立資料庫與角色"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
SELECT 'CREATE ROLE ${DB_USER} LOGIN PASSWORD ' || quote_literal('${DB_PASSWORD}')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}')\gexec

SELECT 'CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}')\gexec
SQL

# pgvector 需在目標資料庫內啟用
sudo -u postgres psql -d "${DB_NAME}" -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

# pytest 會建立 test_ 前綴的資料庫，角色需要建庫權限
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -c "ALTER ROLE ${DB_USER} CREATEDB;"

echo ""
echo "============================================"
echo "完成。"
sudo -u postgres psql -tAc "SELECT 'PostgreSQL ' || version();" \
  | cut -d, -f1 | sed 's/^/  /'
sudo -u postgres psql -d "${DB_NAME}" -tAc \
  "SELECT '  pgvector ' || extversion FROM pg_extension WHERE extname='vector';"
redis-cli ping 2>/dev/null | sed 's/^/  redis: /' || echo "  redis: 未回應"
echo ""
echo "  連線字串： postgresql://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"
if [ "$HAS_SYSTEMD" -eq 0 ]; then
  echo ""
  echo "  ⚠️  無 systemd：WSL 重開後服務不會自動啟動。"
  echo "      用 scripts/dev_services.sh start 手動啟動。"
fi
echo "============================================"
