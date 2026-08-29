"""混合召回的測試。

融合邏輯（``fuse``）是純函式，全部以 small 測試覆蓋——這是刻意的
設計壓力：把「哪些文件排在前面」與「怎麼從資料庫撈文件」分開，
排序規則才能不靠資料庫就驗證。

需要真實資料庫的只有各通道本身（ArrayField overlap 走 GIN、
FTS 走 tsvector、向量走 HNSW，三者在 SQLite 都不存在）。
"""
import datetime as dt

import pytest

from apps.retrieval.hybrid import RRF_K, Candidate, HybridRetriever, fuse


class TestFuse:
    """RRF 融合。純排序邏輯，無 I/O。"""

    def test_兩通道都撈到的排在只有一個通道撈到的前面(self):
        results = fuse({
            "keyword": [10, 20, 30],
            "vector": [20, 40, 50],
        })
        assert results[0].document_id == 20, "兩通道共識應勝過單通道第一名"
        assert results[0].channels == {"keyword": 2, "vector": 1}

    def test_單通道內維持原名次(self):
        results = fuse({"keyword": [7, 8, 9]})
        assert [c.document_id for c in results] == [7, 8, 9]

    def test_分數為名次倒數之和(self):
        results = fuse({"keyword": [5], "vector": [5]})
        assert results[0].score == pytest.approx(2 / (RRF_K + 1))

    def test_同分時以_id_排序使結果可重現(self):
        """名次相同的候選若順序不穩定，召回率量測每次跑會有微小差異，
        無法判斷指標變動來自程式碼還是排序抖動。"""
        a = fuse({"x": [3, 1, 2]})
        b = fuse({"x": [3, 1, 2]})
        assert [c.document_id for c in a] == [c.document_id for c in b]
        tied = fuse({"x": [9], "y": [4]})
        assert [c.document_id for c in tied] == [4, 9]

    def test_置頂者無論分數皆在最前(self):
        results = fuse({"keyword": [1, 2, 3]})
        results[-1].pinned = True
        results.sort(key=lambda c: (-c.pinned, -c.score, c.document_id))
        assert results[0].document_id == 3

    def test_空輸入回傳空清單(self):
        assert fuse({}) == []
        assert fuse({"keyword": []}) == []

    def test_通道名稱可讀出(self):
        candidate = Candidate(document_id=1, score=0.0,
                              channels={"vector": 1, "keyword": 3})
        assert candidate.channel_names == "keyword+vector"


@pytest.mark.medium
class TestChannels:
    """各通道對真實資料庫的查詢。"""

    @pytest.fixture
    def docs(self, source, db):
        from apps.ingest.models import Document

        made = {}
        for key, title, body, cases in [
            ("柯", "北檢起訴柯文哲", "京華城容積案，臺北地院審理", []),
            ("案號", "法院裁定", "本件為 113 年度金訴字第 51 號",
             ["臺灣臺北地方法院113年度金訴字第51號"]),
            ("無關", "颱風假快訊", "明日停班停課", []),
        ]:
            doc = Document.objects.create(
                source=source, url=f"https://t.test/{key}", title=title,
                raw_body=body, content_class=source.content_class,
                published_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            )
            if cases:
                doc.case_numbers = cases
                doc.save(update_fields=["case_numbers"])
            made[key] = doc
        return made

    def test_關鍵字通道命中(self, docs):
        ids = HybridRetriever().by_keyword(_all(), "柯文哲")
        assert docs["柯"].pk in ids
        assert docs["無關"].pk not in ids

    def test_關鍵字通道用寬鬆模式故部分詞命中即可(self, docs):
        """事件核心詞常是多個詞的組合，要求全部出現會漏掉
        「只提到其中兩個」的報導——那正是事件中期的常見形態。"""
        ids = HybridRetriever().by_keyword(_all(), "柯文哲 京華城 應曉薇")
        assert docs["柯"].pk in ids, "缺少『應曉薇』不該讓整篇落榜"

    def test_識別碼通道精確比對(self, docs):
        ids = HybridRetriever().by_identifier(
            _all(), case_numbers=["臺灣臺北地方法院113年度金訴字第51號"])
        assert ids == [docs["案號"].pk]

    def test_無識別碼時不做全表掃描(self, docs):
        assert HybridRetriever().by_identifier(_all()) == []

    def test_轉載重複的文件不進召回(self, docs, source):
        """同一則稿子被三家轉載會佔掉三個名額，而 canonical 已保留
        原始那篇（ADR-0012）。"""
        from apps.ingest.models import Document

        reprint = Document.objects.create(
            source=source, url="https://t.test/reprint",
            title="北檢起訴柯文哲", raw_body="京華城容積案，臺北地院審理",
            content_class=source.content_class,
            canonical_of=docs["柯"],
            published_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
        )
        found = HybridRetriever().search("柯文哲", channels=("keyword",))
        ids = [c.document_id for c in found]
        assert docs["柯"].pk in ids
        assert reprint.pk not in ids

    def test_識別碼命中者置頂且語意分數再高也擠不掉(self, docs):
        """ADR-0005 的「識別碼精確比對優先」：引用了案號的文件就是在
        講那個案子，不該被任何語意分數擠下去。"""
        found = HybridRetriever().search(
            "柯文哲 京華城",
            case_numbers=["臺灣臺北地方法院113年度金訴字第51號"],
            channels=("keyword",))
        assert found[0].document_id == docs["案號"].pk
        assert found[0].pinned
        assert "identifier" in found[0].channels

    def test_沒寫案號的文件不因此被扣分(self, docs):
        """反向的錯誤：多數新聞根本不寫案號，若把識別碼當成一票，
        沒有案號等同扣分，會把整批新聞壓到裁判書底下。"""
        found = HybridRetriever().search(
            "柯文哲",
            case_numbers=["臺灣臺北地方法院113年度金訴字第51號"],
            channels=("keyword",))
        ids = [c.document_id for c in found]
        assert docs["柯"].pk in ids

    def test_置頂者不受_top_k_截斷(self, docs):
        found = HybridRetriever().search(
            "颱風",
            case_numbers=["臺灣臺北地方法院113年度金訴字第51號"],
            channels=("keyword",), top_k=1)
        assert docs["案號"].pk in [c.document_id for c in found]


def _all():
    from apps.ingest.models import Document

    return Document.objects.filter(canonical_of__isnull=True)
