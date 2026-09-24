"""Django settings for the street sanitation inspection API."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# The system of record is PostgreSQL + PostGIS.  For fast local test runs the
# GDAL/GEOS native libraries are not required: GeoDjango field bindings are only
# needed by the (skipped without libs) GIS migrations - see inspections/apps.py.
DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": _env("PGDATABASE", "sanitation"),
        "USER": _env("PGUSER", "node"),
        "PASSWORD": _env("PGPASSWORD", ""),
        "HOST": _env("PGHOST", "/tmp/pgsock"),
        "PORT": _env("PGPORT", "5432"),
    }
}

# Native GIS libraries live in the local conda prefix when apt packages are
# unavailable; LD_LIBRARY_PATH / these two settings make GeoDjango find them.
_gdal = _env("GDAL_LIBRARY_PATH", "/tmp/pg/lib/libgdal.so")
_geos = _env("GEOS_LIBRARY_PATH", "/tmp/pg/lib/libgeos_c.so")
if os.path.exists(_gdal):
    GDAL_LIBRARY_PATH = _gdal
if os.path.exists(_geos):
    GEOS_LIBRARY_PATH = _geos

SECRET_KEY = _env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = _env("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "inspections",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "sanitation_api.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

WSGI_APPLICATION = "sanitation_api.wsgi.application"

USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "zh-cn"

STATIC_URL = "static/"
MEDIA_ROOT = _env("MEDIA_ROOT", str(BASE_DIR / "media"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%SZ",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "街道环卫考核 API",
    "DESCRIPTION": (
        "道路网格、拍摄证据、问题事件、保洁合同与扣分处罚。\n\n"
        "感知哈希仅用于生成疑似重复候选；事件关联以位置、时间及人工判断为准。"
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # File/Image fields become binary in request bodies; response bodies keep
    # the URL representation via split request/response components.
    "COMPONENT_SPLIT_REQUEST": True,
}

# ---- Domain configuration -------------------------------------------------
# Two photos count as a perceptual-hash candidate only when they look alike
# AND are taken within this spatial / temporal window.  Looking alike alone
# never merges two distinct locations.
PHASH_HAMMING_THRESHOLD = int(_env("PHASH_HAMMING_THRESHOLD", "8"))
PHOTO_SAME_SPOT_METERS = int(_env("PHOTO_SAME_SPOT_METERS", "50"))
PHOTO_SAME_EVENT_HOURS = int(_env("PHOTO_SAME_EVENT_HOURS", "72"))

# SLA escalation ladder (hours after the first photo of an OPEN event).
ESCALATION_LADDER_HOURS = [24, 48, 72]
ESCALATION_POINTS_BY_LEVEL = {1: 1, 2: 2, 3: 3}

# Base deduction points keyed by problem category.
BASE_POINTS = {
    "garbage_overflow": 2,
    "road_stain": 1,
    "illegal_dump": 3,
}
DEFAULT_BASE_POINTS = 2
