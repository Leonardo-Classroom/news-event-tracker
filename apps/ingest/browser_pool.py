"""Celery worker 內的瀏覽器生命週期（ADR-0007）。

既有爬蟲 ``crawl_robust.py`` 的 docstring 記錄了實證發現：

    全新瀏覽器/記者 = 不累積劣化；程序隔離 = 一個卡住不影響其他。

亦即「每位記者重開瀏覽器」不是隨意選擇，而是針對「瀏覽器長時間執行
後劣化卡死」的解方。

對應到 Celery 的作法是**持久瀏覽器 + `worker_max_tasks_per_child`**：
瀏覽器在 ``worker_process_init`` 建立、``worker_process_shutdown`` 關閉，
Celery 每 N 個任務重生子程序，瀏覽器隨之重建。啟動成本攤提於 N 個任務，
同時保留防劣化的效果——等價於既有設計，但由框架管理而非自製看門狗。

N 的初始值為 20（ADR-0007 實作要點 4），依實測的記憶體成長與失敗率調整。
"""
from __future__ import annotations

import logging

from celery.signals import worker_process_init, worker_process_shutdown

logger = logging.getLogger(__name__)

#: 程序區域的瀏覽器實例。prefork 模式下每個子程序各持有一個；
#: fork 後不可繼承已建立的瀏覽器連線，故必須在 process_init 才建立。
_browser = None


@worker_process_init.connect
def _init_browser(**kwargs) -> None:
    """僅在 browser 佇列的 worker 建立瀏覽器。

    以延遲建立實現：這裡不主動啟動，等第一次 ``get_browser()`` 才建。
    fetch 佇列的 worker 永遠不會呼叫它，因此不會付出啟動成本。
    """
    global _browser
    _browser = None


@worker_process_shutdown.connect
def _shutdown_browser(**kwargs) -> None:
    global _browser
    if _browser is not None:
        logger.info("關閉 worker 子程序的瀏覽器")
        _browser.close()
        _browser = None


def get_browser():
    """取得此子程序的瀏覽器，必要時建立。

    延遲建立的用意：只有真正跑 browser 佇列任務的子程序才會付出
    啟動成本（約 1–2 秒與數百 MB 記憶體）。
    """
    global _browser
    if _browser is None:
        from apps.ingest.browser import BrowserFetcher

        logger.info("建立 worker 子程序的瀏覽器")
        _browser = BrowserFetcher(headless=True)
    return _browser
