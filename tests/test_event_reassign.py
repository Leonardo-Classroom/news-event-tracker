"""事件頁的「重新歸屬文件」按鈕（手動觸發 L3）。

批次指令 auto_assign_events 對全部事件重跑；這裡是單事件版本——
在某個事件頁看到「這案子少了幾篇報導」時，不該為此對全部事件
重跑一輪 LLM 判定。
"""
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from apps.events.models import Event, EventStatus


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_user("ra", password="x", is_staff=True)


@pytest.fixture
def event(db):
    return Event.objects.create(slug="e", title="京華城容積案",
                                status=EventStatus.ACTIVE)


@pytest.mark.medium
class TestReassignButton:
    def setup_method(self):
        from django.core.cache import cache
        cache.clear()

    def test_事件頁有按鈕(self, client, staff, event):
        client.force_login(staff)
        html = client.get(f"/e/{event.slug}/").content.decode()
        assert 'id="btn-reassign"' in html
        assert "重新歸屬文件" in html

    def test_一般使用者看不到按鈕(self, client, db, event, django_user_model):
        user = django_user_model.objects.create_user("plain", password="x")
        client.force_login(user)
        html = client.get(f"/e/{event.slug}/").content.decode()
        assert 'id="btn-reassign"' not in html

    def test_按下去派工(self, client, staff, event):
        client.force_login(staff)
        with patch("apps.events.reassign_tasks.reassign_event.delay") as task:
            r = client.post(f"/e/{event.slug}/reassign/")
        task.assert_called_once_with(event.slug)
        assert r.json()["running"] is True

    def test_進行中不重複派工(self, client, staff, event):
        """重複派工只會讓兩份判定互相覆寫，並付兩次 LLM 的錢。"""
        client.force_login(staff)
        with patch("apps.events.reassign_tasks.reassign_event.delay"):
            client.post(f"/e/{event.slug}/reassign/")
        with patch("apps.events.reassign_tasks.reassign_event.delay") as task:
            r = client.post(f"/e/{event.slug}/reassign/")
        task.assert_not_called()
        assert r.json()["already"] is True

    def test_不同事件可並行(self, client, staff, event, db):
        other = Event.objects.create(slug="e2", title="另一案",
                                     status=EventStatus.ACTIVE)
        client.force_login(staff)
        with patch("apps.events.reassign_tasks.reassign_event.delay"):
            client.post(f"/e/{event.slug}/reassign/")
        with patch("apps.events.reassign_tasks.reassign_event.delay") as task:
            client.post(f"/e/{other.slug}/reassign/")
        task.assert_called_once()

    def test_一般使用者不能派工(self, client, db, event, django_user_model):
        user = django_user_model.objects.create_user("plain2", password="x")
        client.force_login(user)
        assert client.post(f"/e/{event.slug}/reassign/").status_code == 403

    def test_狀態端點回進行中與篇數(self, client, staff, event):
        client.force_login(staff)
        d = client.get(f"/e/{event.slug}/reassign/status/").json()
        assert d["running"] is False
        assert d["doc_count"] == 0

    def test_預算用罄時保留已寫入的歸屬(self, event, db):
        """auto_assign 每批即時寫入，中止時已掛上的不該回滾。"""
        from apps.events.reassign_tasks import reassign_event
        from apps.llm.provider import LlmError

        with patch("apps.events.assignment.auto_assign",
                   side_effect=LlmError("預算已用罄")):
            out = reassign_event(event.slug)
        assert "預算" in out["error"]
