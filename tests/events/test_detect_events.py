"""事件偵測命令測試（任務 24）。用小規模合成向量驗證叢集轉候選事件的
流程，不依賴真實語料的規模——那由 detect_events 手動跑一次驗證
（任務 24 完成的樣子：對歷史語料的某個月份跑一次，人工檢視是否合理）。
"""
import datetime as dt

import numpy as np
import pytest
from django.core.management import call_command

from apps.events.models import AssignmentMethod, Event, EventDocument, EventStatus

UTC = dt.timezone.utc


def _blob(center, n, spread=0.02, seed=0):
    rng = np.random.default_rng(seed)
    v = rng.normal(loc=center, scale=spread, size=(n, len(center)))
    return [list(x / np.linalg.norm(x)) for x in v]


@pytest.mark.medium
class TestDetectEvents:
    @pytest.fixture
    def clustered_documents(self, source):
        """建立兩組明顯分離的向量群，模擬兩個尚未被發現的事件。"""
        from apps.ingest.models import Document

        cluster_a = _blob([1.0] + [0.0] * 1023, 6, seed=1)
        cluster_b = _blob([0.0, 1.0] + [0.0] * 1022, 6, seed=2)
        docs = []
        # relevance_signals 需通過 is_corroborated（任務 24 的候選池
        # 二次過濾）——弊案專屬類別單獨命中即足夠
        signals = {"categories": ["corruption_charge"], "has_identifier": False}
        for i, vec in enumerate(cluster_a):
            docs.append(Document.objects.create(
                source=source, url=f"https://t.test/a{i}", title=f"甲案報導{i}",
                raw_body="內文", content_class=source.content_class,
                relevant=True, embedding=vec, relevance_signals=signals,
                published_at=dt.datetime(2026, 1, i + 1, tzinfo=UTC),
            ))
        for i, vec in enumerate(cluster_b):
            docs.append(Document.objects.create(
                source=source, url=f"https://t.test/b{i}", title=f"乙案報導{i}",
                raw_body="內文", content_class=source.content_class,
                relevant=True, embedding=vec, relevance_signals=signals,
                published_at=dt.datetime(2026, 2, i + 1, tzinfo=UTC),
            ))
        return docs

    def test_乾跑不寫入資料庫(self, clustered_documents, db):
        call_command("detect_events", "--min-cluster-size", "5", "--dry-run")
        assert Event.objects.count() == 0

    def test_每個叢集建立一個候選事件(self, clustered_documents, db):
        call_command("detect_events", "--min-cluster-size", "5")
        events = Event.objects.all()
        assert events.count() == 2
        for event in events:
            assert event.status == EventStatus.CANDIDATE

    def test_文件關聯記錄為叢集方法(self, clustered_documents, db):
        call_command("detect_events", "--min-cluster-size", "5")
        links = EventDocument.objects.all()
        assert links.count() == 12
        assert all(link.method == AssignmentMethod.CLUSTER for link in links)

    def test_已歸屬文件不進入偵測(self, clustered_documents, db):
        # 狀態刻意設為 ACTIVE（而非預設的 CANDIDATE）——測試要驗證的是
        # 「這篇文件沒有被重複掛到叢集新建的候選事件上」，若既有事件
        # 也剛好是 CANDIDATE 狀態，就無法用狀態區分兩者
        pre_existing = Event.objects.create(slug="existing", title="既有事件",
                                            status=EventStatus.ACTIVE)
        EventDocument.objects.create(
            event=pre_existing, document=clustered_documents[0],
            method=AssignmentMethod.MANUAL,
        )
        call_command("detect_events", "--min-cluster-size", "5")

        # 第一群少了一篇已歸屬的文件，剩 5 篇仍達門檻可成簇；
        # 驗證那篇已歸屬文件沒有被重複掛到新的候選事件上
        assert not EventDocument.objects.filter(
            document=clustered_documents[0], event__status=EventStatus.CANDIDATE
        ).exists()
        # 而且原本的歸屬應該還在，沒有被覆蓋或刪除
        assert EventDocument.objects.filter(
            document=clustered_documents[0], event=pre_existing
        ).exists()

    def test_無候選文件時不報錯(self, db):
        call_command("detect_events")
        assert Event.objects.count() == 0

    def test_僅鬆散相關的文件不進入候選池(self, source, db):
        """實測發現的落差：相關性過濾單一類別命中即通過（偏向召回），
        但「大法官人事任命」這類政治新聞只透過 judicial_body（命中
        「法官」）就會通過過濾，若直接拿來分群會被誤判成候選事件。
        本測試建一組語意緊密但僅鬆散相關的文件，驗證它們不會被
        detect_events 撈進候選池、也就不會產生候選事件。"""
        from apps.ingest.models import Document

        cluster = _blob([1.0] + [0.0] * 1023, 6, seed=5)
        weak_signals = {"categories": ["judicial_body"], "has_identifier": False}
        for i, vec in enumerate(cluster):
            Document.objects.create(
                source=source, url=f"https://t.test/weak{i}", title=f"政治新聞{i}",
                raw_body="內文", content_class=source.content_class,
                relevant=True, embedding=vec, relevance_signals=weak_signals,
                published_at=dt.datetime(2026, 3, i + 1, tzinfo=UTC),
            )

        call_command("detect_events", "--min-cluster-size", "5")

        assert Event.objects.count() == 0
