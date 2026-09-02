"""監察院公文正文抽取（任務 57）。

正文不在內容頁的 HTML 裡——那一頁的可見文字幾乎都是選單（實測
1,907 字）。真正的公文是附件，連結形如
``https://cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/{id}``，
同一案常同時提供 DOCX 與 PDF。

**DOCX 優先於 PDF。** DOCX 用標準庫（zipfile + 正則）就能取出乾淨的
段落文字；PDF 需要版面還原，換行與分欄常出錯。實測同一案的 DOCX 為
3,587 字且段落完整，故只有沒有 DOCX 時才退回 PDF。
"""
from __future__ import annotations

import logging
import re
import zipfile
from io import BytesIO

from selectolax.parser import HTMLParser

from apps.ingest.article import ExtractedArticle
from apps.ingest.fetchers import FetchError, Fetcher

logger = logging.getLogger(__name__)

#: 附件下載連結。主機與內容頁不同網域，因此比對完整網址。
ATTACHMENT = re.compile(
    r"https://cybsbox\.cy\.gov\.tw/CYBSBoxSSL/edoc/download/\d+")

_MIN_BODY_LENGTH = 80
#: DOCX 的段落結束標記——換成換行才不會讓整份文件黏成一行。
_W_P_END = re.compile(r"</w:p>")
_XML_TAG = re.compile(r"<[^>]+>")


def attachment_urls(html: str) -> list[str]:
    """內容頁裡的附件連結，保持出現順序且去重。"""
    seen: dict[str, None] = {}
    for url in ATTACHMENT.findall(html or ""):
        seen.setdefault(url, None)
    return list(seen)


def _docx_text(blob: bytes) -> str:
    """從 DOCX 取出段落文字。不引入 python-docx——這裡只需要純文字，
    標準庫的 zipfile 加一個正則就夠，少一個相依套件。"""
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "replace")
    text = _XML_TAG.sub("", _W_P_END.sub("\n", xml))
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines)


def _pdf_text(blob: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(BytesIO(blob)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    lines = [line.strip() for line in "\n".join(parts).split("\n") if line.strip()]
    return "\n".join(lines)


def _looks_like_docx(blob: bytes) -> bool:
    return blob[:2] == b"PK"


def extract_control_yuan_article(
    html: str, *, fetcher: Fetcher, title: str = "",
) -> ExtractedArticle:
    """下載並解析監察院公文附件。介面與 ``extract_article`` 一致。

    ``html`` 是內容頁；附件另外抓。這與其他來源不同（其他來源的正文
    就在傳進來的 HTML 裡），所以需要傳入 ``fetcher``。
    """
    urls = attachment_urls(html)
    if not urls:
        # 內容頁若連附件都沒有，多半是站台改版而非這份公文沒有正文。
        raise ValueError("監察院內容頁找不到附件連結——頁面結構可能已變更")

    docx_body = ""
    pdf_body = ""
    for url in urls:
        try:
            response = fetcher.get(url)
            if not response.ok:
                raise FetchError(f"HTTP {response.status_code}")
        except FetchError as exc:
            logger.warning("監察院附件下載失敗 %s：%s", url, exc)
            continue

        blob = getattr(response, "content", None)
        if blob is None:
            blob = response.text.encode("utf-8", "replace")

        try:
            if _looks_like_docx(blob):
                docx_body = docx_body or _docx_text(blob)
            else:
                pdf_body = pdf_body or _pdf_text(blob)
        except Exception as exc:                       # noqa: BLE001
            # 單一附件解析失敗不該讓整份公文失敗——同一案常有多個附件。
            logger.warning("監察院附件解析失敗 %s：%s", url, exc)

        if docx_body:
            break        # DOCX 品質優於 PDF，取到就不必再抓其他附件

    body = docx_body or pdf_body
    if len(body) < _MIN_BODY_LENGTH:
        raise ValueError(
            f"監察院附件未取得足夠內文（{len(body)} 字，門檻 {_MIN_BODY_LENGTH}）"
        )

    if not title:
        node = HTMLParser(html).css_first("title")
        title = node.text().strip() if node else ""

    return ExtractedArticle(
        body=body,
        title=title[:512],
        strategy="control-yuan-attachment",
    )
