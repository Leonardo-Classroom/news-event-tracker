"""L1 抽取結果的後台。

主要用途是**人工檢視抽取品質**——規格 §8.3 要求時間線只記錄可查證的
事實，而抽取是那些事實的來源。失敗的抽取也保留紀錄，因為失敗率本身
是需要被看見的訊號。
"""
import json

from django.contrib import admin
from django.utils.html import format_html

from apps.extract.models import Extraction


@admin.register(Extraction)
class ExtractionAdmin(admin.ModelAdmin):
    date_hierarchy = "created_at"
    list_display = ("created_at", "document_link", "schema_kind", "model",
                    "prompt_version", "status_display", "summary_preview")
    list_filter = ("schema_kind", "succeeded", "prompt_version", "model")
    list_select_related = ("document",)
    readonly_fields = tuple(f.name for f in Extraction._meta.fields) + ("payload_pretty",)
    raw_id_fields = ("document",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="文件")
    def document_link(self, obj):
        return format_html('<a href="/admin/ingest/document/{}/change/">{}</a>',
                           obj.document_id, obj.document.title[:56])

    @admin.display(description="狀態", ordering="succeeded")
    def status_display(self, obj):
        if obj.succeeded:
            return format_html('<span style="color:#1a7f37">✓</span>')
        return format_html('<span style="color:#cf222e" title="{}">✗</span>',
                           (obj.error or "")[:200])

    @admin.display(description="摘要")
    def summary_preview(self, obj):
        return format_html('<small>{}</small>',
                           (obj.payload or {}).get("summary", "")[:60] or "—")

    @admin.display(description="抽取結果")
    def payload_pretty(self, obj):
        return format_html("<pre style='white-space:pre-wrap;max-width:900px'>{}</pre>",
                           json.dumps(obj.payload, ensure_ascii=False, indent=2))
