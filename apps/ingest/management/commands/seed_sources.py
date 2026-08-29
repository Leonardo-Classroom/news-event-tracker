"""建立初始的採集來源。

冪等：重複執行只會更新既有來源，不會產生重複。

來源清單經實際探測驗證（2026-08-29）。兩點值得記錄：

- **聯合新聞網與中時新聞網無可用 RSS。** 聯合的 feed 回傳結構完整但
  內容全空的項目；中時的 RSS 網址全部 404。這兩家是既有語料的最大
  來源（21.9 萬與 3.2 萬篇），必須走 Playwright 爬蟲（Scope 1 任務 9），
  沿用 news report/crawler 既有的解析邏輯。
- **三立的 feed 格式損壞**（feedparser 無法辨識版本），暫不納入。
"""
from django.core.management.base import BaseCommand

from apps.ingest.models import ContentClass, Source, SourceType

# (slug, 名稱, feed 網址, 站台網址, 輪詢間隔分鐘)
RSS_SOURCES = [
    ("cna-society", "中央社－社會",
     "https://feeds.feedburner.com/rsscna/social", "https://www.cna.com.tw", 20),
    ("cna-politics", "中央社－政治",
     "https://feeds.feedburner.com/rsscna/politics", "https://www.cna.com.tw", 20),
    ("cna-mainland", "中央社－兩岸",
     "https://feeds.feedburner.com/rsscna/mainland", "https://www.cna.com.tw", 30),
    ("cna-finance", "中央社－產經",
     "https://feeds.feedburner.com/rsscna/finance", "https://www.cna.com.tw", 30),
    ("pts", "公視新聞",
     "https://news.pts.org.tw/xml/newsfeed.xml", "https://news.pts.org.tw", 20),
    ("ltn", "自由時報",
     "https://news.ltn.com.tw/rss/all.xml", "https://news.ltn.com.tw", 20),
    ("ettoday", "ETtoday",
     "https://feeds.feedburner.com/ettoday/news", "https://www.ettoday.net", 20),
    ("mirrormedia", "鏡週刊",
     "https://www.mirrormedia.mg/rss/news.xml", "https://www.mirrormedia.mg", 60),
    # 深度調查報導，對重大案件的長期追蹤特別有價值
    ("twreporter", "報導者",
     "https://www.twreporter.org/a/rss2.xml", "https://www.twreporter.org", 180),
]

# 無 RSS，需 Playwright（Scope 1 任務 9）。先建立來源記錄但預設停用，
# 待 browser adapter 完成後啟用。
SCRAPE_SOURCES = [
    ("udn", "聯合新聞網", "https://udn.com", 30),
    ("chinatimes", "中時新聞網", "https://www.chinatimes.com", 30),
]


class Command(BaseCommand):
    help = "建立或更新初始採集來源（冪等）"

    def add_arguments(self, parser):
        parser.add_argument("--enable-scrape", action="store_true",
                            help="一併啟用需 Playwright 的來源")

    def handle(self, *args, **options):
        created_total = updated_total = 0

        for slug, name, feed_url, base_url, interval in RSS_SOURCES:
            _, created = Source.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "type": SourceType.NEWS_RSS,
                    "base_url": base_url,
                    "feed_url": feed_url,
                    "content_class": ContentClass.COPYRIGHTED,
                    "poll_interval_minutes": interval,
                    "enabled": True,
                },
            )
            created_total += created
            updated_total += not created
            self.stdout.write(f"  {'＋' if created else '　'} {slug:<14} {name}")

        for slug, name, base_url, interval in SCRAPE_SOURCES:
            _, created = Source.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "type": SourceType.NEWS_SCRAPE,
                    "base_url": base_url,
                    "feed_url": "",
                    "content_class": ContentClass.COPYRIGHTED,
                    "poll_interval_minutes": interval,
                    "enabled": options["enable_scrape"],
                },
            )
            created_total += created
            updated_total += not created
            state = "啟用" if options["enable_scrape"] else "停用（待 Playwright adapter）"
            self.stdout.write(f"  {'＋' if created else '　'} {slug:<14} {name}　{state}")

        self.stdout.write(self.style.SUCCESS(
            f"\n完成：新增 {created_total}、更新 {updated_total}，"
            f"共 {Source.objects.count()} 個來源"
        ))
