from django.urls import path

from apps.public import views

app_name = "public"

urlpatterns = [
    path("", views.case_list, name="case_list"),
    path("<slug:slug>/", views.case_detail, name="case_detail"),
]
