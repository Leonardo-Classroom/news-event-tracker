"""後台頁面的煙霧測試。

admin 的錯誤（欄位名打錯、N+1、模板變數缺漏）只會在瀏覽器中出現，
單元測試不會碰到。這些測試確保每個註冊的頁面至少能開。

在 23.8 萬列的資料表上，list_display 中任何一個逐列查詢的方法
都會讓頁面實質不可用——因此也一併驗證查詢次數。
"""
import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.medium


@pytest.fixture
def admin_client(db):
    User.objects.create_superuser("smoke", "s@test.local", "pw")
    client = Client()
    client.login(username="smoke", password="pw")
    return client


def registered_models():
    return [m for m in admin.site._registry
            if m._meta.app_label in {"ingest", "events", "extract", "llm"}]


@pytest.mark.parametrize("model", registered_models(),
                         ids=lambda m: m._meta.label)
def test_列表頁可載入(admin_client, model):
    url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
    assert admin_client.get(url).status_code == 200


@pytest.mark.parametrize("model", registered_models(),
                         ids=lambda m: m._meta.label)
def test_搜尋不報錯(admin_client, model):
    url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
    assert admin_client.get(url, {"q": "起訴"}).status_code == 200


class TestDocumentAdmin:
    def test_列表查詢次數不隨列數增長(self, admin_client, source):
        """list_display 中若有逐列查詢的方法，在 23.8 萬列上會讓頁面
        實質不可用。以不同列數比較查詢次數即可發現。"""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from apps.ingest.models import Document

        url = reverse("admin:ingest_document_changelist")

        def count_queries(n_docs):
            Document.objects.all().delete()
            for i in range(n_docs):
                Document.objects.create(
                    source=source, url=f"https://q.test/{i}", title=f"標題{i}",
                    raw_body="內文" * 20, content_class=source.content_class,
                )
            with CaptureQueriesContext(connection) as ctx:
                admin_client.get(url)
            return len(ctx.captured_queries)

        few, many = count_queries(3), count_queries(15)
        assert many <= few + 2, (
            f"查詢次數隨列數增長（3 列 {few} 次 → 15 列 {many} 次），"
            f"list_display 中可能有逐列查詢"
        )

    def test_篩選器可用(self, admin_client, source):
        url = reverse("admin:ingest_document_changelist")
        for params in ({"rel": "1"}, {"vec": "0"}, {"ident": "case"}):
            assert admin_client.get(url, params).status_code == 200


class TestWebInterface:
    """自訂介面的煙霧測試。

    模板錯誤（變數打錯、標籤未閉合、缺 context）只會在瀏覽器出現，
    單元測試碰不到。這些測試確保每頁至少能渲染。
    """

    @pytest.mark.parametrize("path", [
        "/", "/review/", "/documents/", "/pipeline/", "/crawlers/", "/costs/",
    ])
    def test_主要頁面可載入(self, admin_client, path):
        assert admin_client.get(path).status_code == 200

    def test_文件搜尋不報錯(self, admin_client):
        assert admin_client.get("/documents/", {"q": "起訴"}).status_code == 200

    def test_未登入導向登入頁而非_404(self, db):
        """LOGIN_URL 未設定時，login_required 會導向不存在的
        /accounts/login/ 而回 404——所有頁面看起來都壞掉。"""
        from django.test import Client
        response = Client().get("/")
        assert response.status_code == 302
        assert "/admin/login/" in response["Location"]

    def test_事件詳情顯示時間線與空白期(self, admin_client, source, db):
        """空白期的呈現是本系統的核心價值——規格 §2.1 的問題陳述是
        「報導呈雙峰分布、中間長期空白」，把空白畫出來才看得出填補了沒有。"""
        import datetime as dt
        from apps.events.models import (
            AssignmentMethod, Event, EventDocument, EventStatus,
        )
        from apps.ingest.models import Document

        event = Event.objects.create(slug="gap-test", title="空白期測試",
                                     status=EventStatus.ACTIVE)
        utc = dt.timezone.utc
        for i, when in enumerate([dt.datetime(2024, 1, 5, tzinfo=utc),
                                  dt.datetime(2025, 6, 5, tzinfo=utc)]):
            doc = Document.objects.create(
                source=source, url=f"https://gap.test/{i}", title=f"報導{i}",
                raw_body="內文", content_class=source.content_class,
                published_at=when,
            )
            EventDocument.objects.create(event=event, document=doc,
                                         method=AssignmentMethod.MANUAL)

        html = admin_client.get(f"/e/{event.slug}/").content.decode()
        assert "天無報導" in html, "跨越一年半的間隔未被標示為空白期"
        assert "報導0" in html and "報導1" in html
