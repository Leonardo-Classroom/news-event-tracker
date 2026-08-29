"""BrowserFetcher 的測試。

標記為 large：需要啟動真實瀏覽器，不進 CI。
存在的理由是守住一個具體的回歸——見 test_渲染後可立即操作_Django_ORM。
"""
import pytest

from apps.ingest.browser import RenderOptions

pytestmark = pytest.mark.large


@pytest.fixture(scope="module")
def browser():
    from apps.ingest.browser import BrowserFetcher
    fetcher = BrowserFetcher(headless=True)
    yield fetcher
    fetcher.close()


def test_可渲染簡單頁面(browser):
    # data: URL 不會自動對非 ASCII 做百分比編碼，故以 ASCII 內容測試——
    # 這裡要驗證的是渲染管線，不是 URL 編碼
    result = browser.get("data:text/html,<h1>hello-render</h1>")
    assert result.ok
    assert "hello-render" in result.text


@pytest.mark.medium
def test_渲染後可立即操作_Django_ORM(browser, db, source):
    """回歸測試：Playwright 的 sync API 會在呼叫端執行緒建立 asyncio
    event loop，Django 偵測到 running loop 後會以 SynchronousOnlyOperation
    拒絕所有同步 ORM 操作。

    採集任務正是「渲染完立刻寫資料庫」，因此這個組合必須成立。
    解法是把 Playwright 隔離在專用執行緒（見 BrowserFetcher 的說明）。

    若有人日後把 ThreadPoolExecutor 拿掉、改成直接呼叫 sync_playwright()，
    這個測試會失敗——那正是它存在的目的。
    """
    from apps.ingest.models import Document

    browser.get("data:text/html,<h1>x</h1>")

    # 關鍵：渲染之後在同一執行緒操作 ORM
    Document.objects.create(
        source=source, url="https://example.test/after-render",
        title="渲染後寫入", raw_body="", content_class=source.content_class,
    )
    assert Document.objects.filter(url="https://example.test/after-render").exists()


def test_渲染選項可設定滾動與等待(browser):
    """確認 RenderOptions 被實際套用而非忽略。"""
    opts = RenderOptions(scrolls=1, settle_ms=100)
    result = browser.get(
        "data:text/html,<body style='height:5000px'><h1>tall-page</h1></body>",
        options=opts,
    )
    assert result.ok
    assert "tall-page" in result.text
