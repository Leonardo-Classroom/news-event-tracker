from django.contrib import admin

from apps.eventbuilder.models import Conversation, EventSuggestion, Message


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "deleted_at", "updated_at")
    list_filter = ("deleted_at",)
    search_fields = ("title",)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("conversation", "role", "created_at")
    list_filter = ("role",)


@admin.register(EventSuggestion)
class EventSuggestionAdmin(admin.ModelAdmin):
    list_display = ("title", "conversation", "created_event", "deleted_at")
    list_filter = ("deleted_at",)
    search_fields = ("title",)
