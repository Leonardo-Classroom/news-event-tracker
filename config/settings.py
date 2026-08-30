"""Django 設定。

環境變數優先，開發預設值內建——單人專案不需要多份 settings 檔，
但正式部署的敏感值一律由環境變數覆寫。
"""
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# 載入 .env（不進版控）。刻意不引入額外套件——只需最基本的解析。
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        os.environ.setdefault(_key.strip(), _value.strip())


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, str(default)).lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 基本

SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",       # SEO 是一級需求（規格 §4.7）
    "apps.ingest",
    "apps.extract",
    "apps.retrieval",
    "apps.events",
    "apps.llm",
    "apps.timeline",
    "apps.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

# login_required 預設導向 /accounts/login/，而本專案沒有該路徑——
# 未設定會讓所有需登入的頁面回 404，而非導向登入頁。
# **不用 /admin/login/**——Django admin 的登入表單刻意限制
# is_staff=True 才能登入（AdminAuthenticationForm.clean 的內建行為），
# 這是正確的（/admin/ 本來就該只給工作人員），但代表一般 user 角色
# 的帳號永遠無法通過那個表單登入。內部工具需要三個角色都能登入，
# 因此改用一般的 django.contrib.auth 登入頁（apps/web/urls.py 的
# /login/），只檢查帳號密碼與 is_active，角色高低留給
# apps.web.permissions.require_role 在各別視圖層判斷。
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context.sidebar",
            ],
        },
    },
]

# ---------------------------------------------------------------- 資料庫
# PostgreSQL + pgvector，由 conda 環境 newstrack-db 提供（ADR-0010）。
# SQLite 不可用：無向量型別與 HNSW 索引，且單一寫入者鎖擋不住
# ADR-0007 的雙 Celery 佇列並行寫入。

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "newstrack"),
        "USER": env("DB_USER", "newstrack"),
        "PASSWORD": env("DB_PASSWORD", "newstrack_dev"),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"connect_timeout": 10},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------- 在地化

LANGUAGE_CODE = "zh-hant"
TIME_ZONE = "Asia/Taipei"
USE_I18N = True
USE_TZ = True          # 全系統以 aware datetime 運作（見 apps.core.clock）

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ---------------------------------------------------------------- Celery
# 雙佇列：fetch（高併發 HTTP）與 browser（低併發 Playwright），見 ADR-0007

CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_TASK_ACKS_LATE = True           # 被硬殺的任務重新入列（要求任務冪等）
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1  # 長任務不預取，避免卡住的 worker 囤積任務
CELERY_TASK_DEFAULT_QUEUE = "fetch"
CELERY_TIMEZONE = TIME_ZONE

# ---------------------------------------------------------------- LLM
# 全部推論走 DeepSeek API，不自建本地推論服務（ADR-0009）

LLM_API_KEY = env("DEEPSEEK_API_KEY", "")
LLM_BASE_URL = env("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL_CHEAP = env("LLM_MODEL_CHEAP", "deepseek-v4-flash")  # L1 抽取
LLM_MODEL_FLAGSHIP = env("LLM_MODEL_FLAGSHIP", "deepseek-v4-pro")    # L3/L4

# 台幣匯率。每筆用量記錄會存下當時使用的匯率——匯率每天在變，
# 依當前值重算歷史記錄會讓過去的帳目失真。
LLM_USD_TO_NTD = env("LLM_USD_TO_NTD", "32.5")

# 支出上限（美元）。累計花費達此額度時所有 LLM 呼叫會被拒絕，
# 直到以 `manage.py llm_budget --approve` 明確追加。
# 閘門設在 LLMProvider 內部，呼叫端無法繞過。
LLM_BUDGET_INCREMENT_USD = env("LLM_BUDGET_INCREMENT_USD", "5.00")

# ---------------------------------------------------------------- Embedding
# BGE-M3 於 CPU 執行（ADR-0011）。dense 1024 維，以 halfvec 儲存。

EMBEDDING_MODEL = env("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_DEVICE = env("EMBEDDING_DEVICE", "cpu")
EMBEDDING_DIM = 1024
# 1024 而非 512：實測 512 雖快一倍且向量餘弦相似度達 0.9866，
# 但 top-5 檢索重疊率僅 82%（最低 40%），排序會實質改變。
EMBEDDING_MAX_LENGTH = int(env("EMBEDDING_MAX_LENGTH", "1024"))
EMBEDDING_BATCH_SIZE = int(env("EMBEDDING_BATCH_SIZE", "8"))

# ⚠️ 實測值，非 os.cpu_count()。i9-14900K 為混合架構（8 P-core + 16 E-core），
# 實測 16 執行緒比 32 快 2.6 倍——分派到 E-core 會讓每個同步點都等最慢的核心。
#
#   threads=4  → 1.27 篇/秒     threads=16 → 2.43 篇/秒  ← 最佳
#   threads=8  → 1.83 篇/秒     threads=32 → 0.92 篇/秒
#
# 換硬體時必須重新實測，不可沿用此值，也不可改回 os.cpu_count()。
EMBEDDING_THREADS = int(env("EMBEDDING_THREADS", "16"))

# ---------------------------------------------------------------- 日誌

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},  # 避免 DEBUG 時洗版
    },
}
