"""把事件轉成檢索查詢。

任務 21 決定用 L1 抽取的實體當 ``core_terms``（前三種自動抽取法都失敗：
詞頻 n-gram 選出「起訴」「檢方」，TF-IDF 對比系統性地獎勵無意義的斷詞
碎片因為碎片最罕見，子字串包含法仍是碎片）。那個決定是對的，但留下
一個代價，直到量測召回率才顯現：

    京華城容積案的 core_terms 是「柯文哲 沈慶京 李文宗…」，
    **沒有「京華城」**。漏掉的 26 篇正例標題全都有京華城。

原因很直接：**實體是人與機構，而事件的定義性名詞不是實體。**
「京華城」是建物，「新竹棒球場」是場館，「超思進口蛋」是事由——
L1 抽取不會把它們列為 person 或 organization。

因此查詢由兩部分組成：事件標題去掉案由後綴（事件的身分），
加上經過濾的 core_terms（事件的當事人）。
"""
from __future__ import annotations

import dataclasses
import re

__all__ = ["EventQuery", "build_event_query", "title_term", "is_generic_term"]


@dataclasses.dataclass(frozen=True)
class EventQuery:
    """事件的檢索查詢，分成身分與涉案人兩部分。

    **兩者必須分開送進召回，不能併成一個詞袋。** 實測京華城案：

        「京華城 柯文哲 沈慶京 李文宗 …」併成一袋　16/30
        「京華城」與「柯文哲 沈慶京 …」兩個通道投票　27/30

    原因是 PostgreSQL 的 ``ts_rank`` **沒有 IDF**——每個 bigram 等重。
    併成一袋時，九個人名的 bigram 數量遠多於一個建物名，「柯文哲」
    （2,453 篇）的貢獻和「京華城」一樣大，排序被人名主導，變成
    「柯文哲的所有新聞」而非「京華城案的新聞」。加詞反而降低召回率：
    單用「京華城」是 11/30，加了九個人名之後剩 4/30。

    分成兩個通道後，身分詞固定拿到一半的投票權，不因涉案人多寡而
    稀釋。這也解釋了為什麼「每個詞各自一個通道」（十一個通道）沒用
    ——那樣身分詞只剩 1/11 的權重，問題原封不動。
    """

    identity: str
    support: tuple[str, ...] = ()

    def __str__(self) -> str:
        return " ".join([self.identity, *self.support]).strip()

    def __bool__(self) -> bool:
        return bool(self.identity or self.support)

#: 標題的案由後綴。「京華城容積案」→「京華城容積」。
#: 保留後綴會讓 bigram「積案」「件案」進入查詢，那些是案由詞不是識別詞。
TITLE_SUFFIXES = ("弊案", "案件", "事件", "案", "疑雲", "爭議")

#: 匿名化格式。**這些必須靠格式規則排除，不能靠文件頻率。**
#:
#: 實測各詞的文件頻率（238,357 篇語料）：
#:     李姓 0.75%　劉姓 0.44%　陳姓 1.51%　黃姓女子 0.12%
#:     柯文哲 1.03%　高虹安 0.46%
#:
#: 「陳姓」比「柯文哲」更常見，「李姓」比「高虹安」更常見。任何以
#: 頻率為界線的規則，要嘛留下匿名姓氏，要嘛砍掉案件的核心人物——
#: 兩者無法用同一個門檻分開。區分它們的不是頻率，是「X姓」本身就是
#: 匿名化格式，指涉的是一整類人而非特定人。
ANONYMISED = re.compile(r"^.姓(男子|女子|男|女|嫌|婦|翁|童)?$")

#: 文件頻率上限。超過此比例的詞不具識別力。
#:
#: 定在 1.5% 而非更嚴，是為了保住「柯文哲」（1.03%）——他是京華城案
#: 的核心被告，卻因為是知名政治人物而在語料中頻繁出現。門檻設在
#: 1% 會砍掉他。1.5% 能濾掉「國防部」（1.82%）而保住柯文哲，
#: 這個間隙很窄，是靠實測撐開的，不是憑感覺挑的。
MAX_DOCUMENT_FREQUENCY = 0.015


def title_term(title: str) -> str:
    """事件標題去掉案由後綴。

    >>> title_term("京華城容積案")
    '京華城容積'
    >>> title_term("南方澳大橋斷橋")
    '南方澳大橋斷橋'
    """
    for suffix in TITLE_SUFFIXES:
        if title.endswith(suffix) and len(title) > len(suffix) + 1:
            return title[: -len(suffix)]
    return title


def is_generic_term(term: str) -> bool:
    """該詞是否泛到不具識別力（不含頻率判斷，僅看格式）。"""
    return bool(ANONYMISED.match(term)) or len(term) < 2


def build_event_query(event, *, document_frequency=None) -> EventQuery:
    """組出該事件的檢索查詢。

    Args:
        document_frequency: ``term -> 出現該詞的文件比例`` 的函式。
            傳入才會做頻率過濾；不傳則只做格式過濾（讓純邏輯可以
            用 small 測試覆蓋，不必碰資料庫）。

    **標題詞永遠保留，即使頻率很高。** 「大巨蛋」出現在 1,799 篇
    （0.75%）——那是場館名，該場館的所有賽事新聞都會命中。但它是
    該事件唯一的身分詞，砍掉就沒有東西指向這個事件了。過泛的標題
    詞要靠 core_terms 的佐證來收斂，不是靠刪除。這與任務 20 標註集
    建構時學到的「主要關鍵字 + 佐證詞」是同一件事。
    """
    identity = title_term(event.title)
    support = []
    for term in event.core_terms or []:
        if is_generic_term(term) or term == identity:
            continue
        if document_frequency and document_frequency(term) > MAX_DOCUMENT_FREQUENCY:
            continue
        support.append(term)
    # 去重並保序：同一個人可能在 core_terms 裡出現多次
    return EventQuery(identity=identity, support=tuple(dict.fromkeys(support)))
