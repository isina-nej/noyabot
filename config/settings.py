import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# DEBUG default: when unset, stay local-friendly for SQLite and secure-by-default
# for non-SQLite (typical production). Explicit DEBUG= always wins.
_debug_env = os.getenv("DEBUG")
if _debug_env is None:
    _db_engine = os.getenv("DB_ENGINE", "django.db.backends.sqlite3")
    DEBUG = _db_engine.endswith("sqlite3")
else:
    DEBUG = _debug_env.lower() in {"1", "true", "yes", "on"}
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-development-only-change-me",
)
if not DEBUG and SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DEBUG is false.")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "botapp",
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

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": os.getenv("DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.getenv("DB_NAME", BASE_DIR / "db.sqlite3"),
        "USER": os.getenv("DB_USER", ""),
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", ""),
        "PORT": os.getenv("DB_PORT", ""),
        "OPTIONS": (
            {"charset": "utf8mb4"}
            if os.getenv("DB_ENGINE") == "django.db.backends.mysql"
            else {}
        ),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "fa-ir"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Tehran")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEMORY_SHORT_TERM_TTL_HOURS = int(os.getenv("SHORT_TERM_TTL_HOURS", "24"))
MEMORY_MEDIUM_TERM_TTL_DAYS = int(os.getenv("MEDIUM_TERM_TTL_DAYS", "14"))
MEMORY_LONG_TERM_TTL_DAYS = int(os.getenv("LONG_TERM_TTL_DAYS", "60"))
MEMORY_MAX_CONTEXT_TOKENS = int(os.getenv("MEMORY_MAX_CONTEXT_TOKENS", "600"))
MEMORY_MAX_ITEMS_PER_SCOPE = int(os.getenv("MEMORY_MAX_ITEMS_PER_SCOPE", "200"))
MEMORY_EXTRACTION_ENABLED = os.getenv("MEMORY_EXTRACTION_ENABLED", "true").lower() in {
    "1", "true", "yes", "on"
}
MEMORY_RETRIEVAL_ENABLED = os.getenv("MEMORY_RETRIEVAL_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_INGESTION_ENABLED = os.getenv("MEMORY_INGESTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_USER_COMMANDS_ENABLED = os.getenv("MEMORY_USER_COMMANDS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_FAIL_OPEN = os.getenv("MEMORY_FAIL_OPEN", "true").lower() in {"1", "true", "yes", "on"}
MEMORY_LATENCY_LOGGING_ENABLED = os.getenv("MEMORY_LATENCY_LOGGING_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

NOYA_SYSTEM_PROMPT_ENABLED = os.getenv("NOYA_SYSTEM_PROMPT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
NOYA_SEARCH_ENABLED = os.getenv("NOYA_SEARCH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
# Noya creator identity (defaults resolved inside prompts.py).
NOYA_CREATOR_IDS = os.getenv("NOYA_CREATOR_IDS", "").strip()
NOYA_CREATOR_NAME = os.getenv("NOYA_CREATOR_NAME", "Sina").strip() or "Sina"
NOYA_CREATOR_USERNAME = os.getenv("NOYA_CREATOR_USERNAME", "").strip().lstrip("@")
NOYA_CREATOR_ALIASES = os.getenv("NOYA_CREATOR_ALIASES", "").strip()

# --- Admin agent (natural-language admin assistant) --------------------------
_TRUE = {"1", "true", "yes", "on"}
AGENT_ENABLED = os.getenv("AGENT_ENABLED", "true").lower() in _TRUE
AGENT_AI_ENABLED = os.getenv("AGENT_AI_ENABLED", "true").lower() in _TRUE
AGENT_MODEL = os.getenv("AGENT_MODEL", "").strip()
AGENT_CONFIRMATION_TTL_SECONDS = int(os.getenv("AGENT_CONFIRMATION_TTL_SECONDS", "120"))
AGENT_MIN_CONFIDENCE = float(os.getenv("AGENT_MIN_CONFIDENCE", "0.80"))
AGENT_MAX_COMMAND_LENGTH = int(os.getenv("AGENT_MAX_COMMAND_LENGTH", "2000"))
# ReAct investigation harness (multi-step read tools + sandboxed code).
AGENT_HARNESS_ENABLED = os.getenv("AGENT_HARNESS_ENABLED", "true").lower() in _TRUE
AGENT_HARNESS_AI_ENABLED = os.getenv("AGENT_HARNESS_AI_ENABLED", "true").lower() in _TRUE
AGENT_HARNESS_MAX_STEPS = int(os.getenv("AGENT_HARNESS_MAX_STEPS", "8"))
AGENT_CLASSIFY_ENABLED = os.getenv("AGENT_CLASSIFY_ENABLED", "true").lower() in _TRUE
AGENT_CACHE_TTL_SECONDS = int(os.getenv("AGENT_CACHE_TTL_SECONDS", "600"))
MESSAGE_ARCHIVE_ENABLED = os.getenv("MESSAGE_ARCHIVE_ENABLED", "false").lower() in _TRUE
MESSAGE_ARCHIVE_RETENTION_DAYS = int(os.getenv("MESSAGE_ARCHIVE_RETENTION_DAYS", "30"))

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = (
    not DEBUG and os.getenv("SECURE_SSL_REDIRECT", "true").lower() in {"1", "true", "yes", "on"}
)
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000")) if not DEBUG else 0

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "timed": {
            "format": "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "timed",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "botapp": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
X_FRAME_OPTIONS = "DENY"
