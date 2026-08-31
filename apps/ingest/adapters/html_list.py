"""HTML 新聞清單 adapter。

給沒有可用 RSS 的站台使用——實測聯合新聞網的 RSS 回傳內容全空的項目，
中時新聞網的 RSS 網址全部 404，兩家都只能走 Playwright 渲染後解析 HTML。
它們是既有語料最大的兩個來源（21.9 萬與 3.2 萬篇）。

解析與渲染分離：本模組收到的是已渲染的 HTML 字串（由
``apps.ingest.browser.BrowserFetcher`` 產出），因此可用 fixture 做
small 測試，不必在測試中啟動瀏覽器。

兩站的結構相似（一串連結加標題），因此以設定驅動單一實作，
而非各寫一個近乎重複的類別。新增同類站台只需加一組設定。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from .base import ParsedDocument, register_adapter
from .rss import normalize_url

logger = logging.getLogger(__name__)

# 標題末尾常黏著時間戳（如「…冰川崩塌一刻\n\t\t\t21:41」），需剝除
_TRAILING_TIME = re.compile(r"\s*\d{1,2}:\d{2}\s*$")


@dataclass(frozen=True)
class ListConfig:
    """站台的清單頁結構。"""

    #: 文章連結的 href 樣式。用正規表示式而非僅靠 CSS，
    #: 因為清單頁混雜大量導覽、廣告與推薦連結。
    href_pattern: re.Pattern
    #: 取得連結的 CSS 選擇器
    link_selector: str = "a"
    #: 站台名稱，僅用於日誌
    label: str = ""


def _clean_title(raw: str) -> str:
    title = re.sub(r"\s+", " ", (raw or "").strip())
    return _TRAILING_TIME.sub("", title).strip()


class HtmlListAdapter:
    """從已渲染的 HTML 清單頁抽出文章連結與標題。"""

    def __init__(self, config: ListConfig):
        self.config = config

    def list_documents(self, raw: str, *, base_url: str = "") -> list[ParsedDocument]:
        if not raw or not raw.strip():
            raise ValueError("空的 HTML 回應")

        tree = HTMLParser(raw)
        seen: dict[str, ParsedDocument] = {}
        anchors = 0

        for node in tree.css(self.config.link_selector):
            href = node.attributes.get("href") or ""
            if not self.config.href_pattern.search(href):
                continue
            anchors += 1

            url = normalize_url(href, base_url)
            title = _clean_title(node.text())
            if not url or not title:
                continue
            # 同一篇文章常同時出現在圖片連結與標題連結上，
            # 圖片連結沒有文字。以先出現且有標題者為準。
            seen.setdefault(url, ParsedDocument(url=url, title=title))

        if not seen:
            # 與 RSS adapter 相同的判準：不可靜默回傳空清單。
            # 清單頁改版或渲染失敗時，若回報「成功但沒新聞」，
            # 來源健康度監測會完全失明（ADR-0007）。
            raise ValueError(
                f"{self.config.label or 'HTML'} 清單頁未解析出任何文章"
                f"（比對到 {anchors} 個符合樣式的連結）——"
                f"頁面結構可能已變更，或渲染未完成"
            )

        logger.info("%s 清單頁解析出 %d 篇（連結 %d 個）",
                    self.config.label or "HTML", len(seen), anchors)
        return list(seen.values())


# ---------------------------------------------------------------- 站台設定
# 樣式沿用既有爬蟲 crawler/sites.py 的實證知識

UDN_CONFIG = ListConfig(
    href_pattern=re.compile(r"/news/story/\d+/\d+"),
    link_selector="div.story-list__text a, div.context-box__content a",
    label="聯合新聞網",
)

CHINATIMES_CONFIG = ListConfig(
    href_pattern=re.compile(r"/(?:realtimenews|newspapers|opinion)/\d{14}-\d+"),
    link_selector="h3.title a, section.article-list h3 a",
    label="中時新聞網",
)

# 工商時報：/livenews 首頁被 Cloudflare 擋，分類頁 /livenews/ctee
# 實測純 HTTP 200。文章 URL 形如 /news/20260831700477-431401。
CTEE_CONFIG = ListConfig(
    href_pattern=re.compile(r"/news/\d+-\d+"),
    link_selector="h3.news-title a",
    label="工商時報",
)


@register_adapter("udn")
class UdnListAdapter(HtmlListAdapter):
    def __init__(self):
        super().__init__(UDN_CONFIG)


@register_adapter("chinatimes")
class ChinaTimesListAdapter(HtmlListAdapter):
    def __init__(self):
        super().__init__(CHINATIMES_CONFIG)


@register_adapter("ctee")
class CteeListAdapter(HtmlListAdapter):
    def __init__(self):
        super().__init__(CTEE_CONFIG)
