"""政府採購網（任務 55）。查詢驅動，不做全量入庫。"""
import json

import pytest

from apps.events.procurement_check import looks_like_company
from apps.ingest.fetchers import FakeFetcher
from apps.ingest.procurement import (
    API_BASE, SEARCH_BY_COMPANY_NAME, SEARCH_BY_TITLE,
    hit_to_parsed_document, parse_records, search,
)

# 真實回應的結構（2026-09-02 實測）
PAYLOAD = {
    "query": "京華城", "page": 1, "total_records": 1, "total_pages": 1,
    "records": [{
        "date": 20240925,
        "filename": "BDM-1-70677704",
        "brief": {
            "type": "決標公告",
            "title": "「京華城容積案聘任律師協助法律諮詢服務」委託專業服務案",
            "companies": {"ids": ["88116097"], "names": ["洪範法律事務所"]},
        },
        "job_number": "11330910",
        "unit_name": "臺北市政府都市發展局",
        "url": "/index/case/3.79.56/11330910/20240925/BDM-1-70677704",
    }],
}


class TestParse:
    def test_解析標案欄位(self):
        hit = parse_records(PAYLOAD)[0]
        assert hit.title.startswith("「京華城容積案")
        assert hit.tender_type == "決標公告"
        assert hit.unit_name == "臺北市政府都市發展局"
        assert hit.company_ids == ("88116097",)
        assert (hit.date.year, hit.date.month, hit.date.day) == (2024, 9, 25)
        assert hit.url.endswith("/index/case/3.79.56/11330910/20240925/BDM-1-70677704")

    def test_日期格式錯誤不炸掉(self):
        payload = {"records": [{"date": "壞掉", "brief": {}, "url": "/x"}]}
        assert parse_records(payload)[0].date is None

    def test_標題含統編讓精確比對生效(self):
        """Document.save() 從 title+raw_body 抽識別碼。採購網公告沒有
        內文頁可補，統編放進標題才能讓任務 15 的精確比對直接生效。"""
        doc = hit_to_parsed_document(parse_records(PAYLOAD)[0])
        assert "88116097" in doc.title
        assert doc.external_id == "11330910"

    def test_摘要當內文避免永遠缺內文(self):
        """公告本身就是全部資訊。若不給 body，這些文件會永遠停在
        「缺內文」被反覆重試，直到 MAX_BODY_ATTEMPTS 才退場。"""
        doc = hit_to_parsed_document(parse_records(PAYLOAD)[0])
        assert doc.body
        assert "臺北市政府都市發展局" in doc.body
        assert "洪範法律事務所" in doc.body


class TestSearch:
    def test_翻頁到最後一頁為止(self):
        fetcher = FakeFetcher()
        for page in (1, 2):
            body = dict(PAYLOAD, page=page, total_pages=2)
            fetcher.register(
                f"{API_BASE}/{SEARCH_BY_TITLE}?query=%E4%BA%AC%E8%8F%AF%E5%9F%8E"
                f"&page={page}", json.dumps(body))
        hits = search("京華城", fetcher=fetcher)
        assert len(hits) == 2
        assert len(fetcher.calls) == 2

    def test_查詢失敗回空清單不拋錯(self):
        """單一查詢失敗不該中斷整輪事件檢查。"""
        assert search("查不到", fetcher=FakeFetcher()) == []


class TestCompanyHeuristic:
    def test_公司名走廠商端點人名不走(self):
        """searchbytitle 只比對標案名稱——實測「巨佳營造」走 title
        為 0 筆、走 companyname 為 252 筆。而 build_event_query 的
        support 混了被告姓名與公司名，人名拿去查廠商一定是雜訊。"""
        assert looks_like_company("巨佳營造")
        assert looks_like_company("福麥國際室內裝修公司")
        assert looks_like_company("洪範法律事務所")
        assert not looks_like_company("柯文哲")
        assert not looks_like_company("沈慶京")


@pytest.mark.medium
class TestCheckEvents:
    def test_採購網公告可全文公開(self, db):
        """公告是公文，依著作權法第 9 條不得為著作權之標的。"""
        from apps.events.procurement_check import get_or_create_source
        from apps.ingest.models import ContentClass

        assert get_or_create_source().content_class == ContentClass.PUBLIC_RECORD

    def test_dry_run不寫入(self, db):
        from unittest.mock import patch
        from apps.events.models import Event, EventStatus
        from apps.events.procurement_check import check_events
        from apps.ingest.models import Document

        Event.objects.create(slug="e", title="京華城容積案",
                             status=EventStatus.ACTIVE)
        with patch("apps.events.procurement_check.search",
                   return_value=parse_records(PAYLOAD)):
            summary = check_events(dry_run=True, fetcher=FakeFetcher())
        assert summary.hits == 1
        assert Document.objects.count() == 0

    def test_實際入庫並抽出統編(self, db):
        from unittest.mock import patch
        from apps.events.models import Event, EventStatus
        from apps.events.procurement_check import check_events
        from apps.ingest.models import Document

        Event.objects.create(slug="e", title="京華城容積案",
                             status=EventStatus.ACTIVE)
        with patch("apps.events.procurement_check.search",
                   return_value=parse_records(PAYLOAD)):
            check_events(fetcher=FakeFetcher())
        doc = Document.objects.get()
        assert "88116097" in doc.tax_ids      # GIN 索引，供識別碼精確比對
        assert doc.raw_body
