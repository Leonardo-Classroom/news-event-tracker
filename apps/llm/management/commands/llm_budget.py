"""LLM 預算的查詢與追加。

額度追加是一筆有紀錄的動作，而非修改設定值——日後要回答
「這個月為什麼花了 40 美元」時，需要看到是誰、何時、為何追加。
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Count, Sum

from apps.llm.budget import (
    approved_usd, default_increment, grant_budget, remaining_usd, spent_usd,
)
from apps.llm.models import LlmBudgetGrant, LlmPurpose, LlmUsage


class Command(BaseCommand):
    help = "查詢 LLM 支出與額度；--approve 追加額度"

    def add_arguments(self, parser):
        parser.add_argument("--approve", action="store_true",
                            help=f"追加額度（預設 US${default_increment()}）")
        parser.add_argument("--amount", type=str, default="",
                            help="指定追加金額（美元）")
        parser.add_argument("--note", default="", help="追加原因")

    def handle(self, *args, **options):
        if options["approve"]:
            amount = Decimal(options["amount"]) if options["amount"] else None
            grant = grant_budget(amount, granted_by="cli", note=options["note"])
            self.stdout.write(self.style.WARNING(
                f"已追加額度 US${grant.amount_usd}"))

        spent, approved = spent_usd(), approved_usd()
        remaining = remaining_usd()
        rate = Decimal("32.5")

        self.stdout.write("\n=== LLM 支出 ===")
        self.stdout.write(f"  已花費   US${spent:>10.4f}   (NT${spent*rate:>9.2f})")
        self.stdout.write(f"  額度     US${approved:>10.4f}   (NT${approved*rate:>9.2f})")
        style = self.style.SUCCESS if remaining > 0 else self.style.ERROR
        self.stdout.write(style(
            f"  剩餘     US${remaining:>10.4f}   (NT${remaining*rate:>9.2f})"))
        if remaining <= 0:
            self.stdout.write(self.style.ERROR(
                "\n  ⚠ 額度已用盡，所有 LLM 呼叫將被拒絕。"
                "\n    追加： python manage.py llm_budget --approve"))

        rows = (LlmUsage.objects.values("purpose")
                .annotate(n=Count("id"), usd=Sum("cost_usd")).order_by("-usd"))
        if rows:
            labels = dict(LlmPurpose.choices)
            self.stdout.write("\n  依用途：")
            for r in rows:
                self.stdout.write(
                    f"    {labels.get(r['purpose'], r['purpose']):<16}"
                    f"{r['n']:>6} 次  US${r['usd'] or 0:>9.4f}")

        grants = LlmBudgetGrant.objects.all()[:5]
        if grants:
            self.stdout.write("\n  額度追加紀錄：")
            for g in grants:
                note = f"  {g.note}" if g.note else ""
                self.stdout.write(f"    {g.created_at:%Y-%m-%d %H:%M}  "
                                  f"+US${g.amount_usd}{note}")
