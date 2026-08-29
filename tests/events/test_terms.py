"""事件查詢建構。純邏輯，全部 small——頻率過濾以注入的函式取代資料庫。"""
import dataclasses

import pytest

from apps.events.terms import (
    MAX_DOCUMENT_FREQUENCY, build_event_query, is_generic_term, title_term,
)


@dataclasses.dataclass
class FakeEvent:
    title: str
    core_terms: list


class TestTitleTerm:
    @pytest.mark.parametrize("title,expected", [
        ("京華城容積案", "京華城容積"),
        ("新竹棒球場案", "新竹棒球場"),
        ("大巨蛋案", "大巨蛋"),
        ("南方澳大橋斷橋", "南方澳大橋斷橋"),   # 無後綴，原樣保留
        ("超思進口蛋案", "超思進口蛋"),
    ])
    def test_去掉案由後綴(self, title, expected):
        assert title_term(title) == expected

    def test_不把整個標題吃掉(self):
        """「案」本身若被去掉會變成空字串，那比不去掉更糟。"""
        assert title_term("案") == "案"
        assert title_term("弊案") == "弊案"


class TestGenericTerm:
    @pytest.mark.parametrize("term", ["李姓", "陳姓男子", "黃姓女子", "王姓嫌"])
    def test_匿名姓氏被排除(self, term):
        assert is_generic_term(term)

    @pytest.mark.parametrize("term", ["柯文哲", "高虹安", "沈慶京", "巨佳營造"])
    def test_具名人物與公司不被排除(self, term):
        assert not is_generic_term(term)

    def test_單字被排除(self):
        assert is_generic_term("柯")


class TestBuildEventQuery:
    def test_標題詞排在最前(self):
        """漏掉標題詞正是初版 13% 召回率的原因——京華城案的
        core_terms 全是人名，沒有「京華城」。"""
        query = build_event_query(FakeEvent("京華城容積案", ["柯文哲", "沈慶京"]))
        assert query.identity == "京華城容積"
        assert "柯文哲" in query.support

    def test_匿名姓氏被濾掉(self):
        """罕見的匿名姓氏一樣要濾掉。「康姓」只出現在 43 篇，遠低於
        「李姓」的 1,776 篇，但罕見的原因是康是罕見姓氏，不是它指向
        特定某人——它與「李姓」同樣指涉一整類人。這正是格式規則能做
        而頻率規則做不到的判斷。"""
        query = build_event_query(
            FakeEvent("誠新綠能案", ["李姓", "劉姓", "陳姓", "康姓"]))
        assert query.identity == "誠新綠能"
        assert query.support == ()

    def test_過泛的詞依頻率濾掉(self):
        query = build_event_query(
            FakeEvent("超思進口蛋案", ["陳吉仲", "國防部"]),
            document_frequency=lambda t: {"陳吉仲": 0.0002, "國防部": 0.0182}[t])
        assert "陳吉仲" in query.support
        assert "國防部" not in query.support

    def test_頻率門檻保住知名被告(self):
        """柯文哲 1.03%、國防部 1.82%——門檻要落在兩者之間。
        設在 1% 會砍掉案件的核心被告。"""
        assert 0.0103 < MAX_DOCUMENT_FREQUENCY < 0.0182

    def test_標題詞即使過泛也保留(self):
        """大巨蛋出現在 1,799 篇（0.75%），是場館名不是案件名。
        但它是該事件唯一的身分詞，砍掉就沒有東西指向這個事件。"""
        query = build_event_query(
            FakeEvent("大巨蛋案", []), document_frequency=lambda t: 0.9)
        assert query.identity == "大巨蛋"

    def test_不重複(self):
        query = build_event_query(FakeEvent("京華城案", ["京華城", "柯文哲"]))
        assert "京華城" not in query.support

    def test_沒有_core_terms_也能用(self):
        assert str(build_event_query(FakeEvent("南方澳大橋斷橋", []))) == "南方澳大橋斷橋"
