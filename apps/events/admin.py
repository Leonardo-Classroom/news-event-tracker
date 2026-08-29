"""事件的後台。

**內部後台，非對外網站。** 對外公開頁面在 Scope 7，必須先完成
Scope 6 的安全閘門。

設計重點是讓「事件的狀態與內容」一眼可見——事件是本系統的核心，
而它的正確性主要靠人看出來，不是靠測試。
"""
from django.contrib import admin
from django.db.models import Count, Max
from django.utils import timezone
from django.utils.html import format_html

from apps.events.models import (
    DORMANT_AFTER_DAYS, Event, EventAlias, EventDocument, EventMergeLog,
    EventStatus, EventVisibility,
)


class EventDocumentInline(admin.TabularInline):
    model = EventDocument
    extra = 0
    fields = ("document_link", "method", "relevance_score", "reason", "created_at")
    readonly_fields = ("document_link", "created_at")
    raw_id_fields = ("document",)
    ordering = ("-document__published_at",)

    @admin.display(description="文件")
    def document_link(self, obj):
        doc = obj.document
        date = doc.published_at.strftime("%Y-%m-%d") if doc.published_at else "—"
        return format_html(
            '<a href="/admin/ingest/document/{}/change/">{}</a>　'
            '<small style="color:#888">{}｜{}</small>',
            doc.pk, doc.title[:56], date, doc.source.slug,
        )


class EventAliasInline(admin.TabularInline):
    model = EventAlias
    extra = 1


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "status_display", "visibility_display", "domain",
                    "risk_tier", "document_count", "progress_display", "terms_preview")
    list_filter = ("status", "visibility", "domain", "risk_tier")
    search_fields = ("title", "slug", "aliases__name")
    inlines = [EventAliasInline, EventDocumentInline]
    readonly_fields = ("created_at", "updated_at", "first_seen_at",
                       "core_terms", "case_numbers")
    prepopulated_fields = {}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_docs=Count("event_documents"))

    @admin.display(description="狀態", ordering="status")
    def status_display(self, obj):
        colours = {
            EventStatus.ACTIVE: "#1a7f37",
            EventStatus.DORMANT: "#9a6700",
            EventStatus.CLOSED: "#57606a",
            EventStatus.DRAFT: "#0969da",
            EventStatus.CANDIDATE: "#888",
            EventStatus.REJECTED: "#cf222e",
        }
        return format_html('<span style="color:{}">{}</span>',
                           colours.get(obj.status, "#000"), obj.get_status_display())

    @admin.display(description="公開", ordering="visibility")
    def visibility_display(self, obj):
        if obj.visibility == EventVisibility.PUBLIC:
            return format_html('<span style="color:#1a7f37">已公開</span>')
        return format_html('<span style="color:#888">未公開</span>')

    @admin.display(description="文件", ordering="_docs")
    def document_count(self, obj):
        return obj._docs

    @admin.display(description="最後進展", ordering="last_progress_at")
    def progress_display(self, obj):
        """同時顯示距今天數——沉寂判定以此為準，是事件是否被追蹤的關鍵。"""
        if not obj.last_progress_at:
            return "—"
        days = (timezone.now() - obj.last_progress_at).days
        colour = "#cf222e" if days >= DORMANT_AFTER_DAYS else "#1a7f37"
        return format_html('{}　<span style="color:{}">（{} 天前）</span>',
                           obj.last_progress_at.strftime("%Y-%m-%d"), colour, days)

    @admin.display(description="識別特徵")
    def terms_preview(self, obj):
        terms = obj.core_terms or []
        shown = "、".join(terms[:4])
        more = f" +{len(terms)-4}" if len(terms) > 4 else ""
        return format_html('<small>{}{}</small>', shown or "—", more)


@admin.register(EventMergeLog)
class EventMergeLogAdmin(admin.ModelAdmin):
    """合併紀錄唯讀——這是稽核軌跡，事後修改會破壞它存在的意義。"""

    list_display = ("source_title", "source_slug", "target_event", "document_count",
                    "merged_at")
    readonly_fields = [f.name for f in EventMergeLog._meta.fields]
    search_fields = ("source_title", "source_slug", "target_event__title")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
