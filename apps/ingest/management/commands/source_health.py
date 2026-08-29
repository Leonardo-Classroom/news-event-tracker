"""來源健康度報表。

fixture 測試只能偵測「解析程式被改壞」，**偵測不到「網站改版了」**。
後者只會表現為連續失敗計數上升，而那個數字若沒有人看，等於不存在。

這支指令是那個數字的查詢介面。設計為可直接用於監測告警：
有來源處於不健康狀態時以非零碼結束。
"""
import datetime as dt

from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone

from apps.ingest.models import Source
from apps.ingest.services import FAILURE_THRESHOLD, effective_interval_minutes

#: 超過此時間未成功即視為停滯，即使尚未累計到失敗門檻。
#: 用意是抓出「一直回傳成功但沒有新內容」以外的靜默失效。
STALE_MULTIPLIER = 4


class Command(BaseCommand):
    help = "顯示各採集來源的健康狀態；有來源不健康時以非零碼結束"

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true",
                            help="只輸出有問題的來源")

    def handle(self, *args, **options):
        now = timezone.now()
        sources = (Source.objects.filter(enabled=True)
                   .annotate(
                       docs=Count("documents"),
                       with_body=Count("documents", filter=~Q(documents__raw_body="")),
                   ).order_by("-consecutive_failures", "slug"))

        unhealthy = []
        rows = []

        for source in sources:
            issues = []
            if source.consecutive_failures >= FAILURE_THRESHOLD:
                issues.append(f"連續失敗 {source.consecutive_failures}（已降頻至 "
                              f"{effective_interval_minutes(source)} 分）")
            elif source.consecutive_failures:
                issues.append(f"失敗 {source.consecutive_failures}")

            stale_after = dt.timedelta(
                minutes=source.poll_interval_minutes * STALE_MULTIPLIER)
            if source.last_success_at is None:
                issues.append("從未成功")
            elif now - source.last_success_at > stale_after:
                hours = (now - source.last_success_at).total_seconds() / 3600
                issues.append(f"已 {hours:.1f} 小時未成功")

            if issues:
                unhealthy.append(source.slug)

            rows.append((source, issues))

        header = f"{'狀態':<4}{'來源':<15}{'文件':>6}{'有內文':>7}{'最後成功':<18} 問題"
        if not options["quiet"]:
            self.stdout.write(header)
            self.stdout.write("-" * 78)

        for source, issues in rows:
            if options["quiet"] and not issues:
                continue
            mark = "✗" if issues else "✓"
            last = (source.last_success_at.strftime("%m-%d %H:%M")
                    if source.last_success_at else "—")
            line = (f"{mark:<4}{source.slug:<15}{source.docs:>6}{source.with_body:>7}"
                    f"  {last:<16} {'；'.join(issues)}")
            style = self.style.ERROR if issues else self.style.SUCCESS
            self.stdout.write(style(line))

        total = len(rows)
        if unhealthy:
            self.stdout.write(self.style.ERROR(
                f"\n{len(unhealthy)}/{total} 個來源不健康： {', '.join(unhealthy)}"))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(f"\n全部 {total} 個來源健康"))
