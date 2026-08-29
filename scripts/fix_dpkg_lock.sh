#!/usr/bin/env bash
# 修復卡死的 dpkg 交易
#
#   sudo bash scripts/fix_dpkg_lock.sh
#
# 情境：unattended-upgrades 底下的 dpkg 在 --configure libssl3 階段睡死超過 11 天，
#       長期持有 dpkg 鎖，導致任何 apt 操作都無法進行。
#
# 這與本專案無關，是系統的既有問題，但它擋住了 PostgreSQL 的安裝。
#
# 安全考量：
#   - 只終止「持有鎖且處於 sleeping 狀態」的程序，不盲目殺 dpkg
#   - 卡在 --configure 階段時檔案已解壓到位，dpkg --configure -a 是既定復原路徑
#   - 每步驗證，非預期狀況即中止並回報，不繼續往下衝
#   - 刻意不使用 pgrep / pidof / fuser——這台機器上掃描完整程序表會卡住

set -uo pipefail

DISABLE_CONF=/etc/apt/apt.conf.d/99-temp-disable-unattended
MAX_ROUNDS=5

if [ "$(id -u)" -ne 0 ]; then
  echo "請以 root 執行：sudo bash $0" >&2
  exit 1
fi

banner() { echo; echo "=============== $* ==============="; }

# ---------------------------------------------------------------- 1. 暫停自動更新
banner "1/4 暫時停用 unattended-upgrades"
if [ -f "$DISABLE_CONF" ]; then
  echo "    已存在，略過"
else
  printf 'APT::Periodic::Unattended-Upgrade "0";\n' > "$DISABLE_CONF"
  echo "    已建立 $DISABLE_CONF（最後會提示如何還原）"
fi

# ---------------------------------------------------------------- 2. 解除鎖定
banner "2/4 解除 dpkg 鎖"

describe_pid() {
  local pid="$1"
  [ -d "/proc/$pid" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null
}

kill_lock_holder() {
  # 從 dpkg 的錯誤訊息取得持鎖 PID——不必掃描程序表
  local out pid ppid state
  out="$(dpkg --configure -a 2>&1)"
  if ! grep -q "locked by another process" <<<"$out"; then
    return 1   # 沒有鎖問題了
  fi

  pid="$(grep -oP 'pid \K[0-9]+' <<<"$out" | head -1)"
  if [ -z "$pid" ] || [ ! -d "/proc/$pid" ]; then
    echo "    偵測到鎖但找不到對應程序，可能是殘留狀態" >&2
    return 1
  fi

  state="$(awk '/^State:/{print $2}' "/proc/$pid/status" 2>/dev/null)"
  ppid="$(awk '/^PPid:/{print $2}' "/proc/$pid/status" 2>/dev/null)"

  echo "    持鎖程序 PID $pid（狀態 $state）"
  echo "      指令： $(describe_pid "$pid")"

  # 只處理 sleeping / stopped。R（執行中）代表它真的在做事，不該殺。
  case "$state" in
    S|D|T|Z) ;;
    *)
      echo "    ❌ 該程序狀態為 $state（可能正在運作中），中止以免破壞交易。" >&2
      echo "       請稍候再試；若持續如此請回報。" >&2
      exit 2
      ;;
  esac

  if [ -n "$ppid" ] && [ "$ppid" != "1" ] && [ -d "/proc/$ppid" ]; then
    echo "      父程序 $ppid： $(describe_pid "$ppid")"
    kill -9 "$ppid" 2>/dev/null || true
  fi
  kill -9 "$pid" 2>/dev/null || true
  sleep 3

  if [ -d "/proc/$pid" ]; then
    echo "    ❌ PID $pid 無法終止（可能處於不可中斷的 D 狀態）。" >&2
    echo "       這通常需要重啟 WSL 才能清除。" >&2
    exit 3
  fi
  echo "    已終止"
  return 0
}

for round in $(seq 1 "$MAX_ROUNDS"); do
  if kill_lock_holder; then
    echo "    第 $round 輪：已清除一個持鎖程序，重新檢查…"
  else
    echo "    鎖已釋放"
    break
  fi
done

# ---------------------------------------------------------------- 3. 完成中斷交易
banner "3/4 完成中斷的 dpkg 交易"
echo "    （libssl3 的檔案早已解壓到位，此步僅執行未完成的設定腳本）"
if ! DEBIAN_FRONTEND=noninteractive dpkg --configure -a; then
  echo >&2
  echo "❌ dpkg --configure -a 失敗。請把上方錯誤訊息回報，先不要繼續。" >&2
  exit 4
fi

echo "    修補相依關係…"
DEBIAN_FRONTEND=noninteractive apt-get -f install -y \
  -o DPkg::Lock::Timeout=300 || true

# ---------------------------------------------------------------- 4. 驗證
banner "4/4 驗證"
bad=0
while read -r pkg status; do
  if [ "$status" = "install ok installed" ]; then
    printf "  ✓ %-12s %s\n" "$pkg" "$status"
  else
    printf "  ✗ %-12s %s\n" "$pkg" "$status"
    bad=1
  fi
done < <(dpkg-query -W -f='${Package} ${Status}\n' libssl3 libssl-dev libc-bin 2>/dev/null)

leftover="$(ls -A /var/lib/dpkg/updates/ 2>/dev/null | wc -l)"
if [ "$leftover" -eq 0 ]; then
  echo "  ✓ /var/lib/dpkg/updates/ 已清空（無殘留交易）"
else
  echo "  ✗ /var/lib/dpkg/updates/ 仍有 $leftover 個檔案"
  bad=1
fi

echo
if [ "$bad" -eq 0 ]; then
  cat <<'MSG'
============================================
✅ 修復完成，apt 可正常使用。

下一步：
  sudo bash scripts/setup_services.sh

全部裝完後，記得還原自動更新：
  sudo rm /etc/apt/apt.conf.d/99-temp-disable-unattended
============================================
MSG
else
  echo "⚠️  仍有項目未回復正常，請將上方輸出回報。" >&2
  exit 5
fi
