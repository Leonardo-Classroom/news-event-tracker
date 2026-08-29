from django.urls import path

from apps.web import views

app_name = "web"

urlpatterns = [
    path("", views.events, name="events"),
    path("e/<slug:slug>/", views.event_detail, name="event_detail"),
    path("review/", views.review, name="review"),
    path("documents/", views.documents, name="documents"),
    path("documents/<int:pk>/", views.document_detail, name="document_detail"),
    path("pipeline/", views.pipeline, name="pipeline"),
    path("costs/", views.costs, name="costs"),
]
