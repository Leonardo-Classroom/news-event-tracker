"""相關性過濾的 small 測試。

**規則刻意偏向召回**（ADR-0009 第 5 點）：漏掉一篇相關文件的代價是
「某個事件的時間線缺一塊」，而且不會報錯——那是靜默失效。
多送一篇無關文件的代價只是幾分錢。

因此測試的重點是「該進來的有沒有進來」，而非「不該進來的有沒有被擋」。
"""
import pytest

from apps.extract.relevance import RELEVANCE_TERMS, assess_relevance, is_corroborated


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


class TestSubstringContamination:
    """中文沒有詞界，子字串比對會被更長的詞誤觸發。

    這不是理論顧慮：實測「法院」的命中有 47.9% 純粹來自「立法院」，
    導致「星鏈條款闖關」「中油不買天然氣」「金石堂分店熄燈」等
    完全無關的政治與商業新聞被判為司法案件。

    這類誤判不會報錯——它只會讓大量雜訊進入事件偵測，
    產生無意義的事件候選並淹沒審核量能。
    """

    @pytest.mark.parametrize("text", [
        "立法院三讀通過明年度中央政府總預算案，朝野協商破局。",
        "立法院教育委員會今日審查相關法案。",
        "民進黨立法院黨團召開記者會說明立場。",
    ])
    def test_立法院不應觸發法院(self, text):
        assert assess_relevance(body=text).relevant is False

    @pytest.mark.parametrize("text", [
        "業者提高檢驗頻率以確保食品安全。",
        "衛生局將拉高檢查標準。",
    ])
    def test_提高檢驗不應觸發高檢(self, text):
        assert assess_relevance(body=text).relevant is False

    @pytest.mark.parametrize("text,term", [
        ("法院裁定羈押禁見。", "法院"),
        ("司法院公布裁判書統計。", "司法院"),
        ("高檢署指揮本案偵辦。", "高檢署"),
        ("台灣高等法院今日開庭。", "高等法院"),
    ])
    def test_真實司法脈絡仍要命中(self, text, term):
        """排除誤觸發不可傷及真實命中——那會變成用精確度換召回率，
        而 ADR-0009 明確要求偏向召回。"""
        result = assess_relevance(body=text)
        assert result.relevant is True

    def test_司法院不被排除(self):
        """只排除立法院。司法院本身就是司法脈絡。"""
        assert assess_relevance(body="司法院表示將研議修法。").relevant is True

    def test_同文中兼有立法院與真實法院(self):
        """立法院出現不該遮蔽同文中真實的司法內容。"""
        text = "立法院修法之際，台北地方法院正審理相關案件。"
        assert assess_relevance(body=text).relevant is True


class TestIsCorroborated:
    """事件偵測（任務 24）候選池的第二層過濾，比相關性過濾更嚴格。

    實測發現的落差：相關性過濾單一類別命中即通過（偏向召回是對的，
    抽取成本低），但 HDBSCAN 直接拿這個寬鬆候選池分群，讓「大法官
    人事任命」這類只透過 judicial_body（命中「法官」）通過過濾的
    政治新聞也被分群成候選事件——跑一次全量偵測，343 個候選裡
    245 個（71%）標題完全不含任何司法弊案核心詞。
    """

    def test_單靠judicial_body不夠具體(self):
        """「大法官人事再遭封殺」只會命中 judicial_body 的「法官」
        （子字串比對對「大法官」一樣命中），這正是實測揪出的漏洞。"""
        signals = {"categories": ["judicial_body"], "has_identifier": False}
        assert not is_corroborated(signals)

    def test_弊案專屬類別單獨命中即足夠(self):
        """「圖利」「回扣」這類詞本身就具體指向弊案，
        不會出現在無關的政治新聞裡，不需要第二個類別佐證。"""
        for category in ["corruption_charge", "corruption_context", "oversight"]:
            signals = {"categories": [category], "has_identifier": False}
            assert is_corroborated(signals), f"{category} 應單獨即足夠"

    def test_有識別碼即足夠(self):
        signals = {"categories": ["judicial_body"], "has_identifier": True}
        assert is_corroborated(signals)

    def test_兩個類別互相佐證(self):
        """與 fixtures/LABELING_GUIDE.md 的「主要關鍵字 + 佐證詞」
        是同一個道理——單一類別命中不足以排除同名但無關的可能。"""
        signals = {"categories": ["judicial_process", "judicial_body"],
                  "has_identifier": False}
        assert is_corroborated(signals)

    def test_空訊號不通過(self):
        assert not is_corroborated({})
        assert not is_corroborated({"categories": []})
