"""Source 與 Document 的 medium 測試（需要真實 PostgreSQL）。

無法降級為 small 測試：GeneratedField、GIN 索引、tsvector 在 SQLite 上
都不存在，而這些正是要驗證的對象（ADR-0001、ADR-0010）。
"""
import datetime as dt

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.ingest.models import (
    ContentClass,
    Document,
    Source,
    SourceType,
    to_signed64,
    to_unsigned64,
)

pytestmark = pytest.mark.medium


def make_doc(source, url="https://example.test/a", title="標題", body="內文", **kw):
    return Document.objects.create(
        source=source, url=url, title=title, raw_body=body,
        content_class=kw.pop("content_class", source.content_class), **kw
    )


class TestDocumentIdempotency:
    """ADR-0007 的 acks_late 要求所有寫入冪等——被硬殺的任務會重跑。"""

    def test_同一_url_重複寫入被唯一約束擋下(self, source):
        make_doc(source, url="https://example.test/x")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_doc(source, url="https://example.test/x")

    def test_update_or_create_為冪等的_upsert(self, source):
        for _ in range(3):
            Document.objects.update_or_create(
                url="https://example.test/y",
                defaults={
                    "source": source,
                    "title": "標題",
                    "raw_body": "內文",
                    "content_class": source.content_class,
                },
            )
        assert Document.objects.filter(url="https://example.test/y").count() == 1

    def test_重跑時內容更新而非新增(self, source):
        Document.objects.update_or_create(
            url="https://example.test/z",
            defaults={"source": source, "title": "舊標題", "raw_body": "舊內文",
                      "content_class": source.content_class},
        )
        Document.objects.update_or_create(
            url="https://example.test/z",
            defaults={"source": source, "title": "新標題", "raw_body": "新內文",
                      "content_class": source.content_class},
        )
        doc = Document.objects.get(url="https://example.test/z")
        assert doc.title == "新標題"
        assert Document.objects.count() == 1


class TestSearchVector:
    """ADR-0001：bigram 在 Python 切分，tsvector 由資料庫產生。"""

    def test_search_text_以_bigram_填入(self, source):
        doc = make_doc(source, title="起訴書", body="")
        doc.refresh_from_db()
        assert "起訴" in doc.search_text
        assert "訴書" in doc.search_text

    def test_generated_column_自動產生_tsvector(self, source):
        doc = make_doc(source, title="檢方偵結起訴", body="涉貪案件")
        doc.refresh_from_db()
        assert doc.search_vector is not None
        assert str(doc.search_vector) != ""

    def test_可用_bigram_查詢命中(self, source):
        make_doc(source, url="https://example.test/1",
                 title="北檢偵結前市長貪污案", body="依貪污治罪條例起訴")
        make_doc(source, url="https://example.test/2",
                 title="氣象署發布豪雨特報", body="山區注意坍方")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_document "
                "WHERE search_vector @@ to_tsquery('simple', %s)",
                ["貪污"],
            )
            assert cur.fetchone()[0] == 1

    def test_內容更新後_tsvector_跟著更新(self, source):
        doc = make_doc(source, title="原標題", body="原內文")
        doc.title = "颱風警報"
        doc.raw_body = "中度颱風接近"
        doc.save()
        doc.refresh_from_db()
        assert "颱風" in doc.search_text


class TestSimhash:
    def test_儲存與讀回一致(self, source):
        doc = make_doc(source, title="測試", body="一段內容")
        doc.refresh_from_db()
        assert doc.simhash is not None
        # bigint 為有號 64 位元，SimHash 為無號——轉換必須可逆
        assert to_signed64(to_unsigned64(doc.simhash)) == doc.simhash

    def test_高位為一的指紋可儲存並讀回(self, source):
        """SimHash 是無號 64 位元，bigint 是有號——高位為 1 時不轉號會溢位。

        以直接 SQL 寫入繞過模型的衍生欄位重算，確保驗證的是儲存層本身。
        """
        big = (1 << 63) + 12345
        doc = make_doc(source, title="x", body="y")
        with connection.cursor() as cur:
            cur.execute("UPDATE ingest_document SET simhash = %s WHERE id = %s",
                        [to_signed64(big), doc.id])
        doc.refresh_from_db()
        assert doc.simhash < 0
        assert doc.simhash_unsigned == big

    def test_部分儲存時衍生欄位一併更新(self, source):
        """save(update_fields=["title"]) 若不補上衍生欄位，
        資料庫會留著舊的 simhash 與 search_text，且不會報錯。"""
        doc = make_doc(source, title="原標題", body="原內文")
        doc.refresh_from_db()
        before = doc.simhash

        doc.title = "完全不同的颱風警報標題"
        doc.save(update_fields=["title"])

        doc.refresh_from_db()
        assert doc.simhash != before, "衍生欄位未隨 update_fields 寫入資料庫"
        assert "颱風" in doc.search_text

    def test_未觸及來源欄位時不重算(self, source):
        """只更新無關欄位時不該做無謂的 SimHash 與 bigram 計算。"""
        doc = make_doc(source, title="標題", body="內文")
        doc.refresh_from_db()
        before = doc.simhash

        doc.author = "某記者"
        doc.save(update_fields=["author"])

        doc.refresh_from_db()
        assert doc.simhash == before
        assert doc.author == "某記者"

    def test_相同內容產生相同指紋(self, source):
        a = make_doc(source, url="https://example.test/a", title="同標題", body="同內文")
        b = make_doc(source, url="https://example.test/b", title="同標題", body="同內文")
        a.refresh_from_db(); b.refresh_from_db()
        assert a.simhash == b.simhash


class TestContentClass:
    """全文能否公開是法律約束，必須在資料層可區分（規格 §8.1）。"""

    def test_新聞來源預設為受著作權保護(self, source):
        doc = make_doc(source)
        assert doc.content_class == ContentClass.COPYRIGHTED

    def test_官方來源預設為公文(self, official_source):
        doc = make_doc(official_source, url="https://example.test/judgment")
        assert doc.content_class == ContentClass.PUBLIC_RECORD

    def test_queryset_可分離兩類(self, source, official_source):
        make_doc(source, url="https://example.test/news")
        make_doc(official_source, url="https://example.test/doc")
        assert Document.objects.news().count() == 1
        assert Document.objects.public_records().count() == 1


class TestCanonicalOf:
    def test_轉載指向首發版本(self, source):
        original = make_doc(source, url="https://example.test/orig", title="通稿")
        reprint = make_doc(source, url="https://example.test/reprint", title="通稿",
                           canonical_of=original)
        assert reprint.canonical_of == original
        assert list(original.reprints.all()) == [reprint]

    def test_canonical_queryset_排除轉載(self, source):
        original = make_doc(source, url="https://example.test/o", title="通稿")
        make_doc(source, url="https://example.test/r", title="通稿", canonical_of=original)
        assert Document.objects.canonical().count() == 1


class TestServiceWindow:
    """司法院 API 僅 00:00–06:00 開放（規格 R6）。"""

    def test_無時段設定者一律可用(self, source):
        assert source.is_within_service_window() is True

    @pytest.mark.parametrize("hour,expected", [(0, True), (3, True), (5, True),
                                               (6, False), (12, False), (23, False)])
    def test_時段內外判定(self, official_source, hour, expected):
        tz = timezone.get_current_timezone()
        moment = dt.datetime(2026, 3, 5, hour, 30, tzinfo=tz)
        assert official_source.is_within_service_window(moment) is expected

    def test_跨午夜的時段(self, db):
        src = Source.objects.create(
            slug="overnight", name="跨夜來源", type=SourceType.CY_SCRAPE,
            base_url="https://example.test",
            service_window_start_hour=22, service_window_end_hour=4,
        )
        tz = timezone.get_current_timezone()
        assert src.is_within_service_window(dt.datetime(2026, 3, 5, 23, tzinfo=tz)) is True
        assert src.is_within_service_window(dt.datetime(2026, 3, 5, 2, tzinfo=tz)) is True
        assert src.is_within_service_window(dt.datetime(2026, 3, 5, 12, tzinfo=tz)) is False
