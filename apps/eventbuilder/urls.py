from django.urls import path

from apps.eventbuilder import views

app_name = "eventbuilder"

urlpatterns = [
    path("", views.builder, name="builder"),
    path("new/", views.new_conversation, name="new"),
    path("trash/", views.trash, name="trash"),
    path("c/<int:pk>/", views.builder, name="conversation"),
    path("c/<int:pk>/send/", views.post_message, name="send"),
    path("c/<int:pk>/rename/", views.rename_conversation, name="rename"),
    path("c/<int:pk>/trash/", views.trash_conversation, name="trash_conversation"),
    path("c/<int:pk>/restore/", views.restore_conversation, name="restore"),
    path("c/<int:pk>/delete/", views.delete_forever, name="delete_forever"),
    path("s/<int:pk>/track/", views.track_suggestion, name="track_suggestion"),
    path("s/<int:pk>/delete/", views.delete_suggestion, name="delete_suggestion"),
]
