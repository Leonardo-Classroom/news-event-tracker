"""Playwright 渲染層（ADR-0007 的 ``browser`` 佇列）。

**設計：渲染與解析分離。** 本模組只負責把頁面渲染成 HTML 字串——
處理 Cloudflare 挑戰、無限滾動、「載入更多」按鈕——之後交給
一般的 adapter 以純字串解析。好處是 adapter 仍可用 fixture 做
small 測試，不必在測試中啟動瀏覽器。

反偵測設定與 Cloudflare 等待邏輯移植自既有爬蟲 ``crawler/common.py``。
那是對特定站台實際行為的實證知識，不是可以憑直覺重寫的東西。

生命週期由 Celery 的 ``worker_max_tasks_per_child`` 管理：既有爬蟲
發現「瀏覽器長時間執行後會累積劣化並卡死」，其解法是每位記者重開
瀏覽器。對應到 Celery 就是讓子程序每 N 個任務重生一次，瀏覽器隨之
重建——等價的效果，但由框架管理而非自製看門狗。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from apps.ingest.fetchers import FetchError, FetchResult

logger = logging.getLogger(__name__)

# 移植自 crawler/common.py
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_STEALTH_SCRIPT = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
)


@dataclass
class RenderOptions:
    """各站台的渲染行為差異。放在設定而非程式碼分支，方便新增來源。"""

    #: 需等待 Cloudflare 的 "Just a moment" 挑戰通過
    cloudflare: bool = False
    #: 無限滾動的次數（0 表示不滾動）
    scrolls: int = 0
    #: 「載入更多」按鈕的點擊次數
    load_more_clicks: int = 0
    #: 「載入更多」按鈕的選擇器
    load_more_selector: str = "button:has-text('載入更多'), a:has-text('載入更多')"
    #: 渲染後的靜置時間（毫秒）
    settle_ms: int = 1500
    #: 等待此選擇器出現才視為載入完成
    wait_for_selector: str = ""


class BrowserFetcher:
    """以 Playwright 渲染頁面並回傳 HTML。

    **所有 Playwright 操作都在專用的單一執行緒內進行。** 這不是為了
    並行，而是為了正確性：``sync_playwright()`` 會在呼叫端的執行緒
    建立 asyncio event loop，而 Django 偵測到 running loop 後會以
    ``SynchronousOnlyOperation`` 拒絕所有同步 ORM 操作——採集任務
    正好需要在渲染後立即寫入資料庫。

    把 Playwright 隔離到自己的執行緒，讓 Django 所在的執行緒維持
    無 event loop 的狀態，兩者互不干擾。這比設
    ``DJANGO_ALLOW_ASYNC_UNSAFE=1`` 好——後者是把安全檢查整個關掉，
    而那個檢查存在的理由（避免阻塞 event loop）依然成立。

    單一 worker 也滿足 Playwright 的要求：其物件只能在建立它的
    執行緒中使用。

    每個實例持有一個瀏覽器。由 ``browser_pool`` 在 Celery 子程序中
    延遲建立、於 ``worker_process_shutdown`` 關閉。
    """

    def __init__(self, headless: bool = True):
        self._headless = headless
        # max_workers=1：Playwright 物件只能在建立它的執行緒使用
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="playwright"
        )
        self._executor.submit(self._start).result()

    # ---------------------------------------------------------------- 執行緒內

    def _start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-TW",
            viewport={"width": 1366, "height": 900},
        )
        self._context.add_init_script(_STEALTH_SCRIPT)

    def _render(self, url: str, opts: RenderOptions, timeout: float) -> FetchResult:
        page = self._context.new_page()
        try:
            self._goto(page, url, opts, timeout)
            self._interact(page, opts)
            return FetchResult(url=page.url, status_code=200, text=page.content())
        finally:
            page.close()

    # ---------------------------------------------------------------- 對外

    def get(self, url: str, *, timeout: float = 45.0,
            options: RenderOptions | None = None) -> FetchResult:
        opts = options or RenderOptions()
        try:
            return self._executor.submit(self._render, url, opts, timeout).result()
        except Exception as exc:                    # noqa: BLE001
            raise FetchError(f"{url}: {exc}") from exc

    def run_in_page(self, fn):
        """在瀏覽器執行緒中執行 ``fn(page)``，並保證頁面被關閉。

        給需要互動的情境使用——填表單、按鈕、讀取 iframe——這些無法
        以單純的 ``get()`` 表達。

        **必須透過此方法，不可從外部直接取用 ``_context`` 或 ``_executor``。**
        Playwright 的 sync API 以 greenlet 實作，其狀態綁在建立它的執行緒；
        從外部跨執行緒建立與使用頁面會觸發
        「greenlet.error: Cannot switch to a different thread」，
        而該錯誤的訊息完全指不出真正的原因。
        """
        def _wrapped():
            page = self._context.new_page()
            try:
                return fn(page)
            finally:
                try:
                    page.close()
                except Exception:               # noqa: BLE001
                    pass

        try:
            return self._executor.submit(_wrapped).result()
        except Exception as exc:                # noqa: BLE001
            raise FetchError(str(exc)) from exc

    # ---------------------------------------------------------------- 內部

    def _goto(self, page, url: str, opts: RenderOptions, timeout: float) -> None:
        # Cloudflare 挑戰頁需要在 commit 階段就取得控制權，
        # 等到 domcontentloaded 時挑戰可能已逾時（移植自 common.py）
        wait_until = "commit" if opts.cloudflare else "domcontentloaded"
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
        except Exception as exc:                    # noqa: BLE001
            # goto 逾時不一定表示失敗——部分站台的第三方資源會一直載入。
            # 先繼續，由後續的內容檢查決定成敗。
            logger.debug("goto 未正常結束（%s），繼續嘗試取得內容", exc)

        if opts.cloudflare:
            self._wait_cloudflare(page)
        else:
            page.wait_for_timeout(opts.settle_ms)

        if opts.wait_for_selector:
            try:
                page.wait_for_selector(opts.wait_for_selector, timeout=15000)
            except Exception:                       # noqa: BLE001
                logger.warning("等待選擇器 %r 逾時：%s", opts.wait_for_selector, url)

    @staticmethod
    def _wait_cloudflare(page, max_seconds: int = 35) -> None:
        """等待 Cloudflare 挑戰通過。以標題判斷，移植自 common.py。"""
        for _ in range(max_seconds):
            page.wait_for_timeout(1000)
            title = page.title() or ""
            if title and "moment" not in title.lower() and "請稍候" not in title:
                return
        logger.warning("Cloudflare 挑戰未在 %d 秒內通過", max_seconds)

    @staticmethod
    def _interact(page, opts: RenderOptions) -> None:
        """無限滾動與「載入更多」。

        兩者都採停滯偵測而非固定次數——內容不再增加就提早結束，
        避免對已到底的頁面持續操作。
        """
        if opts.scrolls:
            stagnant = last_height = 0
            for _ in range(opts.scrolls):
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(1200)
                height = page.evaluate("document.body.scrollHeight")
                if height == last_height:
                    stagnant += 1
                    if stagnant >= 3:
                        break
                else:
                    stagnant, last_height = 0, height

        for _ in range(opts.load_more_clicks):
            button = page.query_selector(opts.load_more_selector)
            if not button or not button.is_visible():
                break
            try:
                button.click()
            except Exception:                       # noqa: BLE001
                break
            page.wait_for_timeout(1400)

    def close(self) -> None:
        def _stop():
            for closer in (self._context.close, self._browser.close, self._pw.stop):
                try:
                    closer()
                except Exception:                   # noqa: BLE001
                    pass

        try:
            self._executor.submit(_stop).result(timeout=30)
        except Exception:                           # noqa: BLE001
            logger.warning("關閉瀏覽器時發生問題，強制結束執行緒")
        finally:
            self._executor.shutdown(wait=False)
