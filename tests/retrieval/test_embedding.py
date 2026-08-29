"""Embedding 服務的測試。

大部分標為 large：需要載入 2.3GB 模型。
純邏輯（文本組裝）則為 small。
"""
import pytest

from apps.retrieval.embedding import EMBEDDING_MODEL_VERSION, build_embedding_text


class TestBuildEmbeddingText:
    def test_標題重複一次(self):
        """標題是編輯對全文的濃縮，在事件歸屬時的鑑別力高於內文任一段落。"""
        text = build_embedding_text("案件標題", "內文段落")
        assert text.count("案件標題") == 2

    def test_內文截斷取前段(self):
        """新聞的倒金字塔結構把關鍵事實放在前面。"""
        body = "前段重要" + "尾" * 5000
        text = build_embedding_text("標題", body, max_chars=100)
        assert "前段重要" in text
        assert len(text) < 200

    def test_空內文仍可組出文本(self):
        assert build_embedding_text("只有標題", "").strip() == "只有標題\n只有標題"

    def test_空標題不報錯(self):
        assert "內文" in build_embedding_text("", "內文")

    def test_模型版本已定義(self):
        """版本會寫入每筆向量記錄——換模型時據此辨識哪些需重算。
        沒有它，換模型就只能全部重算或含糊地混用兩種向量空間。"""
        assert EMBEDDING_MODEL_VERSION
        assert "/" in EMBEDDING_MODEL_VERSION      # 形如 model/date


@pytest.mark.large
class TestEncoder:
    def test_產生正規化的_1024_維向量(self):
        from apps.retrieval.embedding import embed_texts
        vectors = embed_texts(["台北地檢署偵結起訴前市長涉貪案"])
        assert len(vectors) == 1
        assert len(vectors[0]) == 1024
        norm = sum(v * v for v in vectors[0]) ** 0.5
        assert abs(norm - 1.0) < 0.01        # 正規化後 L2 範數為 1

    def test_語意相近者向量較近(self):
        from apps.retrieval.embedding import embed_texts
        a, b, c = embed_texts([
            "檢方偵結起訴前市長涉嫌貪污",
            "北檢依貪污治罪條例起訴前市長",
            "中央氣象署發布豪雨特報",
        ])
        dot = lambda x, y: sum(i * j for i, j in zip(x, y))
        assert dot(a, b) > dot(a, c)

    def test_空清單回傳空結果(self):
        from apps.retrieval.embedding import embed_texts
        assert embed_texts([]) == []
