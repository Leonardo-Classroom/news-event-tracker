"""相關性過濾的 small 測試。

**規則刻意偏向召回**（ADR-0009 第 5 點）：漏掉一篇相關文件的代價是
「某個事件的時間線缺一塊」，而且不會報錯——那是靜默失效。
多送一篇無關文件的代價只是幾分錢。

因此測試的重點是「該進來的有沒有進來」，而非「不該進來的有沒有被擋」。
"""
import pytest

from apps.extract.relevance import RELEVANCE_TERMS, assess_relevance


class TestRelevantCases:
    """這些必須全部通過——漏掉任何一類都是靜默的資料缺口。"""

    @pytest.mark.parametrize("text", [
        "台北地檢署今日偵結，依貪污治罪條例起訴前市長。",
        "最高法院駁回上訴，全案判決確定。",
        "檢方向法院聲請羈押禁見，法官裁定以三百萬元交保。",
        "監察院通過彈劾案，移送懲戒法院審理。",
        "調查局約談相關人士，並搜索多處住所扣押帳冊。",
        "該案經高等法院發回更審，日前言詞辯論終結。",
        "北檢依證券交易法起訴，指其涉嫌內線交易與掏空公司資產。",
        "公共工程標案傳出綁標弊端，檢廉已介入偵辦。",
        "法院認定被告涉犯偽造文書與圖利罪嫌。",
        "廉政署查獲基層公務員收賄，全案依貪污治罪條例偵辦。",
    ])
    def test_司法弊案文件通過(self, text):
        assert assess_relevance(body=text).relevant is True

    def test_短篇快訊也要通過(self):
        """門檻刻意設為命中 1 個詞即可。若要求命中多個，
        會系統性漏掉短篇快訊——而快訊往往是事件的第一則報導。"""
        assert assess_relevance(title="某某遭起訴").relevant is True

    def test_僅標題命中也算(self):
        assert assess_relevance(title="前市長貪污案宣判", body="（內文從缺）").relevant is True

    def test_含案號者一律通過(self):
        """案號是強訊號，也是 ADR-0001 事件歸屬的主要依據。"""
        result = assess_relevance(body="本院111年度金重訴字第123號民事事件。")
        assert result.relevant is True
        assert result.has_identifier is True


class TestIrrelevantCases:
    """這些應被擋下。但誤擋的代價低於漏放，因此不追求完美。"""

    @pytest.mark.parametrize("text", [
        "中央氣象署發布豪雨特報，山區注意坍方與落石。",
        "台積電第三季營收創新高，股價開高走高。",
        "瓊斯盃女籃冠軍賽，日本隊以九分之差擊敗中華藍。",
        "本季最受歡迎的秋冬穿搭：格紋大衣與樂福鞋。",
        "知名歌手宣布世界巡迴演唱會，門票開賣即完售。",
    ])
    def test_無關文件被擋下(self, text):
        assert assess_relevance(body=text).relevant is False

    def test_空輸入(self):
        assert assess_relevance().relevant is False
        assert assess_relevance(title="", body="   ").relevant is False


class TestSignals:
    """記錄命中的詞彙與類別，供日後調校規則——
    只存布林值就無從判斷「是哪一類規則帶進來的」。"""

    def test_記錄命中類別(self):
        result = assess_relevance(body="北檢依貪污治罪條例起訴，法院裁定羈押。")
        assert "judicial_process" in result.categories
        assert "judicial_body" in result.categories
        assert "corruption_charge" in result.categories

    def test_記錄具體命中詞(self):
        result = assess_relevance(body="檢方偵結起訴。")
        assert "起訴" in result.signals["judicial_process"]

    def test_命中數(self):
        assert assess_relevance(body="起訴。").hit_count == 1
        assert assess_relevance(body="起訴、判決、羈押。").hit_count == 3

    def test_無關文件無_signals(self):
        assert assess_relevance(body="今天天氣很好。").signals == {}


class TestTermTable:
    def test_詞彙表無重複(self):
        """同一詞出現在兩類會讓 hit_count 虛增，也讓調校時難以判斷歸屬。"""
        seen = {}
        for category, terms in RELEVANCE_TERMS.items():
            for term in terms:
                assert term not in seen, f"{term!r} 同時出現在 {seen[term]} 與 {category}"
                seen[term] = category

    def test_各類別皆非空(self):
        for category, terms in RELEVANCE_TERMS.items():
            assert terms, f"{category} 沒有詞彙"
