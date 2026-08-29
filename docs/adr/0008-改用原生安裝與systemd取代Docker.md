# 8. 改用原生安裝與 systemd，取代 Docker Compose

## Status

Accepted（取代 [ADR-0006](0006-自架WSL2主機以CloudflareTunnel對外.md) 中的容器編排部分）

## Context

ADR-0006 決定「全部服務 Docker Compose 化」。實際動工時，專案擁有者明確表示不採用 Docker。重新檢視後，**該決定在本專案的條件下確實是淨損失，而且它從未經過與原生安裝的正面比較**——Docker 是被順帶引入的，不是被選擇的。ADR-0006 的選項比較表只比較了三種對外連線方式，容器化這一項沒有列出任何替代方案。

重新評估的具體事實：

1. **GPU 是本專案的核心資源，而 Docker 讓它變複雜。** BGE-M3（ADR-0004）與未來的本地 vLLM（ADR-0002）都需要 GPU。在 WSL2 上讓容器存取 CUDA 需要 nvidia-container-toolkit，多一層設定與除錯面；原生執行則是既有 conda 環境直接可用。
2. **既有工作流程已是 conda。** 環境 `leo3.10` 已安裝 torch 2.9.1、playwright 1.55、Django 5.2.13 並可運作。容器化等於把已經可用的環境再包一層。
3. **單機、單一維運者。** 容器的主要價值——環境可攜性、多機一致性、依賴隔離——在此都換不到對等回報。
4. **原生安裝順帶滿足 ADR-0006 的硬性要求。** Debian/Ubuntu 套件的 PostgreSQL 資料目錄預設在 `/var/lib/postgresql`，位於 WSL2 原生 ext4，自動符合「資料目錄不得置於 `/mnt/d`」這項不可妥協的前提。

**仍然成立的需求：** 移除 Docker 不會移除它原本要解決的問題——行程監管、崩潰自動重啟、開機自啟、服務依賴排序。這些必須另有答案。

**環境現況（實測）：** Ubuntu 22.04.4 LTS；PostgreSQL 與 Redis 皆未安裝；systemd 未啟用（`/etc/wsl.conf` 使用舊式 `[boot] command="service cron start"`）。

### 選項比較

| 選項 | 優點 | 缺點 | 風險 |
|------|------|------|------|
| **原生安裝 + systemd** | 標準的 Linux 服務管理；`Restart=always`、依賴排序、`journalctl` 集中查看日誌；GPU 直接可用 | 需啟用 systemd，代價是一次 `wsl --shutdown` 重啟 | 啟用 systemd 後 systemd-resolved 可能干擾既有的 `generateResolvConf = false` 自訂 DNS 設定 |
| 原生安裝 + sysvinit + supervisord | 不需重啟 WSL | 系統服務走 `service`、自家服務走 supervisord，兩套機制並存 | 除錯時需分兩邊查看，日誌分散；supervisord 本身也需要被啟動 |
| 開發期手動啟動（tmux / nohup） | 立即可開始 | 無崩潰重啟、無開機自啟 | P0–P2 期間爬蟲排程會因關閉視窗或重開機而靜默中斷，且不易察覺 |
| 維持 Docker Compose（ADR-0006 原案） | 環境可攜、單一編排檔 | GPU 需 nvidia-container-toolkit；與既有 conda 流程重複 | 對單機單人專案，複雜度換不到對等價值 |

## Decision

我們將**原生安裝所有服務，並以 systemd 管理**。

**啟用 systemd**

1. `/etc/wsl.conf` 設 `[boot] systemd=true`，並移除舊式 `command=`（cron 改由 `systemctl enable cron` 管理）。需執行一次 `wsl --shutdown` 生效。

**系統服務（apt 安裝）**

2. **PostgreSQL 16** 自 PGDG apt repository 安裝，搭配 `postgresql-16-pgvector` 套件取得 pgvector。資料目錄維持套件預設的 `/var/lib/postgresql`（位於 ext4）。
3. **Redis** 自 apt 安裝。
4. 兩者皆以 `systemctl enable` 設為開機自啟。

**自家服務（systemd unit）**

5. 以下各寫一個 systemd unit，全部設 `Restart=always`，並以 `After=postgresql.service redis-server.service` 宣告依賴：
   - `newstrack-web`（gunicorn）
   - `newstrack-worker-fetch`（Celery，高併發佇列）
   - `newstrack-worker-browser`（Celery，Playwright 佇列，含 `--max-tasks-per-child`）
   - `newstrack-beat`（Celery Beat）
   - `newstrack-embedding`（BGE-M3 常駐服務）
   - `cloudflared`（對外隧道）
6. 各 unit 以 conda 環境 `leo3.10` 的 Python 絕對路徑執行，不依賴 shell 的 conda 初始化。

**自 ADR-0006 保留、不受本決策影響的部分**

7. Cloudflare Tunnel 對外，不開放入向 port
8. Cloudflare CDN 邊緣快取事件頁，內容更新時主動 purge
9. PostgreSQL 資料目錄位於 WSL2 原生 ext4（現由套件預設自動滿足）
10. `pg_dump` 定期備份至 `/mnt/d` 並保留異地副本
11. 外部 uptime 監測

## Consequences

**變得更容易：**

- GPU 存取：embedding 服務與未來的本地 vLLM 直接使用既有 conda 環境，無需 nvidia-container-toolkit
- 開發迭代：改程式後 `systemctl restart` 即可，不需重建 image
- 日誌：`journalctl -u newstrack-*` 集中查看所有服務，比 `docker compose logs` 更貼近系統本身
- 資源：省去容器層的記憶體與檔案系統開銷；對同時要跑 Playwright 與 GPU 推論的單機有實質幫助
- ext4 要求：由套件預設自動滿足，不再需要刻意設定 volume 路徑

**變得更困難：**

- **可重現性下降。** 沒有 `docker-compose.yml` 作為環境的單一真實來源。緩解：以一份 `docs/04-部署手冊.md` 記錄完整安裝步驟與版本，並確保 `requirements.txt` 鎖定版本
- **遷移成本上升。** 若日後 ADR-0006 提到的「遷移至 VPS」成為必要，沒有現成的容器映像可搬。這是明確接受的代價——該遷移本來就需要重新設計資料同步層
- **版本升級需手動處理。** PostgreSQL 大版本升級要走 `pg_upgrade`，不能靠換 image tag
- 需要一次 `wsl --shutdown`，會中斷當下 WSL 內所有作業

**已知風險：**

啟用 systemd 後，`systemd-resolved` 可能與既有的 `[network] generateResolvConf = false` 自訂 DNS 設定衝突。若重啟後出現 DNS 解析失敗，此為第一嫌疑，處置方式是停用 `systemd-resolved` 或調整 `/etc/resolv.conf` 的產生方式。

**未被取代的部分：**

ADR-0006 關於對外連線（Cloudflare Tunnel）、儲存位置、備份與監測的決策全部維持有效。本 ADR 只取代其中的容器編排（第 3 點）與開機自啟機制（第 5 點）。
