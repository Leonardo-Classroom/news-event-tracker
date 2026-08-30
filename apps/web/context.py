"""側邊欄的共用數字。

放在 context processor 而非每個 view 各自計算——這些數字在每一頁都要
顯示，散落各處必然有某一頁忘記更新而顯示過期值。
"""
from decimal import Decimal


def sidebar(request):
    if request.path.startswith("/admin") or not request.user.is_authenticated:
        # 未登入時（如 /login/ 頁）側欄本來就不會顯示，算這些數字
        # 只是白白多跑幾次查詢。
        return {}
    from apps.events.models import Event, EventStatus
    from apps.ingest.models import Document, Source
    from apps.ingest.services import FAILURE_THRESHOLD
    from apps.llm.budget import spent_usd

    return {
        "nav_counts": {
            "tracking": Event.objects.tracking().count(),
            "review": Event.objects.filter(
                status__in=[EventStatus.CANDIDATE, EventStatus.DRAFT]).count(),
            "documents": f"{Document.objects.count():,}",
            "unhealthy": Source.objects.filter(
                enabled=True,
                consecutive_failures__gte=FAILURE_THRESHOLD).count(),
            "spent": f"{spent_usd():.2f}",
        }
    }
