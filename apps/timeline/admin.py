from django.contrib import admin

from apps.timeline.models import CausalEdge, GenerationLog, TimelineNode


class CausalEdgeInline(admin.TabularInline):
    model = CausalEdge
    fk_name = "event"
    extra = 0
    fields = ("from_node", "to_node", "kind", "confidence", "citation_document")
    raw_id_fields = ("from_node", "to_node", "citation_document")


@admin.register(TimelineNode)
class TimelineNodeAdmin(admin.ModelAdmin):
    list_display = ("event", "occurred_on", "summary", "citation_document")
    list_filter = ("event",)
    search_fields = ("summary", "event__title")
    raw_id_fields = ("event", "citation_document", "generation")


@admin.register(CausalEdge)
class CausalEdgeAdmin(admin.ModelAdmin):
    list_display = ("event", "from_node", "to_node", "kind", "confidence")
    list_filter = ("kind", "event")
    raw_id_fields = ("event", "from_node", "to_node", "citation_document", "generation")


@admin.register(GenerationLog)
class GenerationLogAdmin(admin.ModelAdmin):
    list_display = ("purpose", "model", "prompt_version", "created_at")
    list_filter = ("purpose", "model")
    readonly_fields = [f.name for f in GenerationLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
