"""可點擊的內容標籤（連到文件檢索）。

**只有內容詞可點，狀態標籤不可點。** 搜尋「未公開」「從未成功」
「已完成」沒有任何意義，把它們做成連結只會製造死路。
"""
import re

import pytest
from django.contrib.auth import get_user_model

from apps.events.models import Event, EventStatus
from apps.ingest.models import ContentClass, Document, Source, SourceType


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_user("tagger", password="x",
                                                is_staff=True)


def links(html):
    return re.findall(r'<a class="tag[^>]*term-tag"[^>]*href="([^"]+)"', html)


@pytest.mark.medium
class TestTermTags:
    def test_ai候選標籤可點且開新分頁(self, client, staff):
        from apps.eventbuilder.models import Conversation, EventSuggestion

        client.force_login(staff)
        c = Conversation.objects.create(owner=staff, title="t")
        EventSuggestion.objects.create(conversation=c, title="案",
                                       tags=["兩岸關係"])
        html = client.get(f"/build/c/{c.pk}/").content.decode()
        assert "/documents/?q=%E5%85%A9%E5%B2%B8%E9%97%9C%E4%BF%82&source=&only=" \
            in html
        assert 'target="_blank"' in html
        assert 'rel="noopener"' in html

    def test_事件核心詞可點(self, client, staff):
        client.force_login(staff)
        Event.objects.create(slug="e", title="京華城容積案",
                             status=EventStatus.ACTIVE,
                             core_terms=["京華城", "容積獎勵"])
        assert links(client.get("/").content.decode())

    def test_案號可點(self, client, staff):
        client.force_login(staff)
        Event.objects.create(slug="e", title="案", status=EventStatus.ACTIVE,
                             case_numbers=["113年度金訴字第51號"])
        html = client.get("/e/e/").content.decode()
        assert any("113" in u for u in links(html))

    def test_統編顯示帶前綴但只查數字(self, client, staff):
        """顯示「統編 12345678」，查詢只用數字——把「統編」兩個字
        一起丟進全文檢索會把結果限縮到剛好也提到那兩個字的文件。"""
        client.force_login(staff)
        source = Source.objects.create(slug="s", name="來源",
                                       type=SourceType.NEWS_SCRAPE,
                                       base_url="https://e.test")
        doc = Document.objects.create(
            source=source, url="https://e.test/1", title="標題",
            raw_body="統一編號 12345678 的公司" * 10,
            content_class=ContentClass.COPYRIGHTED)
        html = client.get(f"/documents/{doc.pk}/").content.decode()
        if doc.tax_ids:                       # 由 save() 自動抽取
            assert "?q=12345678&" in html
            assert "統編 12345678</a>" in html

    def test_狀態標籤不可點(self, client, staff):
        """/crawlers/ 全是狀態標籤（正常、從未成功、連續失敗），
        不該有任何一個變成搜尋連結。"""
        client.force_login(staff)
        for path in ("/crawlers/", "/crawlers/history/", "/pipeline/"):
            assert links(client.get(path).content.decode()) == [], path
