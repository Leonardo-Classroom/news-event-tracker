"""關鍵字檢索的測試。

build_tsquery 是純函式（small）；實際查詢需要真實 PostgreSQL（medium）。
"""
import pytest

from apps.retrieval.keyword import BigramFtsBackend, build_tsquery


class TestBuildTsquery:
    def test_兩字詞為單一_bigram(self):
        assert build_tsquery("起訴") == "起訴"

    def test_三字詞切成兩個_bigram(self):
        """查詢未切分正是實測中「檢察官」命中 0 的原因。"""
        assert build_tsquery("檢察官") == "檢察 & 察官"

    def test_四字詞(self):
        assert build_tsquery("貪污治罪") == "貪污 & 污治 & 治罪"

    def test_寬鬆模式使用_or(self):
        assert build_tsquery("檢察官", operator="|") == "檢察 | 察官"

    def test_英數保持整詞(self):
        assert build_tsquery("Covid19") == "covid19"

    def test_空查詢(self):
        assert build_tsquery("") == ""
        assert build_tsquery("，。！") == ""

    def test_查詢切分與文件切分一致(self):
        """兩者必須用同一個 tokenizer，否則索引與查詢對不上。"""
        from apps.core.text.bigram import to_tsvector_input
        doc_tokens = set(to_tsvector_input("檢察官提起公訴").split())
        query_tokens = set(build_tsquery("檢察官").split(" & "))
        assert query_tokens <= doc_tokens


@pytest.mark.medium
class TestBigramFtsBackend:
    @pytest.fixture(autouse=True)
    def _docs(self, source):
        from apps.ingest.models import Document
        for i, title in enumerate([
            "檢察官偵結起訴前市長貪污案",
            "地方法院一審宣判有罪",
            "氣象署發布豪雨特報",
            "警方逮捕竊盜嫌犯",
        ]):
            Document.objects.create(
                source=source, url=f"https://example.test/{i}",
                title=title, raw_body="", content_class=source.content_class,
            )
        self.qs = Document.objects.all()
        self.backend = BigramFtsBackend()

    def test_三字詞可命中(self):
        """未切分查詢時此測試會失敗——這正是實測發現的 bug。"""
        assert self.backend.search(self.qs, "檢察官").count() == 1

    def test_兩字詞可命中(self):
        assert self.backend.search(self.qs, "起訴").count() == 1

    def test_不相關查詢不命中(self):
        assert self.backend.search(self.qs, "颱風").count() == 0

    def test_寬鬆模式召回較多(self):
        strict = self.backend.search(self.qs, "檢察法院", mode="all").count()
        loose = self.backend.search(self.qs, "檢察法院", mode="any").count()
        assert loose > strict

    def test_空查詢回傳空集合(self):
        assert self.backend.search(self.qs, "").count() == 0

    def test_已知限制_單字查詢無法命中(self):
        """記錄 bigram 索引的固有邊界，避免日後誤以為是 bug。"""
        assert self.backend.search(self.qs, "警").count() == 0
