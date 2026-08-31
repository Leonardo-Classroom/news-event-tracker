"""HTML 清單 adapter 的 small 測試。

以真實抓取的頁面快照（fixtures/html/）回放，不啟動瀏覽器、不連網。
這正是「渲染與解析分離」的目的。

能力邊界同 RSS adapter：fixture 只能偵測「解析程式被改壞」，
偵測不到「網站改版」——後者靠生產環境的來源健康度監測。
"""
import pathlib

import pytest

from apps.ingest.adapters import get_adapter
from apps.ingest.adapters.html_list import HtmlListAdapter, ListConfig
import re

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "html"


def load(name: str) -> str:
    path = FIXTURES / f"{name}.html"
    if not path.exists():
        pytest.skip(f"缺少 fixture：{path}")
    return path.read_text(encoding="utf-8")


class TestUdnAdapter:
    def setup_method(self):
        self.adapter = get_adapter("udn")
        self.html = load("udn_breaknews")

    def test_解析出多篇文章(self):
        docs = self.adapter.list_documents(self.html, base_url="https://udn.com")
        assert len(docs) > 30

    def test_url_為絕對路徑且已正規化(self):
        docs = self.adapter.list_documents(self.html, base_url="https://udn.com")
        for d in docs:
            assert d.url.startswith("https://udn.com/news/story/")
            assert "from=" not in d.url          # 追蹤參數已移除
            assert "#" not in d.url

    def test_標題非空且已去除尾端時間戳(self):
        docs = self.adapter.list_documents(self.html, base_url="https://udn.com")
        for d in docs:
            assert d.title
            assert not re.search(r"\d{1,2}:\d{2}$", d.title)
            assert "\n" not in d.title

    def test_無重複_url(self):
        """同一篇文章會同時出現在圖片連結與標題連結上。"""
        docs = self.adapter.list_documents(self.html, base_url="https://udn.com")
        assert len({d.url for d in docs}) == len(docs)


class TestChinaTimesAdapter:
    def setup_method(self):
        self.adapter = get_adapter("chinatimes")
        self.html = load("chinatimes_realtime")

    def test_解析出文章(self):
        docs = self.adapter.list_documents(self.html, base_url="https://www.chinatimes.com")
        assert len(docs) >= 10

    def test_url_符合中時的文章樣式(self):
        docs = self.adapter.list_documents(self.html, base_url="https://www.chinatimes.com")
        for d in docs:
            assert re.search(r"/(realtimenews|newspapers|opinion)/\d{14}-\d+", d.url)


class TestCteeAdapter:
    def setup_method(self):
        self.adapter = get_adapter("ctee")
        self.html = load("ctee_livenews")

    def test_解析出文章(self):
        docs = self.adapter.list_documents(
            self.html, base_url="https://www.ctee.com.tw")
        assert len(docs) == 3

    def test_url_符合工商時報文章樣式且已正規化(self):
        docs = self.adapter.list_documents(
            self.html, base_url="https://www.ctee.com.tw")
        for d in docs:
            assert re.search(r"https://www\.ctee\.com\.tw/news/\d+-\d+$", d.url)

    def test_圖片連結不覆蓋已有標題(self):
        docs = self.adapter.list_documents(
            self.html, base_url="https://www.ctee.com.tw")
        by_url = {d.url: d.title for d in docs}
        assert "沙德爾" in by_url["https://www.ctee.com.tw/news/20260831700477-431401"]


class TestFailureModes:
    """靜默回傳空清單會讓來源健康度監測失效——與 RSS adapter 同一原則。"""

    def setup_method(self):
        self.adapter = HtmlListAdapter(ListConfig(
            href_pattern=re.compile(r"/news/\d+"), label="測試站"
        ))

    def test_空回應拋錯(self):
        with pytest.raises(ValueError, match="空的"):
            self.adapter.list_documents("", base_url="https://example.test")

    def test_無符合文章的頁面拋錯(self):
        """渲染失敗或改版時，頁面可能只剩導覽列。"""
        with pytest.raises(ValueError, match="未解析出任何文章"):
            self.adapter.list_documents(
                "<html><body><a href='/about'>關於我們</a></body></html>",
                base_url="https://example.test",
            )

    def test_有連結但全無標題時拋錯(self):
        """圖片連結沒有文字。若整頁只剩圖片連結，代表渲染未完成。"""
        with pytest.raises(ValueError, match="未解析出任何文章"):
            self.adapter.list_documents(
                "<html><body><a href='/news/123'><img src='x.jpg'></a></body></html>",
                base_url="https://example.test",
            )

    def test_正常頁面可解析(self):
        docs = self.adapter.list_documents(
            "<html><body>"
            "<a href='/news/123'>正常標題</a>"
            "<a href='/about'>關於我們</a>"
            "</body></html>",
            base_url="https://example.test",
        )
        assert len(docs) == 1
        assert docs[0].title == "正常標題"
        assert docs[0].url == "https://example.test/news/123"
