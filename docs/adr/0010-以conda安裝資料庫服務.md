# 10. 以 conda 安裝 PostgreSQL、pgvector 與 Redis

## Status

Accepted（取代 [ADR-0008](0008-改用原生安裝與systemd取代Docker.md) 的套件安裝方式）

## Context

ADR-0008 決定以 apt 從 PGDG repository 安裝 PostgreSQL 與 Redis，並以 systemd 管理。實作時該路徑連續遭遇三重阻礙，且最終發現根因與本專案完全無關：

1. `pidof` 在本機永久卡住 → 安裝腳本的 systemd 偵測無限期停滯
2. 系統既有的 GitHub CLI apt repository 網址錯誤（404）→ `apt-get update` 非零結束
3. dpkg 鎖被 `unattended-upgrades` 持有 **11 天**

追查後確認根因：一個使用者自行啟動的 `llama-server` 程序在 **19.8 天前**卡死於 D 狀態（`wchan: __vma_start_write`，核心 VMA 記憶體鎖）。其 `/proc` 條目自此無法讀取，導致**任何掃描 `/proc` 的工具**（`ps`、`pgrep`、`pidof`、`lsof`、`fuser`）全部永久阻塞，累積 1529 個 D 狀態殭屍程序。libssl3 的 postinst 第 219 行呼叫 `pidof /usr/lib/xorg/Xorg`，因而卡死並拖住整個 dpkg 交易。

D 狀態程序無法以任何訊號終止，只有重啟 WSL 核心可清除。

**關鍵觀察：apt 路徑要求系統套件管理器處於健康狀態，而本機的系統套件管理器已損壞且短期內無法修復。** 專案不應被一個與自身無關的系統故障阻塞。

同時發現既有條件更適合另一條路徑：

- 專案已使用 conda（`leo3.10`），且該環境**已含 PostgreSQL 16.12 的 client 端**（`pg_config`）
- conda-forge 提供 `postgresql` 18.6、`pgvector` 0.8.6、`redis-server`
- conda 安裝於使用者目錄，**不需 sudo、不碰 apt / dpkg**

### 選項比較

| 選項 | 優點 | 缺點 | 風險 |
|------|------|------|------|
| apt + PGDG（ADR-0008 原案） | 系統標準路徑；與 systemd 整合自然 | 需 sudo；**當前被損壞的 dpkg 狀態完全阻塞** | 專案進度綁在一個無關的系統故障上 |
| **conda（獨立環境 `newstrack-db`）** | 免 sudo、免 apt；與既有 conda 工作流一致；版本由環境檔鎖定 | 非系統服務，需自行處理啟停與開機自啟 | conda-forge 的 PostgreSQL 更新節奏落後於 PGDG |
| conda（裝進 `leo3.10`） | 環境數量少 | 為裝 PG server 而讓 conda 重解 torch、playwright 等相依 | 可能破壞已能運作的應用環境 |
| SQLite | Django 預設，零安裝 | **無 pgvector**；單一寫入者鎖 | 違反 ADR-0004／0005 的向量檢索需求；與 ADR-0007 的雙佇列並行寫入直接衝突 |

**關於 SQLite 的補充：** SQLite 實際上支援 CHECK constraint（ADR-0003 的引用強制）、generated column、以及可用於 bigram 的 FTS5，這三項都不構成阻礙。真正的阻礙只有兩個——沒有向量型別與 HNSW 索引，以及單一寫入者鎖無法承受雙 Celery 佇列的並行 upsert。

## Decision

1. **以 conda 於獨立環境 `newstrack-db` 安裝** `postgresql`、`pgvector`、`redis-server`（皆自 conda-forge）。應用環境 `leo3.10` 維持不變。
2. **資料目錄置於 `$HOME/newstrack/pgdata`**，位於 WSL2 原生 ext4。安裝腳本明確拒絕 `/mnt/*` 路徑——ADR-0006 第 6 點的要求由腳本強制，不靠人記得。
3. **驗證必須實際建立 `halfvec` 欄位與 HNSW 索引並執行最近鄰查詢**，而非僅檢查 extension 版本。ADR-0004 依賴的是這兩項具體能力，不是「pgvector 有裝」。
4. **開發期以 `pg_ctl` / `redis-server --daemonize` 啟停**，包裝於 `scripts/dev_services.sh`。
5. **開機自啟延後至 Scope 8 任務 51**，屆時再決定以 systemd unit 或其他機制承載（ADR-0008 的 systemd 決定本身不受本 ADR 影響）。

## Consequences

**變得更容易：**

- 專案立即解除阻塞，不需等待系統的 dpkg 故障修復，也不需為此重啟 WSL
- 零 sudo 需求：整個資料庫環境由使用者權限建立與管理
- 版本可重現：`newstrack-db` 環境可匯出為 environment 檔，比 apt 的版本狀態更容易複製
- 取得較新版本：PostgreSQL 18.6 與 pgvector 0.8.6，皆新於 PGDG 對 Ubuntu 22.04 的供應

**變得更困難：**

- **不是系統服務。** 沒有 `systemctl`，啟停需透過 `pg_ctl`，且需先 `conda activate newstrack-db`。開發期以腳本包裝，正式上線的自啟仍待 Scope 8 處理
- 多一個 conda 環境要維護
- conda-forge 的 PostgreSQL 安全更新節奏落後於 PGDG。對僅監聽 localhost 的開發資料庫可接受；若日後對外暴露資料庫需重新評估（但架構上不會有此需求——僅 Cloudflare Tunnel 對外，見 ADR-0006）

**未解決但已與專案脫鉤的問題：**

系統的 dpkg 損壞（libssl3 半設定）、1529 個 D 狀態殭屍、以及卡死的 `llama-server` 佔用 23GB VRAM，**都仍然存在**。本 ADR 只讓專案不再被它們阻塞。

其中 **VRAM 佔用仍會在 Scope 2 造成阻礙**：BGE-M3 需約 2.5GB，而目前僅剩約 1.5GB 可用。Scope 1（採集入庫）不需要 GPU，可先行進行；在進入 Scope 2 的 embedding 任務前，必須重啟 WSL 以釋放 VRAM 並清除殭屍程序。
