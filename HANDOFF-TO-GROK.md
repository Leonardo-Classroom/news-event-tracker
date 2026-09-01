# 交接文件：Claude → Grok

寫給接手的 Grok。我（Claude）查過了：你的 `~/.grok/bundled/skills/`
裡沒有 `pm`／`spec-writer`／`architecture-advisor`／`task-breakdown`／
`test-strategy` 這幾個技能的正式登記項目，所以你**不能用 Grok 自己的
技能呼叫語法**（例如類似 `/pm` 的指令）去啟動它們。

但這幾個技能本身只是純文字的 markdown 指示文件，不是需要特殊執行環境
的程式，你有檔案讀取能力，所以可以**直接讀取原始檔案內容**、把它當
操作手冊照著做，效果跟「正式呼叫技能」是一樣的，只是要自己動手讀而
不是靠指令自動載入。這幾個規劃流程本身這個專案在最初期就已經跑過
一輪（見下方 git 歷史與各文件），所以你目前**不需要重跑**這些技能來
規劃新東西，除非要規劃全新的、還沒被 `docs/02-任務拆解.md` 涵蓋的
大範圍功能。真的需要時，檔案在這裡，直接讀：

- `/home/leonardo890229/.claude/skills/pm/SKILL.md`——四個技能怎麼
  串起來用（spec-writer → architecture-advisor → task-breakdown →
  test-strategy 的順序與交接方式）
- `/home/leonardo890229/.claude/skills/spec-writer/SKILL.md`——怎麼寫
  規格（one-pager vs 完整規格的判斷、Goals/Non-Goals/Alternatives
  Considered 等章節）
- `/home/leonardo890229/.claude/skills/architecture-advisor/SKILL.md`
  ——怎麼做架構決策比較與寫 ADR（Nygard 格式：Status/Context/
  Decision/Consequences，且**不可回頭改舊 ADR，只能寫新的取代**）
- `/home/leonardo890229/.claude/skills/task-breakdown/SKILL.md`——怎麼
  把規格拆成可執行任務（按 scope 分組、垂直切分而非按技術層切分）
- `/home/leonardo890229/.claude/skills/test-strategy/SKILL.md`——測試
  分級（small/medium/large）與抓「這真的需要真實資料庫/網路嗎」的
  判斷方式

這些檔案在 WSL 檔案系統裡，跟你執行的環境是同一台機器，路徑可以直接
讀取，不需要複製到 `~/.grok/` 底下才能用。

## 第一步：照這個順序讀文件

1. **這份文件全文**（你正在讀）——當下狀態、規則、地雷，這裡都有
2. `README.md`——怎麼啟動、怎麼測試、架構速覽
3. `docs/02-任務拆解.md`——**最重要的操作文件**。每個任務標
   `[x]`（完成）、`[~]`（部分完成，內文說明剩什麼）、`[ ]`（未做）。
   本文件下面列的「現在的狀態」是這份文件的摘要，但那份才是權威版本，
   若有出入以它為準。
4. `docs/01-規格書.md`——目標（G1-G7）、非目標、風險（R1-R11）、
   驗收指標（M1-M9）。**M7、M9 是 release blocker，不可退讓。**
5. `docs/adr/0001` 到 `0012`——架構決策記錄。**這是不可變的日誌，
   不是維基頁面**：要改變某個決策時寫一份新的 ADR、把舊的標記為
   「Superseded by」，絕對不要回頭改舊 ADR 的內容。每份都值得讀，
   但若時間有限，至少讀 0001（中文全文檢索）、0003（知識子圖）、
   0004（embedding）、0005（事件偵測）、0007（Celery）、0009（LLM）、
   0010（服務安裝）、0012（去重）。
6. `docs/03-測試策略.md`——測試分級（small/medium/large）、三道
   測試接縫（Clock/Fetcher/LLMProvider）。
7. `fixtures/LABELING_GUIDE.md`——包含底下的「修正紀錄」，記錄了
   標註集踩過的陷阱，末尾還有一筆真實案例的除錯故事。
8. `系統規劃書.md`——統整版，可獨立閱讀，但內容已被上面幾份文件
   取代大半，優先度最低。

## 使用者是誰、怎麼溝通

- 用繁體中文溝通，程式碼注釋也是。
- 使用者是這個系統的唯一開發者兼維運者，非常在意「先量測、後下結論」
  ——這個 session 裡好幾次先憑直覺寫的規則或參數被真實資料打臉，
  然後才用量測結果修正。**不要對任何門檻、關鍵字表、參數猜測作答，
  找得到真實語料就先量再說**（本專案的語料庫就在同一台機器的資料庫
  裡，見下方環境資訊）。
- 使用者會直接指出你的錯誤（例如問「怎麼不用 API」「怎麼還是顯示
  '—'」），這種時候不要辯解，先去查證他說的是不是真的問題，如果是
  就承認並修正，不要說「這是設計如此」來搪塞。
- 使用者偏好簡短、有實據的回覆，不要长篇大論。給結論、給數字、
  給檔案路徑，不要重複他已經知道的背景。
- **每次修改都要跑過完整測試套件、通過後才 commit，commit 完要
  `git push`**——這個 session 裡每一個完成的功能都是這樣做的，
  沒有例外。commit message 要說明「為什麼」而非「做了什麼」，
  尤其是量測出來的發現（見下面「已經踩過的坑」，commit history
  裡的訊息本身就是最好的範例，用 `git log` 看過去的訊息抓風格）。

## 環境

- 作業系統：WSL2（Windows 11 主機，有 WSLg，`DISPLAY=:0` 可跳出
  真實瀏覽器視窗）。
- conda 環境：`leo3.10`（應用程式）、`newstrack-db`（PostgreSQL 18 +
  pgvector、Redis，免 sudo 安裝，見 ADR-0010）。
- 啟動：`./run.sh`（啟動全部）、`./run.sh status`、`./run.sh web`
  （前景執行看 log）。停止：`./stop.sh`（預設不停資料庫與背景作業，
  `--all` 全停）。**不要用 `pkill`／`pgrep`／`ps` 找程序**——這台機器
  有個程序卡在 D state，任何掃描 `/proc` 的工具會永久掛住。所有腳本
  都用 PID 檔，你也照做。
- 長時間背景作業：`scripts/run_job.sh <名稱> <manage.py 指令>`，
  `./stop.sh --jobs` 可停。
- 測試：`pytest`（small，秒級）、`pytest -m medium`（需要真資料庫）、
  `pytest -m large`（不進 CI）。**目前 685 個測試全過**，這是你接手
  時的基準線，任何改動後都要維持全過。
- 埠號：5861（`http://localhost:5861/`）。內部工具在根目錄，公開網站
  在 `/case/`，Django admin 在 `/admin/`。
- 帳號：`leo`（superadmin，密碼使用者自己知道，存在資料庫裡不在任何
  檔案）。`.env`（gitignored）裡有 DeepSeek API key 與司法院開放資料
  平台的帳密。

## LLM 預算——這是使用者明確訂下的硬規則

**每次累計花費超過 US$5 就要停下來問使用者要不要繼續**，這不是我的
建議，是使用者在這個 session 早期明確說的規則
（`llm_budget --approve` 可追加額度，但只有使用者可以決定要不要加）。

目前狀態：**已花費 US$1.3446，剩餘 US$3.6554**（額度 US$5）。
`apps/llm/budget.py` 的 `check_budget()` 會在超額時拋
`BudgetExceeded`，這個機制已經內建在 `LLMProvider` 裡，你不太可能
不小心繞過去，但規劃工作時仍要把這個上限放在心上——尤其任務 14
（L1 全量抽取）估計要 US$48–96，遠超目前額度，不要嘗試全量執行。

## 現在的狀態（2026-08-31，摘自 docs/02-任務拆解.md）

**Scope 0-6 全部完成。Scope 7（公開網站）做了 v1。**

語料庫：239,907 篇文件、36,400 篇相關、36,398 篇已向量化、
6 個真實追蹤中事件、567 筆 L1 抽取（皆為已歸屬事件的文件，
全庫未跑，見上方預算說明）。

已完成的重點（不逐條列，詳見任務拆解文件）：
- 冪等入庫、事件偵測（HDBSCAN）、自動歸屬（M4 實測誤歸屬率 0%）
- Hybrid 召回（M4 之外，召回率 91.3%，過程detailed在任務19條目下）
- 時間線與因果子圖的資料層（`apps/timeline/`），DB CHECK constraint
  強制 stated 邊要有引用、inferred 邊要有 confidence
- 司法院月封存檔已接進 pipeline（`apps/ingest/judicial_opendata.py`），
  用人工登入後重放的 session cookie 下載（見下方「已經踩過的坑」）
- 安全閘門全部完成：措辭檢查器（100% 攔截率）、風險分級、公開白名單、
  inferred 因果邊預設不公開
- 內部工具三級角色權限（superadmin 唯一／admin／user），獨立登入頁
  `/login/`
- 公開網站 v1（`/case/`）：完全不需登入、SEO（sitemap、schema.org、
  Open Graph）、分頁系統

**還沒做的**（按優先順序，完整清單見任務拆解文件）：

| 任務 | 內容 | 備註 |
|---|---|---|
| 45 | 審核後台 | **建議下一步**——目前發布事件只能用 Django admin 或 shell 手動呼叫 `event.publish()` |
| 30 | 因果生成 LLM 服務 | 護欄都有，生成邏輯沒寫，卡在任務 14 全量抽取沒跑（沒資料可練） |
| 44 | SEO 收尾 | 事件合併的 301 導向、`EntityAlias` 模型 |
| 46 | 更正下架申訴管道 | release blocker 相關，24hr SLA |
| 47-49 | CDN、帳號訂閱、公開 API | 未開始 |
| 50-53 | 真正對外部署（Cloudflare Tunnel、開機自啟、備份、監測） | **需要使用者決定要不要真的開放外部連線，不要自己決定** |
| 54-57 | 立法院、採購網、監察院 adapter | 未開始 |
| 58-60 | 訂閱通知、回顧卡、喚醒推播 | 未開始，任務60是「讓大家想起來」的最終實現 |

## 目前卡著等使用者回覆的兩個決定

1. **要不要現在發布一個真實事件到公開頁**——問過使用者，還沒回覆。
   公開頁目前是空的（`http://localhost:5861/case/` 顯示「目前沒有
   公開追蹤中的案件」）。
2. **要不要真的對外部署**（任務 50 的 Cloudflare Tunnel）——這會讓
   外部網路連得到這台主機，屬於「一旦做了很難假裝沒做過」的動作，
   不要自己決定要不要做。

## 已經踩過的坑（不要重踩）

這些是這個 session 裡實測發現、花了工夫才修正的問題，寫下來是為了
不要重複同樣的除錯過程：

- **`ps`／`pgrep`／`pidof`／`pkill` 會永久掛住**：這台機器有個程序
  卡在 D state，任何掃描 `/proc` 的工具都會被卡住。全部腳本改用
  PID 檔管理程序。
- **PostgreSQL 對 `ORDER BY x DESC` 預設把 NULL 排最前面**：文件列表
  排序若不加 `nulls_last=True`，少數缺日期的文件會佔滿列表最前面，
  看起來像「大部分文件都沒日期」。
- **中文 bigram 全文檢索：查詢字串一定要跟索引用同樣的方式切分**，
  否則三字以上的查詢詞會完全查不到（`apps/retrieval/keyword.py` 的
  `build_tsquery`）。
- **SimHash 對這個語料無效**（高頻無鑑別力的 bigram 主導指紋），已
  換成 bigram 集合的 Jaccard 相似度（ADR-0012），門檻 0.70。
- **相關性過濾的中文子字串比對會被更長詞誤觸發**：「法院」命中的
  47.9% 其實來自「立法院」。有歧義的詞要用正則的負向零寬斷言排除。
- **事件偵測（HDBSCAN）的 `allow_single_cluster` 參數**：直覺應該設
  True（讓孤立的單一事件不被判成全體 noise），但拿真實標註集回測後
  發現在多事件混合資料上表現差得多（全部塌縮成一坨，ARI 約
  -0.05），改回 sklearn 預設的 False 反而在 min_cluster_size=5 時
  正確切出乾淨的簇（ARI 0.43）。**真實資料回測結果永遠比合成測試或
  直覺可信**，任務 19、24 都有這種「先直覺後反轉」的案例，讀一下
  能省很多繞路時間。
- **混合召回的向量通道**：查詢構成方式（完整查詢 vs. 只用身分詞）
  的最佳選擇因事件而異（被告人數多的事件適合拆開、少的適合合併），
  解法不是二選一，是兩種都做再用 RRF 融合。
- **司法院資料開放平台的月封存檔需要登入，登入頁掛 Cloudflare
  Turnstile**：自動化瀏覽器（不論 headless 或 headed，Playwright
  控制的都一樣）**一律被拒發驗證 token**，這不是參數能調的問題，是
  Turnstile 刻意要擋自動化。解法是人工登入一次、把 session cookie
  存進 `ExternalSession` 模型重放（見 `/crawlers/` 頁的登入卡片）。
  **不要嘗試用瀏覽器指紋偽裝或反偵測套件去繞過**，那已經超出「模擬
  瀏覽器」的範疇。
- **RAR 解壓**：p7zip 內建的 RAR 解碼器解不開司法院封存檔的壓縮
  方法（會生出全 0 位元組的空檔，看起來像成功但內容是空的）。
  已用 `conda install -c conda-forge unrar` 裝真正的 unrar 二進位檔。
- **Django admin 的登入表單（`/admin/login/`）只接受 `is_staff=True`
  的帳號**：一般 user 角色的帳號送出正確帳密也會被拒絕，錯誤訊息是
  「請輸入正確的工作人員帳號」。這代表三級角色系統若共用
  `/admin/login/` 當登入頁，`user` 角色永遠登不進去。已改用獨立的
  `django.contrib.auth.views.LoginView`（`/login/`）。
- **案件類型排除的關鍵字要用真實法律用語**：性侵害案件的案由寫的是
  刑法罪名「妨害性自主」，不是「性侵害」三個字，原本的詞表用直覺
  寫「性侵害」會漏掉幾乎所有真實案由。
- **在 `<script>` 標籤裡塞 JSON-LD 前要手動跳脫 `<`**：`json.dumps`
  不會跳脫這個字元，若內容剛好含 `</script>` 字樣會被瀏覽器解析成
  標籤提前結束。Django 內建的 `json_script` 過濾器做同樣的跳脫，但
  它把 `type` 屬性寫死成 `application/json`，搜尋引擎要的是
  `application/ld+json`，所以自己動手做同樣的跳脫。
- **Python 3.10 的 `datetime.fromisoformat` 無法解析秒數小數只有
  1-2 位的 ISO 字串**（如 `.95`），3.11 起才放寬。司法院開放資料平台
  的 API 真的會回傳這種格式，對真實 API 首次執行時會直接炸掉。
- **PostgreSQL 大量 UPDATE 會造成表膨脹**：對 23.8 萬列做過幾次全表
  UPDATE 後，表從 3.4GB 膨脹到 8.2GB，還造成鎖車陣卡住其他寫入。
  只更新真的有變動的列，批次不要超過 500-2000 列。

## 程式碼風格（沿用這個 session 建立的慣例）

- 預設不寫注釋，寫的話只寫「為什麼」，不寫「做了什麼」——變數與
  函式命名清楚就不需要後者。
- 每個非顯而易見的設計決策，注釋裡要帶著實測數字或反例，不要空泛
  地說「這樣比較好」。整個程式碼庫的注釋風格都是這樣，隨便開一個
  檔案（例如 `apps/retrieval/hybrid.py`、`apps/ingest/judicial_opendata.py`）
  照著抄語氣即可。
- 不要不必要地引入新套件、新抽象層。三行類似的程式碼不需要抽成
  函式；沒有第二個呼叫端的東西不需要做成可設定的參數。
- 新功能一律連測試一起交，並且優先寫 small 測試（不碰資料庫），
  只有真的需要真實 PostgreSQL 特性（pgvector、GIN 索引、CHECK
  constraint、GeneratedField）才寫 medium 測試。
- 每個 ADR 都是不可變日誌，不要編輯舊的，要否定舊決策時寫新的一份
  並把舊的狀態改成 `Superseded by [新 ADR]`。

## 最後提醒

任務拆解文件（`docs/02-任務拆解.md`）本身在這個 session 裡持續被
更新——每完成一個任務就即時勾選並補上實測發現，**你接手後也要維持
這個習慣**，這是使用者能一眼看出專案真實進度的唯一依據，不要讓它
跟程式碼脫節。
