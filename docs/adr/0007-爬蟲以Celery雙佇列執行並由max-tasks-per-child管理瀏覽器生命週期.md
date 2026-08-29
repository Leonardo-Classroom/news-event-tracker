# 7. 爬蟲以 Celery 雙佇列執行，瀏覽器生命週期由 max-tasks-per-child 管理

## Status

Accepted

## Context

既有爬蟲 `news report/crawler/crawl_robust.py`（599 行）已在實務中演化出一套穩定機制，其 docstring 記錄了設計理由：

> 每個子程序：對每位記者「啟動全新瀏覽器 → 收集連結 → 多分頁並行抓文章 → 關閉瀏覽器」。全新瀏覽器/記者 = 不累積劣化；程序隔離 = 一個卡住不影響其他。

亦即「每位記者重開瀏覽器」不是隨意選擇，而是**針對實際觀察到的瀏覽器長時間執行後劣化卡死問題**的解方。此外還有：每位記者 1 小時硬性逾時、每篇文章 90 秒逾時、主程序看門狗偵測總文章數停滯後重啟所有子程序、以 `done_urls.json` 與 `robust_completed.log` 支援續爬。

新系統改用 Celery 後，這些機制需要對應，而非丟棄。

同時，新系統的採集策略改變（規格 §4.9）：主力從 Playwright 改為 RSS/sitemap 輪詢，Playwright 僅用於無 feed 或有 Cloudflare 的站台。這兩類任務的資源特性差異極大：

- **RSS/HTTP 抓取**：每個任務數 MB 記憶體、數百毫秒，可高併發
- **Playwright 抓取**：每個瀏覽器實例數百 MB 記憶體、數秒至數分鐘，併發度必須低

### 選項比較

**瀏覽器生命週期管理**

| 選項 | 優點 | 缺點 | 風險 |
|------|------|------|------|
| 每個 task 開關瀏覽器 | 隔離最徹底；不可能累積劣化 | 每次 1–2 秒啟動成本，任務量大時開銷顯著 | 吞吐量受限，回填歷史資料時特別痛 |
| Worker 全程持久瀏覽器 | 啟動成本攤提至零 | **會重現既有系統已知的劣化卡死問題** | 已被實務驗證會失敗，不應重蹈 |
| **持久瀏覽器 + `worker_max_tasks_per_child=N`** | 啟動成本攤提於 N 個任務；Celery 每 N 個任務重生子程序，順帶重建瀏覽器 | 需正確處理程序初始化與關閉掛鉤 | N 值需依實測調校；設太大則劣化重現，太小則退化為方案一 |

**佇列配置**

| 選項 | 優點 | 缺點 | 風險 |
|------|------|------|------|
| 單一佇列 | 設定簡單 | 併發度只能取兩類任務的最小值 | Playwright 任務會拖垮輕量 HTTP 任務的吞吐；或反之記憶體爆掉 |
| **雙佇列（`fetch` / `browser`）** | 各自獨立設定併發度與資源上限 | 需明確路由任務 | 路由錯誤會導致任務進錯佇列（緩解：以任務裝飾器綁定佇列）|

## Decision

我們將以 **Celery 雙佇列**執行採集，並以 `worker_max_tasks_per_child` 管理瀏覽器生命週期。

**佇列配置**

| 佇列 | 用途 | Worker 設定 |
|------|------|------------|
| `fetch` | RSS/sitemap 輪詢、官方 API 呼叫、靜態頁抓取（httpx + feedparser） | 高併發（如 `--concurrency=16`），prefork |
| `browser` | Playwright 抓取（無 feed 或有 Cloudflare 的站台） | 低併發（如 `--concurrency=3`），prefork，`--max-tasks-per-child=N` |

**既有機制的對應**

| 既有機制 | Celery 對應 |
|---------|------------|
| 多程序子 worker | Celery prefork worker pool |
| 每位記者重開瀏覽器（防劣化） | `worker_max_tasks_per_child=N` 觸發子程序重生，瀏覽器隨之重建 |
| 每位記者 1 小時硬性逾時 | 任務層級 `time_limit` |
| 每篇文章 90 秒逾時 | 任務內部的 asyncio timeout（保留既有實作） |
| 主程序看門狗偵測停滯後重啟 | `time_limit`（硬殺）+ `acks_late=True`（重新入列）+ 有限次 `autoretry_for` |
| `done_urls.json` 續爬 | `Document.url` 的 UNIQUE constraint；重複抓取為冪等的 no-op |
| `robust_completed.log` | 資料庫的採集任務狀態表 |

**實作要點**

1. 瀏覽器於 `worker_process_init` 訊號建立、`worker_process_shutdown` 關閉，存於程序區域變數
2. 任務必須設 `acks_late=True` 並保證冪等——被硬殺的任務會重新入列並重跑
3. `Source` 表記錄每個來源的連續失敗次數；超過門檻自動降頻並告警（對應規格 R5 的來源健康度監測）
4. N 的初始值設為 20，依實測的記憶體成長與失敗率調整

## Consequences

**變得更容易：**

- 逾時、重試、排程、監測全部由 Celery 提供，`crawl_robust.py` 中約半數的自製基礎設施程式碼可以刪除
- 續爬邏輯從檔案狀態變為資料庫約束，天然支援多 worker 並行且無競爭條件
- 資源隔離：Playwright 的記憶體壓力不影響 RSS 輪詢的吞吐，反之亦然
- 新增來源只需實作 adapter 並指定佇列，不需碰執行框架

**變得更困難：**

- Playwright 與 Celery prefork 的組合有陷阱：sync API 在子程序中可用，但 asyncio event loop 的管理需謹慎，且 fork 後不可繼承已建立的瀏覽器連線。程序初始化掛鉤必須正確，否則會出現難以診斷的當機
- `acks_late` 要求所有採集任務嚴格冪等。這是實質約束，寫入邏輯必須全面採用 upsert 而非 insert
- N 值的調校需要實際運行資料，初期可能經歷幾輪調整

**保留的既有資產：**

`common.py` 的反偵測 context 設定與 Cloudflare 等待邏輯、`sites.py` 的各站解析函式，均為對特定站台行為的實證知識，直接移植。被取代的只有執行框架，不是抓取邏輯。
