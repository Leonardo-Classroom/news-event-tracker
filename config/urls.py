from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path
from django.views.generic import TemplateView

from apps.public.sitemaps import CaseSitemap

urlpatterns = [
    path("admin/", admin.site.urls),
    # 公開網站（Scope 7）：完全不需要登入，SEO 是主要觸達管道
    # （規格 G1、M8）。掛在 /case/ 而非根目錄——根目錄已經是內部
    # 工具（apps.web），改動內部工具的網址會牽動大量既有測試與
    # 已經在用的路徑，公開網站另闢路徑風險小得多。
    path("case/", include("apps.public.urls")),
    path("sitemap.xml", sitemap, {"sitemaps": {"cases": CaseSitemap}},
        name="sitemap"),
    path("robots.txt", TemplateView.as_view(
        template_name="robots.txt", content_type="text/plain"), name="robots"),
    # Django admin 保留作為原始資料的檢視與修補工具；
    # 內部日常使用走 apps.web 的介面（以工作為中心，而非以資料表為中心，
    # 強制登入＋三級角色，見 apps/web/permissions.py）
    path("build/", include("apps.eventbuilder.urls")),
    path("", include("apps.web.urls")),
]
