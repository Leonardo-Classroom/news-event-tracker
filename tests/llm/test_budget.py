"""預算閘門的測試。

這是花錢的閘門，行為必須明確：超額時拒絕、不留假帳、可稽核地追加。
"""
from decimal import Decimal

import pytest

from apps.llm.budget import (
    BudgetExceeded, approved_usd, check_budget, grant_budget,
    remaining_usd, spent_usd,
)
from apps.llm.models import LlmBudgetGrant, LlmPurpose, LlmUsage
from apps.llm.provider import FakeProvider, LlmError

pytestmark = pytest.mark.medium


def spend(usd: str):
    return LlmUsage.objects.create(
        purpose=LlmPurpose.EXTRACTION, model="deepseek-v4-flash",
        cached_input_tokens=0, uncached_input_tokens=1000, output_tokens=100,
        total_tokens=1100, cost_usd=Decimal(usd), cost_ntd=Decimal(usd) * 32,
        usd_to_ntd=Decimal("32.5"),
    )


class TestBudgetAccounting:
    def test_初始額度來自設定(self, db):
        assert approved_usd() == Decimal("5.00")

    def test_已花費以用量記錄為準(self, db):
        """帳與用量若分兩處記錄，不一致時無從判斷哪個才對。"""
        spend("1.25"); spend("0.75")
        assert spent_usd() == Decimal("2.00")
        assert remaining_usd() == Decimal("3.00")

    def test_追加額度累加(self, db):
        grant_budget(Decimal("5.00"))
        grant_budget(Decimal("3.00"))
        assert approved_usd() == Decimal("8.00")

    def test_追加留下可稽核紀錄(self, db):
        grant_budget(Decimal("5.00"), granted_by="cli", note="回填批次")
        g = LlmBudgetGrant.objects.get()
        assert g.granted_by == "cli" and g.note == "回填批次"


class TestCheckBudget:
    def test_額度內通過(self, db):
        spend("1.00")
        check_budget()          # 不應拋錯

    def test_達到額度即拒絕(self, db):
        spend("5.00")
        with pytest.raises(BudgetExceeded) as exc:
            check_budget()
        assert "已達上限" in str(exc.value)
        assert "llm_budget --approve" in str(exc.value)

    def test_超過額度即拒絕(self, db):
        spend("7.50")
        with pytest.raises(BudgetExceeded):
            check_budget()

    def test_追加後恢復(self, db):
        spend("5.00")
        with pytest.raises(BudgetExceeded):
            check_budget()
        grant_budget(Decimal("5.00"))    # 總額度 5（初始被取代）→ 需再看實際
        grant_budget(Decimal("5.00"))    # 總額度 10
        check_budget()                    # 已花 5、額度 10 → 通過

    def test_預估成本納入判斷(self, db):
        spend("4.90")
        check_budget()                                    # 尚有餘裕
        with pytest.raises(BudgetExceeded):
            check_budget(estimated_usd=Decimal("0.20"))   # 加上預估即超額


class TestProviderEnforcesBudget:
    """閘門設在 provider 內部——呼叫端沒有繞過的餘地。"""

    def test_超額時_provider_拒絕呼叫(self, db):
        spend("5.00")
        provider = FakeProvider(responses=['{"ok": true}'])
        with pytest.raises(BudgetExceeded):
            provider.complete(messages=[{"role": "user", "content": "hi"}])
        assert provider.calls == []          # 根本沒有送出

    def test_被拒絕的呼叫不留用量紀錄(self, db):
        """沒有發生的呼叫不該出現在帳上。"""
        spend("5.00")
        before = LlmUsage.objects.count()
        with pytest.raises(BudgetExceeded):
            FakeProvider().complete(messages=[{"role": "user", "content": "hi"}])
        assert LlmUsage.objects.count() == before

    def test_額度內可正常呼叫並記帳(self, db):
        provider = FakeProvider(responses=['{"ok": true}'])
        response = provider.complete(
            messages=[{"role": "user", "content": "hi"}],
            purpose=LlmPurpose.EXTRACTION, task_name="test.task",
        )
        assert response.json() == {"ok": True}
        usage = LlmUsage.objects.get()
        assert usage.purpose == LlmPurpose.EXTRACTION
        assert usage.task_name == "test.task"
        assert usage.cost_usd > 0
        assert usage.succeeded is True

    def test_失敗的呼叫仍留紀錄(self, db):
        """供應商對已消耗的 token 仍可能計費，且失敗率本身需要被看見。"""
        provider = FakeProvider(responses=[RuntimeError("上游錯誤")])
        with pytest.raises(LlmError):
            provider.complete(messages=[{"role": "user", "content": "hi"}])
        usage = LlmUsage.objects.get()
        assert usage.succeeded is False
        assert "上游錯誤" in usage.error

    def test_未登記價格的模型不記為零成本(self, db):
        """成本為 0 的帳看起來是正確的，那才危險。"""
        provider = FakeProvider(responses=['{}'])
        provider.complete(messages=[{"role": "user", "content": "hi"}],
                          model="unknown-model-x")
        usage = LlmUsage.objects.get()
        assert usage.total_tokens > 0            # 用量有記
        assert "未登記價格" in usage.error        # 但明確標記成本未計入
