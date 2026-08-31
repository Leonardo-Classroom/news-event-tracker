"""內頁抽取：發布時間必須能從 JSON-LD 或 meta 取得。"""
from apps.ingest.article import extract_published_at


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
