"""側邊欄的共用數字。

放在 context processor 而非每個 view 各自計算——這些數字在每一頁都要
顯示，散落各處必然有某一頁忘記更新而顯示過期值。
"""
from decimal import Decimal


def sidebar(request):
    if not request.path.startswith("/") or request.path.startswith("/admin"):
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
