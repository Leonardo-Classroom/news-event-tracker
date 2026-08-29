from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Django admin 保留作為原始資料的檢視與修補工具；
    # 日常使用走 apps.web 的介面（以工作為中心，而非以資料表為中心）
    path("", include("apps.web.urls")),
]
