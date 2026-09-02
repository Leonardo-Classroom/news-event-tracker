"""三級角色權限測試（superadmin 唯一／admin／user）。

刻意重用 Django 內建的 is_staff／is_superuser，不另建角色欄位——
理由見 apps/web/permissions.py 的模組說明。這裡驗證的重點：
角色階層是否正確（user < admin < superadmin）、唯一 superadmin
是否真的在資料庫層被強制（不只是應用層檢查）。
"""
import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory

from apps.web.permissions import Role, has_role, require_role, role_level


@pytest.fixture
def anon_user():
    return AnonymousUser()


@pytest.fixture
def plain_user(db):
    return User.objects.create_user("plain", "p@test.local", "pw")


@pytest.fixture
def admin_user(db):
    return User.objects.create_user("staffer", "a@test.local", "pw", is_staff=True)


@pytest.fixture
def superadmin_user(db):
    return User.objects.create_superuser("boss", "b@test.local", "pw")


class TestRoleLevel:
    def test_匿名者無角色(self, anon_user):
        assert role_level(anon_user) == 0
        assert not has_role(anon_user, Role.USER)

    def test_停用帳號視同無角色(self, plain_user):
        plain_user.is_active = False
        assert role_level(plain_user) == 0

    def test_一般帳號為user(self, plain_user):
        assert has_role(plain_user, Role.USER)
        assert not has_role(plain_user, Role.ADMIN)
        assert not has_role(plain_user, Role.SUPERADMIN)

    def test_staff帳號為admin且具備user權限(self, admin_user):
        assert has_role(admin_user, Role.USER)
        assert has_role(admin_user, Role.ADMIN)
        assert not has_role(admin_user, Role.SUPERADMIN)

    def test_superuser帳號為superadmin且具備全部權限(self, superadmin_user):
        assert has_role(superadmin_user, Role.USER)
        assert has_role(superadmin_user, Role.ADMIN)
        assert has_role(superadmin_user, Role.SUPERADMIN)


@pytest.mark.django_db
class TestRequireRoleDecorator:
    def _call(self, view, user):
        request = RequestFactory().get("/x")
        request.user = user
        return view(request)

    def test_未登入導向登入頁(self, anon_user):
        """導向 /login/ 而非 /admin/login/——後者刻意限制只有
        is_staff 才能登入，一般 user 帳號會永遠卡在登入頁。"""
        view = require_role(Role.USER)(lambda r: HttpResponse("ok"))
        response = self._call(view, anon_user)
        assert response.status_code == 302
        assert "/login/" in response["Location"]

    def test_角色足夠時放行(self, admin_user):
        view = require_role(Role.ADMIN)(lambda r: HttpResponse("ok"))
        response = self._call(view, admin_user)
        assert response.status_code == 200

    def test_角色不足時拋出PermissionDenied(self, plain_user):
        view = require_role(Role.ADMIN)(lambda r: HttpResponse("ok"))
        with pytest.raises(PermissionDenied):
            self._call(view, plain_user)

    def test_user級視圖一般帳號可存取(self, plain_user):
        view = require_role(Role.USER)(lambda r: HttpResponse("ok"))
        assert self._call(view, plain_user).status_code == 200


@pytest.mark.django_db
class TestOneSuperadminConstraint:
    """資料庫層強制，不是應用層口頭約定——直接繞過 ORM 的高階方法，
    用最基本的 save() 也一樣擋得住，證明約束是加在資料表本身。"""

    def test_第二個superuser被資料庫拒絕(self, superadmin_user):
        second = User(username="second", is_superuser=True, is_staff=True)
        second.set_password("pw")
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                second.save()

    def test_只有一個superuser時可以正常新增staff與一般帳號(self, superadmin_user):
        User.objects.create_user("normal", "n@test.local", "pw")
        User.objects.create_user("staffer2", "s2@test.local", "pw", is_staff=True)
        assert User.objects.filter(is_superuser=True).count() == 1

    def test_將現有帳號改回superuser前必須先卸任原本的(self, superadmin_user, plain_user):
        """驗證約束在『改』而非只在『新增』時也生效。"""
        plain_user.is_superuser = True
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                plain_user.save()


@pytest.mark.django_db
class TestViewLevelEnforcement:
    """對實際掛在 URL 上的視圖驗證角色檢查真的生效，
    不只是裝飾器單元測試——URL 路由設定錯誤（如忘記包裝）不會被
    上面的單元測試發現。"""

    def test_一般帳號可看事件列表(self, plain_user):
        client = Client()
        client.force_login(plain_user)
        assert client.get("/").status_code == 200

    def test_一般帳號不能操作爬蟲(self, plain_user):
        client = Client()
        client.force_login(plain_user)
        assert client.get("/crawlers/").status_code == 403

    def test_admin帳號可操作爬蟲(self, admin_user):
        client = Client()
        client.force_login(admin_user)
        assert client.get("/crawlers/").status_code == 200

    def test_一般帳號不能看成本頁(self, plain_user):
        client = Client()
        client.force_login(plain_user)
        assert client.get("/costs/").status_code == 403

    def test_未登入導向登入而非403(self, db):
        client = Client()
        response = client.get("/crawlers/")
        assert response.status_code == 302


@pytest.mark.medium
class TestPublicSiteLink:
    def test_側欄有公開頁入口且開新分頁(self, client, django_user_model):
        """公開頁是給讀者看的另一個站，不該把正在操作的內部頁面蓋掉。"""
        user = django_user_model.objects.create_user("viewer", password="x")
        client.force_login(user)
        html = client.get("/").content.decode()
        assert 'class="brand-public"' in html
        assert 'href="/case/"' in html
        assert 'target="_blank"' in html
        assert 'rel="noopener"' in html

    def test_未登入的頁面不顯示側欄(self, client):
        """側欄整塊在 is_authenticated 之內，公開頁入口也一樣。"""
        html = client.get("/login/").content.decode()
        assert 'class="brand-public"' not in html
