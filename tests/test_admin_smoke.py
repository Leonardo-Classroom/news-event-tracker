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
