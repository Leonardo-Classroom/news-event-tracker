# 新聞事件持續追蹤系統

以**事件**為單位追蹤台灣重大司法案件與政商弊案。新聞停止報導後，系統繼續比對司法院裁判書、立法院議案、監察院糾正案與決標公告，把散落的官方紀錄接回同一條時間線。

**一句話定位：新聞停了，追蹤沒停。**

## 為什麼

台灣重大案件在公共視野中的生命週期，與它在司法程序中的實際生命週期嚴重脫節。新聞的報導密度呈雙峰分布——事件爆發時與判決宣判時——中間常隔三到八年幾乎零報導。但案件本身持續推進，這些進展都有官方紀錄，只是沒人把它轉譯回大眾語言。

## 文件

| 文件 | 內容 |
|------|------|
| [系統規劃書.md](系統規劃書.md) | 統整版，可獨立閱讀 |
| [docs/01-規格書.md](docs/01-規格書.md) | 目標、非目標、設計、替代方案、風險、驗收 |
| [docs/02-任務拆解.md](docs/02-任務拆解.md) | 任務清單，含相依順序與風險標記 |
| [docs/03-測試策略.md](docs/03-測試策略.md) | 測試分級、三道接縫、評估與測試的區分 |
| [docs/adr/](docs/adr/) | 架構決策記錄（Nygard 格式） |

ADR 是不可變的決策日誌：改變決策時寫新的一份，並將舊的標記為被取代，不修改既有內容。

## 環境

```bash
conda activate leo3.10
pip install -r requirements.txt

# PostgreSQL 18 + pgvector + Redis（裝在獨立的 conda 環境，免 sudo）
bash scripts/setup_db_conda.sh

python manage.py migrate
python manage.py seed_sources
```

## 執行

```bash
./run.sh                    # 啟動全部（PostgreSQL、Redis、Celery、Django）
./run.sh status             # 服務狀態、資料概況、LLM 餘額
./run.sh web                # 前景執行 Django，可直接看 traceback

./stop.sh                   # 停止 Django 與 Celery
./stop.sh --db              # 一併停止 PostgreSQL 與 Redis
./stop.sh --jobs            # 一併停止長時間背景作業
./stop.sh --all             # 全部停止

scripts/run_job.sh embed embed_documents    # 啟動可追蹤的背景作業
```

後台： http://localhost:5861/admin/

`stop.sh` 預設不停 PostgreSQL、Redis 與背景作業——前者持有全部語料與向量，
後者可能已跑數小時，誤停會浪費那些時間。腳本會偵測背景作業並顯示已執行
時間，由人決定是否中止。

腳本不使用 `pgrep` / `pkill`：本機曾有程序卡在 D 狀態，
使任何掃描 `/proc` 的工具永久阻塞（見 ADR-0010），改以 PID 檔管理。

資料庫資料目錄置於 `$HOME/newstrack/pgdata`（WSL2 原生 ext4）。**不可放在 `/mnt/*`**——那裡經 9P 協定存取，PostgreSQL 的 I/O 效能會是災難。安裝腳本會主動拒絕。

## 測試

```bash
pytest                    # small：無 I/O，秒級，每次修改都跑
pytest -m medium          # 需真實 PostgreSQL
pytest -m large           # 需瀏覽器或外部網路，不進 CI
```

分級依據 [Google 的 test size 分類](https://testing.googleblog.com/2010/12/test-sizes.html)——依「允許怎麼跑」而非「概念上測什麼」。

## 架構要點

```
L0 採集   RSS/sitemap 輪詢 + Playwright（無 feed 或有 Cloudflare 的站台）+ 官方 API
L1 抽取   LLM 結構化抽取（走 DeepSeek API，不自建推論）
L2 召回   識別碼精確比對 + PostgreSQL FTS（bigram）+ pgvector
L3 事件   先歸屬既有事件，未歸屬者才叢集；per-event 知識子圖
L4 敘事   時間線、因果鏈、進度變更通知
L5 應用   Django SSR + 審核後台
```

三個決定性的設計選擇：

1. **不建全庫知識圖譜**，只有被認定為事件的內容才建子圖
2. **事件是資料庫的一等公民**，不是查詢時臨時湊出來的
3. **沉寂事件的官方源檢查頻率不降低**——新聞沉寂期正是判決出爐的時期

## 法律約束

這幾項是硬約束，不是最佳實務建議：

- **新聞全文永不對外呈現**。記者撰寫的報導是受保護的語文著作；公開頁僅放自製摘要與原文連結
- **裁判書等公文可全文呈現**。依著作權法第 9 條不得為著作權之標的
- **未判決確定者一律用「被起訴」「被控」**，措辭檢查器為不可繞過的閘門
- **因果關係分「報導明述」與「系統推論」**，後者預設不公開，且 `stated` 邊的引用以資料庫 CHECK constraint 強制

## 狀態

規劃完成，Scope 1（採集入庫）進行中。
