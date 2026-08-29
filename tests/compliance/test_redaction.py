"""公開內容白名單測試（任務 41，M7 release blocker）。"""
import datetime as dt

import pytest

from apps.compliance.redaction import (
    assert_no_copyrighted_body, public_document_fields,
)
from apps.ingest.models import ContentClass


@pytest.mark.medium
class TestPublicDocumentFields:
    def test_新聞不含全文(self, source, db):
        from apps.ingest.models import Document

        doc = Document.objects.create(
            source=source, url="https://t.test/news",
            title="京華城案偵結", raw_body="這是完整新聞內文" * 50,
            content_class=ContentClass.COPYRIGHTED,
            published_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        fields = public_document_fields(doc)
        assert "body" not in fields
        assert fields["title"] == "京華城案偵結"
        assert fields["url"] == doc.url

    def test_公文含全文(self, official_source, db):
        from apps.ingest.models import Document

        doc = Document.objects.create(
            source=official_source, url="https://t.test/judgment",
            title="113年度金訴字第51號判決", raw_body="主文：被告柯文哲…" * 50,
            content_class=ContentClass.PUBLIC_RECORD,
            published_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        fields = public_document_fields(doc)
        assert fields["body"] == doc.raw_body

    def test_新增未知欄位不會意外外流(self, source, db):
        """白名單的核心保證：just because Document 有這個欄位，
        不代表它會出現在公開回應裡。"""
        from apps.ingest.models import Document

        doc = Document.objects.create(
            source=source, url="https://t.test/x", title="t",
            raw_body="內文", content_class=ContentClass.COPYRIGHTED,
            relevance_signals={"categories": ["judicial_process"]},
        )
        fields = public_document_fields(doc)
        assert "relevance_signals" not in fields
        assert "raw_body" not in fields
        assert "canonical_of" not in fields


class TestAssertNoCopyrightedBody:
    def test_全文出現時拋出(self):
        body = "這是一篇很長的新聞內文" * 30
        with pytest.raises(AssertionError):
            assert_no_copyrighted_body({"summary": body}, corpus=[body])

    def test_摘要引用片語不誤判(self):
        """自製摘要合理引用原文的一兩句話是允許的，
        全文轉載才是規格禁止的——用全文比對而非子字串比對避免誤判。"""
        body = "這是一篇很長的新聞內文，包含許多細節與背景說明。" * 20
        summary = "摘要：這是一篇很長的新聞內文，法院裁定被告交保。"
        assert_no_copyrighted_body({"summary": summary}, corpus=[body])

    def test_短文不觸發避免誤判標題(self):
        # 200 字以下的內容（如標題）不比對，因為短字串重複出現的機率高，
        # 誤判成本（測試變得不可信）高於漏抓的風險（標題本來就允許公開）
        assert_no_copyrighted_body({"title": "京華城案偵結"}, corpus=["京華城案偵結"])

    def test_無語料時永遠通過(self):
        assert_no_copyrighted_body({"anything": "都可以"}, corpus=[])
