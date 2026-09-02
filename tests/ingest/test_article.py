"""內頁抽取：發布時間必須能從 JSON-LD 或 meta 取得。"""
import json

import pytest

from apps.ingest.article import (
    NEEDS_BROWSER, extract_published_at, extract_twreporter_article,
    twreporter_body_url,
)


JSON_LD = """
<html><head>
<script type="application/ld+json">
{"@type":"NewsArticle","datePublished":"2026-08-31T08:35:27+08:00",
 "articleBody":"內文內文內文內文內文內文內文內文內文內文內文內文內文內文內文內文內文內文內文內文"}
</script>
</head><body></body></html>
"""

META_ONLY = """
<html><head>
<meta property="article:published_time" content="2026-08-31T07:48:44+08:00">
</head><body><p>沒有 JSON-LD</p></body></html>
"""


class TestExtractPublishedAt:
    def test_json_ld(self):
        got = extract_published_at(JSON_LD)
        assert got is not None
        assert got.year == 2026 and got.month == 8 and got.day == 31
        assert got.hour == 8 and got.minute == 35

    def test_只有meta也能解析(self):
        got = extract_published_at(META_ONLY)
        assert got is not None
        assert got.hour == 7 and got.minute == 48

    def test_沒有時間回傳None(self):
        assert extract_published_at("<html><body>無</body></html>") is None


# 真實回應的結構（2026-09-02 實測）：內文在 content.api_data 的區塊裡，
# 夾雜 HTML 標籤與 <!--__ANNOTATION__=...--> 註解，都要清掉。
TWREPORTER_API = json.dumps({"data": {
    "title": "她從未後悔公開自身煉獄",
    "published_date": "2026-09-01T16:00:00Z",
    "content": {"api_data": [
        {"type": "unstyled", "content": [""]},
        {"type": "header-two", "content": ["在滿地灰燼中，搜尋和守護殘痕"]},
        {"type": "annotation", "content": [
            "律師如此說道。<!--__ANNOTATION__={\"text\":\"札瓦侯\"}--><!--札瓦侯-->"
            "整個審判的過程，儘管肩負為多明尼克辯護的職責，"]},
        {"type": "unstyled", "content": [
            "他有時會從被告席向我做出一些隱晦的手勢，例如將手輕輕按在胸口。"
            "而我從未回應。這真的是那個我以為與我共度半生的男人的手勢嗎？"]},
        {"type": "infobox", "content": [{"body": "<p>2020年，住在法國南部的吉賽兒。</p>"}]},
        {"type": "youtube", "content": [{"url": "https://youtu.be/x"}]},
    ]},
}}, ensure_ascii=False)


class TestTwreporterApi:
    def test_由文章網址組出api網址(self):
        url = twreporter_body_url("https://www.twreporter.org/a/some-slug")
        assert url == "https://go-api.twreporter.org/v2/posts/some-slug?full=true"

    def test_結尾斜線也能處理(self):
        assert "some-slug?full=true" in twreporter_body_url(
            "https://www.twreporter.org/a/some-slug/")

    def test_抽出內文並清掉標籤與註解(self):
        article = extract_twreporter_article(TWREPORTER_API)
        assert "在滿地灰燼中" in article.body
        assert "他有時會從被告席" in article.body
        assert "2020年，住在法國南部" in article.body    # infobox 的 body
        assert "<!--" not in article.body and "<p>" not in article.body
        assert "__ANNOTATION__" not in article.body
        assert article.title == "她從未後悔公開自身煉獄"
        assert article.published_at.year == 2026
        assert article.strategy == "twreporter-api"

    def test_內文太短視為失敗(self):
        """與 extract_article 一致：靜默回傳空字串會讓站台改版
        看起來像「這篇文章本來就很短」。"""
        raw = json.dumps({"data": {"content": {"api_data": [
            {"type": "unstyled", "content": ["太短"]}]}}})
        with pytest.raises(ValueError):
            extract_twreporter_article(raw)

    def test_報導者不再需要瀏覽器(self):
        """曾被列入 NEEDS_BROWSER 而被 fill_missing_bodies 永久排除，
        導致 5,925 篇內文永遠補不到。"""
        assert "twreporter" not in NEEDS_BROWSER
