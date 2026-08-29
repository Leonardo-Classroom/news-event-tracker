"""自動歸屬命令列介面（任務 23）。

CLI 而非直接呼叫 ``auto_assign()``，理由是這個作業會呼叫 LLM 數十次、
跑數分鐘——必須能背景執行（``scripts/run_job.sh``），而背景執行需要
一個可以用 ``python manage.py`` 啟動的進入點。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.events.assignment import auto_assign
from apps.events.models import Event, EventStatus
from apps.llm.budget import remaining_usd, spent_usd


class Command(BaseCommand):
    help = "對指定事件執行自動歸屬（識別碼優先 + LLM 判定）"

    def add_arguments(self, parser):
        parser.add_argument("--slugs", nargs="*", default=None,
                            help="只處理指定 slug 的事件，預設全部 candidate/draft/active/dormant")
        parser.add_argument("--max-llm-calls", type=int, default=None)

    def handle(self, *args, **options):
        queryset = Event.objects.exclude(status=EventStatus.REJECTED)
        if options["slugs"]:
            queryset = queryset.filter(slug__in=options["slugs"])
        events = list(queryset)
        if not events:
            self.stderr.write("沒有符合條件的事件")
            return

        self.stdout.write(f"開始處理 {len(events)} 個事件："
                          f"{', '.join(e.slug for e in events)}")
        before = spent_usd()

        results = auto_assign(events, max_llm_calls=options["max_llm_calls"])

        self.stdout.write(self.style.SUCCESS(
            f"完成，新建立 {len(results)} 筆歸屬"
            f"（本次花費 US${spent_usd() - before:.4f}，"
            f"剩餘 US${remaining_usd():.4f}）"
        ))
        from collections import Counter
        by_event = Counter(r.event.slug for r in results)
        for slug, n in by_event.most_common():
            self.stdout.write(f"  {slug:<20}{n}")
