"""採集層的後台。

**這是內部後台，不是對外網站。** 對外的公開頁面在 Scope 7，且必須
先完成 Scope 6 的安全閘門——在閘門完成前對外公開涉及具名個人的
未定案司法內容是實質的法律風險。此處僅供維運者瀏覽自有資料。

效能是主要設計考量：``Document`` 有 23.8 萬列。因此
- 搜尋走 bigram FTS 的 GIN 索引，而非 ``icontains``（後者會全表掃描）
- 不對高基數欄位設 ``list_filter``
- 列表不載入 ``raw_body``
"""
from django.contrib import admin
from django.db.models import Count, Func, IntegerField, Q
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.ingest.models import ContentClass, Document, ExternalSession, Source
from apps.ingest.services import FAILURE_THRESHOLD, effective_interval_minutes


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "type", "enabled", "health_display",
                    "interval_display", "document_count", "last_success_at")
    list_filter = ("type", "enabled", "content_class")
    search_fields = ("slug", "name")
    readonly_fields = ("last_success_at", "last_attempt_at",
                       "consecutive_failures", "last_error",
                       "created_at", "updated_at")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_docs=Count("documents"))

    @admin.display(description="文件數", ordering="_docs")
    def document_count(self, obj):
        return f"{obj._docs:,}"

    @admin.display(description="健康度", ordering="consecutive_failures")
    def health_display(self, obj):
        if obj.consecutive_failures >= FAILURE_THRESHOLD:
            return format_html(
                '<span style="color:#cf222e">✗ 連續失敗 {}</span>',
                obj.consecutive_failures)
        if obj.consecutive_failures:
            return format_html('<span style="color:#9a6700">⚠ {}</span>',
                               obj.consecutive_failures)
        if obj.last_success_at is None:
            return format_html('<span style="color:#888">— 從未成功</span>')
        return format_html('<span style="color:#1a7f37">✓</span>')

    @admin.display(description="輪詢間隔")
    def interval_display(self, obj):
        effective = effective_interval_minutes(obj)
        if effective != obj.poll_interval_minutes:
            # 降頻是健康度監測的實際效果，應該看得見
            return format_html('<span style="color:#9a6700">{} 分（已降頻）</span>',
                               effective)
        return f"{effective} 分"


class RelevanceFilter(admin.SimpleListFilter):
    title = "相關性"
    parameter_name = "rel"

    def lookups(self, request, model_admin):
        return (("1", "相關"), ("0", "不相關"), ("none", "尚未評估"))

    def queryset(self, request, queryset):
        value = self.value()
        if value == "1":
            return queryset.filter(relevant=True)
        if value == "0":
            return queryset.filter(relevant=False)
        if value == "none":
            return queryset.filter(relevant__isnull=True)
        return queryset


class VectorFilter(admin.SimpleListFilter):
    title = "向量"
    parameter_name = "vec"

    def lookups(self, request, model_admin):
        return (("1", "已向量化"), ("0", "未向量化"))

    def queryset(self, request, queryset):
        if self.value() == "1":
            return queryset.exclude(embedding=None)
        if self.value() == "0":
            return queryset.filter(embedding=None)
        return queryset


class IdentifierFilter(admin.SimpleListFilter):
    title = "識別碼"
    parameter_name = "ident"

    def lookups(self, request, model_admin):
        return (("case", "有案號"), ("tax", "有統編"))

    def queryset(self, request, queryset):
        if self.value() == "case":
            return queryset.exclude(case_numbers=[])
        if self.value() == "tax":
            return queryset.exclude(tax_ids=[])
        return queryset


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    date_hierarchy = "published_at"
    list_display = ("published_at_display", "source_slug", "title_link",
                    "flags_display", "body_length")
    list_filter = (RelevanceFilter, VectorFilter, IdentifierFilter,
                   "content_class", "source")
    list_select_related = ("source", "canonical_of")
    readonly_fields = ("simhash", "search_text", "case_numbers", "tax_ids",
                       "relevance_signals", "embedding_version", "fetched_at")
    exclude = ("embedding", "search_vector")   # 1024 維向量不適合在表單呈現
    raw_id_fields = ("canonical_of", "source")

    def get_queryset(self, request):
        # 列表不需要內文與向量，載入它們會讓 23.8 萬列的查詢變慢。
        # 但仍要顯示「有無內文」，因此以 annotate 取長度——
        # 若在 display 方法中逐列查詢，會是 N+1（每頁 100 次額外查詢）。
        return (super().get_queryset(request)
                .defer("raw_body", "embedding", "search_text")
                .annotate(_body_len=Func("raw_body", function="length",
                                         output_field=IntegerField())))

    def get_search_results(self, request, queryset, search_term):
        """走 bigram FTS 的 GIN 索引，而非 icontains。

        ``icontains`` 在 23.8 萬列上是全表掃描，實測需數十秒；
        FTS 走索引則在 22ms 內完成（ADR-0001）。
        """
        if not search_term:
            return queryset, False
        from apps.retrieval.keyword import BigramFtsBackend

        return BigramFtsBackend().search(queryset, search_term), False

    @admin.display(description="發布時間", ordering="published_at")
    def published_at_display(self, obj):
        return obj.published_at.strftime("%Y-%m-%d %H:%M") if obj.published_at else "—"

    @admin.display(description="來源", ordering="source__slug")
    def source_slug(self, obj):
        return obj.source.slug

    @admin.display(description="標題", ordering="title")
    def title_link(self, obj):
        return format_html('<a href="{}" target="_blank" title="開啟原文">{}</a>',
                           obj.url, obj.title[:70])

    @admin.display(description="標記")
    def flags_display(self, obj):
        parts = []
        if obj.relevant is True:
            parts.append('<span style="color:#1a7f37">相關</span>')
        elif obj.relevant is False:
            parts.append('<span style="color:#888">不相關</span>')
        if obj.case_numbers:
            parts.append(f'<span style="color:#0969da">案號 {len(obj.case_numbers)}</span>')
        if obj.canonical_of_id:
            parts.append('<span style="color:#9a6700">轉載</span>')
        if obj.content_class == ContentClass.PUBLIC_RECORD:
            parts.append('<span style="color:#8250df">公文</span>')
        return mark_safe("&nbsp;".join(parts) or "—")

    @admin.display(description="內文", ordering="_body_len")
    def body_length(self, obj):
        length = getattr(obj, "_body_len", 0) or 0
        return f"{length:,} 字" if length else "—"


@admin.register(ExternalSession)
class ExternalSessionAdmin(admin.ModelAdmin):
    """cookie_header 不進 list_display——那是憑證，不該在列表頁一覽無遺。"""

    list_display = ("name", "slug", "is_set", "captured_at", "likely_expired")
    readonly_fields = ("captured_at", "updated_at")
