from django.contrib.auth import views as auth_views
from django.urls import path

from apps.web import views

app_name = "web"

urlpatterns = [
    # 一般登入頁，供三個角色（user／admin／superadmin）共用——
    # 不能用 /admin/login/，那個表單刻意限制只有 is_staff 才能登入
    # （見 config/settings.py 的 LOGIN_URL 說明）。
    path("login/", auth_views.LoginView.as_view(template_name="web/login.html"),
        name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="web:login"), name="logout"),
    path("", views.events, name="events"),
    path("e/<slug:slug>/", views.event_detail, name="event_detail"),
    path("e/<slug:slug>/publish/", views.event_publish, name="event_publish"),
    path("e/<slug:slug>/unpublish/", views.event_unpublish, name="event_unpublish"),
    path("review/", views.review, name="review"),
    path("review/batch/", views.review_batch, name="review_batch"),
    path("review/<slug:slug>/", views.review_detail, name="review_detail"),
    path("review/<slug:slug>/decide/", views.review_decide, name="review_decide"),
    path("documents/", views.documents, name="documents"),
    path("documents/<int:pk>/", views.document_detail, name="document_detail"),
    path("pipeline/", views.pipeline, name="pipeline"),
    path("crawlers/", views.crawlers, name="crawlers"),
    path("crawlers/history/", views.crawler_history_page, name="crawler_history_page"),
    path("crawlers/<slug:slug>/run/", views.crawler_run, name="crawler_run"),
    path("crawlers/<slug:slug>/update/", views.crawler_update, name="crawler_update"),
    path("crawlers/<slug:slug>/history/", views.crawler_history, name="crawler_history"),
    path("crawlers/history/all/", views.crawler_history_all, name="crawler_history_all"),
    path("crawlers/sessions/<slug:slug>/save/", views.save_external_session, name="save_external_session"),
    path("costs/", views.costs, name="costs"),
]
