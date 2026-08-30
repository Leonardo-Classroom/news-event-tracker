"""動態 sitemap（任務 44，規格 M8）。

``lastmod`` 對應 ``last_progress_at`` 而非 ``updated_at``——後者任何
欄位變動（如內部審核備註）都會觸發，讓 sitemap 頻繁回報「有更新」，
削弱這個訊號原本要告訴 Google 的事：這個事件的**內容**是不是真的
有新進展。
"""
from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from apps.events.models import Event


class CaseSitemap(Sitemap):
    #: 事件的更新頻率天生不規律（可能沉寂數月又突然有進展），
    #: changefreq 只是給爬蟲的粗略提示，不影響實際被爬取的頻率。
    changefreq = "weekly"
    priority = 0.7

    def items(self):
        return Event.objects.public().order_by("-last_progress_at")

    def lastmod(self, event: Event):
        return event.last_progress_at

    def location(self, event: Event):
        return reverse("public:case_detail", args=[event.slug])
