"""抓取接縫。

測試策略的接縫 2：爬蟲測試不得觸及真實網站。所有網路存取走此介面，
測試時注入 ``FakeFetcher`` 回放錄製的 fixture。

重要區分：fixture 測試只能偵測「你改壞了解析程式」，**偵測不到
「網站改版了」**。後者只能靠生產環境的來源健康度監測（`Source`
的連續失敗計數）。兩者機制不同、時機不同，缺一不可。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

# 沿用既有爬蟲 crawler/common.py 的 UA——那是對實際站台行為的實證知識
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    """抓取失敗。由呼叫端決定要重試或記為來源失敗。"""


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class Fetcher(Protocol):
    def get(self, url: str, *, timeout: float = 30.0) -> FetchResult: ...


class HttpFetcher:
    """正式環境使用。RSS/sitemap/靜態頁走這裡，不啟動瀏覽器。

    ADR-0007 的 ``fetch`` 佇列即以此為主力——相較 Playwright，
    成本差一到兩個數量級。
    """

    def __init__(self, user_agent: str = USER_AGENT):
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "zh-TW,zh;q=0.9"},
            follow_redirects=True,
        )

    def get(self, url: str, *, timeout: float = 30.0) -> FetchResult:
        try:
            resp = self._client.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            raise FetchError(f"{url}: {exc}") from exc
        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text,
            headers=dict(resp.headers),
        )

    def close(self) -> None:
        self._client.close()


class FakeFetcher:
    """測試使用。以 URL 對應到預錄的回應內容。

    未登記的 URL 會拋錯而非回傳空字串——靜默回傳空內容會讓測試
    在「解析器根本沒拿到資料」時仍然通過。
    """

    def __init__(self, responses: dict[str, str | FetchResult] | None = None):
        self.responses: dict[str, str | FetchResult] = responses or {}
        self.calls: list[str] = []

    def register(self, url: str, body: str, status_code: int = 200) -> None:
        self.responses[url] = FetchResult(url=url, status_code=status_code, text=body)

    def get(self, url: str, *, timeout: float = 30.0) -> FetchResult:
        self.calls.append(url)
        if url not in self.responses:
            raise FetchError(f"FakeFetcher 未登記的 URL：{url}")
        value = self.responses[url]
        if isinstance(value, FetchResult):
            return value
        return FetchResult(url=url, status_code=200, text=value)
