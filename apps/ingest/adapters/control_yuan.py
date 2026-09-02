"""監察院公文清單 adapter（任務 56 調查、任務 57 實作）。

**2026-09-02 實測結論：可純 HTTP 取得，且資料品質高於預期。**

站台有四類公文，都走同一支清單頁：

    /CyBsBox.aspx?CSN={1..4}&n={133,134,136,135}&sms=0&page={N}

    CSN=1 n=133  調查報告
    CSN=2 n=134  糾正案文
    CSN=3 n=136  糾舉案文
    CSN=4 n=135  彈劾案文

分頁是**真的**（實測 page=1/2/27 內容各異），頁碼過大會夾到最後一頁
並回傳與最後一頁相同的內容——因此終止條件是「與前一頁相同」，而不是
「與第一頁相同」（後者是公視那種繞回首頁的樣式）。

清單的每一列就已經包含所需的全部欄位，不必進內容頁：

    td[0] 民國日期 115/07/28
    td[1] 案號     115年劾字第31號     ← 可直接餵給識別碼歸屬
    td[2] 案由     （摘要）
    td[3] 附件檔名

**正文不在 HTML 裡。** 內容頁 `CyBsBoxContent.aspx?n={n}&s={id}` 的
可見文字幾乎都是選單（實測 1,907 字），真正的公文是附件檔，位於
`cybsbox.cy.gov.tw/CYBSBoxSSL/edoc/download/{id}`，格式為 DOCX 或 PDF
（同一案常兩者都有）。抽取見 ``apps.ingest.control_yuan_body``。

**這些是公文，``content_class`` 為 PUBLIC_RECORD——可全文公開。**
依著作權法第 9 條不得為著作權之標的，這是官方源相對新聞源的實質優勢
（規格 §4.6）。
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from selectolax.parser import HTMLParser

from apps.core.dates import parse_date

from .base import ParsedDocument, register_adapter
from .rss import normalize_url

logger = logging.getLogger(__name__)

TAIPEI = dt.timezone(dt.timedelta(hours=8))


def _published_at(cell: str) -> dt.datetime | None:
    """清單的 ``115/07/28`` 是民國日期，且沒有時間。

    ``prefer_roc=True`` 是必要的：``parse_date`` 預設不把 2–3 位數年
    當民國年（避免誤讀新聞日期），但官方文件來源一律是民國紀年。
    沒有時間時補當日 12:00 +08，與 ``archives.date_at_noon`` 同樣的
    理由——補午夜會讓這些公文在依時間排序時全部排到當天最前面。
    """
    day = parse_date(cell, prefer_roc=True)
    if day is None:
        return None
    return dt.datetime(day.year, day.month, day.day, 12, 0, tzinfo=TAIPEI)

#: 監察院案號：115年劾字第31號、114年調字第10號、115年糾字第5號…
#: 抽出來當 external_id，並供識別碼歸屬比對。
CASE_NO = re.compile(r"\d{2,3}\s*年[^\s，、]{0,4}字第\s*\d+\s*號")


@register_adapter("control-yuan")
class ControlYuanListAdapter:
    """從清單頁的表格列抽出公文。"""

    def list_documents(self, raw: str, *, base_url: str = "") -> list[ParsedDocument]:
        if not raw or not raw.strip():
            raise ValueError("空的 HTML 回應")

        tree = HTMLParser(raw)
        docs: list[ParsedDocument] = []
        for row in tree.css("tr"):
            link = row.css_first("a[href*=CyBsBoxContent]")
            if link is None:
                continue
            href = link.attributes.get("href") or ""
            url = normalize_url(href, base_url or "https://www.cy.gov.tw")
            if not url:
                continue

            cells = [c.text().replace("\xa0", " ").strip() for c in row.css("td")]
            published_at = _published_at(cells[0]) if cells else None
            case_no = ""
            summary = ""
            if len(cells) > 1:
                match = CASE_NO.search(cells[1])
                case_no = match.group(0).replace(" ", "") if match else cells[1]
            if len(cells) > 2:
                summary = " ".join(cells[2].split())

            # 標題用「案號＋案由」：清單頁的連結文字一律是「…詳全文」，
            # 完全沒有辨識度，直接拿來當標題會讓所有公文長得一樣。
            title = f"{case_no} {summary}".strip() or link.text().strip()
            docs.append(ParsedDocument(
                url=url,
                title=title[:512],
                published_at=published_at,
                external_id=case_no[:128],
            ))

        if not docs:
            # 與其他 adapter 相同的判準：不可靜默回傳空清單，否則
            # 站台改版時來源健康度監測會完全失明（ADR-0007）。
            raise ValueError("監察院清單頁未解析出任何公文——頁面結構可能已變更")

        logger.info("監察院清單頁解析出 %d 筆公文", len(docs))
        return docs
