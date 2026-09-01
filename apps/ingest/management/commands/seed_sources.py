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

# 2026-09-01：RSS 已知的問題不只聯合／中時（feed 全空／404）——多數
# RSS 只給近況（實測 20–50 則、更新不穩），且完全無法回補歷史。
# 任務 61 已經替這些站台的清單／JSON API 建好 ArchiveSpec
# （apps.ingest.archives）並在歷史回補中實測過，即時輪詢改直接
# 沿用同一份規格抓「最新」（``services.ingest_source`` 的
# ``archive_spec`` 分支），不再靠 RSS。逐一以 HttpFetcher 對真實站台
# 驗證過（pts/ettoday/ltn/twreporter/cna×4 皆 200 且能正確解析）。
#
# 2026-09-01 起連鏡週刊也不用 RSS 了（改走 sitemap，見下方
# ARCHIVE_SPEC_SOURCES），這裡暫時沒有純 RSS 的來源；保留清單與流程
# 是因為將來新增來源時未必每家都有可爬的清單頁。
RSS_SOURCES: list[tuple[str, str, str, str, int]] = []

# 無 RSS，需 Playwright（Scope 1 任務 9）。先建立來源記錄但預設停用，
# 待 browser adapter 完成後啟用。
SCRAPE_SOURCES = [
    ("udn", "聯合新聞網", "https://udn.com", 30),
    ("chinatimes", "中時新聞網", "https://www.chinatimes.com", 30),
]

# 無 RSS，但清單頁可純 HTTP（/livenews 首頁被 Cloudflare 擋，
# 分類頁 /livenews/ctee 實測 200）。走 fetch 佇列。
HTTP_LIST_SOURCES = [
    ("ctee", "工商時報", "https://www.ctee.com.tw",
     "https://www.ctee.com.tw/livenews/ctee", 60),
]

# 有 ArchiveSpec 的來源：即時輪詢走 archive_spec 而非 feed_url，
# 因此不設 feed_url——網址單一真相留在 ARCHIVE_SPECS，避免這裡
# 和那邊各存一份、日後漂移。
ARCHIVE_SPEC_SOURCES = [
    ("cna-society", "中央社－社會", "https://www.cna.com.tw", 20),
    ("cna-politics", "中央社－政治", "https://www.cna.com.tw", 20),
    ("cna-mainland", "中央社－兩岸", "https://www.cna.com.tw", 30),
    ("cna-finance", "中央社－產經", "https://www.cna.com.tw", 30),
    ("pts", "公視新聞", "https://news.pts.org.tw", 20),
    ("ltn", "自由時報", "https://news.ltn.com.tw", 20),
    ("ettoday", "ETtoday", "https://www.ettoday.net", 20),
    # 深度調查報導，對重大案件的長期追蹤特別有價值
    ("twreporter", "報導者", "https://www.twreporter.org", 180),
    # SPA，API 逾時，但 sitemap 純 HTTP 可取且比 RSS 多兩個數量級
    ("mirrormedia", "鏡週刊", "https://www.mirrormedia.mg", 60),
]


class Command(BaseCommand):
    help = "建立或更新初始採集來源（冪等）"

    def add_arguments(self, parser):
        parser.add_argument("--enable-scrape", action="store_true",
                            help="一併啟用需 Playwright 的來源")

    def _upsert(self, slug, *, name, type_, base_url, feed_url, interval,
                enabled_on_create):
        """``enabled`` 只在建立時套用，更新時絕不覆寫。

        2026-09-01 踩過的坑：先前 ``enabled`` 放在每次都套用的
        ``defaults`` 裡，導致重跑這個「冪等」指令會把已手動啟用的
        來源（聯合、中時）打回預設的停用狀態，兩個既有語料最大的
        來源因此被誤停用。用 Django 5 的 ``create_defaults`` 把
        ``enabled`` 隔到只在建立時生效，修掉這個根因，而不是每次
        跑完再手動改回來。
        """
        defaults = {
            "name": name, "type": type_, "base_url": base_url,
            "feed_url": feed_url, "content_class": ContentClass.COPYRIGHTED,
            "poll_interval_minutes": interval,
        }
        return Source.objects.update_or_create(
            slug=slug, defaults=defaults,
            create_defaults={**defaults, "enabled": enabled_on_create},
        )

    def handle(self, *args, **options):
        created_total = updated_total = 0

        for slug, name, feed_url, base_url, interval in RSS_SOURCES:
            _, created = self._upsert(
                slug, name=name, type_=SourceType.NEWS_RSS,
                base_url=base_url, feed_url=feed_url, interval=interval,
                enabled_on_create=True)
            created_total += created
            updated_total += not created
            self.stdout.write(f"  {'＋' if created else '　'} {slug:<14} {name}")

        for slug, name, base_url, interval in SCRAPE_SOURCES:
            source, created = self._upsert(
                slug, name=name, type_=SourceType.NEWS_SCRAPE,
                base_url=base_url, feed_url="", interval=interval,
                enabled_on_create=options["enable_scrape"])
            created_total += created
            updated_total += not created
            # 印目前資料庫裡的實際狀態，不是這次執行的旗標——
            # enabled 只在建立時套用，重跑不代表現在的狀態跟旗標一致。
            state = "啟用" if source.enabled else "停用（待 Playwright adapter）"
            self.stdout.write(f"  {'＋' if created else '　'} {slug:<14} {name}　{state}")

        for slug, name, base_url, feed_url, interval in HTTP_LIST_SOURCES:
            _, created = self._upsert(
                slug, name=name, type_=SourceType.NEWS_SCRAPE,
                base_url=base_url, feed_url=feed_url, interval=interval,
                enabled_on_create=True)
            created_total += created
            updated_total += not created
            self.stdout.write(f"  {'＋' if created else '　'} {slug:<14} {name}　啟用（HTTP 清單）")

        for slug, name, base_url, interval in ARCHIVE_SPEC_SOURCES:
            _, created = self._upsert(
                slug, name=name, type_=SourceType.NEWS_SCRAPE,
                base_url=base_url, feed_url="", interval=interval,
                enabled_on_create=True)
            created_total += created
            updated_total += not created
            self.stdout.write(f"  {'＋' if created else '　'} {slug:<14} {name}　啟用（archive spec）")

        self.stdout.write(self.style.SUCCESS(
            f"\n完成：新增 {created_total}、更新 {updated_total}，"
            f"共 {Source.objects.count()} 個來源"
        ))
