"""公開網站（Scope 7）。給一般訪客看的頁面，**完全不需要登入**——
這是刻意的，跟內部工具（apps.web，強制登入＋角色分級）是相反的設計
目標。SEO 是這裡的主要觸達管道（規格 G1、M8）：讀者忘記某個案子，
要靠 Google 搜到事件頁才會想起來，若這裡也要求登入，Google 完全
無法收錄，整個「讓大家想起來」的核心動機就達不成。

**只顯示 ``visibility=public`` 的事件**，且僅顯示已核准公開的內容
（inferred 因果邊須 ``publicly_visible=True``，見任務 42）。新聞全文
永不出現在這裡——`apps.compliance.redaction` 的白名單是唯一合法的
序列化路徑（規格 M7，release blocker）。
"""
from __future__ import annotations

import json

from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render

from apps.compliance.redaction import public_document_fields
from apps.events.models import Event, EventDocument, EventVisibility
from apps.timeline.models import CausalEdge, TimelineNode


def case_list(request):
    """公開事件列表（首頁）。類似新聞網站的案件專題列表頁。"""
    events = (Event.objects.public()
             .order_by("-last_progress_at"))
    page = Paginator(events, 20).get_page(request.GET.get("page"))
    page_range = page.paginator.get_elided_page_range(
        page.number, on_each_side=2, on_ends=1)
    return render(request, "public/case_list.html", {
        "page": page, "page_range": page_range,
    })


def case_detail(request, slug):
    """單一事件頁——公開網站的核心頁面（規格 G1：首屏顯示目前進度）。

    時間線只顯示引用該事件的節點；因果邊只顯示
    ``publicly_visible=True`` 者（stated 邊預設可見，inferred 邊
    須人工核准，見任務 42）。文件連結一律走
    ``public_document_fields()``，不直接把 ``Document`` 物件丟給
    模板——避免模板不小心存取到 ``raw_body`` 而外洩新聞全文。
    """
    event = get_object_or_404(Event.objects.public(), slug=slug)

    nodes = (TimelineNode.objects.filter(event=event)
            .select_related("citation_document", "citation_document__source")
            .order_by("-occurred_on", "-created_at"))
    timeline = []
    for node in nodes:
        timeline.append({
            "node": node,
            "citation": public_document_fields(node.citation_document),
        })

    edges = (CausalEdge.objects.filter(event=event).publicly_visible()
            .select_related("from_node", "to_node", "citation_document"))

    return render(request, "public/case_detail.html", {
        "event": event, "timeline": timeline, "causal_edges": edges,
        "ld_json": _safe_json_ld(_schema_org_article(event)),
    })


def _safe_json_ld(data: dict) -> str:
    """序列化並跳脫 ``<``——``json.dumps`` 不會跳脫這個字元，若事件
    標題或摘要恰好含有 ``</script>`` 字樣（合法輸入，沒有理由排除），
    直接把結果塞進 ``<script>`` 標籤會被瀏覽器解析成標籤提前結束，
    後面的 JSON 內容變成裸露在頁面上的文字，構成注入風險。
    Django 內建的 ``json_script`` 過濾器也是用同一招，但它把
    ``type`` 屬性寫死成 ``application/json``，搜尋引擎的 JSON-LD
    文件明確要求 ``application/ld+json``，因此在這裡自己做同樣的跳脫。
    """
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def _schema_org_article(event: Event) -> dict:
    """schema.org 結構化資料，供搜尋引擎理解頁面內容（規格 M8）。

    **在 view 端組成 dict 再序列化，不在模板裡拼 JSON 字串**——模板
    的字串過濾器（如 escapejs）處理的是「嵌進單一 JS 字串」的跳脫，
    拼裝完整 JSON 語法（大括號、逗號、多個欄位）容易在編輯時漏改
    某個逗號或引號而產生無法解析的 JSON，且不易發現，因為瀏覽器
    只會靜默忽略解析失敗的 structured data，不會顯示錯誤。
    """
    return {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": event.title,
        "datePublished": event.first_seen_at.isoformat() if event.first_seen_at else None,
        "dateModified": event.last_progress_at.isoformat() if event.last_progress_at else None,
        "description": event.current_status_text or event.summary,
        "publisher": {"@type": "Organization", "name": "新聞事件追蹤"},
    }
