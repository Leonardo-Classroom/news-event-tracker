"""RSS adapter 的 small 測試——不觸及網路，以 fixture 回放。

注意能力邊界：這些測試只能偵測「解析程式被改壞」，**偵測不到
「來源網站改版」**。後者靠 Source 的連續失敗計數在生產環境發現。
"""
import datetime as dt

import pytest

from apps.ingest.adapters.rss import RssAdapter, normalize_url

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>測試新聞網</title>
  <link>https://example.test/</link>
  <item>
    <title>北檢偵結前市長貪污案 求刑十二年</title>
    <link>https://example.test/news/1?utm_source=rss&amp;utm_medium=feed</link>
    <guid>news-1</guid>
    <author>記者甲</author>
    <pubDate>Thu, 05 Mar 2026 08:30:00 +0000</pubDate>
    <description>檢方今日偵查終結，依貪污治罪條例起訴…</description>
  </item>
  <item>
    <title>立法院三讀通過預算案</title>
    <link>/news/2</link>
    <guid>news-2</guid>
    <pubDate>Thu, 05 Mar 2026 09:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>測試 Atom</title>
  <entry>
    <title>監察院通過糾正案</title>
    <link href="https://example.test/atom/1"/>
    <id>atom-1</id>
    <updated>2026-03-05T10:00:00Z</updated>
  </entry>
</feed>
"""


class TestNormalizeUrl:
    def test_移除追蹤參數(self):
        url = "https://example.test/a?utm_source=rss&utm_medium=feed&id=7"
        assert normalize_url(url) == "https://example.test/a?id=7"

    def test_全部都是追蹤參數時不留問號(self):
        assert normalize_url("https://example.test/a?fbclid=xyz") == "https://example.test/a"

    def test_移除_fragment(self):
        assert normalize_url("https://example.test/a#section") == "https://example.test/a"

    def test_相對路徑補上網域(self):
        assert normalize_url("/news/2", "https://example.test/") == "https://example.test/news/2"

    def test_保留非追蹤參數(self):
        assert normalize_url("https://example.test/a?page=2") == "https://example.test/a?page=2"

    def test_空值(self):
        assert normalize_url("") == ""

    def test_同一文章的不同來路正規化為同一_url(self):
        """不做這件事，Document.url 的唯一約束就無法提供冪等保證。"""
        variants = [
            "https://example.test/news/1?utm_source=rss",
            "https://example.test/news/1?fbclid=abc",
            "https://example.test/news/1#top",
            "https://example.test/news/1",
        ]
        assert len({normalize_url(v) for v in variants}) == 1


class TestRssAdapter:
    def setup_method(self):
        self.adapter = RssAdapter()

    def test_解析出所有項目(self):
        docs = self.adapter.list_documents(RSS_SAMPLE, base_url="https://example.test/")
        assert len(docs) == 2

    def test_標題與網址(self):
        docs = self.adapter.list_documents(RSS_SAMPLE, base_url="https://example.test/")
        assert docs[0].title == "北檢偵結前市長貪污案 求刑十二年"
        assert docs[0].url == "https://example.test/news/1"   # 追蹤參數已移除

    def test_相對連結被補完(self):
        docs = self.adapter.list_documents(RSS_SAMPLE, base_url="https://example.test/")
        assert docs[1].url == "https://example.test/news/2"

    def test_發布時間為_aware_datetime(self):
        docs = self.adapter.list_documents(RSS_SAMPLE, base_url="https://example.test/")
        assert docs[0].published_at == dt.datetime(2026, 3, 5, 8, 30, tzinfo=dt.timezone.utc)

    def test_缺少作者時為空字串而非_None(self):
        docs = self.adapter.list_documents(RSS_SAMPLE, base_url="https://example.test/")
        assert docs[0].author == "記者甲"
        assert docs[1].author == ""

    def test_body_留空由內頁抓取補上(self):
        """RSS 的 description 只是摘要，不是全文——不可當內文存入。"""
        docs = self.adapter.list_documents(RSS_SAMPLE, base_url="https://example.test/")
        assert docs[0].body == ""
        assert "檢方今日偵查終結" in docs[0].extra["summary"]

    def test_支援_atom(self):
        docs = self.adapter.list_documents(ATOM_SAMPLE, base_url="https://example.test/")
        assert len(docs) == 1
        assert docs[0].title == "監察院通過糾正案"
        assert docs[0].published_at == dt.datetime(2026, 3, 5, 10, 0, tzinfo=dt.timezone.utc)

    def test_跳過缺標題或連結的項目(self):
        """部分項目損壞時略過即可——只要還有可用的，來源就仍在供稿。"""
        broken = """<?xml version="1.0"?><rss version="2.0"><channel>
          <item><title>有標題沒連結</title></item>
          <item><link>https://example.test/x</link></item>
          <item><title>正常</title><link>https://example.test/ok</link></item>
        </channel></rss>"""
        docs = self.adapter.list_documents(broken, base_url="https://example.test/")
        assert [d.title for d in docs] == ["正常"]

    def test_全部項目皆空時拋錯(self):
        """取自聯合新聞網 RSS 的真實故障：結構完整但內容全空。

        這種回應通過 version 檢查、也不會觸發 bozo，若不明確攔截，
        ingest_source 會記為成功並把 consecutive_failures 歸零，
        導致一個永久壞掉的來源永遠看起來健康。
        """
        udn_style = """<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0">
          <channel><title><![CDATA[聯合新聞網]]></title>
            <item>
              <title><![CDATA[]]></title>
              <link></link>
              <pubDate>Thu, 01 Jan 1970 08:00:00 +0800</pubDate>
              <description><![CDATA[]]></description>
            </item>
            <item>
              <title><![CDATA[]]></title>
              <link></link>
              <pubDate>Thu, 01 Jan 1970 08:00:00 +0800</pubDate>
            </item>
          </channel></rss>"""
        with pytest.raises(ValueError, match="無一可用"):
            self.adapter.list_documents(udn_style, base_url="https://udn.com/")

    @pytest.mark.parametrize("raw,label", [
        ("這根本不是 XML", "純文字"),
        ("<html><body>網站維護中，請稍後再試</body></html>", "HTML 維護頁"),
        ("<html><h1>404 Not Found</h1></html>", "HTML 錯誤頁"),
        ("", "空回應"),
        ("{\"error\": \"forbidden\"}", "JSON 錯誤"),
    ])
    def test_非_feed_回應一律拋錯(self, raw, label):
        """靜默回傳空清單會讓來源健康度監測失效——壞掉的來源看起來像沒新聞。

        HTML 維護頁特別危險：feedparser 不會將它標記為 bozo，
        只是解析出零個項目。若不明確檢查 feed 版本，來源改回維護頁時
        consecutive_failures 永遠不會增加，網站改版將無法被偵測。
        """
        with pytest.raises(ValueError):
            self.adapter.list_documents(raw, base_url="https://example.test/")

    def test_空_feed_回傳空清單而不拋錯(self):
        empty = """<?xml version="1.0"?><rss version="2.0"><channel>
          <title>空的</title></channel></rss>"""
        assert self.adapter.list_documents(empty, base_url="https://example.test/") == []
