"""RAG 面板：檢索管線的可見度。

存在的理由是實際踩過的問題——檢索出錯時（「查柯文哲拿到的全是三月
宣判日，拿不到九月的二審開庭」）從外面看不出是哪個通道的問題：
關鍵字沒命中、向量沒覆蓋、還是融合把它擠掉了。
"""
import pytest
from django.contrib.auth import get_user_model

from apps.ingest.models import ContentClass, Document, Source, SourceType


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_user("rag", password="x",
                                                is_staff=True)


@pytest.fixture
def corpus(db):
    source = Source.objects.create(slug="s", name="測試報",
                                   type=SourceType.NEWS_SCRAPE,
                                   base_url="https://e.test")
    return [Document.objects.create(
        source=source, url=f"https://e.test/{i}",
        title=f"京華城案進度報導{i}", raw_body="京華城容積獎勵案" * 30,
        content_class=ContentClass.COPYRIGHTED) for i in range(3)]


@pytest.mark.medium
class TestRagPanel:
    def test_需要登入(self, client):
        assert client.get("/rag/").status_code == 302

    def test_空查詢只顯示覆蓋率(self, client, staff):
        client.force_login(staff)
        html = client.get("/rag/").content.decode()
        assert "可被檢索" in html
        assert "融合結果" not in html

    def test_試查顯示各通道命中(self, client, staff, corpus):
        client.force_login(staff)
        html = client.get("/rag/", {"q": "京華城"}).content.decode()
        assert "融合結果" in html
        assert "關鍵字通道裡最新的" in html
        assert corpus[0].title in html

    def test_標示每篇有無向量(self, client, staff, corpus):
        """兩個通道覆蓋範圍不同：關鍵字對全庫有效，向量只涵蓋已向量化
        的部分。查不到某篇時，這一欄是第一個要看的。"""
        client.force_login(staff)
        html = client.get("/rag/", {"q": "京華城"}).content.decode()
        assert "<th>向量</th>" in html

    def test_檢索出錯不讓整頁掛掉(self, client, staff):
        from unittest.mock import patch

        client.force_login(staff)
        with patch("apps.retrieval.hybrid.HybridRetriever.search",
                   side_effect=RuntimeError("索引壞了")):
            r = client.get("/rag/", {"q": "京華城"})
        assert r.status_code == 200
        assert "索引壞了" in r.content.decode()

    def test_側欄有入口(self, client, staff):
        client.force_login(staff)
        assert 'href="/rag/"' in client.get("/pipeline/").content.decode()
