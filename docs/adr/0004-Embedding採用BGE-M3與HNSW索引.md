# 4. Embedding 採用 BGE-M3，向量索引採用 HNSW

## Status

Accepted（執行裝置部分由 [ADR-0011](0011-Embedding以CPU執行並限用P-core執行緒數.md) 修訂）

模型選擇（BGE-M3）、維度、halfvec 與 HNSW 索引的決定維持有效。
「於 GPU 執行、佔用 2.5GB VRAM」改為於 CPU 執行，執行緒數限為 16。

## Context

L2 召回層需要向量檢索，用於事件歸屬與事件偵測。語料為繁體中文新聞與司法文書。硬體為 RTX 4090 24GB，該卡未來還需承載本地 LLM 推論（見 ADR-0002），因此 embedding 模型的 VRAM 佔用是實質約束。

**模型候選：**

| 模型 | 參數量 | 維度 | Context | 特點 |
|------|--------|------|---------|------|
| BGE-M3 | 560M | 1024 | 8192 | 同時輸出 dense、sparse（learned lexical weights）、ColBERT 三種表示 |
| Qwen3-Embedding-0.6B | 600M | 1024（支援 MRL 降維） | 32k | 輕量 |
| Qwen3-Embedding-8B | 8B | 4096（支援 MRL） | 32k | MTEB 中文榜首 |

Qwen3-Embedding-8B 品質最高，但 8B 模型佔約 16GB VRAM，與本地 LLM 直接衝突。

**BGE-M3 的 sparse 輸出與 ADR-0001 有耦合。** ADR-0001 決定不安裝中文斷詞 extension，改用 bigram FTS，並將「改用 BGE-M3 sparse 取代 bigram」列為第一升級路徑。BGE-M3 的 lexical weights 是模型學出的詞彙權重，對中文的關鍵字檢索品質優於機械式 bigram，且不需新增任何服務——因為模型本來就要載入。這使 BGE-M3 一個模型同時解決 dense 與 sparse 兩種檢索需求。

**索引策略：** pgvector 提供 IVFFlat 與 HNSW 兩種。本系統的資料特性是**持續新增、永不刪除**（新聞每日入庫）。IVFFlat 的分群在建索引時固定，新增資料若分布偏移則召回率逐漸衰退，需定期重建；HNSW 支援增量插入且召回率較高，代價是建索引較慢、記憶體佔用較大。主機有 62GB RAM，記憶體不是瓶頸。

### 選項比較

| 選項 | 優點 | 缺點 | 風險 |
|------|------|------|------|
| **BGE-M3** | dense + sparse 一個模型解決兩種檢索；VRAM 僅約 2.5GB，不排擠本地 LLM；8192 context 足以涵蓋新聞全文 | 純 dense 品質略遜於 Qwen3-Embedding-8B | 若 dense 召回品質不足以達成事件歸屬準確率，需升級模型並全量重算向量 |
| Qwen3-Embedding-8B | 中文檢索品質最佳 | 佔約 16GB VRAM，與 ADR-0002 的本地 LLM 直接衝突；無 sparse 輸出，仍需另解關鍵字檢索 | 迫使本地 LLM 計畫放棄，或需第二張卡 |
| Qwen3-Embedding-0.6B | 輕量；MRL 可降維省儲存 | 無 sparse 輸出 | 同上，關鍵字檢索仍需另尋方案 |
| 雲端 embedding API | 零 VRAM 佔用 | 每篇文件都要付費且資料外流；回填數十萬篇成本高 | 與 ADR-0002「本地化以支撐回填」的方向矛盾 |

## Decision

我們將採用 **BGE-M3** 作為 embedding 模型，向量索引採用 **HNSW**。

具體參數：

1. **Dense 向量 1024 維，以 `halfvec`（float16）儲存**，建 HNSW 索引。相較 float32 減半儲存與索引記憶體，對召回率影響可忽略。
2. **Sparse 向量以 pgvector 的 `sparsevec` 型別儲存**，作為 ADR-0001 中 `KeywordSearchBackend` 的候選實作。P1 先用 bigram FTS，量測後決定是否切換。
3. **Embedding 服務獨立為單一常駐程序**（非每個 Celery worker 各載一份模型），透過內部 HTTP 或佇列呼叫，避免多 worker 重複佔用 VRAM。
4. **模型版本寫入向量記錄。** 換模型時可辨識哪些向量需重算，支援漸進式遷移。

## Consequences

**變得更容易：**

- 元件數量：一個模型服務同時供應 dense 與 sparse，不需為關鍵字檢索另立方案
- VRAM 規劃：2.5GB 的佔用讓 4090 仍有約 20GB 可供本地 LLM 使用，ADR-0002 的遷移路徑保持開放
- 增量寫入：HNSW 支援直接插入，每日新增文件不需重建索引，也不需為索引維護安排維護窗口
- 資料不外流：全程地端運算

**變得更困難：**

- HNSW 建索引比 IVFFlat 慢得多。首次對歷史資料建索引時需預留數小時，且期間資料庫負載高
- HNSW 索引記憶體佔用較大。以 100 萬文件 × 1024 維 halfvec 估算，向量本身約 2GB，索引另需數 GB。目前 62GB RAM 充裕，但若語料成長到千萬級需重新評估
- 更換 embedding 模型的代價高：需全量重算向量並重建索引。因此模型版本記錄是必要投資，不是可選項

**待驗證的假設：**

BGE-M3 的 dense 品質足以支撐規格 M4 的「誤歸屬率 < 5%」。這需在 P1 以人工標註測試集量測。若不足，升級路徑為：先嘗試 dense + sparse 混合排序（不需換模型），再考慮換 Qwen3-Embedding 並接受 VRAM 排擠。
