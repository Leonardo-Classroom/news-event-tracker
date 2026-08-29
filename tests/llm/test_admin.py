"""LLM 用量後台的 medium 測試。

彙總數字若渲染失敗或算錯，使用者不會知道——因此頁面必須有測試守住。
"""
import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.llm.models import LlmPurpose, LlmUsage

pytestmark = pytest.mark.medium

UTC = dt.timezone.utc


@pytest.fixture
def admin_client(db):
    User.objects.create_superuser("admin", "a@test.local", "pw")
    client = Client()
    client.login(username="admin", password="pw")
    return client


@pytest.fixture
def usages(db):
    def make(purpose, model, cached, uncached, out, ntd, peak):
        return LlmUsage.objects.create(
            purpose=purpose, model=model,
            cached_input_tokens=cached, uncached_input_tokens=uncached,
            output_tokens=out, total_tokens=cached + uncached + out,
            cost_usd=Decimal(ntd) / 32, cost_ntd=Decimal(ntd),
            usd_to_ntd=Decimal("32.5"), peak=peak, duration_ms=1200,
        )
    make(LlmPurpose.EXTRACTION, "deepseek-v4-flash", 800, 200, 300, "10.00", True)
    make(LlmPurpose.CAUSAL, "deepseek-v4-pro", 0, 5000, 1000, "5.00", False)


class TestLlmUsageAdmin:
    def test_列表頁可載入(self, admin_client, usages):
        url = reverse("admin:llm_llmusage_changelist")
        assert admin_client.get(url).status_code == 200

    def test_彙總顯示總成本(self, admin_client, usages):
        html = admin_client.get(reverse("admin:llm_llmusage_changelist")).content.decode()
        assert "用量彙總" in html
        assert "15.00" in html          # 10 + 5

    def test_彙總依用途分列(self, admin_client, usages):
        html = admin_client.get(reverse("admin:llm_llmusage_changelist")).content.decode()
        assert "L1 結構化抽取" in html
        assert "L4 因果推理" in html

    def test_彙總顯示_cache_命中率(self, admin_client, usages):
        """命中率長期偏低即表示 context caching 未發揮作用——
        那是 ADR-0009 選用 DeepSeek 的理由之一，必須看得見。"""
        html = admin_client.get(reverse("admin:llm_llmusage_changelist")).content.decode()
        assert "cache 命中率" in html

    def test_彙總依時段分列(self, admin_client, usages):
        html = admin_client.get(reverse("admin:llm_llmusage_changelist")).content.decode()
        assert "尖峰" in html and "離峰" in html

    def test_篩選後彙總隨之改變(self, admin_client, usages):
        url = reverse("admin:llm_llmusage_changelist")
        html = admin_client.get(url, {"purpose__exact": LlmPurpose.CAUSAL}).content.decode()
        assert "5.00" in html
        assert "15.00" not in html

    def test_用量記錄不可新增或刪除(self, admin_client, usages):
        """這是帳，不該由人編輯。"""
        from apps.llm.admin import LlmUsageAdmin
        from django.contrib import admin as dj_admin
        instance = LlmUsageAdmin(LlmUsage, dj_admin.site)
        assert instance.has_add_permission(None) is False
        assert instance.has_change_permission(None) is False
        assert instance.has_delete_permission(None) is False
