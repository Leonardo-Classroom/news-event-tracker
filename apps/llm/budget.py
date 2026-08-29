"""LLM 支出上限。

每累計花費達到設定的額度（預設 5 美元）即拒絕所有後續呼叫，
直到人明確追加額度為止。

**閘門設在 ``LLMProvider`` 內部，而非呼叫端。** 若靠每個呼叫端自行檢查，
總有一條路徑會忘記——而忘記的後果是帳單，不是錯誤訊息。

**額度追加是一筆有紀錄的動作**（``LlmBudgetGrant``），不是改設定值。
理由：日後要回答「這個月為什麼花了 40 美元」時，需要看到是誰、在何時、
為了什麼追加了額度，而設定檔的修改沒有這些資訊。
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum

logger = logging.getLogger(__name__)

__all__ = ["BudgetExceeded", "spent_usd", "approved_usd",
           "remaining_usd", "check_budget", "grant_budget"]


class BudgetExceeded(Exception):
    """累計花費已達額度上限。

    刻意不繼承任何可重試的例外類別——Celery 任務設有
    ``autoretry_for=(Exception,)``，若被當成暫時性錯誤重試，
    會在達到上限後反覆嘗試，既無意義又會拖垮佇列。
    任務需明確將本例外排除於重試之外。
    """

    def __init__(self, spent: Decimal, approved: Decimal):
        self.spent = spent
        self.approved = approved
        super().__init__(
            f"LLM 支出已達上限：已花費 US${spent:.4f} / 額度 US${approved:.4f}。"
            f"如需繼續，執行： python manage.py llm_budget --approve"
        )


def _grant_model():
    """延遲匯入，避免 app 尚未載入時觸發 Django 的 AppRegistryNotReady。"""
    from apps.llm.models import LlmBudgetGrant

    return LlmBudgetGrant


def default_increment() -> Decimal:
    return Decimal(str(getattr(settings, "LLM_BUDGET_INCREMENT_USD", "5.00")))


def spent_usd() -> Decimal:
    """累計實際花費。

    以 ``LlmUsage`` 的總和為準而非另設計數器——帳與用量記錄若分兩處，
    兩者就會不一致，而不一致時沒有辦法判斷哪一個才對。
    """
    from apps.llm.models import LlmUsage

    total = LlmUsage.objects.aggregate(total=Sum("cost_usd"))["total"]
    return total or Decimal("0")


def approved_usd() -> Decimal:
    """已核准的總額度。尚未核准過任何額度時，預設給一份初始額度。"""
    total = _grant_model().objects.aggregate(total=Sum("amount_usd"))["total"]
    if total is None:
        # 首次使用給一份初始額度，避免連第一次連通性測試都要先手動核准
        return default_increment()
    return total


def remaining_usd() -> Decimal:
    return approved_usd() - spent_usd()


def check_budget(*, estimated_usd: Decimal = Decimal("0")) -> None:
    """在呼叫前檢查額度。超出時拋 ``BudgetExceeded``。

    ``estimated_usd`` 為本次呼叫的粗估成本，用於預留餘裕。
    即使不預留，超額幅度也受限於「併發數 × 單次呼叫成本」——
    以 fetch 佇列併發 8、單次約 US$0.01 計，最壞情況約 US$0.08，
    相對 5 美元的額度可忽略。預留只是讓這個界線更緊。
    """
    spent = spent_usd()
    approved = approved_usd()
    if spent + estimated_usd >= approved:
        logger.error("LLM 支出達上限：已花費 US$%.4f / 額度 US$%.4f", spent, approved)
        raise BudgetExceeded(spent, approved)


def grant_budget(
    amount: Decimal | None = None,
    *,
    granted_by: str = "",
    note: str = "",
):
    """追加額度並留下紀錄。"""
    grant = _grant_model().objects.create(
        amount_usd=amount if amount is not None else default_increment(),
        granted_by=granted_by,
        note=note,
    )
    logger.warning(
        "LLM 額度已追加 US$%s，總額度 US$%.4f（已花費 US$%.4f）",
        grant.amount_usd, approved_usd(), spent_usd(),
    )
    return grant
