"""站台 adapter 的註冊制。

既有爬蟲把各站解析寫死在 sites.py 的模組層函式，新增來源要改動核心。
改為註冊制後，新增來源只需實作 adapter 並註冊，不必碰執行框架。
"""
from .base import Adapter, ParsedDocument, get_adapter, register_adapter
from .html_list import (
    ChinaTimesListAdapter, CteeListAdapter, EttodayListAdapter,
    HtmlListAdapter, ListConfig, PtsListAdapter, UdnListAdapter,
)
from .rss import RssAdapter

__all__ = [
    "Adapter", "ParsedDocument", "get_adapter", "register_adapter",
    "RssAdapter", "HtmlListAdapter", "ListConfig",
    "UdnListAdapter", "ChinaTimesListAdapter", "CteeListAdapter",
    "EttodayListAdapter", "PtsListAdapter",
]
