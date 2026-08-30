"""強制全站只能有一個 superadmin（``is_superuser=True``）。

用資料庫層的部分唯一索引，不是應用層檢查——後者只能擋住透過
Django ORM 的寫入，擋不住直接下 SQL 或忘記走 ORM 的腳本。索引本身
就不允許第二筆 ``is_superuser = true`` 的資料存在，不論寫入路徑。

見 apps/web/permissions.py 的角色設計說明。
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX one_superadmin_only "
                "ON auth_user (is_superuser) WHERE is_superuser = true;"
            ),
            reverse_sql="DROP INDEX one_superadmin_only;",
        ),
    ]
