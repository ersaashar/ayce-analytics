"""Django settings for the AYCE Analytics V2 local chat application."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env", override=False)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} is required.")
    return value


def _optional(name: str) -> str:
    return os.environ.get(name, "").strip()


def _mcp_url(name: str) -> str:
    """Return a validated absolute HTTP(S) endpoint, or ``""`` when unset.

    ``MCPToolset`` treats a bare string without a URL scheme as a local
    command, so a misconfigured value is rejected here instead of launching a
    subprocess.
    """
    value = _optional(name)
    if not value:
        return ""
    message = f"{name} must be an absolute HTTP or HTTPS URL."
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ImproperlyConfigured(message) from None
    invalid_port = port is not None and not 1 <= port <= 65535
    if invalid_port or parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ImproperlyConfigured(message)
    return value


DEV_JAFFLE_PROJECT = "jaffle-wren"


def _analytics_mode() -> str:
    raw = os.environ.get("AYCE_ANALYTICS_MODE")
    if raw is None or not raw.strip():
        return "off"
    mode = raw.strip()
    if mode not in {"off", "dev_jaffle"}:
        raise ImproperlyConfigured("AYCE_ANALYTICS_MODE must be off or dev_jaffle.")
    return mode


def _loopback_mcp_url(value: str) -> bool:
    """True for an absolute loopback HTTP(S) URL that is not rewritten.

    The host must be the literal ``127.0.0.1`` or ``[::1]`` with an explicit
    port. Userinfo, query, and fragment are rejected. Callers keep the
    original string; this check does not rebuild it.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        return False
    if parts.hostname == "127.0.0.1":
        return parts.netloc == f"127.0.0.1:{port}"
    if parts.hostname == "::1":
        return parts.netloc == f"[::1]:{port}"
    return False


def _require_dev_jaffle() -> tuple[bool, str, str]:
    """Validate DEV gates. Errors name settings only, never endpoint values."""
    confirmed = _flag("AYCE_DEV_JAFFLE_CONFIRMED")
    project = _optional("AYCE_DEV_WREN_PROJECT")
    endpoint = _optional("AYCE_DEV_WREN_MCP_URL")
    approved = WREN_MCP_URL.strip()
    invalid: list[str] = []
    if not DEBUG:
        invalid.append("DJANGO_DEBUG")
    if not confirmed:
        invalid.append("AYCE_DEV_JAFFLE_CONFIRMED")
    if project != DEV_JAFFLE_PROJECT:
        invalid.append("AYCE_DEV_WREN_PROJECT")
    endpoint_ok = _loopback_mcp_url(endpoint)
    approved_ok = _loopback_mcp_url(approved)
    if not endpoint_ok:
        invalid.append("AYCE_DEV_WREN_MCP_URL")
    if not approved_ok or endpoint != approved:
        invalid.append("WREN_MCP_URL")
    if endpoint_ok and approved_ok and endpoint != approved:
        invalid.append("AYCE_DEV_WREN_MCP_URL")
    if invalid:
        raise ImproperlyConfigured(
            "Invalid DEV analytics settings: " + ", ".join(invalid) + "."
        )
    return confirmed, project, endpoint


def dev_jaffle_endpoint() -> str | None:
    """Approved endpoint when the current settings still pass DEV gates.

    Policy calls this again at runtime. Disabled or invalid settings return
    ``None``; startup validation raises separately.
    """
    from django.conf import settings as configured

    if configured.AYCE_ANALYTICS_MODE != "dev_jaffle" or not configured.DEBUG:
        return None
    if configured.AYCE_DEV_JAFFLE_CONFIRMED is not True:
        return None
    if configured.AYCE_DEV_WREN_PROJECT != DEV_JAFFLE_PROJECT:
        return None
    endpoint = configured.AYCE_DEV_WREN_MCP_URL
    approved = configured.WREN_MCP_URL
    if not isinstance(endpoint, str) or not isinstance(approved, str):
        return None
    endpoint = endpoint.strip()
    approved = approved.strip()
    if endpoint != approved or not _loopback_mcp_url(endpoint):
        return None
    return endpoint


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be true or false.")


def _positive_int(name: str, default: int) -> int:
    """Return a configured positive 32-bit integer, or the supplied default.

    Stored sizes, counts and token budgets all live in signed 32-bit columns
    (or in JavaScript-safe integers), so a value outside that range is refused
    at startup instead of being silently clamped later.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be a whole number.") from None
    if not 1 <= value <= 2_147_483_647:
        raise ImproperlyConfigured(f"{name} must be between 1 and 2147483647.")
    return value


def _port(name: str, default: int = 8000) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be a whole number.") from None
    if not 1 <= value <= 65535:
        raise ImproperlyConfigured(f"{name} must be between 1 and 65535.")
    return value


SECRET_KEY = _required("DJANGO_SECRET_KEY")
DEBUG = _flag("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = [
    host.strip()
    for host in _optional("DJANGO_ALLOWED_HOSTS").split(",")
    if host.strip()
] or ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts.apps.AccountsConfig",
    "datasets.apps.DatasetsConfig",
    "chat.apps.ChatConfig",
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
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_SERIALIZER = "django.contrib.sessions.serializers.JSONSerializer"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

CSRF_FAILURE_VIEW = "chat.views.csrf_failure"

LLM_BASE_URL = _optional("LLM_BASE_URL")
LLM_API_KEY = _optional("LLM_API_KEY")
LLM_MODEL = _optional("LLM_MODEL")
WREN_MCP_URL = _mcp_url("WREN_MCP_URL")

AYCE_ANALYTICS_MODE = _analytics_mode()
if AYCE_ANALYTICS_MODE == "dev_jaffle":
    (
        AYCE_DEV_JAFFLE_CONFIRMED,
        AYCE_DEV_WREN_PROJECT,
        AYCE_DEV_WREN_MCP_URL,
    ) = _require_dev_jaffle()
else:
    # off needs no DEV settings; ignore whatever those variables contain.
    AYCE_DEV_JAFFLE_CONFIRMED = False
    AYCE_DEV_WREN_PROJECT = ""
    AYCE_DEV_WREN_MCP_URL = ""

# Attachment and model budgets. These are stored once per request and shared by
# upload preparation, preflight and commit, so one run never mixes policies.
# Defaults keep an existing deployment running without any .env change.
CHAT_MAX_FILES_PER_THREAD = _positive_int("CHAT_MAX_FILES_PER_THREAD", default=5)
CHAT_MAX_FILES_PER_TURN = _positive_int("CHAT_MAX_FILES_PER_TURN", default=5)
CHAT_MAX_FILE_BYTES = _positive_int("CHAT_MAX_FILE_BYTES", default=5 * 1024 * 1024)
CHAT_MAX_TEXT_BYTES_PER_FILE = _positive_int(
    "CHAT_MAX_TEXT_BYTES_PER_FILE", default=64 * 1024
)
CHAT_MAX_TEXT_BYTES_PER_THREAD = _positive_int(
    "CHAT_MAX_TEXT_BYTES_PER_THREAD", default=256 * 1024
)
LLM_CONTEXT_WINDOW = _positive_int("LLM_CONTEXT_WINDOW", default=32_768)
LLM_MAX_OUTPUT_TOKENS = _positive_int("LLM_MAX_OUTPUT_TOKENS", default=4_096)

# Whether chat activity timelines surface detailed tool names, arguments,
# and model reasoning/thinking steps, or stay high-level generic status badges.
# Detailed output is only ever served to the operator's own native superuser
# sessions; every delegated role receives the stripped enum view.
CHAT_SHOW_REASONING_DETAIL = _flag(
    "CHAT_SHOW_REASONING_DETAIL",
    default=_flag("SHOW_REASONING_DETAIL", default=False),
)

if CHAT_MAX_FILES_PER_TURN > CHAT_MAX_FILES_PER_THREAD:
    raise ImproperlyConfigured(
        "CHAT_MAX_FILES_PER_TURN must not exceed CHAT_MAX_FILES_PER_THREAD."
    )
if CHAT_MAX_TEXT_BYTES_PER_FILE > CHAT_MAX_TEXT_BYTES_PER_THREAD:
    raise ImproperlyConfigured(
        "CHAT_MAX_TEXT_BYTES_PER_FILE must not exceed CHAT_MAX_TEXT_BYTES_PER_THREAD."
    )
if LLM_MAX_OUTPUT_TOKENS >= LLM_CONTEXT_WINDOW:
    raise ImproperlyConfigured(
        "LLM_MAX_OUTPUT_TOKENS must be smaller than LLM_CONTEXT_WINDOW."
    )

# Django counts every uploaded file against this; it tracks the per-turn quota
# rather than the framework's default of 100.
DATA_UPLOAD_MAX_NUMBER_FILES = CHAT_MAX_FILES_PER_TURN

# Development server binding used by `just dev` / `just run`.
DJANGO_HOST = _optional("DJANGO_HOST") or "127.0.0.1"
DJANGO_PORT = _port("DJANGO_PORT", default=8000)
