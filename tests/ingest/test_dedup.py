"""轉載偵測的測試（ADR-0012）。

jaccard 是純函式（small）；歸併需要真實資料庫（medium）。
"""
import datetime as dt

import pytest
from django.utils import timezone

from apps.ingest.dedup import (
    DEFAULT_THRESHOLD, dedupe_recent, find_canonical, jaccard,
)
from apps.ingest.models import Document

UTC = dt.timezone.utc

_LONG_BODY = (
    "台北地方檢察署今日偵查終結，依貪污治罪條例之違背職務收受賄賂罪嫌，"
    "起訴前市長甲男等共十二人，並具體求處有期徒刑十二年。檢方指出，"
    "甲男於任內主導都市更新案審查程序，收受廠商所交付之現金賄款，"
    "金額累計高達新臺幣三千萬元，嚴重損害公務員執行職務之公正性。"
    "另查獲乙女涉嫌居間牽線並協助洗錢，一併提起公訴。"
    "全案將由臺灣臺北地方法院審理，被告均否認犯行。"
)


class TestJaccard:
    def test_完全相同為一(self):
        assert jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_完全不同為零(self):
        assert jaccard({"a"}, {"b"}) == 0.0

    def test_部分重疊(self):
        assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)

    def test_空集合為零(self):
        assert jaccard(set(), {"a"}) == 0.0
        assert jaccard(set(), set()) == 0.0


@pytest.mark.medium
class TestFindCanonical:
    """門檻 0.70 必須把「轉載」與「同事件獨立報導」分開——
    誤併後者會摧毀 ADR-0005 事件偵測所依賴的跨媒體訊號。"""

    @pytest.fixture
    def other_source(self, db):
        from apps.ingest.models import ContentClass, Source, SourceType
        return Source.objects.create(
            slug="other-news", name="另一家", type=SourceType.NEWS_RSS,
            base_url="https://other.test", content_class=ContentClass.COPYRIGHTED,
        )

    def _doc(self, source, url, body, when, title="標題"):
        return Document.objects.create(
            source=source, url=url, title=title, raw_body=body,
            content_class=source.content_class, published_at=when,
        )

    def test_逐字轉載被歸併(self, source, other_source):
        body = ("台北地檢署今日偵查終結，依貪污治罪條例起訴前市長甲男，"
                "並具體求處有期徒刑十二年。檢方指出，甲男於任內主導都市更新案"
                "審查程序，收受廠商所交付之現金賄款，金額累計高達三千萬元。") * 2
        t = dt.datetime(2026, 3, 5, 10, tzinfo=UTC)
        first = self._doc(source, "https://a.test/1", body, t)
        second = self._doc(other_source, "https://b.test/1", body,
                           t + dt.timedelta(hours=2))

        match = find_canonical(second)
        assert match is not None
        assert match[0].pk == first.pk
        assert match[1] > 0.9

    def test_同事件不同寫法不被歸併(self, source, other_source):
        """實測案例：新竹棒球場案由四家媒體以不同角度報導，Jaccard 約 0.4。
        這是事件偵測要的訊號，絕不可歸併。"""
        t = dt.datetime(2026, 3, 5, 10, tzinfo=UTC)
        self._doc(source, "https://a.test/2",
                  "新竹市立棒球場工程案偵結，前市長林智堅獲不起訴處分。"
                  "議員表示，這證明過去的政治操作經不起司法檢驗，"
                  "要求相關人士出面道歉並負起政治責任。" * 2, t)
        second = self._doc(other_source, "https://b.test/2",
                           "檢方偵結新竹棒球場弊案，認定罪證不足。"
                           "立委批評檢調拖延四年才有結論，質疑辦案效率，"
                           "並指出年底選舉民意將會反映此事。" * 2,
                           t + dt.timedelta(hours=1))
        assert find_canonical(second) is None

    def test_時間窗外不比對(self, source, other_source):
        body = _LONG_BODY
        t = dt.datetime(2026, 3, 5, 10, tzinfo=UTC)
        self._doc(source, "https://a.test/3", body, t)
        far = self._doc(other_source, "https://b.test/3", body,
                        t + dt.timedelta(hours=200))
        assert find_canonical(far, window_hours=72) is None

    def test_內文過短不判定(self, source, other_source):
        """短文的 bigram 集合小，Jaccard 波動大，判定不可靠。"""
        t = dt.datetime(2026, 3, 5, 10, tzinfo=UTC)
        self._doc(source, "https://a.test/4", "很短的內文", t)
        second = self._doc(other_source, "https://b.test/4", "很短的內文",
                           t + dt.timedelta(hours=1))
        assert find_canonical(second) is None

    def test_較早者不指向較晚者(self, source, other_source):
        """歸併方向必須以發布時間為準，否則結果不穩定。"""
        body = _LONG_BODY
        t = dt.datetime(2026, 3, 5, 10, tzinfo=UTC)
        first = self._doc(source, "https://a.test/5", body, t)
        self._doc(other_source, "https://b.test/5", body, t + dt.timedelta(hours=2))
        assert find_canonical(first) is None      # 較早者不歸併


@pytest.mark.medium
class TestDedupeRecent:
    def test_冪等(self, source):
        body = _LONG_BODY
        now = timezone.now()
        for i in range(2):
            Document.objects.create(
                source=source, url=f"https://x.test/{i}", title=f"標題{i}",
                raw_body=body, content_class=source.content_class,
                published_at=now - dt.timedelta(hours=i),
            )
        first = dedupe_recent()
        second = dedupe_recent()
        assert first.linked == 1
        assert second.linked == 0        # 已歸併者不再處理

    def test_dry_run_不寫入(self, source):
        body = _LONG_BODY
        now = timezone.now()
        for i in range(2):
            Document.objects.create(
                source=source, url=f"https://y.test/{i}", title=f"標題{i}",
                raw_body=body, content_class=source.content_class,
                published_at=now - dt.timedelta(hours=i),
            )
        result = dedupe_recent(dry_run=True)
        assert result.linked == 1
        assert Document.objects.filter(canonical_of__isnull=False).count() == 0
