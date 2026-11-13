"""Django settings. Everything environment-specific comes from environment variables (see .env.example)."""

import base64
import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DEBUG = env("DJANGO_DEBUG", "0") == "1"
SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DJANGO_DEBUG is not 1")
    SECRET_KEY = "dev-only-insecure-secret-key"

# Public URL Campfire calls (ngrok in development).
APP_URL = env("APP_URL", "http://localhost:8000").rstrip("/")
_app_host = urlparse(APP_URL).hostname or "localhost"

ALLOWED_HOSTS = sorted({"localhost", "127.0.0.1", _app_host} | ({".ngrok-free.app", ".ngrok.app"} if DEBUG else set()))
CSRF_TRUSTED_ORIGINS = [APP_URL] if APP_URL.startswith("https://") else []
# ngrok and most hosts terminate TLS in front of Django.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "campfire_integration",
    "entities",
    "investments",
    "gl",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Pages Campfire frames opt out per view (core.framing.campfire_frame).
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
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
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.parse(env("DATABASE_URL"), conn_max_age=600)
    if env("DATABASE_URL")
    else {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

SESSION_COOKIE_SECURE = APP_URL.startswith("https://")
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_HTTPONLY = True

# Encryption for OAuth tokens and other secrets at rest (core.crypto). First key encrypts.
FIELD_ENCRYPTION_KEYS = [k for k in env("FIELD_ENCRYPTION_KEYS").split(",") if k.strip()]
if not FIELD_ENCRYPTION_KEYS:
    if not DEBUG:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEYS is required when DJANGO_DEBUG is not 1")
    FIELD_ENCRYPTION_KEYS = [base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()).decode()]

DEFAULT_CAMPFIRE_SCOPES = [
    "openid", "email", "profile",
    "journal_entries:write", "transactions:read", "bills:write", "bill_payments:read", "reports:read",
    "bank_accounts:read", "bank_transactions:read", "entities:read", "accounts:write", "tags:write",
    "vendors:write", "lock_periods:read",
]

CAMPFIRE = {
    "BASE_URL": env("CAMPFIRE_BASE_URL", "https://api.meetcampfire.com").rstrip("/"),
    "FRONTEND_ORIGIN": env("CAMPFIRE_FRONTEND_ORIGIN", "https://app.meetcampfire.com").rstrip("/"),
    "CLIENT_ID": env("CAMPFIRE_CLIENT_ID"),
    "CLIENT_SECRET": env("CAMPFIRE_CLIENT_SECRET"),
    # Current first; the previous one only during a rotation (Campfire signs with both for 24h).
    "SIGNING_SECRETS": [s for s in (env("CAMPFIRE_SIGNING_SECRET"), env("CAMPFIRE_SIGNING_SECRET_PREVIOUS")) if s],
    # The app's permission ceiling (manifest `allowed_scopes`). See docs/campfire_permissions.md.
    "SCOPES": (env("CAMPFIRE_SCOPES") or " ".join(DEFAULT_CAMPFIRE_SCOPES)).split(),
    # draft: connections request the draft tier for journal entries and bills, so everything the app creates
    # goes through Campfire approval. direct: request write (Campfire may still route it to approval).
    "POSTING_MODE": env("CAMPFIRE_POSTING_MODE") or "draft",
    "REDIRECT_PATH": "/campfire/callback",
    "API_KEY": env("CAMPFIRE_API_KEY"),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
