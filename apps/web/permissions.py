"""內部工具的三級角色權限（superadmin 唯一／admin／user）。

**刻意重用 Django 內建的 ``is_staff``／``is_superuser``，不另建
角色欄位。** 這兩個旗標已經是 Django admin 自己的權限依據——
``is_superuser`` 略過所有權限檢查，``is_staff`` 決定能不能登入
``/admin/``。若另外加一個 ``UserProfile.role`` 欄位，會出現兩套
權限來源可能互相矛盾（例如 is_staff=True 但 role=user），且使用者
管理不必新增介面，Django admin 內建的使用者編輯頁就能勝任。

    superadmin  is_superuser=True（全站唯一，見 migration 0006 的
                部分唯一索引，資料庫層強制，不是應用層口頭約定）
    admin       is_staff=True（不論 is_superuser）
    user        任何已登入帳號（不要求 is_staff）

**「唯一 superadmin」用資料庫層的部分唯一索引強制，不是簽章驗證
或應用層檢查。** 應用層檢查（如 ``save()`` 覆寫、signal）只能擋住
透過 Django ORM 的寫入，擋不住直接下 SQL 或未來某支忘記走 ORM 的
遷移腳本；部分唯一索引在資料庫層面本身就不允許第二筆
``is_superuser = true`` 的資料存在，不論寫入路徑為何。
"""
from __future__ import annotations

import functools

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

__all__ = ["Role", "role_level", "has_role", "require_role"]


class Role:
    USER = "user"
    ADMIN = "admin"
    SUPERADMIN = "superadmin"


#: 階層權重。數字大的角色自動具備數字小的角色的權限——
#: 「至少要 admin」的檢查寫成 level(user_role) >= level(ADMIN)。
_LEVELS = {Role.USER: 1, Role.ADMIN: 2, Role.SUPERADMIN: 3}


def role_level(user) -> int:
    """該使用者的角色權重。未登入或帳號被停用者為 0（低於 USER）。"""
    if not user.is_authenticated or not user.is_active:
        return 0
    if user.is_superuser:
        return _LEVELS[Role.SUPERADMIN]
    if user.is_staff:
        return _LEVELS[Role.ADMIN]
    return _LEVELS[Role.USER]


def has_role(user, minimum: str) -> bool:
    return role_level(user) >= _LEVELS[minimum]


def require_role(minimum: str):
    """view 裝饰器：要求至少 ``minimum`` 角色，隱含 ``login_required``。

    未登入者導向登入頁（與純 ``@login_required`` 行為一致）；
    已登入但角色不足者拋 ``PermissionDenied``（403）——這與「根本
    沒登入」是不同的失效原因，分開處理讓使用者知道是權限不夠，
    不是忘記登入，重新登入同一個帳號也解決不了問題。
    """
    def decorator(view_func):
        @login_required
        @functools.wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not has_role(request.user, minimum):
                raise PermissionDenied(
                    f"此操作需要 {minimum} 以上權限")
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator
