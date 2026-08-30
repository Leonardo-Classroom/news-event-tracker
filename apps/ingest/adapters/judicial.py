"""司法院裁判書公開查詢介面 adapter（任務 33、規格 R11 的驗證延伸）。

Scope 0 任務 2 的人工穿刺驗證已確認：司法院**公開查詢介面**
（`judgment.judicial.gov.tw`，不需 API 帳號）可取得完整裁判書全文，
且裁判書能乾淨對應回既有事件（京華城案 113年度金訴字第51號，
特徵詞 3/3 命中）。任務 1（申請正式 API 帳號）目前受阻於外部審核
時程，不在本專案控制範圍內；本 adapter 先以已驗證可行的公開查詢
介面運作，取得帳號後再切換到 ADR-0006／規格 R6 所述的正式異動清單
+ Token 機制，介面（``list_documents``）不必改變，只需換掉抓取層。

## 為何 adapter 只解析，不判斷 is_final

裁判書全文裡沒有「本判決已定讞」這種明確宣告——一審判決天生可上訴，
是否定讞要看之後有沒有上訴紀錄，這件事無法從單一份判決書本身判斷。
因此本 adapter 只負責抽出案號、法院、被告與全文，**是否定讞交給
L1 抽取的 judicial schema**（``apps.extract.schemas``）依「本院」
「一審」「上訴」等審級用語判斷——那是既有機制，不必為裁判書重造。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import datetime as dt

from apps.core.dates import parse_date
from apps.core.identifiers import extract_case_numbers
from apps.ingest.adapters.base import ParsedDocument, register_adapter

__all__ = ["JudgmentData", "parse_judgment_html", "JudicialAdapter"]

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
#: 判決書開頭的法院名稱，如「臺灣臺北地方法院」（不含後面的
#: 「刑事／民事判決」等案由字樣——那是案件類型，不是法院名稱的一部分，
#: apps.extract.courts.normalize_court 的別名表也是以純法院名稱比對）。
_COURT_NAME_RE = re.compile(r"(臺灣|台灣|最高|智慧財產及商業|懲戒)\S{0,10}法院")

#: 「中華民國 X 年 X 月 X 日」——判決宣示日期的標準格式。
_ROC_DATE_RE = re.compile(r"中\s*華\s*民\s*國\s*\d{2,3}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")


@dataclass(frozen=True)
class JudgmentData:
    case_number: str
    court: str
    title: str
    full_text: str
    defendants: tuple[str, ...]
    decided_on: dt.date | None = None


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub("", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return _WHITESPACE_RE.sub(" ", text).strip()


def parse_judgment_html(html: str) -> JudgmentData | None:
    """解析司法院查詢介面回傳的裁判書頁面。

    找不到案號時回傳 ``None``——沒有案號的「裁判書」沒有意義，
    寧可整篇丟棄，也不要建立一筆無法比對回事件的公文記錄。
    """
    text = _strip_html(html)
    case_numbers = extract_case_numbers(text)
    if not case_numbers:
        return None

    court_match = _COURT_NAME_RE.search(text)
    court = court_match.group(0) if court_match else ""

    # 被告：「被 告 XXX」（原文常見全形空白間隔），取姓名不含後續的
    # 「選任辯護人」等欄位——遇到下一個已知欄位標籤或標點就停止
    defendants = tuple(dict.fromkeys(
        m.group(1).strip() for m in re.finditer(
            r"被\s*告\s+([^\s，。；、選任]{2,10})", text)
    ))

    # 裁判日期：緊接在判決正本的法官簽名之前的「中華民國 X 年 X 月
    # X 日」。實測本document有 3 個日期格式的匹配：第一個（218401）
    # 是案情敘述裡提到的事發日期；最後一個（288603）緊接著「附表
    # 目錄」，是附表本身的內容不是任何日期聲明；只有中間那個
    # （288433）後面接著「刑事第十九庭 審判長法官…」才是宣示日期。
    # **取最後一個是錯的**——曾經這樣寫過，會取到附表目錄裡剛好像
    # 日期格式的文字。正確判準是「後面緊接法官／審判長／庭」。
    decided_on = None
    for match in _ROC_DATE_RE.finditer(text):
        following = text[match.end():match.end() + 30]
        if any(marker in following for marker in ("法官", "法 官", "審判長", "庭")):
            decided_on = parse_date(match.group(), prefer_roc=True)
            break

    return JudgmentData(
        case_number=case_numbers[0],
        court=court or "（法院名稱未能解析）",
        title=f"{court}{case_numbers[0]}判決" if court else case_numbers[0],
        full_text=text,
        defendants=defendants,
        decided_on=decided_on,
    )


@register_adapter("judicial")
class JudicialAdapter:
    """符合 ``apps.ingest.adapters.base.Adapter`` 協定。

    與新聞 adapter 的關鍵差異：一次查詢通常對應**一份**裁判書，
    不是清單頁——``list_documents`` 仍回傳清單以符合共用介面，
    但正常情況下長度為 0 或 1。
    """

    def list_documents(self, raw: str, *, base_url: str) -> list[ParsedDocument]:
        data = parse_judgment_html(raw)
        if data is None:
            return []
        published_at = (
            dt.datetime.combine(data.decided_on, dt.time.min, tzinfo=dt.timezone.utc)
            if data.decided_on else None
        )
        return [ParsedDocument(
            url=base_url, title=data.title, body=data.full_text,
            published_at=published_at, external_id=data.case_number,
            extra={"case_number": data.case_number, "court": data.court,
                  "defendants": list(data.defendants)},
        )]
