"""監察院 adapter 與公文正文抽取（任務 56 調查、57 實作）。"""
import io
import zipfile

import pytest

from apps.ingest.adapters import get_adapter
from apps.ingest.control_yuan_body import (
    attachment_urls, extract_control_yuan_article,
)
from apps.ingest.fetchers import FakeFetcher

LIST_HTML = """
<table><tr><th>日期</th></tr>
<tr>
  <td>115/07/28</td>
  <td>115年劾字第31號</td>
  <td>新竹縣政府工務處前處長江良淵，藏匿鉅額來源不明財產</td>
  <td>115年劾字第31號彈劾案文_公布版.docx</td>
  <td><a href="/CyBsBoxContent.aspx?n=135&s=49759">...詳全文</a></td>
</tr>
<tr>
  <td>115/07/21</td>
  <td>115年劾字第30號</td>
  <td>陳秀雅、陳杉吉分別為國立臺南大學附屬啟聰學校前校長及前教務主任</td>
  <td>檔名.docx</td>
  <td><a href="/CyBsBoxContent.aspx?n=135&s=49738">...詳全文</a></td>
</tr>
</table>
"""


class TestControlYuanList:
    def test_解析出案號日期與案由(self):
        docs = get_adapter("control-yuan").list_documents(
            LIST_HTML, base_url="https://www.cy.gov.tw")
        assert len(docs) == 2
        first = docs[0]
        assert first.external_id == "115年劾字第31號"
        assert "江良淵" in first.title
        assert first.url == "https://www.cy.gov.tw/CyBsBoxContent.aspx?n=135&s=49759"

    def test_民國日期轉西元並補中午(self):
        """115/07/28 是民國。parse_date 預設不把 3 位數年當民國年，
        必須 prefer_roc=True；沒有時間時補中午，補午夜會讓同日公文
        全部排到當天最前面。"""
        docs = get_adapter("control-yuan").list_documents(
            LIST_HTML, base_url="https://www.cy.gov.tw")
        when = docs[0].published_at
        assert (when.year, when.month, when.day) == (2026, 7, 28)
        assert when.hour == 12

    def test_標題不用連結文字(self):
        """清單頁的連結文字一律是「…詳全文」，拿來當標題會讓所有
        公文長得一樣。"""
        docs = get_adapter("control-yuan").list_documents(
            LIST_HTML, base_url="https://www.cy.gov.tw")
        assert all("詳全文" not in d.title for d in docs)

    def test_空清單拋錯而非靜默(self):
        with pytest.raises(ValueError):
            get_adapter("control-yuan").list_documents(
                "<html><body>維護中</body></html>")


def _docx(paragraphs):
    buf = io.BytesIO()
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document><w:body>{body}'
                   f"</w:body></w:document>")
    return buf.getvalue()


CONTENT_HTML = """
<html><head><title>監察院全球資訊網-彈劾案文</title></head><body>
<a href="https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76662">docx</a>
<a href="https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76663">pdf</a>
<a href="https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76662">重複</a>
</body></html>
"""


class TestControlYuanBody:
    def test_附件連結去重且保序(self):
        urls = attachment_urls(CONTENT_HTML)
        assert urls == [
            "https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76662",
            "https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76663",
        ]

    def test_從docx附件抽出正文(self):
        blob = _docx(["彈劾案文【公布版】", "被彈劾人姓名、服務機關及職級：",
                      "江良淵 新竹縣政府工務處前處長。" * 6])
        fetcher = FakeFetcher()
        fetcher.register(
            "https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76662", blob)
        article = extract_control_yuan_article(CONTENT_HTML, fetcher=fetcher)
        assert "彈劾案文" in article.body
        assert "江良淵" in article.body
        assert article.strategy == "control-yuan-attachment"

    def test_docx優先於pdf(self):
        """同一案常兩種格式都有。DOCX 段落乾淨，PDF 需要版面還原、
        換行常出錯，因此取到 DOCX 就不再抓其他附件。"""
        blob = _docx(["彈劾案文【公布版】", "內容" * 60])
        fetcher = FakeFetcher()
        fetcher.register(
            "https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76662", blob)
        extract_control_yuan_article(CONTENT_HTML, fetcher=fetcher)
        assert fetcher.calls == [
            "https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76662"]

    def test_沒有附件連結拋錯(self):
        with pytest.raises(ValueError):
            extract_control_yuan_article(
                "<html><body>沒有附件</body></html>", fetcher=FakeFetcher())

    def test_正文太短拋錯(self):
        blob = _docx(["太短"])
        fetcher = FakeFetcher()
        fetcher.register(
            "https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76662", blob)
        fetcher.register(
            "https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/76663", b"%PDF-1.7 x")
        with pytest.raises(ValueError):
            extract_control_yuan_article(CONTENT_HTML, fetcher=fetcher)


@pytest.mark.medium
class TestControlYuanSource:
    def test_公文的content_class可公開(self, db):
        """依著作權法第 9 條，公文不得為著作權之標的——可全文公開，
        這是官方源相對新聞源的實質優勢（規格 §4.6）。"""
        from django.core.management import call_command
        from apps.ingest.models import ContentClass, Source

        call_command("seed_sources")
        for slug in ("cy-investigation", "cy-correction",
                     "cy-censure", "cy-impeachment"):
            assert Source.objects.get(slug=slug).content_class == \
                ContentClass.PUBLIC_RECORD

    def test_監察院可全站掃描(self, db):
        from unittest.mock import patch
        from apps.ingest.models import Source, SourceType
        from apps.ingest.tasks import dispatch_historical

        source = Source.objects.create(
            slug="cy-impeachment", name="監察院－彈劾案文",
            type=SourceType.CY_SCRAPE, base_url="https://www.cy.gov.tw")
        with patch("apps.ingest.tasks.historical_backfill_source") as mocked:
            dispatch_historical(source)
        mocked.delay.assert_called_once_with(source.pk)
