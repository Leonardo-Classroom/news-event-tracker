"""Adapter 介面與註冊表。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ParsedDocument:
    """adapter 解析出的單筆文件，尚未寫入資料庫。

    刻意與 Django 模型解耦，讓解析邏輯可以用 small 測試驗證，
    不需要資料庫。
    """

    url: str
    title: str
    body: str = ""
    published_at: dt.datetime | None = None
    author: str = ""
    external_id: str = ""
    extra: dict = field(default_factory=dict)


class Adapter(Protocol):
    """把來源的原始回應解析成 ParsedDocument 清單。

    adapter 只負責解析，不負責抓取（抓取走 Fetcher 接縫）也不負責
    寫入（寫入由 tasks 統一以 upsert 處理，確保冪等）。
    """

    def list_documents(self, raw: str, *, base_url: str) -> list[ParsedDocument]: ...


_REGISTRY: dict[str, type] = {}


def register_adapter(slug: str):
    def wrapper(cls):
        _REGISTRY[slug] = cls
        return cls
    return wrapper


def get_adapter(slug: str):
    if slug not in _REGISTRY:
        raise KeyError(f"未註冊的 adapter：{slug}（已註冊：{sorted(_REGISTRY)}）")
    return _REGISTRY[slug]()
