# 6. 自架 WSL2 主機以 Cloudflare Tunnel 對外，資料目錄置於原生檔案系統

## Status

部分被 [ADR-0008](0008-改用原生安裝與systemd取代Docker.md) 取代

容器編排（Decision 第 3 點）與開機自啟機制（第 5 點）已由 ADR-0008 改為原生安裝 + systemd。
其餘部分——Cloudflare Tunnel、CDN 快取、資料目錄置於 ext4、備份、外部監測——維持 Accepted。

## Context

系統將部署於自有主機（Windows + WSL2、RTX 4090 24GB、32 核、62GB RAM），對外提供公開網站。此決策已定（規格 Q3），本 ADR 處理的是「在此前提下如何取得可接受的可用性」。

自架 WSL2 對外服務有幾項具體障礙，都必須明確處理：

1. **無固定 IP。** 家用寬頻多為動態 IP，且常位於 CGNAT 之後，無法直接 port forwarding
2. **WSL2 不隨 Windows 開機自動啟動。** 主機重開後服務不會恢復
3. **WSL2 預設不啟用 systemd**，需在 `/etc/wsl.conf` 明確開啟
4. **跨檔案系統 I/O 效能懸崖。** WSL2 存取 `/mnt/d`（Windows NTFS）經由 9P 協定，效能比原生 ext4 差一到兩個數量級。PostgreSQL 資料目錄若置於 `/mnt/d`，效能會是災難
5. **停電與網路中斷**無備援

此外，SEO 是主要觸達管道（規格 §4.7），因此**站台的可用性與回應速度直接影響搜尋排名**——可用性不只是使用者體驗問題。

內容更新頻率為日級（規格 Non-Goals 明確排除秒級即時性），這使得積極快取成為可行且高效的策略。

### 選項比較

| 選項 | 優點 | 缺點 | 風險 |
|------|------|------|------|
| Port forwarding + DDNS | 不依賴第三方 | 需固定 IP 或動態 DNS；CGNAT 下不可行；直接暴露家用 IP | DDoS 或掃描直擊家用網路；憑證需自行管理 |
| **Cloudflare Tunnel** | 免固定 IP、免開 port（出向連線）；自帶 TLS、CDN 與 DDoS 防護；免費層足夠 | 依賴 Cloudflare 可用性；流量經第三方 | Cloudflare 服務中斷時站台不可達（但其可用性遠高於自架） |
| 反向代理至 VPS | 對外穩定 | 需租 VPS；仍需 VPS ↔ 家用主機的隧道 | 成本與複雜度都高於 Cloudflare Tunnel，收益不明 |

## Decision

我們將採用以下部署架構：

**對外連線**

1. **Cloudflare Tunnel（`cloudflared`）** 對外，不開放任何入向 port。TLS 由 Cloudflare 終結
2. **Cloudflare CDN 邊緣快取事件頁**。因更新為日級，事件頁設較長 TTL；事件內容更新時由應用程式主動呼叫 API purge 該頁

**服務編排**

3. **全部服務 Docker Compose 化**：`postgres`、`redis`、`django`(gunicorn)、`celery-worker-fetch`、`celery-worker-browser`、`celery-beat`、`embedding`、`cloudflared`。全部設 `restart: unless-stopped`
4. **WSL2 啟用 systemd**（`/etc/wsl.conf` 設 `[boot] systemd=true`），Docker daemon 由 systemd 管理
5. **Windows 端設定工作排程器於開機時執行 `wsl -d <distro> -u root service docker start`**（或等效機制），確保主機重開後 WSL 與容器自動恢復

**儲存**

6. **PostgreSQL 資料目錄必須位於 WSL2 原生 ext4 檔案系統**（如 `~/pgdata` 或 Docker named volume），**絕對不可置於 `/mnt/d` 或任何 Windows 掛載路徑**
7. **備份**：`pg_dump` 定期輸出至 `/mnt/d`（Windows 側，順帶進入既有的 OneDrive 同步）並保留異地副本。備份寫入慢速路徑無妨，因其非線上路徑

**監測**

8. **外部 uptime 監測**（第三方服務）對事件頁做定期檢查，失敗時告警。自架環境的失效模式常是「整台機器沒回應」，內部監測無法察覺

## Consequences

**變得更容易：**

- 對外暴露：無需處理固定 IP、port forwarding、憑證更新，家用網路不直接暴露
- 效能與可用性：CDN 邊緣快取使多數請求不觸及自架主機。這同時緩解了主機短暫離線的影響——快取仍能服務讀者
- 硬體成本：零主機租金，且 4090 可同時服務 embedding 與（未來的）本地 LLM
- 資料主權：全部資料在自有硬體上

**變得更困難：**

- 可用性天花板明顯低於雲端。停電、Windows 更新重開、WSL 異常都會造成中斷。**必須接受這是一個盡力而為（best-effort）的服務等級**，不承諾 SLA
- Docker 化增加初期工作量，且 WSL2 上的 Docker 有其特有問題（記憶體回收、檔案系統效能）需要熟悉
- 快取 purge 邏輯成為正確性的一環：事件更新後未 purge 會導致讀者看到過期進度，這與系統「持續追蹤」的核心承諾直接衝突

**明確承擔的風險：**

主機長期離線（例如出遠門期間停電）會使站台不可達數日。緩解措施僅有 CDN 快取（讀者仍能看到快取版本）與外部告警。若日後可用性成為實質問題，遷移路徑是將 web + DB 移至 VPS、爬蟲與推論留在本機——但這需要設計資料同步層，複雜度顯著上升，故不在第一版處理。

**不可妥協的一項：**

第 6 點（資料目錄置於原生 ext4）不是最佳化建議，而是可行性前提。若違反，PostgreSQL 的 I/O 效能會使整個系統無法運作。
