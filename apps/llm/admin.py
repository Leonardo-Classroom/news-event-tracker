"""LLM 用量的後台檢視。

設計重點不是列出每一筆呼叫，而是回答三個問題：

1. **錢花在哪一層？** ADR-0009 的分級策略（L1 用便宜檔、L3/L4 用旗艦檔）
   是否奏效，看 purpose 維度的成本分布就知道。
2. **context caching 有沒有作用？** cache 命中的輸入價格低約 30 倍，
   那是選用 DeepSeek 的實質理由。命中率長期偏低就該檢討 prompt 結構。
3. **有多少花在尖峰？** 離峰價格減半，批次作業若大量落在尖峰就是白花錢。
"""
from decimal import Decimal

from django.contrib import admin
from django.db.models import Avg, Count, Q, Sum
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.llm.models import LlmBudgetGrant, LlmUsage


class PeakFilter(admin.SimpleListFilter):
    title = "時段"
    parameter_name = "peak"

    def lookups(self, request, model_admin):
        return (("1", "尖峰（原價）"), ("0", "離峰（半價）"))

    def queryset(self, request, queryset):
        if self.value() in ("0", "1"):
            return queryset.filter(peak=self.value() == "1")
        return queryset


@admin.register(LlmUsage)
class LlmUsageAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = (
        "created_at", "purpose", "model", "tokens_display",
        "cache_rate_display", "peak_display", "cost_display", "status_display",
    )
    list_filter = ("purpose", "model", "succeeded", PeakFilter)
    search_fields = ("task_name", "error")
    readonly_fields = tuple(f.name for f in LlmUsage._meta.fields)
    list_select_related = ("document",)

    # 用量記錄是帳，不該由人編輯或刪除
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # ---------------------------------------------------------------- 欄位

    @admin.display(description="tokens（入／出）", ordering="total_tokens")
    def tokens_display(self, obj):
        return format_html(
            "{}&nbsp;/&nbsp;{}",
            f"{obj.input_tokens:,}", f"{obj.output_tokens:,}",
        )

    @admin.display(description="cache 命中")
    def cache_rate_display(self, obj):
        rate = obj.cache_hit_rate
        colour = "#1a7f37" if rate >= 0.5 else ("#9a6700" if rate > 0 else "#cf222e")
        # format_html 會先把參數轉成 SafeString，之後 {:.0%} 這類格式指令就失效，
        # 因此數值必須先格式化好再傳入。
        return format_html('<span style="color:{}">{}</span>', colour, f"{rate:.0%}")

    @admin.display(description="時段", ordering="peak")
    def peak_display(self, obj):
        if obj.peak:
            return mark_safe('<span style="color:#cf222e">尖峰</span>')
        return mark_safe('<span style="color:#1a7f37">離峰</span>')

    @admin.display(description="成本", ordering="cost_ntd")
    def cost_display(self, obj):
        return format_html(
            "NT${}<br><small style='color:#888'>US${}</small>",
            f"{obj.cost_ntd:.4f}", f"{obj.cost_usd:.6f}",
        )

    @admin.display(description="狀態", ordering="succeeded")
    def status_display(self, obj):
        if obj.succeeded:
            return format_html('<span style="color:#1a7f37">✓ {}ms</span>',
                               obj.duration_ms)
        return format_html('<span style="color:#cf222e" title="{}">✗ 失敗</span>',
                           (obj.error or "")[:200])

    # ---------------------------------------------------------------- 彙總

    def changelist_view(self, request, extra_context=None):
        """在列表頁上方顯示彙總，讓成本結構一眼可見。

        彙總套用目前的篩選條件，因此可以先篩時間或用途再看小計。
        """
        response = super().changelist_view(request, extra_context)
        try:
            queryset = response.context_data["cl"].queryset
        except (AttributeError, KeyError):
            return response

        totals = queryset.aggregate(
            calls=Count("id"),
            usd=Sum("cost_usd"), ntd=Sum("cost_ntd"),
            tokens=Sum("total_tokens"),
            cached=Sum("cached_input_tokens"),
            uncached=Sum("uncached_input_tokens"),
            output=Sum("output_tokens"),
            avg_ms=Avg("duration_ms"),
            failures=Count("id", filter=Q(succeeded=False)),
        )

        by_purpose = list(
            queryset.values("purpose")
            .annotate(calls=Count("id"), ntd=Sum("cost_ntd"),
                      usd=Sum("cost_usd"), tokens=Sum("total_tokens"))
            .order_by("-ntd")
        )
        by_model = list(
            queryset.values("model")
            .annotate(calls=Count("id"), ntd=Sum("cost_ntd"),
                      usd=Sum("cost_usd"), tokens=Sum("total_tokens"))
            .order_by("-ntd")
        )
        by_peak = list(
            queryset.values("peak")
            .annotate(calls=Count("id"), ntd=Sum("cost_ntd"))
            .order_by("peak")
        )

        cached = totals["cached"] or 0
        uncached = totals["uncached"] or 0
        input_total = cached + uncached
        labels = dict(LlmUsage._meta.get_field("purpose").choices)

        response.context_data["usage_summary"] = {
            "totals": totals,
            # 直接以百分比傳出，模板不再換算——換算散落兩處是顯示錯誤的常見來源
            "cache_hit_rate": (cached / input_total * 100) if input_total else 0,
            "by_purpose": [
                {**row, "label": labels.get(row["purpose"], row["purpose"])}
                for row in by_purpose
            ],
            "by_model": by_model,
            "by_peak": [
                {**row, "label": "尖峰" if row["peak"] else "離峰"}
                for row in by_peak
            ],
        }
        return response

    change_list_template = "admin/llm/llmusage/change_list.html"


@admin.register(LlmBudgetGrant)
class LlmBudgetGrantAdmin(admin.ModelAdmin):
    """額度追加紀錄。

    可在後台新增——這是「核准」這個動作本身，需要有人明確執行。
    但不可修改或刪除：追加是既成事實，事後改動會讓帳目失去意義。
    """

    list_display = ("created_at", "amount_usd", "granted_by", "note", "running_total")
    readonly_fields = ("created_at",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="當時累計額度")
    def running_total(self, obj):
        total = (LlmBudgetGrant.objects
                 .filter(created_at__lte=obj.created_at)
                 .aggregate(t=Sum("amount_usd"))["t"] or Decimal("0"))
        return f"US${total:.2f}"

    def changelist_view(self, request, extra_context=None):
        from apps.llm.budget import approved_usd, remaining_usd, spent_usd

        spent, approved, left = spent_usd(), approved_usd(), remaining_usd()
        extra_context = extra_context or {}
        extra_context["title"] = (
            f"LLM 額度追加　—　已花費 US${spent:.4f} / 額度 US${approved:.2f}"
            f"　剩餘 US${left:.4f}"
        )
        return super().changelist_view(request, extra_context)
