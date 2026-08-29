"""bigram 切分與 SimHash 的 small 測試。"""
import pytest

from apps.core.text.bigram import is_cjk, to_tsvector_input, tokenize
from apps.core.text.simhash import (
    hamming_distance,
    is_near_duplicate,
    simhash,
)

# 典型的檢調新聞，SimHash 門檻即以此文本校準（見 simhash.DEFAULT_THRESHOLD）
_BASE_ARTICLE = (
    "台北地檢署今日偵結，依貪污治罪條例起訴前市長甲男，並具體求處有期徒刑十二年。"
    "檢方指出，甲男於任內收受廠商賄款，金額高達新台幣三千萬元，嚴重損害公務員廉潔性。"
)


class TestTokenize:
    def test_中文切為_bigram(self):
        assert tokenize("起訴書") == ["起訴", "訴書"]

    def test_單一中文字保留自身(self):
        assert tokenize("我") == ["我"]

    def test_英數整詞保留並轉小寫(self):
        """英數本來就有詞界，切 bigram 只會製造雜訊。"""
        assert tokenize("Covid19") == ["covid19"]

    def test_中英數混合(self):
        assert tokenize("111年度") == ["111", "年度"]

    def test_標點作為分隔不產生_token(self):
        assert tokenize("起訴，判決") == ["起訴", "判決"]

    def test_標點切斷_bigram_連續(self):
        """跨標點不該產生 bigram——「訴判」不是一個詞。"""
        assert "訴判" not in tokenize("起訴，判決")

    @pytest.mark.parametrize("raw", [None, "", "，。！？", "   "])
    def test_空輸入與純標點(self, raw):
        assert tokenize(raw) == []

    def test_全形數字正規化(self):
        assert tokenize("１２３") == ["123"]

    def test_案號的數字部分被保留(self):
        """案號雖然主要靠 identifiers 模組精確比對，
        但落到全文檢索時數字也不該被丟掉。"""
        tokens = tokenize("111年度金重訴字第123號")
        assert "111" in tokens
        assert "123" in tokens


class TestToTsvectorInput:
    def test_回傳空格分隔字串(self):
        assert to_tsvector_input("起訴書") == "起訴 訴書"

    def test_空輸入回傳空字串(self):
        assert to_tsvector_input(None) == ""


class TestIsCjk:
    @pytest.mark.parametrize("char", ["中", "文", "臺"])
    def test_漢字(self, char):
        assert is_cjk(char) is True

    @pytest.mark.parametrize("char", ["a", "1", "，", " "])
    def test_非漢字(self, char):
        assert is_cjk(char) is False


class TestSimhash:
    def test_相同文本指紋相同(self):
        text = "檢方今日偵結起訴前市長涉貪案"
        assert simhash(text) == simhash(text)

    def test_決定性_不受程序影響(self):
        """指紋要存進資料庫長期比對，不能依賴 PYTHONHASHSEED。
        此處驗證同一輸入穩定；跨程序一致性由使用 blake2b 保證。"""
        values = {simhash("同一段文字") for _ in range(5)}
        assert len(values) == 1

    def test_空文本回傳零(self):
        assert simhash("") == 0
        assert simhash(None) == 0

    def test_逐字轉載的距離為零(self):
        assert hamming_distance(simhash(_BASE_ARTICLE), simhash(_BASE_ARTICLE)) == 0

    @pytest.mark.parametrize(
        "variant",
        [
            _BASE_ARTICLE.replace("台北地檢署今日偵結", "北檢今偵結"),  # 改標題前綴
            _BASE_ARTICLE + "（本文由中央社提供）",                    # 加編按
            _BASE_ARTICLE.replace("今日", "今").replace("並具體", "具體"),  # 輕度改寫
        ],
    )
    def test_近逐字轉載落在門檻內(self, variant):
        """轉載常見的變體——改標題、加編按、輕度潤稿——都應被判為重複。"""
        assert is_near_duplicate(simhash(_BASE_ARTICLE), simhash(variant)) is True

    def test_不同主題不被判為重複(self):
        weather = (
            "中央氣象署發布豪雨特報，提醒山區注意坍方與落石，"
            "並呼籲民眾避免前往溪邊活動。"
        )
        assert is_near_duplicate(simhash(_BASE_ARTICLE), simhash(weather)) is False

    def test_已知限制_大幅改寫抓不到(self):
        """記錄 SimHash 的能力邊界，不是缺陷而是刻意接受的取捨。

        大幅改寫的距離（約 29）與完全不同主題相同，無法靠調門檻區分。
        重寫稿交由 L2 向量相似度與 L3 事件歸屬處理。
        此測試存在的目的是：若日後有人調高門檻試圖涵蓋重寫稿，
        這裡會失敗並提醒他那條路走不通。
        """
        rewritten = (
            "北檢偵結前市長甲男貪污案，求刑十二年。"
            "檢方查出他任內收賄三千萬元。"
        )
        assert is_near_duplicate(simhash(_BASE_ARTICLE), simhash(rewritten)) is False


class TestHammingDistance:
    def test_相同為零(self):
        assert hamming_distance(0b1010, 0b1010) == 0

    def test_計算相異位元數(self):
        assert hamming_distance(0b1010, 0b1001) == 2


class TestIsNearDuplicate:
    def test_距離在門檻內為真(self):
        assert is_near_duplicate(0b1010, 0b1011, threshold=1) is True

    def test_距離超過門檻為假(self):
        assert is_near_duplicate(0b1010, 0b0101, threshold=1) is False

    def test_空指紋不視為重複(self):
        """否則所有解析失敗的文件會被歸併成同一份，造成真實文件遺失。"""
        assert is_near_duplicate(0, 0) is False
        assert is_near_duplicate(0, 12345) is False
