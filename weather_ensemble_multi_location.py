#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import gzip
import hashlib
import html
import json
import logging
import math
import os
import random
import re
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional
from zoneinfo import ZoneInfo

try:
    import certifi
except ImportError:
    certifi = None

APP_VERSION = "LANGIT v62.0"
QUALITY_VERSION = APP_VERSION
QUALITY_SCHEMA = "langit-quality-1"
OUTPUT_SCHEMA_VERSION = "2026-11-22.langit-v62"
LANGIT_BRAND_NAME = "LANGIT"
LANGIT_PUBLIC_VERSION = APP_VERSION
LANGIT_PRODUCT_NAME = "Platform Prakiraan Cuaca LANGIT"
LANGIT_TAGLINE = "Prakiraan cuaca Indonesia yang disajikan secara ringkas, jelas, dan mudah dipantau."
LANGIT_DISCLAIMER = "Bukan informasi resmi BMKG. Untuk cuaca ekstrem, pantau peringatan dini BMKG dan kondisi setempat."
SENTINEL_PUBLIC_UI_VERSION = "langit-v62-public"
AETHER_VERSION = APP_VERSION

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
TARGET_TIMES = [f"{hour:02d}:00" for hour in range(24)]
CUACA_ORDER = ["Cerah", "Cerah Berawan", "Berawan", "Hujan Ringan", "Hujan Sedang", "Hujan Lebat"]

DEFAULT_LOCATION_NAME = "Dago, Bandung"
DEFAULT_ADM4 = "32.73.02.1004"
DEFAULT_LATITUDE = -6.8890
DEFAULT_LONGITUDE = 107.6100
DEFAULT_TIMEZONE = "Asia/Jakarta"

HTTP_TIMEOUT_SECONDS = 30
MAX_RETRY_HTTP = 3
RETRY_BACKOFF_SECONDS = 2
MAX_WORKERS = 8
RAW_PAYLOAD_DIRNAME = "raw_payloads"
SAVE_RAW_PAYLOADS = True
COMPRESS_RAW_PAYLOADS = False
OBSERVATION_DIRNAME = "observations"
REPORT_DIRNAME = "reports"
LOG_DIRNAME = "logs"
ARCHIVE_DIRNAME = "forecast_archive"
WEIGHTS_FILENAME = "source_weights.json"
HEALTH_FILENAME = "source_health.json"
OBSERVATION_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MIN_SOURCES_FOR_HIGH_CONFIDENCE = 5
MIN_SOURCE_SUCCESS_FOR_RUN = 5
OUTLIER_Z_THRESHOLD = 3.5
DEFAULT_EVALUATION_DAYS = 14
DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETENTION_MAX_MB = 0
DEFAULT_ARCHIVE_RETENTION_DAYS = 120
DEFAULT_KEEP_VERSIONED_FILES = 10
DEFAULT_TIME_BUDGET_MINUTES = 18.0
DEFAULT_ROLLOVER_HOUR = 20
MAX_CONSECUTIVE_FAILURE_PENALTY = 5
DEFAULT_MAX_INFLIGHT_PER_HOST = 3
TRANSPORT_MEMO_TTL_SECONDS = 3600

RUN_DAILY = False
RUN_TIME = "19:00"
RUN_IMMEDIATELY_ON_START = True
SLEEP_INTERVAL_SECONDS = 30
DEBUG = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ROOT_DIRNAME = "outputs"
LOCATIONS_FILENAME = "locations.json"
BMKG_API_URL = "https://api.bmkg.go.id/publik/prakiraan-cuaca"
BMKG_PORTAL_URL = "https://data.bmkg.go.id/prakiraan-cuaca/"
MET_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
HONEST_UA = "langit-weather-ensemble/62 (+https://data.bmkg.go.id/prakiraan-cuaca/; non-commercial research)"
DEFAULT_HTTP_HEADERS = {
    "User-Agent": HONEST_UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip,deflate",
    "Connection": "close",
}
BMKG_HTTP_HEADERS = {
    "User-Agent": HONEST_UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip,deflate",
    "Referer": BMKG_PORTAL_URL,
    "Connection": "close",
}
SOURCE_BASE_WEIGHTS = {
    "BMKG": 1.35, "ECMWF": 1.20, "METEOFRANCE": 1.10, "ICON": 1.05, "GFS": 1.00,
    "METNO": 1.00, "UKMO": 0.95, "KMA": 0.90, "CMA": 0.85,
}
OPEN_METEO_SOURCES = [
    {"source_id": "ECMWF", "provider": "Open-Meteo / ECMWF", "endpoint": "https://api.open-meteo.com/v1/ecmwf"},
    {"source_id": "GFS", "provider": "Open-Meteo / NOAA GFS", "endpoint": "https://api.open-meteo.com/v1/gfs"},
    {"source_id": "ICON", "provider": "Open-Meteo / DWD ICON", "endpoint": "https://api.open-meteo.com/v1/dwd-icon"},
    {"source_id": "CMA", "provider": "Open-Meteo / CMA GRAPES", "endpoint": "https://api.open-meteo.com/v1/cma"},
    {"source_id": "METEOFRANCE", "provider": "Open-Meteo / Meteo-France", "endpoint": "https://api.open-meteo.com/v1/meteofrance"},
    {"source_id": "UKMO", "provider": "Open-Meteo / UK Met Office", "endpoint": "https://api.open-meteo.com/v1/forecast", "models": "ukmo_seamless"},
]
# KMA tidak aktif secara default: respons selalu berisi timestamp dengan seluruh nilai null.
OPTIONAL_SOURCE_CONFIGS = [
    {"source_id": "KMA", "provider": "Open-Meteo / KMA", "kind": "open_meteo",
     "endpoint": "https://api.open-meteo.com/v1/forecast", "models": "kma_seamless"},
]
ALL_SOURCE_CONFIGS = [
    {"source_id": "BMKG", "provider": "BMKG", "kind": "bmkg"},
    *[{"source_id": i["source_id"], "provider": i["provider"], "kind": "open_meteo",
       "endpoint": i["endpoint"], "models": i.get("models")} for i in OPEN_METEO_SOURCES],
    {"source_id": "METNO", "provider": "MET Norway", "kind": "met_no"},
]
ACTIVE_SOURCE_CONFIGS = list(ALL_SOURCE_CONFIGS)

V61_BACKUP_CONFIGS = {
    "jma_seamless": {"source_id": "JMA_BACKUP", "provider": "Open-Meteo / JMA", "kind": "open_meteo",
                     "endpoint": "https://api.open-meteo.com/v1/forecast", "models": "jma_seamless"},
    "cmc_gem_gdps": {"source_id": "GEM_BACKUP", "provider": "Open-Meteo / Environment Canada", "kind": "open_meteo",
                     "endpoint": "https://api.open-meteo.com/v1/forecast", "models": "cmc_gem_gdps"},
    "bom_access_global": {"source_id": "BOM_BACKUP", "provider": "Open-Meteo / BOM", "kind": "open_meteo",
                          "endpoint": "https://api.open-meteo.com/v1/forecast", "models": "bom_access_global"},
}
_SOURCE_FAMILIES = {"METNO": "ECMWF", "ECMWF": "ECMWF", "JMA_BACKUP": "JMA", "GEM_BACKUP": "GEM", "BOM_BACKUP": "BOM"}
_OBS_TYPES = {"station_observation", "sensor_observation", "manual_observation", "external_observation"}
_OBS_FIELDS = ("temp_c", "rh_pct", "rain_mm", "wind_kmh", "category")

AETHER_DB_FILENAME = "sentinel_x_ledger.sqlite"
AETHER_CSV_FILENAME = "sentinel_x.csv"
AETHER_JSON_FILENAME = "sentinel_x.json"
AETHER_DASHBOARD_FILENAME = "command_center_sentinel_x.html"
AETHER_REPORT_FILENAME = "sentinel_x_report.md"
AETHER_REPORT_HTML_FILENAME = "sentinel_x_report.html"
AETHER_CONTRACT_FILENAME = "sentinel_x_forecast_contract.json"
AETHER_SOURCE_STATE_FILENAME = "sentinel_x_source_state.csv"
AETHER_FEEDBACK_FILENAME = "sentinel_x_feedback.csv"
AETHER_BASIC_OPEN_METEO_VARIABLES = ["temperature_2m", "relative_humidity_2m", "precipitation", "weather_code", "wind_speed_10m"]
AETHER_EXTRA_OPEN_METEO_VARIABLES = [
    "apparent_temperature", "dew_point_2m", "precipitation_probability", "rain", "cloud_cover", "pressure_msl",
    "surface_pressure", "wind_direction_10m", "wind_gusts_10m", "visibility", "cape", "uv_index",
]

LOGGER = logging.getLogger("weather_ensemble_multi_location")
ACTIVE_SOURCE_WEIGHTS = dict(SOURCE_BASE_WEIGHTS)
SOURCE_HEALTH = {}
ACTIVE_OUTPUT_DIR = BASE_DIR
ACTIVE_LOCATIONS_FILE = ""
CSV_DELIMITER = ","
_RUN_ISSUED_AT = None
_QUALITY_SKILL = {}
_QUALITY_FAMILY_COUNTS = {}
_QUALITY_TARGET_DT = None
_V61_ENSEMBLES = {}
_V61_SOURCE_DIAGNOSTICS = []

SENTINEL_CONSTITUTION = [
    "Do not assign high trust when source coverage is weak.",
    "Do not hide source disagreement; disagreement is a risk signal.",
    "Do not treat rainfall mean as the only reality; always preserve P90/P95 worst-case signals.",
    "Do not treat BMKG rain proxy as precise rain_mm observation.",
    "For local convective rain, communicate timing and spatial displacement uncertainty.",
    "If the forecast may fail, say how and why it may fail.",
    "When decision cost is asymmetric, prefer safety-first interpretation.",
    "If data quality is poor, use BLACK/RED status and avoid confident recommendations.",
    "Every forecast must include reasoning, limitations, and a recommended action.",
    "The system learns from observations and feedback, but never replaces official warnings.",
]


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LocationConfig:
    slug: str
    location_name: str
    adm4: str
    latitude: float
    longitude: float
    timezone: str = DEFAULT_TIMEZONE
    bmkg_point_name: str = ""
    area_level: str = "adm4"
    is_proxy_bmkg: bool = False
    note: str = ""


@dataclass
class ForecastPoint:
    source_id: str
    provider: str
    target_time: str
    source_datetime: datetime
    temp_c: Optional[float]
    rh_pct: Optional[float]
    rain_mm: Optional[float]
    wind_kmh: Optional[float]
    category: str
    raw_condition: str
    gap_minutes: Optional[float]
    cloud_cover_pct: Optional[float] = None
    pressure_msl_hpa: Optional[float] = None
    surface_pressure_hpa: Optional[float] = None
    wind_gusts_kmh: Optional[float] = None
    wind_direction_deg: Optional[float] = None
    dew_point_c: Optional[float] = None
    apparent_temp_c: Optional[float] = None
    precip_prob_pct: Optional[float] = None
    visibility_m: Optional[float] = None
    shortwave_radiation_wm2: Optional[float] = None
    direct_radiation_wm2: Optional[float] = None
    diffuse_radiation_wm2: Optional[float] = None
    direct_normal_irradiance_wm2: Optional[float] = None
    global_tilted_irradiance_wm2: Optional[float] = None
    cape_jkg: Optional[float] = None
    uv_index: Optional[float] = None
    valid_time: Optional[datetime] = None
    native_resolution_minutes: Optional[float] = None
    alignment_method: str = ""
    quality_flags: tuple = ()
    rain_proxy_mm: Optional[float] = None
    rain_value_type: str = "model_amount"
    rain_interval_start: Optional[datetime] = None
    rain_interval_end: Optional[datetime] = None
    model_run_at: Optional[datetime] = None
    original_fetched_at: Optional[datetime] = None
    is_cached: bool = False
    cache_age_hours: float = 0.0
    precipitation_intervals: tuple = ()
    model_elevation_m: Optional[float] = None
    model_elevation_basis: str = "unknown"


@dataclass
class SourceResult:
    source_id: str
    provider: str
    success: bool
    points: dict
    error: str = ""
    request_url: str = ""
    raw_payload: Optional[Any] = None
    payload_saved_path: str = ""
    base_weight: float = 1.0
    http_status: Optional[int] = None
    duration_ms: Optional[int] = None
    error_content_type: str = ""
    error_snippet: str = ""
    is_cached: bool = False
    live_fetch_success: bool = False
    original_fetched_at: Optional[datetime] = None
    model_run_at: Optional[datetime] = None
    cache_age_hours: float = 0.0
    model_family: str = ""
    availability_reason: str = "unknown"
    requested_source_id: str = ""
    is_backup: bool = False
    model_identifier: str = ""


DEFAULT_LOCATION_PRESET_DATA = {
    "dago": {"location_name": "Dago, Bandung", "adm4": "32.73.02.1004", "latitude": -6.8890, "longitude": 107.6100,
             "bmkg_point_name": "Dago", "area_level": "kelurahan", "is_proxy_bmkg": False,
             "note": "BMKG point: Dago, Coblong, Kota Bandung"},
    "jatinangor": {"location_name": "Jatinangor, Sumedang", "adm4": "32.11.15.2002", "latitude": -6.9380, "longitude": 107.7556,
                   "bmkg_point_name": "Hegarmanah", "area_level": "kecamatan", "is_proxy_bmkg": True,
                   "note": "BMKG representative point: Hegarmanah, Kecamatan Jatinangor"},
    "arjawinangun": {"location_name": "Arjawinangun, Cirebon", "adm4": "32.09.24.2004", "latitude": -6.6453, "longitude": 108.4103,
                     "bmkg_point_name": "Arjawinangun", "area_level": "kecamatan", "is_proxy_bmkg": False,
                     "note": "BMKG point: Arjawinangun, Kecamatan Arjawinangun"},
}
DEFAULT_MULTI_LOCATION_SLUGS = ["dago", "jatinangor", "arjawinangun"]
ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS = list(DEFAULT_MULTI_LOCATION_SLUGS)


# ---------------------------------------------------------------------------
# Logging & file helpers
# ---------------------------------------------------------------------------
def log_info(*args):
    message = " ".join(str(a) for a in args)
    print("[INFO]", message)
    if LOGGER.handlers:
        LOGGER.info(message)


def log_debug(*args):
    if DEBUG:
        message = " ".join(str(a) for a in args)
        print("[DEBUG]", message)
        if LOGGER.handlers:
            LOGGER.debug(message)


def log_warning(*args):
    message = " ".join(str(a) for a in args)
    print("[WARN]", message)
    if LOGGER.handlers:
        LOGGER.warning(message)


def batch_info(*args):
    print("[INFO]", " ".join(str(a) for a in args))


def batch_warning(*args):
    print("[WARN]", " ".join(str(a) for a in args))


def ensure_directory(path):
    os.makedirs(path, exist_ok=True)


def sanitize_filename(text):
    cleaned = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in text)
    return cleaned.strip("_") or "unknown"


def root_output_dir():
    path = os.path.join(BASE_DIR, OUTPUT_ROOT_DIRNAME)
    ensure_directory(path)
    return path


def root_output_path(filename):
    return os.path.join(root_output_dir(), filename)


def path_config(filename):
    return os.path.join(BASE_DIR, filename)


def set_active_output_dir(location_slug):
    global ACTIVE_OUTPUT_DIR
    ACTIVE_OUTPUT_DIR = os.path.join(root_output_dir(), sanitize_filename(location_slug))
    ensure_directory(ACTIVE_OUTPUT_DIR)


def path_output(filename):
    ensure_directory(ACTIVE_OUTPUT_DIR)
    return os.path.join(ACTIVE_OUTPUT_DIR, filename)


def atomic_write_bytes(path, writer_fn):
    directory = os.path.dirname(path) or "."
    ensure_directory(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{sanitize_filename(os.path.basename(path))}_", suffix=".tmp", text=False)
    try:
        with os.fdopen(fd, "wb") as f:
            writer_fn(f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, writer_fn, newline=None):
    directory = os.path.dirname(path) or "."
    ensure_directory(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{sanitize_filename(os.path.basename(path))}_", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", newline=newline, encoding="utf-8") as f:
            writer_fn(f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def safe_float(value):
    if value is None or value == "":
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def round_or_blank(value, digits=2):
    return "" if value is None else round(value, digits)


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _quality_first(row, *keys, default=None):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _quality_datetime(value, tz_name=DEFAULT_TIMEZONE):
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return None  # timestamp tanpa offset tidak aman diinterpretasi
    return dt.astimezone(ZoneInfo(tz_name))


def _quality_jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _quality_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_quality_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(_quality_jsonable(v) for v in value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    return value


def write_csv(path, headers, rows):
    def writer_fn(f):
        writer = csv.writer(f, delimiter=CSV_DELIMITER)
        writer.writerow(headers)
        writer.writerows(rows)
    atomic_write_text(path, writer_fn, newline="")


_CSV_ALIASES = {
    "bin": ["probability_bin"], "probability_bin": ["bin"],
    "mean_forecast_pct": ["mean_forecast_probability"], "mean_forecast_probability": ["mean_forecast_pct"],
    "observed_frequency_pct": ["observed_rain_frequency"], "observed_rain_frequency": ["observed_frequency_pct"],
    "matched_cases": ["n", "cases"], "n": ["matched_cases", "cases"], "cases": ["n", "matched_cases"],
}


def write_dict_csv(path, fieldnames=None, rows=None):
    """write_dict_csv(path, fieldnames, rows) atau write_dict_csv(path, rows). Field ekstra diabaikan."""
    if rows is None and isinstance(fieldnames, (list, tuple)) and fieldnames and isinstance(fieldnames[0], dict):
        rows, fieldnames = fieldnames, None
    rows = list(rows or [])
    names = list(fieldnames or [])
    if not names:
        seen = {}
        for row in rows:
            if isinstance(row, dict):
                for key in row:
                    seen[key] = None
        names = list(seen) or ["empty"]

    def pick(row, name):
        if not isinstance(row, dict):
            return ""
        if name in row:
            return row.get(name, "")
        for alt in _CSV_ALIASES.get(name, []):
            if alt in row:
                return row.get(alt, "")
        return ""

    def writer_fn(f):
        writer = csv.DictWriter(f, fieldnames=names, delimiter=CSV_DELIMITER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({n: pick(row, n) for n in names})

    atomic_write_text(path, writer_fn, newline="")


def write_json(path, payload, indent=2):
    atomic_write_text(path, lambda f: json.dump(_quality_jsonable(payload), f, ensure_ascii=False, indent=indent, allow_nan=False))


def write_json_gz(path, payload):
    encoded = json.dumps(_quality_jsonable(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def writer(f):
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as compressed:
            compressed.write(encoded)

    atomic_write_bytes(path, writer)


def _quarantine(path, reason):
    try:
        target = f"{path}.corrupt-{int(time.time())}"
        os.replace(path, target)
        print(f"[WARN] Berkas rusak dikarantina: {path} -> {target} ({reason})")
    except OSError:
        print(f"[WARN] Berkas rusak tidak dapat dikarantina: {path} ({reason})")


def read_json(path, default=None):
    """Baca JSON apa adanya. Tidak ada auto-repair; berkas rusak dikarantina dan default dikembalikan."""
    if not os.path.exists(path):
        return default
    try:
        if path.lower().endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, gzip.BadGzipFile, zlib.error, EOFError, UnicodeDecodeError) as exc:
        _quarantine(path, type(exc).__name__)
        return default
    except OSError as exc:
        print(f"[WARN] Tidak dapat membaca {path}: {exc}")
        return default


def read_dict_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = CSV_DELIMITER
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"]).delimiter or delimiter
        except Exception:
            pass
        return list(csv.DictReader(f, delimiter=delimiter))


# ---------------------------------------------------------------------------
# Waktu & format
# ---------------------------------------------------------------------------
def now_local(tz_name):
    return datetime.now(ZoneInfo(tz_name))


def parse_local_hour_string(target_date, jam, tz_name):
    return datetime.strptime(f"{target_date.isoformat()} {jam}:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(tz_name))


def parse_naive_local_datetime(text, tz_name):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(tz_name))


def parse_open_meteo_time(text, tz_name):
    dt_obj = datetime.fromisoformat(text)
    if dt_obj.tzinfo is not None:
        return dt_obj.astimezone(ZoneInfo(tz_name))
    return dt_obj.replace(tzinfo=ZoneInfo(tz_name))


def parse_utc_iso_to_local(text, tz_name):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(ZoneInfo(tz_name))


def parse_iso_date(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


def parse_display_date(text):
    return datetime.strptime(text, "%d-%m-%Y").date()


def build_hourly_targets(step_minutes=60):
    if step_minutes <= 0 or (1440 % step_minutes) != 0:
        raise ValueError("step_minutes harus membagi 1440 (mis. 60, 30, 15)")
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 24 * 60, step_minutes)]


def iter_dates(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def build_url(base_url, params):
    return f"{base_url}?{urllib.parse.urlencode(params)}"


def build_ssl_context():
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


_DAYS_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
_MONTHS_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]


def format_date_id(d):
    return f"{_DAYS_ID[d.weekday()]}, {d.day} {_MONTHS_ID[d.month - 1]} {d.year}"


def timezone_label(tz_name):
    return {"Asia/Jakarta": "WIB", "Asia/Makassar": "WITA", "Asia/Jayapura": "WIT"}.get(str(tz_name), str(tz_name or "waktu lokal"))


def pretty_dt(dt):
    if dt is None:
        return "belum tersedia"
    return f"{format_date_id(dt.date())}, {dt:%H:%M} {timezone_label(getattr(dt.tzinfo, 'key', str(dt.tzinfo)))}"


# ---------------------------------------------------------------------------
# Skalar umum
# ---------------------------------------------------------------------------
def _lg_float(x, default=None):
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.strip().replace("%", "").replace(",", ".")
            if not x or x in {"—", "-"} or x.lower() in {"none", "nan", "null", "belum tersedia"}:
                return default
        value = float(x)
        return default if (math.isnan(value) or math.isinf(value)) else value
    except Exception:
        return default


def _lg_prob(x, default=None, *, unit="percent"):
    value = _lg_float(x, None)
    if value is None:
        return default
    if unit == "fraction":
        value *= 100
    elif unit != "percent":
        raise ValueError("Probability unit must be percent or fraction")
    return round(value, 4) if 0 <= value <= 100 else default


def _lg_text(x, default=""):
    if x is None:
        return default
    text = str(x).strip()
    if not text or text in {"—", "-"} or text.lower() in {"none", "nan", "null", "undefined"}:
        return default
    return text


def _lg_esc(x, default=""):
    return html.escape(_lg_text(x, default), quote=True)


def _lg_num(x, suffix="", digits=1, default="—"):
    value = _lg_float(x, None)
    if value is None:
        return default
    return f"{round(value):.0f}{suffix}" if digits == 0 else f"{value:.{digits}f}{suffix}"


def _lg_pct(x, default="—"):
    value = _lg_prob(x)
    return default if value is None else f"{value:.0f}%"


def _lg_hour(x, default="—"):
    text = _lg_text(x, default)
    if text == default:
        return default
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    if len(text) >= 5 and text[2] == ":" and text[:2].isdigit() and text[3:5].isdigit():
        return text[:5]
    if len(text) >= 2 and text[:2].isdigit():
        return f"{text[:2]}:00"
    return default


def _lg_hour_int(hour, default=0):
    try:
        return int(clamp(int(_lg_hour(hour, "00:00")[:2]), 0, 23))
    except Exception:
        return default


def _lg_clean_join(value, sep=" · ", default="—"):
    if value is None:
        return default
    if isinstance(value, (list, tuple, set)):
        parts = [p for p in (_lg_text(v, "") for v in value) if p]
        return sep.join(parts) if parts else default
    return _lg_text(value, default).replace("<br>", sep).replace("<br/>", sep).replace("<br />", sep)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class HttpPayloadError(ValueError):
    def __init__(self, message, *, status=None, content_type="", snippet=""):
        super().__init__(message)
        self.status = status
        self.content_type = content_type or ""
        self.snippet = snippet or ""


def _looks_like_html(text):
    head = (text or "").lstrip().lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or head.startswith("<head")


def _decode_http_bytes(raw_bytes, encoding_header):
    encoding = (encoding_header or "").lower()
    if raw_bytes[:2] == b"\x1f\x8b" and "gzip" not in encoding:
        encoding = (encoding + ",gzip").strip(",")
    if "gzip" in encoding:
        try:
            raw_bytes = gzip.decompress(raw_bytes)
        except OSError:
            pass
    if "deflate" in encoding:
        try:
            raw_bytes = zlib.decompress(raw_bytes)
        except zlib.error:
            try:
                raw_bytes = zlib.decompress(raw_bytes, -zlib.MAX_WBITS)
            except zlib.error:
                pass
    return raw_bytes


def http_get_json(url, headers=None, timeout=HTTP_TIMEOUT_SECONDS):
    effective = dict(DEFAULT_HTTP_HEADERS)
    if headers:
        effective.update(headers)
    request = urllib.request.Request(url, headers=effective)
    ctx = build_ssl_context() if url.lower().startswith("https://") else None
    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout, context=ctx) as response:
        status = getattr(response, "status", None) or response.getcode()
        charset = response.headers.get_content_charset() or "utf-8"
        encoding = response.headers.get("Content-Encoding") or ""
        content_type = response.headers.get("Content-Type") or ""
        raw = _decode_http_bytes(response.read(), encoding)
        payload = raw.decode(charset, errors="replace")
        duration_ms = int((time.time() - started) * 1000)
        if not payload.strip():
            raise HttpPayloadError(f"Empty response body (status={status}, content_type={content_type})", status=status, content_type=content_type)
        if "json" not in content_type.lower() and _looks_like_html(payload):
            snippet = payload.strip().replace("\n", " ")[:200]
            raise HttpPayloadError(f"Non-JSON HTML response (status={status}, content_type={content_type}): {snippet}", status=status, content_type=content_type, snippet=snippet)
        try:
            return json.loads(payload), status, duration_ms
        except json.JSONDecodeError as exc:
            snippet = payload.strip().replace("\n", " ")[:200]
            raise HttpPayloadError(f"JSON decode failed (status={status}, content_type={content_type}): {snippet}", status=status, content_type=content_type, snippet=snippet) from exc


def _parse_retry_after_seconds(exc):
    try:
        value = (getattr(exc, "headers", None) or {}).get("Retry-After")
        return int(value.strip()) if value and value.strip().isdigit() else None
    except Exception:
        return None


def _backoff_seconds(attempt):
    base = max(RETRY_BACKOFF_SECONDS, 0.5)
    return min(base * (2 ** (attempt - 1)) * random.uniform(0.7, 1.4), 45)


def _http_fetch_with_retry(url, headers=None, source_id="UNKNOWN", timeout=HTTP_TIMEOUT_SECONDS, max_retry=None):
    max_retry = max_retry or MAX_RETRY_HTTP
    last_error = None
    for attempt in range(1, max_retry + 1):
        try:
            log_debug(source_id, "HTTP attempt", attempt, url)
            return http_get_json(url, headers=headers, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_error = exc
            status = getattr(exc, "code", None)
            log_debug(source_id, "HTTPError:", status, exc)
            if status in {400, 401, 403, 404}:
                raise
            retryable = status == 429 or (status is not None and 500 <= status <= 599)
            if not retryable or attempt >= max_retry:
                raise
            retry_after = _parse_retry_after_seconds(exc)
            time.sleep(min(max(retry_after, 1), 60) if retry_after is not None else _backoff_seconds(attempt))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, HttpPayloadError) as exc:
            last_error = exc
            log_debug(source_id, "Network/Decode error:", exc)
            if attempt >= max_retry:
                break
            time.sleep(_backoff_seconds(attempt))
    raise last_error


_TRANSPORT_CACHE = {}
_TRANSPORT_LOCK = threading.Lock()
_FETCH_ORIGINALS = {}


def clear_transport_cache():
    with _TRANSPORT_LOCK:
        _TRANSPORT_CACHE.clear()


def fetch_json_with_retry(url, headers=None, source_id="UNKNOWN", timeout=HTTP_TIMEOUT_SECONDS, max_retry=None):
    """Return (payload, http_status, duration_ms). URL identik dalam satu siklus dilayani dari memo."""
    key = (url, tuple(sorted((headers or {}).items())))
    with _TRANSPORT_LOCK:
        memo = _TRANSPORT_CACHE.get(key)
    if memo and time.monotonic() - memo["t"] < TRANSPORT_MEMO_TTL_SECONDS:
        _FETCH_ORIGINALS[source_id] = memo["fetched_at"]
        return memo["result"]
    result = _http_fetch_with_retry(url, headers=headers, source_id=source_id, timeout=timeout, max_retry=max_retry)
    fetched = now_local(DEFAULT_TIMEZONE)
    with _TRANSPORT_LOCK:
        _TRANSPORT_CACHE[key] = {"t": time.monotonic(), "fetched_at": fetched, "result": result}
    _FETCH_ORIGINALS[source_id] = fetched
    return result


# ---------------------------------------------------------------------------
# Matematika, bobot, kesehatan sumber
# ---------------------------------------------------------------------------
def nearest_candidate(candidates, target_dt, max_gap_hours=4):
    best = None
    for item in candidates:
        delta = abs((item["dt"] - target_dt).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, item)
    if best is None or best[0] > max_gap_hours * 3600:
        return None
    return best[1]


def weighted_mean_std(weighted_pairs):
    valid = [(v, w) for v, w in weighted_pairs if v is not None and w is not None and w > 0]
    if not valid:
        return None, None
    total = sum(w for _, w in valid)
    if total <= 0:
        return None, None
    mean = sum(v * w for v, w in valid) / total
    variance = sum(w * (v - mean) ** 2 for v, w in valid) / total
    return round(mean, 2), round(math.sqrt(variance), 2)


def source_base_weight(source_id):
    return SOURCE_BASE_WEIGHTS.get(source_id, 1.0)


def source_active_weight(source_id):
    return ACTIVE_SOURCE_WEIGHTS.get(source_id, source_base_weight(source_id))


def hour_bucket_for_time(jam_text):
    try:
        hh = int(str(jam_text).split(":")[0])
    except Exception:
        return ""
    return "00-05" if hh <= 5 else "06-11" if hh <= 11 else "12-17" if hh <= 17 else "18-23" if hh <= 23 else ""


def load_weight_config(use_legacy=False):
    """Bobot dasar selalu dipakai. source_weights.json lama (tanpa validasi holdout) diabaikan
    kecuali --use-legacy-weights diberikan."""
    global ACTIVE_SOURCE_WEIGHTS
    ACTIVE_SOURCE_WEIGHTS = dict(SOURCE_BASE_WEIGHTS)
    path = path_output(WEIGHTS_FILENAME)
    if not os.path.exists(path):
        return
    if not use_legacy:
        log_debug("source_weights.json lama diabaikan:", path)
        return
    payload = read_json(path, default=None) or {}
    for source_id, value in (payload.get("weights") or {}).items():
        parsed = safe_float(value)
        if parsed is not None and parsed > 0:
            ACTIVE_SOURCE_WEIGHTS[source_id] = round(parsed, 4)


def load_health_config():
    global SOURCE_HEALTH
    payload = read_json(path_output(HEALTH_FILENAME), default=None)
    SOURCE_HEALTH = payload.get("sources", {}) if payload else {}


def save_health_config(results, args, target_date=None):
    if args.disable_health:
        return
    doc = read_json(path_output(HEALTH_FILENAME), {}) or {}
    sources = doc.get("sources") or {}
    for result in results:
        _quality_result_metadata(result, args)
        h = sources.get(result.source_id) or {"ema_success": 1, "ema_completeness": 1, "consecutive_failures": 0}
        live = result.live_fetch_success
        h["ema_success"] = round(0.65 * h.get("ema_success", 1) + 0.35 * int(live), 4)
        completeness = sum(p.gap_minutes == 0 and any(getattr(p, k) is not None for k in ("temp_c", "rh_pct", "rain_mm", "wind_kmh")) for p in result.points.values()) / max(1, len(TARGET_TIMES))
        h["ema_completeness"] = round(0.65 * h.get("ema_completeness", 1) + 0.35 * (completeness if live else 0), 4)
        h["consecutive_failures"] = 0 if live else h.get("consecutive_failures", 0) + 1
        h.update({"last_error": result.error, "last_run_date": (target_date or now_local(args.timezone).date()).isoformat(),
                  "last_live_fetch_success": live, "last_served_from_cache": result.is_cached})
        sources[result.source_id] = h
    write_json(path_output(HEALTH_FILENAME), {"generated_at": now_local(args.timezone).isoformat(), "sources": sources})
    load_health_config()


def source_health_factor(source_id):
    health = SOURCE_HEALTH.get(source_id) or {}
    ema_success = safe_float(health.get("ema_success"))
    ema_completeness = safe_float(health.get("ema_completeness"))
    consecutive = int(health.get("consecutive_failures", 0) or 0)
    ema_success = 1.0 if ema_success is None else ema_success
    ema_completeness = 1.0 if ema_completeness is None else ema_completeness
    penalty = clamp(1 - min(consecutive, MAX_CONSECUTIVE_FAILURE_PENALTY) * 0.08, 0.55, 1.0)
    return round(clamp((0.55 + ema_success * 0.30 + ema_completeness * 0.15) * penalty, 0.45, 1.05), 4)


def _quality_family(source_id):
    return _SOURCE_FAMILIES.get(source_id, source_id)


def _quality_set_families(points):
    global _QUALITY_FAMILY_COUNTS
    ids = {p.source_id for p in points if any(getattr(p, k, None) is not None for k in ("temp_c", "rh_pct", "rain_mm", "wind_kmh"))}
    _QUALITY_FAMILY_COUNTS = {}
    for sid in ids:
        fam = _quality_family(sid)
        _QUALITY_FAMILY_COUNTS[fam] = _QUALITY_FAMILY_COUNTS.get(fam, 0) + 1


def point_weight(point):
    base = source_active_weight(point.source_id) * source_health_factor(point.source_id)
    base /= max(1, _QUALITY_FAMILY_COUNTS.get(_quality_family(point.source_id), 1))
    gap = safe_float(point.gap_minutes) or 0
    completeness = sum(getattr(point, k, None) is not None for k in ("temp_c", "rh_pct", "rain_mm", "wind_kmh")) / 4
    age = safe_float(getattr(point, "cache_age_hours", 0)) or 0
    cache_factor = max(0.1, math.exp(-age / 6)) if getattr(point, "is_cached", False) else 1
    return round(base * clamp(1 - gap / 240, 0.55, 1) * (0.7 + 0.3 * completeness) * cache_factor, 6)


def _quality_skill_section(variable, valid_time, issued_at=None):
    """Prioritaskan scope dari kohort diterima terbaru; fallback ke scope lain yang diterima."""
    sections = (_QUALITY_SKILL.get("variables") or {}).get(variable) or {}
    context = _quality_context(valid_time, issued_at)
    keys = ("lead:" + context["lead"], "hour:" + context["hour"], "month:" + context["month"], "all")
    eligible = [sections[k] for k in keys if sections.get(k) and sections[k].get("accepted")]
    newest = _QUALITY_SKILL.get("cohort_id")
    return next((s for s in eligible if newest and s.get("cohort_id") == newest), eligible[0] if eligible else {})


def point_variable_weight(point, variable):
    valid = getattr(point, "valid_time", None) or point.source_datetime
    section = _quality_skill_section(variable, valid, _RUN_ISSUED_AT)
    multiplier = safe_float((section.get("multipliers") or {}).get(point.source_id)) or 1
    return point_weight(point) * multiplier


def confidence_label(score):
    return "Tinggi" if score >= 80 else "Sedang" if score >= 60 else "Rendah"


def expected_total_weight():
    return round(sum(source_active_weight(i["source_id"]) for i in ACTIVE_SOURCE_CONFIGS), 4)


def compute_confidence(bucket, total_weight, dominant_weight, temp_std, rh_std, rain_std):
    if not bucket:
        return 0.0, "Rendah"
    family_count = len({_quality_family(p.source_id) for p in bucket})
    expected_sources = max(len({_quality_family(c["source_id"]) for c in ACTIVE_SOURCE_CONFIGS}), 1)
    expected_weight = max(expected_total_weight(), 0.0001)
    coverage = clamp(family_count / expected_sources * 100, 0, 100)
    weight_score = clamp(total_weight / expected_weight * 100, 0, 100)
    agreement = clamp(dominant_weight / total_weight * 100, 0, 100) if total_weight else 0
    spread = []
    if temp_std is not None:
        spread.append(clamp(100 - temp_std * 10, 20, 100))
    if rh_std is not None:
        spread.append(clamp(100 - rh_std * 1.5, 20, 100))
    if rain_std is not None:
        spread.append(clamp(100 - rain_std * 15, 20, 100))
    spread_score = sum(spread) / len(spread) if spread else 40
    score = coverage * 0.35 + weight_score * 0.25 + agreement * 0.25 + spread_score * 0.15
    if family_count < MIN_SOURCES_FOR_HIGH_CONFIDENCE:
        score = min(score, 59.0)
    score = round(clamp(score, 0, 100), 1)
    return score, confidence_label(score)


def median(values):
    cleaned = sorted(v for v in values if v is not None)
    if not cleaned:
        return None
    mid = len(cleaned) // 2
    return cleaned[mid] if len(cleaned) % 2 else (cleaned[mid - 1] + cleaned[mid]) / 2


def robust_outlier_bounds(values, threshold=OUTLIER_Z_THRESHOLD):
    cleaned = [v for v in values if v is not None]
    if len(cleaned) < 4:
        return None, None
    med = median(cleaned)
    mad = median([abs(v - med) for v in cleaned])
    if mad in (None, 0):
        return None, None
    scale = 1.4826 * mad
    return med - threshold * scale, med + threshold * scale


def filter_weighted_pairs(weighted_pairs):
    lower, upper = robust_outlier_bounds([v for v, _ in weighted_pairs])
    if lower is None:
        return weighted_pairs
    filtered = [(v, w) for v, w in weighted_pairs if v is not None and lower <= v <= upper]
    return filtered if filtered else weighted_pairs


def heat_index(temp_c, rh):
    if temp_c is None or rh is None:
        return None
    temp_f = temp_c * 9 / 5 + 32
    if temp_f < 80 or rh < 40:
        return round(temp_c, 2)
    hi_f = (-42.379 + 2.04901523 * temp_f + 10.14333127 * rh - 0.22475541 * temp_f * rh - 0.00683783 * temp_f ** 2
            - 0.05481717 * rh ** 2 + 0.00122874 * temp_f ** 2 * rh + 0.00085282 * temp_f * rh ** 2 - 0.00000199 * temp_f ** 2 * rh ** 2)
    if rh < 13 and 80 <= temp_f <= 112:
        hi_f -= ((13 - rh) / 4) * math.sqrt((17 - abs(temp_f - 95)) / 17)
    elif rh > 85 and 80 <= temp_f <= 87:
        hi_f += ((rh - 85) / 10) * ((87 - temp_f) / 5)
    return round(max(temp_c, (hi_f - 32) * 5 / 9), 2)


# ---------------------------------------------------------------------------
# Kategori cuaca
# ---------------------------------------------------------------------------
def kategori_hujan(mm):
    if mm is None or mm <= 0:
        return "Berawan"
    return "Hujan Ringan" if mm <= 5 else "Hujan Sedang" if mm <= 10 else "Hujan Lebat"


def bmkg_to_kategori(cuaca):
    text = (cuaca or "").lower()
    if "cerah berawan" in text:
        return "Cerah Berawan"
    if "cerah" in text:
        return "Cerah"
    if "lebat" in text or "badai" in text or "petir" in text:
        return "Hujan Lebat"
    if "sedang" in text:
        return "Hujan Sedang"
    if "ringan" in text or "gerimis" in text:
        return "Hujan Ringan"
    return "Berawan"


def bmkg_rain_proxy_mm(cuaca):
    return {"Hujan Ringan": 1.5, "Hujan Sedang": 6.0, "Hujan Lebat": 15.0}.get(bmkg_to_kategori(cuaca), 0.0)


def infer_kategori_non_hujan(temp_c, rh):
    if rh is None:
        return "Berawan"
    return "Cerah" if rh <= 70 else "Cerah Berawan" if rh <= 85 else "Berawan"


def category_from_wmo_code(weather_code, rain_mm, rh):
    if weather_code is None:
        return kategori_hujan(rain_mm) if rain_mm is not None and rain_mm > 0 else infer_kategori_non_hujan(None, rh)
    code = int(weather_code)
    if code == 0:
        return "Cerah"
    if code in (1, 2):
        return "Cerah Berawan"
    if code in (3, 45, 48):
        return "Berawan"
    if code in (51, 53, 55, 56, 57, 61, 80):
        return "Hujan Ringan"
    if code in (63, 66, 81):
        return "Hujan Sedang"
    if code in (65, 67, 82, 95, 96, 99):
        return "Hujan Lebat"
    if code in (71, 73, 75, 77, 85, 86):
        return kategori_hujan(rain_mm if rain_mm is not None else 1)
    if rain_mm is not None and rain_mm > 0:
        return kategori_hujan(rain_mm)
    return infer_kategori_non_hujan(None, rh)


def category_from_metno_symbol(symbol_code, rain_mm, rh):
    text = (symbol_code or "").lower()
    if "clearsky" in text:
        return "Cerah"
    if "fair" in text or "partlycloudy" in text:
        return "Cerah Berawan"
    if "cloudy" in text or "fog" in text:
        return "Berawan"
    if "heavyrain" in text or "thunder" in text:
        return "Hujan Lebat"
    if any(k in text for k in ("rain", "drizzle", "sleet", "snow")):
        return kategori_hujan(rain_mm if rain_mm is not None else 1)
    if rain_mm is not None and rain_mm > 0:
        return kategori_hujan(rain_mm)
    return infer_kategori_non_hujan(None, rh)


def validate_point_values(temp_c, rh_pct, rain_mm, wind_kmh):
    raw_values = (temp_c, rh_pct, rain_mm, wind_kmh)
    values = [None if isinstance(v, bool) else safe_float(v) for v in raw_values]
    flags = []
    for idx, (name, low, high) in enumerate((("temp", -80, 60), ("rh", 0, 100), ("rain", 0, 1000), ("wind", 0, 500))):
        value = values[idx]
        if isinstance(raw_values[idx], bool):
            flags.append(name + "_invalid_type")
        elif value is not None and not low <= value <= high:
            values[idx] = None
            flags.append(name + "_out_of_range")
    return (*values, flags)


# ---------------------------------------------------------------------------
# Ekstraksi titik prakiraan
# ---------------------------------------------------------------------------
def _quality_array_value(hourly, field, index):
    array = hourly.get(field) or []
    value = array[index] if index < len(array) else None
    return None if isinstance(value, bool) else safe_float(value)


def _quality_native_step(times):
    diffs = sorted((b - a).total_seconds() / 60 for a, b in zip(times, times[1:]) if b > a)
    return diffs[len(diffs) // 2] if diffs else None


def extract_bmkg_points(target_date, payload, args):
    items = payload.get("data") or []
    if not items:
        raise ValueError("BMKG response tidak memiliki data")
    candidates = []
    for group in items[0].get("cuaca") or []:
        for item in group:
            if not item.get("local_datetime"):
                continue
            dt = parse_naive_local_datetime(item["local_datetime"], args.timezone)
            temp, rh, _, wind, flags = validate_point_values(item.get("t"), item.get("hu"), None, item.get("ws"))
            candidates.append({"dt": dt, "item": item, "temp": temp, "rh": rh, "wind": wind, "flags": flags})
    points = {}
    for jam in TARGET_TIMES:
        valid = parse_local_hour_string(target_date, jam, args.timezone)
        match = nearest_candidate(candidates, valid, 1.5)
        if match is None:
            continue
        item = match["item"]
        gap = abs((match["dt"] - valid).total_seconds()) / 60
        points[jam] = ForecastPoint(
            "BMKG", "BMKG", jam, match["dt"], match["temp"], match["rh"], None, match["wind"],
            bmkg_to_kategori(item.get("weather_desc")), item.get("weather_desc") or "", gap,
            cloud_cover_pct=safe_float(item.get("tcc")), valid_time=valid, native_resolution_minutes=180,
            alignment_method="exact" if gap == 0 else "nearest_category_and_instant",
            quality_flags=tuple(match["flags"]), rain_proxy_mm=bmkg_rain_proxy_mm(item.get("weather_desc")),
            rain_value_type="category_only", model_run_at=_quality_datetime(item.get("analysis_date"), args.timezone))
    if not points:
        raise ValueError("BMKG tidak mengembalikan kandidat target date")
    return points


_OM_EXTRA = {
    "apparent_temp_c": "apparent_temperature", "dew_point_c": "dew_point_2m", "precip_prob_pct": "precipitation_probability",
    "cloud_cover_pct": "cloud_cover", "pressure_msl_hpa": "pressure_msl", "surface_pressure_hpa": "surface_pressure",
    "wind_direction_deg": "wind_direction_10m", "wind_gusts_kmh": "wind_gusts_10m", "visibility_m": "visibility",
    "cape_jkg": "cape", "uv_index": "uv_index", "shortwave_radiation_wm2": "shortwave_radiation",
    "direct_radiation_wm2": "direct_radiation", "diffuse_radiation_wm2": "diffuse_radiation",
    "direct_normal_irradiance_wm2": "direct_normal_irradiance", "global_tilted_irradiance_wm2": "global_tilted_irradiance",
}


def extract_open_meteo_points(target_date, payload, config, args):
    hourly = payload.get("hourly") or {}
    times = [parse_open_meteo_time(t, args.timezone) for t in hourly.get("time") or []]
    if not times:
        raise ValueError("Open-Meteo response tidak memiliki hourly.time")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Open-Meteo hourly timestamps must be unique and strictly increasing")
    units = payload.get("hourly_units") or {}
    if (units.get("precipitation", "mm") != "mm" or units.get("temperature_2m", "°C") not in {"°C", "celsius"}
            or units.get("wind_speed_10m", "km/h") not in {"km/h", "kmh"}):
        raise ValueError("Unsupported units in Open-Meteo payload")
    candidates = [{"dt": dt, "index": i} for i, dt in enumerate(times)]
    elevation = safe_float(payload.get("elevation"))
    points = {}
    for jam in TARGET_TIMES:
        valid = parse_local_hour_string(target_date, jam, args.timezone)
        match = nearest_candidate(candidates, valid, 1)
        if not match:
            continue
        idx = match["index"]
        exact = match["dt"] == valid
        temp, rh, rain, wind, flags = validate_point_values(*(_quality_array_value(hourly, k, idx) for k in ("temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m")))
        if not exact:
            rain = None
            flags.append("precipitation_interval_not_aligned")
        codes = hourly.get("weather_code") or []
        code = safe_float(codes[idx]) if idx < len(codes) else None
        kwargs = {attr: _quality_array_value(hourly, field, idx) for attr, field in _OM_EXTRA.items()}
        kwargs["precip_prob_pct"] = _lg_prob(kwargs["precip_prob_pct"]) if exact else None
        if kwargs["cloud_cover_pct"] is not None and not 0 <= kwargs["cloud_cover_pct"] <= 100:
            kwargs["cloud_cover_pct"] = None
            flags.append("cloud_out_of_range")
        points[jam] = ForecastPoint(
            config["source_id"], config["provider"], jam, match["dt"], temp, rh, rain, wind,
            category_from_wmo_code(code, rain, rh), f"wmo:{code}", abs((match["dt"] - valid).total_seconds()) / 60,
            **kwargs, valid_time=valid, native_resolution_minutes=_quality_native_step(times),
            alignment_method="exact" if exact else "nearest_instant_only", quality_flags=tuple(flags),
            rain_interval_start=valid - timedelta(hours=1) if exact and rain is not None else None,
            rain_interval_end=valid if exact and rain is not None else None,
            rain_value_type="model_amount" if exact and rain is not None else "missing",
            model_elevation_m=elevation, model_elevation_basis="api_downscaled_elevation")
    if not points:
        raise ValueError("Open-Meteo tidak mengembalikan kandidat target date")
    return points


def extract_met_no_points(target_date, payload, config, args):
    series = (payload.get("properties") or {}).get("timeseries") or []
    candidates, rain_by_end, intervals = [], {}, []
    for entry in series:
        dt = parse_utc_iso_to_local(entry["time"], args.timezone)
        data = entry.get("data") or {}
        details = (data.get("instant") or {}).get("details") or {}
        candidates.append({"dt": dt, "details": details})
        for name, hours in (("next_1_hours", 1), ("next_6_hours", 6), ("next_12_hours", 12)):
            bucket = data.get(name) or {}
            amount = safe_float((bucket.get("details") or {}).get("precipitation_amount"))
            if amount is None or amount < 0:
                continue
            record = {"interval_start": dt.isoformat(), "interval_end": (dt + timedelta(hours=hours)).isoformat(), "amount_mm": amount,
                      "duration_hours": hours, "symbol": (bucket.get("summary") or {}).get("symbol_code", "")}
            intervals.append(record)
            if hours == 1:
                rain_by_end[dt + timedelta(hours=1)] = record
    if not candidates:
        raise ValueError("METNO tidak memiliki timeseries")
    day_intervals = tuple(i for i in intervals if (_quality_datetime(i["interval_end"], args.timezone) or datetime.min.replace(tzinfo=ZoneInfo(args.timezone))).date() == target_date)
    step = _quality_native_step([c["dt"] for c in candidates])
    points = {}
    for jam in TARGET_TIMES:
        valid = parse_local_hour_string(target_date, jam, args.timezone)
        match = nearest_candidate(candidates, valid, 2)
        if not match:
            continue
        details = match["details"]
        rain = rain_by_end.get(valid)
        speed = safe_float(details.get("wind_speed"))
        temp, rh, amount, wind, flags = validate_point_values(
            details.get("air_temperature"), details.get("relative_humidity"), rain.get("amount_mm") if rain else None,
            speed * 3.6 if speed is not None else None)
        symbol = rain.get("symbol", "") if rain else ""
        category = category_from_metno_symbol(symbol, amount, rh) if rain else ""
        points[jam] = ForecastPoint(
            config["source_id"], config["provider"], jam, match["dt"], temp, rh, amount, wind, category, symbol,
            abs((match["dt"] - valid).total_seconds()) / 60, valid_time=valid,
            cloud_cover_pct=safe_float(details.get("cloud_area_fraction")), wind_direction_deg=safe_float(details.get("wind_from_direction")),
            pressure_msl_hpa=safe_float(details.get("air_pressure_at_sea_level")), dew_point_c=safe_float(details.get("dew_point_temperature")),
            native_resolution_minutes=step, alignment_method="exact_instant_with_aligned_precipitation" if match["dt"] == valid else "nearest_instant_only",
            quality_flags=tuple(flags), rain_interval_start=valid - timedelta(hours=1) if rain else None,
            rain_interval_end=valid if rain else None, rain_value_type="model_amount" if rain else "missing",
            precipitation_intervals=day_intervals)
    if not points:
        raise ValueError("METNO tidak mengembalikan kandidat target date")
    return points


# ---------------------------------------------------------------------------
# Cache, fetcher sumber, circuit breaker
# ---------------------------------------------------------------------------
def _raw_payload_ext(args):
    return ".json.gz" if args.compress_raw_payloads else ".json"


def _quality_payload_run(payload, args):
    if not isinstance(payload, dict):
        return None
    stamp = ((payload.get("properties") or {}).get("meta") or {}).get("updated_at")
    if stamp:
        return _quality_datetime(stamp, args.timezone)
    stamps = []
    for day in payload.get("data") or []:
        for group in day.get("cuaca") or []:
            for item in group:
                value = _quality_datetime(item.get("analysis_date"), args.timezone)
                if value:
                    stamps.append(value)
    return max(stamps) if stamps else None


def load_cached_source_payload(target_date, source_id, extractor_fn, args):
    directory = path_output(RAW_PAYLOAD_DIRNAME)
    if not os.path.isdir(directory):
        return None
    prefix = sanitize_filename(source_id.lower()) + "_"
    paths = [e.path for e in os.scandir(directory) if e.is_file() and e.name.lower().startswith(prefix) and e.name.lower().endswith((".json", ".json.gz"))]
    now = now_local(args.timezone)
    max_age = safe_float(getattr(args, "cache_max_age_hours", 6))
    if max_age is None or max_age <= 0:
        return None
    candidates = []
    for path in paths:
        doc = read_json(path, {}) or {}
        if not isinstance(doc.get("payload"), dict) or not doc.get("success"):
            continue
        if "cached:" in str(doc.get("request_url", "")) and not doc.get("original_fetched_at"):
            continue
        original = _quality_datetime(doc.get("original_fetched_at") or doc.get("generated_at"), args.timezone)
        run = _quality_datetime(doc.get("model_run_at"), args.timezone) or _quality_payload_run(doc["payload"], args)
        if original is None:
            continue
        age = (now - original).total_seconds() / 3600
        model_age = (now - run).total_seconds() / 3600 if run else age
        if age < 0 or age > max_age or model_age < 0 or model_age > max_age:
            continue
        if doc.get("location_slug") and doc["location_slug"] != getattr(args, "location_slug", ""):
            continue
        try:
            points = extractor_fn(target_date, doc["payload"])
        except (ValueError, TypeError, KeyError):
            continue
        if points:
            candidates.append((original, path, doc, points, age, run))
    if not candidates:
        return None
    original, path, doc, points, age, run = max(candidates, key=lambda item: item[0])
    for point in points.values():
        point.is_cached = True
        point.original_fetched_at = original
        point.cache_age_hours = age
        point.model_run_at = run
    return {"path": path, "payload": doc["payload"], "points": points, "request_url": doc.get("request_url", ""),
            "original_fetched_at": original.isoformat(), "cache_age_hours": age}


def _cache_fallback(target_date, source_id, extractor, args, url, label, exc):
    cached = load_cached_source_payload(target_date, source_id, extractor, args)
    if not cached:
        return None
    name = os.path.basename(cached["path"])
    note = f"Live {label} gagal ({exc}); memakai cache {name}"
    log_warning(note)
    return {"points": cached["points"], "raw_payload": cached["payload"], "request_url": f"{url} [cached:{name}]",
            "note": note, "http_status": None, "duration_ms": None}


def fetch_bmkg_forecast(target_date, config, args):
    url = build_url(BMKG_API_URL, {"adm4": args.adm4})
    headers = dict(BMKG_HTTP_HEADERS)
    if getattr(args, "bmkg_user_agent", ""):
        headers["User-Agent"] = args.bmkg_user_agent
    try:
        payload, status, duration_ms = fetch_json_with_retry(url, headers=headers, source_id=config["source_id"], timeout=args.http_timeout, max_retry=args.max_retry_http)
        points = extract_bmkg_points(target_date, payload, args)
        return {"points": points, "raw_payload": payload, "request_url": url, "http_status": status, "duration_ms": duration_ms}
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, HttpPayloadError, ValueError) as exc:
        fallback = _cache_fallback(target_date, "BMKG", lambda d, p: extract_bmkg_points(d, p, args), args, url, "BMKG", exc)
        if not fallback:
            raise
        return fallback


def aether_open_meteo_variables(args=None, include_extra=None):
    if include_extra is None:
        include_extra = bool(getattr(args, "aether_extra_vars", False)) if args is not None else False
    variables = list(AETHER_BASIC_OPEN_METEO_VARIABLES)
    if include_extra:
        variables += [v for v in AETHER_EXTRA_OPEN_METEO_VARIABLES if v not in variables]
    return variables


def fetch_open_meteo_forecast(target_date, config, args):
    horizon = getattr(args, "forecast_days_request", None) or max(3, min(5, (target_date - now_local(args.timezone).date()).days + 1))
    params = {"latitude": args.latitude, "longitude": args.longitude, "timezone": args.timezone, "forecast_days": horizon,
              "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm",
              "hourly": ",".join(aether_open_meteo_variables(args, include_extra=getattr(args, "aether_extra_vars", False)))}
    if config.get("models"):
        params["models"] = config["models"]
    url = build_url(config["endpoint"], params)
    try:
        try:
            payload, status, duration_ms = fetch_json_with_retry(url, source_id=config["source_id"], timeout=args.http_timeout, max_retry=args.max_retry_http)
        except Exception as first_exc:
            if not getattr(args, "aether_extra_vars", False):
                raise
            fallback_params = dict(params, hourly=",".join(aether_open_meteo_variables(args, include_extra=False)))
            url = build_url(config["endpoint"], fallback_params)
            log_warning(config["source_id"], "extra vars gagal, fallback variabel dasar:", first_exc)
            payload, status, duration_ms = fetch_json_with_retry(url, source_id=config["source_id"], timeout=args.http_timeout, max_retry=args.max_retry_http)
        points = extract_open_meteo_points(target_date, payload, config, args)
        return {"points": points, "raw_payload": payload, "request_url": url, "http_status": status, "duration_ms": duration_ms}
    except Exception as exc:
        fallback = _cache_fallback(target_date, config["source_id"], lambda d, p: extract_open_meteo_points(d, p, config, args), args, url, "Open-Meteo " + config["source_id"], exc)
        if not fallback:
            raise
        return fallback


def fetch_met_no_forecast(target_date, config, args):
    url = build_url(MET_NO_URL, {"lat": round(args.latitude, 4), "lon": round(args.longitude, 4)})
    headers = {"User-Agent": getattr(args, "metno_user_agent", "") or "LANGIT/62 (contact: local-script)", "Accept": "application/json",
               "Accept-Encoding": "gzip,deflate", "Connection": "close"}
    try:
        payload, status, latency = fetch_json_with_retry(url, headers=headers, source_id=config["source_id"], timeout=max(1, args.http_timeout), max_retry=max(1, args.max_retry_http))
        points = extract_met_no_points(target_date, payload, config, args)
        if not any(any(getattr(p, k) is not None for k in ("temp_c", "rh_pct", "rain_mm", "wind_kmh")) for p in points.values()):
            raise ValueError("MET.no contains no usable weather values")
        return {"points": points, "raw_payload": payload, "request_url": url, "http_status": status, "duration_ms": latency}
    except Exception as exc:
        fallback = _cache_fallback(target_date, config["source_id"], lambda d, p: extract_met_no_points(d, p, config, args), args, url, "METNO", exc)
        if not fallback:
            raise
        return fallback


def preview_request_url(config, args):
    kind = config["kind"]
    if kind == "bmkg":
        return build_url(BMKG_API_URL, {"adm4": args.adm4})
    if kind == "open_meteo":
        params = {"latitude": args.latitude, "longitude": args.longitude, "timezone": args.timezone, "forecast_days": 3,
                  "hourly": ",".join(AETHER_BASIC_OPEN_METEO_VARIABLES)}
        if config.get("models"):
            params["models"] = config["models"]
        return build_url(config["endpoint"], params)
    if kind == "met_no":
        return build_url(MET_NO_URL, {"lat": args.latitude, "lon": args.longitude})
    return ""


def _quality_result_metadata(result, args):
    now = now_local(args.timezone)
    original_live = _FETCH_ORIGINALS.get(result.source_id)
    if original_live and result.success and not any(p.is_cached for p in result.points.values()):
        for p in result.points.values():
            p.original_fetched_at = original_live
    result.is_cached = any(getattr(p, "is_cached", False) for p in result.points.values())
    result.live_fetch_success = result.success and not result.is_cached
    originals = [p.original_fetched_at for p in result.points.values() if p.original_fetched_at]
    result.original_fetched_at = min(originals) if originals else (now if result.live_fetch_success else None)
    result.model_run_at = _quality_payload_run(result.raw_payload, args)
    result.cache_age_hours = max((p.cache_age_hours or 0 for p in result.points.values()), default=0)
    result.model_family = _quality_family(result.source_id)
    for p in result.points.values():
        p.original_fetched_at = result.original_fetched_at
        p.model_run_at = p.model_run_at or result.model_run_at
    return result


def save_raw_payload_snapshot(target_date, result, tz_name, args):
    _quality_result_metadata(result, args)
    directory = path_output(RAW_PAYLOAD_DIRNAME)
    ensure_directory(directory)
    stub = sanitize_filename(result.source_id.lower())
    ext = _raw_payload_ext(args)
    stamp = now_local(tz_name).strftime("%Y%m%d_%H%M%S_%f")
    doc = {"generated_at": now_local(tz_name).isoformat(), "original_fetched_at": result.original_fetched_at,
           "model_run_at": result.model_run_at, "is_cached": result.is_cached, "live_fetch_success": result.live_fetch_success,
           "cache_age_hours": result.cache_age_hours, "location_slug": getattr(args, "location_slug", ""),
           "target_date": target_date.isoformat(), "source_id": result.source_id, "provider": result.provider,
           "model_family": result.model_family, "success": result.success, "request_url": result.request_url,
           "http_status": result.http_status, "duration_ms": result.duration_ms, "points_collected": len(result.points),
           "error": result.error, "payload": result.raw_payload}
    writer = write_json_gz if args.compress_raw_payloads else write_json
    path = os.path.join(directory, f"{stub}_{target_date:%Y%m%d}_{stamp}{ext}")
    writer(path, doc)
    writer(os.path.join(directory, f"{stub}_latest{ext}"), doc)
    if result.live_fetch_success:  # cache fallback tidak boleh menggeser pointer live-success
        writer(os.path.join(directory, f"{stub}_latest_success{ext}"), doc)
    elif not result.success:
        writer(os.path.join(directory, f"{stub}_latest_failure{ext}"), doc)
    return path


_HOST_SEMAPHORES_LOCK = threading.Lock()
_HOST_SEMAPHORES = {}
_HOST_CIRCUIT_LOCK = threading.Lock()
_HOST_CIRCUIT = {}


def _circuit_state(host):
    with _HOST_CIRCUIT_LOCK:
        return _HOST_CIRCUIT.setdefault(host, {"fails": 0, "open_until": 0.0, "last_error": "", "last_status": None})


def _circuit_is_open(host):
    until = float(_circuit_state(host).get("open_until") or 0.0)
    return until > time.time(), max(0.0, until - time.time())


def _circuit_note_failure(host, error_text, status, args):
    if not args.enable_circuit_breaker:
        return
    state = _circuit_state(host)
    state["fails"] = int(state.get("fails") or 0) + 1
    state["last_error"], state["last_status"] = str(error_text)[:300], status
    backoff = min(args.circuit_max_backoff_seconds, args.circuit_base_seconds * (2 ** (min(state["fails"], 6) - 1)))
    state["open_until"] = time.time() + backoff * random.uniform(0.8, 1.4)


def _circuit_note_success(host):
    state = _circuit_state(host)
    state.update({"fails": 0, "open_until": 0.0, "last_error": "", "last_status": None})


def _get_host_semaphore(url, max_inflight_per_host):
    host = urllib.parse.urlparse(url).netloc.lower() or "unknown"
    with _HOST_SEMAPHORES_LOCK:
        sem = _HOST_SEMAPHORES.get(host)
        if sem is None:
            sem = threading.Semaphore(max(1, int(max_inflight_per_host)))
            _HOST_SEMAPHORES[host] = sem
        return sem, host


def source_reason(result):
    if result.success:
        return "cache_fallback" if result.is_cached else "live_data"
    text = str(result.error or "").lower()
    if "circuit_open" in text:
        return "circuit_open"
    if "timed out" in text or "timeout" in text:
        return "network_timeout"
    if result.http_status == 429:
        return "rate_limited"
    if result.http_status in (401, 403):
        return "access_denied"
    if result.http_status == 400:
        return "invalid_or_unsupported_request"
    payload = result.raw_payload or {}
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if isinstance(hourly, dict) and hourly.get("time"):
        meaningful = [v for k, vals in hourly.items() if k != "time" and isinstance(vals, list) for v in vals if safe_float(v) is not None]
        return "no_usable_target_coverage" if meaningful else "provider_returned_null_data"
    if "0 points" in text:
        return "no_usable_target_coverage"
    return "provider_or_network_error"


def fetch_source(target_date, config, args):
    source_id, provider, kind = config["source_id"], config["provider"], config["kind"]
    request_url = preview_request_url(config, args)
    sem, host = _get_host_semaphore(request_url, args.max_inflight_per_host)
    started = time.time()
    is_open, wait_s = _circuit_is_open(host)
    if is_open:
        result = SourceResult(source_id, provider, False, {}, error=f"circuit_open host={host} wait={int(wait_s)}s", request_url=request_url,
                              base_weight=source_active_weight(source_id), duration_ms=0)
    else:
        with sem:
            try:
                if kind == "bmkg":
                    fetched = fetch_bmkg_forecast(target_date, config, args)
                elif kind == "open_meteo":
                    fetched = fetch_open_meteo_forecast(target_date, config, args)
                elif kind == "met_no":
                    fetched = fetch_met_no_forecast(target_date, config, args)
                else:
                    raise ValueError(f"Unknown source kind: {kind}")
                points = {k: p for k, p in fetched["points"].items() if any(getattr(p, f) is not None for f in ("temp_c", "rh_pct", "rain_mm", "wind_kmh"))}
                note = fetched.get("note", "")
                result = SourceResult(source_id, provider, len(points) > 0, points, error=note if points else (note or "source returned 0 points"),
                                      request_url=fetched.get("request_url", request_url), raw_payload=fetched["raw_payload"],
                                      base_weight=source_active_weight(source_id), http_status=fetched.get("http_status"), duration_ms=fetched.get("duration_ms"))
                _quality_result_metadata(result, args)
                if result.live_fetch_success:
                    _circuit_note_success(host)
            except Exception as exc:
                log_info(f"{source_id} gagal (host={host}):", exc)
                if DEBUG:
                    traceback.print_exc()
                status = getattr(exc, "code", None) if isinstance(exc, urllib.error.HTTPError) else None
                _circuit_note_failure(host, str(exc), status, args)
                ctype = snippet = ""
                if isinstance(exc, HttpPayloadError):
                    status = exc.status if exc.status is not None else status
                    ctype, snippet = exc.content_type, exc.snippet
                result = SourceResult(source_id, provider, False, {}, error=str(exc), request_url=request_url, base_weight=source_active_weight(source_id),
                                      http_status=status, duration_ms=int((time.time() - started) * 1000), error_content_type=ctype, error_snippet=snippet)
                _quality_result_metadata(result, args)
    result.availability_reason = source_reason(result)
    result.requested_source_id = source_id
    result.is_backup = bool(config.get("is_backup", False))
    result.model_identifier = config.get("models") or source_id
    if not result.success and result.availability_reason == "provider_returned_null_data":
        result.error = "API returned timestamps but all requested weather values are null; source not counted as successful."
    if args.save_raw_payloads:
        result.payload_saved_path = save_raw_payload_snapshot(target_date, result, args.timezone, args)
    return result


def collect_all_sources(target_date, args):
    global _V61_ENSEMBLES, _V61_SOURCE_DIAGNOSTICS
    results = []
    workers = max(1, min(int(args.max_workers), len(ACTIVE_SOURCE_CONFIGS), MAX_WORKERS or 8))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch_source, target_date, c, args) for c in ACTIVE_SOURCE_CONFIGS]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda r: r.source_id)
    minimum = getattr(args, "min_success_sources", None) or len(ACTIVE_SOURCE_CONFIGS)
    configured = {c.get("models") for c in ACTIVE_SOURCE_CONFIGS}
    for name in str(getattr(args, "backup_models", "")).split(","):
        name = name.strip()
        if sum(r.success for r in results) >= minimum:
            break
        if not name or name in configured or name not in V61_BACKUP_CONFIGS:
            continue
        log_info("Supplemental forecast model:", name)
        results.append(fetch_source(target_date, dict(V61_BACKUP_CONFIGS[name], is_backup=True), args))
    _V61_SOURCE_DIAGNOSTICS = [{
        "source_id": r.source_id, "requested_source_id": r.requested_source_id or r.source_id, "success": r.success,
        "live_fetch_success": r.live_fetch_success, "is_cached": r.is_cached, "is_backup": r.is_backup,
        "reason": r.availability_reason, "error": r.error, "model_identifier": r.model_identifier,
        "http_status": r.http_status, "points": len(r.points)} for r in results]
    write_json(path_output("source_availability.json"), {
        "issued_at": _quality_issued(args).isoformat(), "configured_sources": len(ACTIVE_SOURCE_CONFIGS),
        "successful_sources": sum(r.success for r in results), "successful_live_sources": sum(r.live_fetch_success for r in results),
        "target_success_sources": minimum, "target_met": sum(r.success for r in results) >= minimum, "sources": _V61_SOURCE_DIAGNOSTICS,
        "note": "Provider yang gagal tetap tercantum; model suplemen memakai identitasnya sendiri."})
    _V61_ENSEMBLES = v61_fetch_ensembles(target_date, args)
    write_json(path_output("ensemble_member_status.json"), {k: v for k, v in _V61_ENSEMBLES.items() if k != "hourly"})
    return results


def _quality_issued(args):
    return _RUN_ISSUED_AT or now_local(args.timezone)


def aether_lead_bucket(lead_hours):
    if lead_hours is None:
        return "unknown"
    for limit, name in ((3, "lead_0_3h"), (6, "lead_3_6h"), (12, "lead_6_12h"), (24, "lead_12_24h"), (48, "lead_24_48h")):
        if lead_hours <= limit:
            return name
    return "lead_48h_plus"


def _quality_context(valid_time, issued_at=None):
    lead = max(0, (valid_time - (issued_at or valid_time)).total_seconds() / 3600)
    return {"lead": aether_lead_bucket(lead), "hour": hour_bucket_for_time(valid_time.strftime("%H:%M")), "month": str(valid_time.month)}


# ---------------------------------------------------------------------------
# Observasi: normalisasi, QC stasiun, impor
# ---------------------------------------------------------------------------
_V61_OBSERVATION_QC_VERSION = "langit-observation-qc-1"
_V61_OBSERVATION_NUMERIC = ("temp_c", "rh_pct", "rain_mm", "wind_kmh")
_V61_OBSERVATION_LIMITS = {"temp_c": (-80.0, 60.0), "rh_pct": (0.0, 100.0), "rain_mm": (0.0, 1000.0), "wind_kmh": (0.0, 500.0)}
_QC_FAIL_PREFIXES = ("range:", "non_numeric:", "duplicate_conflict:", "temporal_jump:", "invalid_interval:", "invalid_timestamp:")


def observation_dir():
    path = path_output(OBSERVATION_DIRNAME)
    ensure_directory(path)
    return path


def report_dir():
    path = path_output(REPORT_DIRNAME)
    ensure_directory(path)
    return path


def observation_file_for_date(target_date):
    return os.path.join(observation_dir(), f"observations_{target_date.strftime('%Y%m%d')}.csv")


def observation_master_file():
    return path_output("observations.csv")


def sentinel_obs_date_to_iso(text):
    text = (text or "").strip()
    if not text:
        return ""
    try:
        if len(text) == 10 and text[4] == "-":
            return parse_iso_date(text).isoformat()
        return parse_display_date(text).isoformat()
    except Exception:
        return text


def _normalize_observation_base(row):
    row = dict(row or {})
    date = sentinel_obs_date_to_iso(_quality_first(row, "tanggal", "date", "target_date", default=""))
    jam = _lg_hour(_quality_first(row, "jam", "time"), "")
    try:
        parse_iso_date(date)
        if len(jam) != 5 or not 0 <= int(jam[:2]) <= 23 or not 0 <= int(jam[3:]) <= 59:
            return None
    except (ValueError, TypeError):
        return None
    kind = row.get("observation_type") or "legacy_unclassified"
    values = validate_point_values(*(_quality_first(row, f, "observed_" + f) for f in ("temp_c", "rh_pct", "rain_mm", "wind_kmh")))
    normalized = {**row, "tanggal": parse_iso_date(date).strftime("%d-%m-%Y"), "target_date": date, "jam": jam,
                  "observed_datetime": row.get("observed_datetime") or "", "observation_type": kind,
                  "source_id": row.get("source_id") or kind,
                  "qc": row.get("qc") or ("passed" if kind in _OBS_TYPES and not values[4] else "unverified"),
                  "qc_flags": ";".join(values[4]), "available_at": row.get("available_at") or now_local(DEFAULT_TIMEZONE).isoformat()}
    for field, value in zip(("temp_c", "rh_pct", "rain_mm", "wind_kmh"), values[:4]):
        normalized[field] = value
    normalized["category"] = _quality_first(row, "category", "observed_category", default="")
    for field in _OBS_FIELDS:
        if normalized.get(field) not in (None, ""):
            for suffix, fallback in (("type", kind), ("qc", normalized["qc"]), ("available_at", normalized["available_at"]), ("source", normalized["source_id"])):
                normalized[field + "_" + suffix] = row.get(field + "_" + suffix) or fallback
    return normalized


def _v61_qc_list(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    if not value:
        return []
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (ValueError, TypeError):
        return [f for f in str(value).split(";") if f]


def _v61_observation_add_flag(row, field, flag):
    flags = _v61_qc_list(row.get(field + "_qc_flags"))
    if flag not in flags:
        flags.append(flag)
    row[field + "_qc_flags"] = json.dumps(sorted(flags), ensure_ascii=False)


def _v61_observation_finalize(row):
    all_flags, states = [], []
    for field in _OBS_FIELDS:
        if row.get(field) in (None, ""):
            continue
        flags = _v61_qc_list(row.get(field + "_qc_flags"))
        state = "failed" if any(str(f).startswith(_QC_FAIL_PREFIXES) for f in flags) else "unverified" if flags else "passed"
        row[field + "_qc"] = state
        states.append(state)
        all_flags.extend(field + ":" + str(f) for f in flags)
    row["qc"] = "failed" if "failed" in states else "unverified" if "unverified" in states else "passed" if states else "unverified"
    row["qc_flags"] = ";".join(sorted(set(all_flags + _v61_qc_list(row.get("record_qc_flags")))))
    row["qc_version"] = _V61_OBSERVATION_QC_VERSION
    row["qc_evidence_json"] = json.dumps({"version": _V61_OBSERVATION_QC_VERSION, "checks": _v61_qc_list(row.get("qc_checks_json")),
                                          "flags": row["qc_flags"], "policy": row.get("qc_policy", "station_series")}, ensure_ascii=False)
    return row


def normalize_observation_row(row):
    """QC yang sudah dihitung dipertahankan saat dimuat ulang; impor lama tidak bisa menyatakan dirinya 'passed'."""
    raw = dict(row or {})
    normalized = _normalize_observation_base(raw)
    if not normalized:
        return None
    if raw.get("qc_version") == _V61_OBSERVATION_QC_VERSION:
        for key, value in raw.items():
            if key.endswith("_qc") or key.endswith("_qc_flags") or key in {"qc", "qc_flags", "qc_evidence_json", "qc_checks_json", "record_qc_flags"}:
                normalized[key] = value
        return normalized
    kind = normalized.get("observation_type")
    if kind == "model_reference":
        normalized["qc"] = "reference_only"
        for field in _OBS_FIELDS:
            if normalized.get(field) not in (None, ""):
                normalized[field + "_qc"] = "reference_only"
        return normalized
    if kind == "manual_observation":
        normalized["qc_policy"] = "manual_report_range_only"
        normalized["qc_checks_json"] = json.dumps(["manual_report", "finite_numeric", "physical_range"])
        for field in _OBS_FIELDS:
            if normalized.get(field) not in (None, ""):
                normalized[field + "_qc_flags"] = "[]"
                if field == "rain_mm" and not (raw.get("rain_interval_start") and raw.get("rain_interval_end")):
                    _v61_observation_add_flag(normalized, field, "unknown_rain_interval")
        for field in _V61_OBSERVATION_NUMERIC:
            value = _quality_first(raw, field, "observed_" + field)
            if value not in (None, ""):
                number = safe_float(value)
                if number is None:
                    normalized[field] = value
                    _v61_observation_add_flag(normalized, field, "non_numeric:" + field)
                elif not _V61_OBSERVATION_LIMITS[field][0] <= number <= _V61_OBSERVATION_LIMITS[field][1]:
                    normalized[field] = number
                    _v61_observation_add_flag(normalized, field, "range:" + field)
        return _v61_observation_finalize(normalized)
    normalized["qc"] = "unverified" if kind in _OBS_TYPES else normalized.get("qc", "unverified")
    for field in _OBS_FIELDS:
        if normalized.get(field) not in (None, ""):
            normalized[field + "_qc"] = "unverified"
    normalized["qc_flags"] = ";".join(sorted(set(str(raw.get("qc_flags", "")).split(";") + ["metadata_or_series_qc_not_checked"]))).strip(";")
    return normalized


def load_external_observation_rows(path):
    """Klaim QC dari berkas dianggap tidak tepercaya; QC penuh dilakukan di import_external_observations."""
    rows = []
    for raw in read_dict_csv(path):
        claimed = dict(raw)
        raw = dict(raw)
        raw.pop("qc_version", None)
        raw["claimed_qc"], raw["claimed_qc_flags"], raw["qc"] = claimed.get("qc", ""), claimed.get("qc_flags", ""), "unverified"
        raw["observation_type"] = raw.get("observation_type") or "external_observation"
        observed = _quality_datetime(_quality_first(raw, "observed_datetime", "timestamp", "datetime"))
        if observed is not None:
            observed = observed.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
            raw.setdefault("target_date", observed.date().isoformat())
            raw.setdefault("jam", observed.strftime("%H:%M"))
        if raw["observation_type"] == "manual_observation":
            raw["observation_type"] = "external_observation"
        for field in _OBS_FIELDS:
            raw[field + "_qc"] = "unverified"
        row = normalize_observation_row(raw)
        if row:
            rows.append(row)
    return rows


def _v61_distance_km(lat1, lon1, lat2, lon2):
    values = [safe_float(x) for x in (lat1, lon1, lat2, lon2)]
    if any(x is None for x in values):
        return None
    a, b, c, d = map(math.radians, values)
    term = math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(max(0.0, term))))


def _v61_station_registry(args):
    path = getattr(args, "stations_file", None)
    if not path:
        return {}
    if not os.path.isfile(path):
        raise ValueError("File stasiun tidak ditemukan: " + str(path))
    if str(path).lower().endswith(".json"):
        with open(path, encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        rows = payload.get("stations", []) if isinstance(payload, dict) else payload
        if isinstance(rows, dict):
            rows = [{"station_id": k, **v} for k, v in rows.items()]
        if not isinstance(rows, list):
            raise ValueError("JSON stasiun harus berupa daftar atau {stations: [...]}.")
    else:
        rows = read_dict_csv(path)
    registry = {}
    for raw in rows:
        station = dict(raw)
        identifier = str(_quality_first(station, "station_id", "id", default="")).strip()
        if not identifier:
            raise ValueError("Setiap stasiun harus mempunyai station_id.")
        if identifier in registry and registry[identifier] != station:
            raise ValueError("Metadata station_id bertentangan: " + identifier)
        registry[identifier] = station
    return registry


def _v61_station_metadata(raw, registry):
    station_id = str(_quality_first(raw, "station_id", "sensor_station_id", default="")).strip()
    station = dict(registry.get(station_id, {}))
    conflicts = []
    aliases = {"latitude": ("latitude", "lat", "station_latitude"), "longitude": ("longitude", "lon", "station_longitude"),
               "elevation_m": ("elevation_m", "elevation", "station_elevation_m"), "timezone": ("timezone", "station_timezone")}
    for name, keys in aliases.items():
        incoming, original = _quality_first(raw, *keys), _quality_first(station, *keys)
        if incoming not in (None, "") and original not in (None, ""):
            a, b = safe_float(incoming), safe_float(original)
            if (abs(a - b) > 1e-6) if a is not None and b is not None else str(incoming) != str(original):
                conflicts.append("station_metadata_conflict:" + name)
        station[name] = original if original not in (None, "") else incoming
    station["station_id"] = station_id
    return station, conflicts


def _v61_observation_datetime(raw, station, args):
    text = _quality_first(raw, "observed_datetime", "timestamp", "datetime")
    try:
        station_zone = ZoneInfo(str(station.get("timezone"))) if station.get("timezone") else None
    except (ValueError, KeyError, TypeError):
        station_zone = None
    if text:
        try:
            observed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        except ValueError:
            return None, "invalid_timestamp:observed_datetime"
    else:
        day = sentinel_obs_date_to_iso(_quality_first(raw, "target_date", "tanggal", "date", default=""))
        jam = _lg_hour(_quality_first(raw, "jam", "time"), "")
        try:
            observed = datetime.fromisoformat(day + "T" + jam)
        except (TypeError, ValueError):
            return None, "invalid_timestamp:date_time"
    if observed.tzinfo is None or observed.utcoffset() is None:
        if station_zone is None:
            return None, "invalid_timestamp:missing_timezone"
        observed = observed.replace(tzinfo=station_zone)
    try:
        return observed.astimezone(ZoneInfo(getattr(args, "timezone", DEFAULT_TIMEZONE))), ""
    except (ValueError, KeyError):
        return None, "invalid_timestamp:target_timezone"


def _v61_convert_observation(raw, field, station):
    value = _quality_first(raw, field, "observed_" + field)
    if value in (None, ""):
        return None, []
    parsed = safe_float(value)
    if parsed is None:
        return value, ["non_numeric:" + field]
    unit_keys = {"temp_c": ("temp_unit", "temperature_unit"), "rh_pct": ("rh_unit", "humidity_unit"),
                 "rain_mm": ("rain_unit", "precipitation_unit"), "wind_kmh": ("wind_unit", "wind_speed_unit")}
    unit = _quality_first(raw, *unit_keys[field], default=_quality_first(station, *unit_keys[field]))
    u = str(unit or "").strip().lower().replace(" ", "")
    canonical = {"temp_c": {"c", "°c", "celsius"}, "rh_pct": {"%", "percent", "percentage"},
                 "rain_mm": {"mm", "millimeter", "millimetre"}, "wind_kmh": {"km/h", "kmh", "kph"}}
    if u in canonical[field]:
        return parsed, []
    if field == "temp_c" and u in {"f", "°f", "fahrenheit"}:
        return (parsed - 32) * 5 / 9, []
    if field == "rh_pct" and u in {"fraction", "0-1"}:
        return parsed * 100, []
    if field == "rain_mm" and u in {"inch", "in", "inches"}:
        return parsed * 25.4, []
    if field == "wind_kmh" and u in {"m/s", "ms", "mps"}:
        return parsed * 3.6, []
    if field == "wind_kmh" and u in {"mph", "mi/h"}:
        return parsed * 1.609344, []
    return parsed, ["unknown_unit:" + field]


_FIELD_META_SUFFIXES = ("qc_flags", "qc_computed_at", "type", "source", "available_at", "station_id", "station_latitude",
                        "station_longitude", "station_elevation_m", "station_distance_km", "interval_start", "interval_end", "interval_minutes")


def v61_quality_control_observations(rows, args):
    """Kembalikan (baris teraudit, laporan). Nilai tidak pernah diinterpolasi; hujan per jam wajib berinterval 60 menit eksplisit."""
    registry = _v61_station_registry(args)
    normalized, rejected, record_issues = [], [], []
    now_stamp = now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).isoformat()
    max_distance = safe_float(getattr(args, "station_max_distance_km", 30.0))
    max_distance = 30.0 if max_distance is None else max_distance
    if max_distance <= 0:
        raise ValueError("--station-max-distance-km harus positif")
    for index, item in enumerate(rows):
        raw = dict(item or {})
        station, metadata_flags = _v61_station_metadata(raw, registry)
        observed, timestamp_flag = _v61_observation_datetime(raw, station, args)
        if observed is None:
            rejected.append({"input_row": index + 1, "reason": timestamp_flag, "raw": raw})
            continue
        kind = raw.get("observation_type") or ("station_observation" if station.get("station_id") else "external_observation")
        row = {**raw, "input_row": index + 1, "station_id": station.get("station_id", ""),
               "station_latitude": safe_float(station.get("latitude")), "station_longitude": safe_float(station.get("longitude")),
               "station_elevation_m": safe_float(station.get("elevation_m")), "station_timezone": station.get("timezone") or "",
               "expected_interval_minutes": safe_float(_quality_first(raw, "expected_interval_minutes", default=station.get("expected_interval_minutes"))) or 60.0,
               "observed_datetime": observed.isoformat(), "target_date": observed.date().isoformat(),
               "tanggal": observed.strftime("%d-%m-%Y"), "jam": observed.strftime("%H:%M"), "observation_type": kind,
               "source_id": raw.get("source_id") or station.get("station_id") or kind,
               "available_at": raw.get("available_at") or now_stamp, "qc_policy": "station_series", "qc_computed_at": now_stamp,
               "qc_checks_json": json.dumps(["station_metadata", "timezone", "finite_numeric", "explicit_units", "physical_range", "rain_interval", "duplicate", "temporal_jump", "stuck_sensor", "missing_hours", "location_distance"]),
               "claimed_qc": raw.get("qc", ""), "claimed_qc_flags": raw.get("qc_flags", ""),
               "raw_row_json": json.dumps(raw, ensure_ascii=False), "record_qc_flags": "[]"}
        lat, lon, elevation = row["station_latitude"], row["station_longitude"], row["station_elevation_m"]
        if not row["station_id"]:
            metadata_flags.append("missing_station_id")
        if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            metadata_flags.append("invalid_or_missing_coordinates")
        if elevation is None or not -500 <= elevation <= 9000:
            metadata_flags.append("invalid_or_missing_elevation")
        try:
            ZoneInfo(row["station_timezone"])
        except (ValueError, KeyError, TypeError):
            metadata_flags.append("missing_or_invalid_station_timezone")
        row["station_distance_km"] = None if "invalid_or_missing_coordinates" in metadata_flags else _v61_distance_km(lat, lon, getattr(args, "latitude", None), getattr(args, "longitude", None))
        if row["station_distance_km"] is None:
            metadata_flags.append("target_location_not_associated")
        elif row["station_distance_km"] > max_distance:
            metadata_flags.append("station_outside_location_radius")
        available = _quality_datetime(row["available_at"])
        if available is None:
            metadata_flags.append("invalid_available_at")
        elif available < observed:
            metadata_flags.append("available_at_before_observation")
        for field in _V61_OBSERVATION_NUMERIC:
            value, flags = _v61_convert_observation(raw, field, station)
            label = str(_quality_first(raw, field + "_source", "source_id", default="")).lower()
            if label in {"metno_forecast", "dwd_mosmix", "open_meteo_archive", "era5", "era5_land", "cmorph", "cmorph_icdr"}:
                flags.append("not_a_station_measurement:" + label)
            row[field] = value
            if value not in (None, ""):
                numeric = safe_float(value)
                if numeric is not None:
                    low, high = _V61_OBSERVATION_LIMITS[field]
                    if not low <= numeric <= high:
                        flags.append("range:" + field)
                row[field + "_qc_flags"] = json.dumps(sorted(set(flags + metadata_flags)))
        row["category"] = _quality_first(raw, "category", "observed_category", default="")
        if row["category"]:
            row["category_qc_flags"] = json.dumps(sorted(set(metadata_flags)))
        for field in _OBS_FIELDS:
            if row.get(field) not in (None, ""):
                row[field + "_type"] = kind
                row[field + "_source"] = _quality_first(raw, field + "_source", default=row["source_id"])
                row[field + "_available_at"] = row["available_at"]
                row[field + "_qc_computed_at"] = now_stamp
                row[field + "_station_id"] = row["station_id"]
                for suffix in ("latitude", "longitude", "elevation_m", "distance_km"):
                    row[field + "_station_" + suffix] = row.get("station_" + suffix)
        if row.get("rain_mm") not in (None, ""):
            start, end = _quality_datetime(raw.get("rain_interval_start")), _quality_datetime(raw.get("rain_interval_end"))
            minutes = safe_float(_quality_first(raw, "rain_interval_minutes", default=station.get("rain_interval_minutes")))
            if end is None and minutes is not None and minutes > 0:
                end = observed
            if start is None and end is not None and minutes is not None and minutes > 0:
                start = end - timedelta(minutes=minutes)
            if start is None or end is None:
                _v61_observation_add_flag(row, "rain_mm", "unknown_rain_interval")
            elif end <= start or abs((end - observed).total_seconds()) > 1:
                _v61_observation_add_flag(row, "rain_mm", "invalid_interval:rain_mm")
            else:
                row["rain_interval_start"], row["rain_interval_end"] = start.isoformat(), end.isoformat()
                row["rain_interval_minutes"] = (end - start).total_seconds() / 60
                row["rain_mm_interval_start"], row["rain_mm_interval_end"] = start.isoformat(), end.isoformat()
                row["rain_mm_interval_minutes"] = row["rain_interval_minutes"]
                if abs(row["rain_interval_minutes"] - 60) > 1e-6:
                    _v61_observation_add_flag(row, "rain_mm", "rain_interval_not_hourly")
        if observed.minute or observed.second or observed.microsecond:
            for field in _OBS_FIELDS:
                if row.get(field) not in (None, ""):
                    _v61_observation_add_flag(row, field, "timestamp_not_on_hour")
        normalized.append(row)

    groups = defaultdict(list)
    for row in normalized:
        groups[(row["station_id"] or row["source_id"], row["observed_datetime"])].append(row)
    duplicate_rows = conflict_rows = 0
    excluded_ids = set()
    for key, dups in sorted(groups.items()):
        if len(dups) < 2:
            continue
        duplicate_rows += len(dups) - 1
        conflicts = [f for f in _OBS_FIELDS if len({str(r.get(f)) for r in dups if r.get(f) not in (None, "")}) > 1]
        if conflicts:
            conflict_rows += len(dups)
            for row in dups:
                for field in conflicts:
                    if row.get(field) not in (None, ""):
                        _v61_observation_add_flag(row, field, "duplicate_conflict:" + field)
        else:
            first = dups[0]
            for dup in dups[1:]:
                for field in _OBS_FIELDS:
                    if first.get(field) in (None, "") and dup.get(field) not in (None, ""):
                        first[field] = dup[field]
                        for suffix in _FIELD_META_SUFFIXES:
                            first[field + "_" + suffix] = dup.get(field + "_" + suffix)
                excluded_ids.add(id(dup))
            first["record_qc_flags"] = json.dumps(["exact_or_complementary_duplicate_collapsed"])
        record_issues.append({"station_id": key[0], "observed_datetime": key[1], "kind": "duplicate_conflict" if conflicts else "duplicate_collapsed",
                              "fields": conflicts, "input_rows": [r["input_row"] for r in dups]})
    normalized = [r for r in normalized if id(r) not in excluded_ids]

    series = defaultdict(list)
    for row in normalized:
        series[row["station_id"] or row["source_id"]].append(row)
    gap_rows = []
    jump_temp = safe_float(getattr(args, "observation_jump_temp_c", 8.0))
    stuck_hours = safe_float(getattr(args, "observation_stuck_hours", 8.0))
    jump_temp = 8.0 if jump_temp is None else jump_temp
    stuck_hours = 8.0 if stuck_hours is None else stuck_hours
    if jump_temp <= 0 or stuck_hours <= 0:
        raise ValueError("Ambang lonjakan dan durasi sensor macet harus positif")
    for station_id, values in sorted(series.items()):
        values.sort(key=lambda r: (r["observed_datetime"], r["input_row"]))
        for left, right in zip(values, values[1:]):
            a, b = _quality_datetime(left["observed_datetime"]), _quality_datetime(right["observed_datetime"])
            gap_hours = (b - a).total_seconds() / 3600
            expected = max(1.0, safe_float(right.get("expected_interval_minutes")) or 60.0)
            if gap_hours * 60 > expected * 1.5:
                gap_rows.append({"station_id": station_id, "start": a.isoformat(), "end": b.isoformat(), "expected_interval_minutes": expected,
                                 "missing_intervals": max(1, int(round(gap_hours * 60 / expected)) - 1)})
            if 0 < gap_hours <= 3:
                for field, threshold in (("temp_c", jump_temp), ("rh_pct", 40.0), ("wind_kmh", 80.0)):
                    v1, v2 = safe_float(left.get(field)), safe_float(right.get(field))
                    already_bad = any(str(fl).startswith(("range:", "non_numeric:", "duplicate_conflict:")) for it in (left, right) for fl in _v61_qc_list(it.get(field + "_qc_flags")))
                    if not already_bad and v1 is not None and v2 is not None and abs(v2 - v1) > threshold * gap_hours:
                        _v61_observation_add_flag(left, field, "temporal_jump:" + field)
                        _v61_observation_add_flag(right, field, "temporal_jump:" + field)
        for field in ("temp_c", "rh_pct", "wind_kmh"):
            def mark_stuck(items, field=field):
                if len(items) < 3:
                    return
                duration = (_quality_datetime(items[-1]["observed_datetime"]) - _quality_datetime(items[0]["observed_datetime"])).total_seconds() / 3600
                if duration >= stuck_hours:
                    for it in items:
                        _v61_observation_add_flag(it, field, "suspected_stuck_sensor:" + field)
            chain = []
            for row in values:
                number = safe_float(row.get(field))
                invalid = any(str(fl).startswith(("range:", "non_numeric:", "duplicate_conflict:")) for fl in _v61_qc_list(row.get(field + "_qc_flags")))
                if number is None or invalid:
                    mark_stuck(chain)
                    chain = []
                    continue
                continues = bool(chain) and abs(number - safe_float(chain[-1].get(field))) < 1e-9 and 0 < (_quality_datetime(row["observed_datetime"]) - _quality_datetime(chain[-1]["observed_datetime"])).total_seconds() <= max(1, row["expected_interval_minutes"]) * 90
                if not continues:
                    mark_stuck(chain)
                    chain = []
                chain.append(row)
            mark_stuck(chain)
    for row in normalized:
        _v61_observation_finalize(row)
        if row.get("observation_type") not in _OBS_TYPES:
            row["qc"] = "reference_only" if row.get("observation_type") == "model_reference" else "unverified"
            for field in _OBS_FIELDS:
                if row.get(field) not in (None, ""):
                    row[field + "_qc"] = row["qc"]
    cross, simultaneous = [], defaultdict(list)
    for row in normalized:
        simultaneous[(row["target_date"], row["jam"])].append(row)
    for key, values in sorted(simultaneous.items()):
        for field in _V61_OBSERVATION_NUMERIC:
            comparable = [r for r in values if r.get(field + "_qc") == "passed" and safe_float(r.get(field)) is not None]
            if len({r.get("station_id") for r in comparable}) > 1:
                low, high = min(safe_float(r[field]) for r in comparable), max(safe_float(r[field]) for r in comparable)
                if high - low > 1e-6:
                    cross.append({"target_date": key[0], "jam": key[1], "field": field, "spread": high - low,
                                  "stations": [{"station_id": r.get("station_id"), "value": r[field], "distance_km": r.get("station_distance_km")} for r in comparable],
                                  "policy": "Record disagreement; pilih stasiun lolos terdekat per variabel tanpa rata-rata."})
    counts = {s: sum(r.get("qc") == s for r in normalized) for s in ("passed", "unverified", "failed", "reference_only")}
    field_counts = {f: {s: sum(r.get(f) not in (None, "") and r.get(f + "_qc") == s for r in normalized) for s in ("passed", "unverified", "failed")} for f in _OBS_FIELDS}
    report = {"qc_version": _V61_OBSERVATION_QC_VERSION, "input_rows": len(rows), "normalized_rows": len(normalized),
              "rejected_timestamp_rows": rejected, "row_counts": counts, "field_counts": field_counts,
              "duplicate_extra_rows": duplicate_rows, "duplicate_conflict_rows": conflict_rows, "record_issues": record_issues,
              "missing_hour_intervals": gap_rows, "station_ids": sorted(series), "station_max_distance_km": max_distance,
              "cross_station_disagreement": cross,
              "thresholds": {"temp_jump_c_per_hour": jump_temp, "rh_jump_percent_per_hour": 40.0, "wind_jump_kmh_per_hour": 80.0, "stuck_duration_hours": stuck_hours},
              "policy": "Heuristic QC; sensor macet tetap unverified. Observasi hilang tidak diinterpolasi."}
    return normalized, report


def _quality_observation_rank(row, field):
    kind = row.get(field + "_type") or row.get("observation_type") or "legacy_unclassified"
    qc = row.get(field + "_qc") or row.get("qc")
    return (3 if kind in {"station_observation", "sensor_observation"} else 2 if kind in _OBS_TYPES else 0) if qc == "passed" else 0


def _quality_merge_observation(left, right):
    out = dict(left or {})
    for key in ("tanggal", "target_date", "jam", "observed_datetime", "observation_type", "source_id", "available_at", "qc"):
        if key not in out and right.get(key) is not None:
            out[key] = right[key]
    meta_map = {"type": "observation_type", "qc": "qc", "available_at": "available_at", "source": "source_id"}
    for field in _OBS_FIELDS:
        incoming = right.get(field)
        if incoming in (None, ""):
            continue
        old = out.get(field)
        incoming_rank, old_rank = _quality_observation_rank(right, field), _quality_observation_rank(out, field)
        if old not in (None, "") and str(old) != str(incoming):
            try:
                conflicts = json.loads(out.get("conflicts_json") or "[]")
                conflicts = conflicts if isinstance(conflicts, list) else []
            except (ValueError, TypeError):
                conflicts = []
            conflicts.append({"field": field, "previous": old, "incoming": incoming, "incoming_source": right.get(field + "_source") or right.get("source_id")})
            out["conflicts_json"] = json.dumps(conflicts, ensure_ascii=False)
        selected = old in (None, "") or incoming_rank > old_rank
        if left and right.get("qc_version") == _V61_OBSERVATION_QC_VERSION and not selected:
            new_t = _quality_datetime(right.get(field + "_qc_computed_at") or right.get("qc_computed_at"))
            old_t = _quality_datetime(left.get(field + "_qc_computed_at") or left.get("qc_computed_at"))
            same_station = bool(right.get(field + "_station_id")) and right.get(field + "_station_id") == left.get(field + "_station_id")
            new_selection = right.get("qc_policy") == "station_selection" and (left.get(field + "_type") or left.get("observation_type")) in {"station_observation", "sensor_observation", "external_observation"}
            same_target = (left.get("target_date"), left.get("jam")) == (right.get("target_date"), right.get("jam"))
            selected = same_target and (same_station or new_selection) and new_t is not None and (old_t is None or new_t >= old_t)
        if selected:
            out[field] = incoming
            for suffix, fallback_key in meta_map.items():
                out[field + "_" + suffix] = right.get(field + "_" + suffix) or right.get(fallback_key)
        source = right if selected else out
        for suffix in _FIELD_META_SUFFIXES[:1] + _FIELD_META_SUFFIXES[1:2] + _FIELD_META_SUFFIXES[5:]:
            key = field + "_" + suffix
            if key in source:
                out[key] = source[key]
    for key in ("qc_version", "qc_computed_at", "qc_policy", "qc_checks_json", "qc_evidence_json", "qc_flags", "record_qc_flags",
                "station_id", "station_latitude", "station_longitude", "station_elevation_m", "station_distance_km", "station_timezone", "location_slug"):
        if key not in out and key in right:
            out[key] = right[key]
    if out.get("qc_version") == _V61_OBSERVATION_QC_VERSION:
        states = [out.get(f + "_qc") for f in _OBS_FIELDS if out.get(f) not in (None, "")]
        out["qc"] = "failed" if "failed" in states else "unverified" if "unverified" in states else "passed" if states else "unverified"
        out["qc_flags"] = ";".join(sorted({f + ":" + str(fl) for f in _OBS_FIELDS if out.get(f) not in (None, "") for fl in _v61_qc_list(out.get(f + "_qc_flags"))}))
        out["qc_evidence_json"] = json.dumps({"version": _V61_OBSERVATION_QC_VERSION, "policy": out.get("qc_policy"), "checks": _v61_qc_list(out.get("qc_checks_json")),
            "fields": {f: {"qc": out.get(f + "_qc"), "flags": _v61_qc_list(out.get(f + "_qc_flags")), "station_id": out.get(f + "_station_id"), "source": out.get(f + "_source")}
                       for f in _OBS_FIELDS if out.get(f) not in (None, "")}}, ensure_ascii=False)
    return out


def _quality_observed_value(row, field):
    if _quality_observation_rank(row, field) == 0:
        return None
    return row.get(field) if field == "category" else safe_float(row.get(field))


def _quality_observation_available(row, field, issued_at):
    value = _quality_observed_value(row, field)
    available = _quality_datetime(row.get(field + "_available_at") or row.get("available_at"))
    return value if value is not None and available is not None and available <= issued_at else None


def _v61_select_station_observations(rows, args):
    groups = defaultdict(list)
    for row in rows:
        if row.get("observation_type") in _OBS_TYPES:
            groups[(row["target_date"], row["jam"])].append(row)
    inf = float("inf")
    selected_rows = []
    for key in sorted(groups):
        candidates = groups[key]
        composite = None
        for field in _OBS_FIELDS:
            available = [r for r in candidates if r.get(field) not in (None, "")]
            if not available:
                continue
            available.sort(key=lambda r: (0 if _quality_observation_rank(r, field) > 0 else 1,
                                          safe_float(r.get("station_distance_km")) if safe_float(r.get("station_distance_km")) is not None else inf,
                                          str(r.get("station_id")), r.get("input_row", 0)))
            best = dict(available[0])
            for other in _OBS_FIELDS:
                if other != field:
                    best[other] = None if other != "category" else ""
            composite = _quality_merge_observation(composite, best)
        if composite:
            composite["location_slug"] = getattr(args, "location_slug", "")
            composite["qc_policy"] = "station_selection"
            composite["qc_computed_at"] = max((r.get("qc_computed_at", "") for r in candidates), default="")
            _v61_observation_finalize(composite)
            selected_rows.append(composite)
    return selected_rows


def write_observation_rows(target_date, rows):
    normalized = [r for row in rows if (r := normalize_observation_row(row))]
    references = [r for r in normalized if r.get("observation_type") == "model_reference"]
    actual = [r for r in normalized if r.get("observation_type") != "model_reference"]
    if references:
        write_dict_csv(os.path.join(observation_dir(), f"model_reference_{target_date:%Y%m%d}.csv"), rows=references)
    if not actual:
        return

    def merge_into(path, incoming):
        index = {}
        for raw in read_dict_csv(path) + incoming:
            item = normalize_observation_row(raw)
            if item:
                key = (item["target_date"], item["jam"])
                index[key] = _quality_merge_observation(index.get(key), item)
        return [index[k] for k in sorted(index)]

    write_dict_csv(observation_file_for_date(target_date), rows=merge_into(observation_file_for_date(target_date), actual))
    write_dict_csv(observation_master_file(), rows=merge_into(observation_master_file(), actual))


def import_external_observations(args):
    path = getattr(args, "observations_csv", None)
    if not path:
        raise ValueError("--observations-csv diperlukan")
    raw = read_dict_csv(path)
    if not raw:
        raise ValueError("File observasi kosong atau tidak dapat dibaca")
    history_path = os.path.join(observation_dir(), "raw_station_history.csv")
    index = {json.dumps(dict(r), sort_keys=True, ensure_ascii=False): dict(r) for r in read_dict_csv(history_path)}
    for row in raw:
        index[json.dumps(dict(row), sort_keys=True, ensure_ascii=False)] = dict(row)
    history = list(index.values())
    rows, report = v61_quality_control_observations(history, args)
    selected = _v61_select_station_observations(rows, args)
    stamp = now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).strftime("%Y%m%d_%H%M%S_%f")
    directory = os.path.join(observation_dir(), "qc_archive")
    ensure_directory(directory)
    write_dict_csv(os.path.join(directory, "raw_import_" + stamp + ".csv"), rows=raw)
    write_dict_csv(history_path, rows=history)
    write_dict_csv(os.path.join(directory, "station_observations_" + stamp + ".csv"), rows=rows)
    write_json(os.path.join(directory, "qc_report_" + stamp + ".json"), report)
    write_dict_csv(os.path.join(directory, "flagged_observations_" + stamp + ".csv"), rows=[r for r in rows if r.get("qc") != "passed"])
    dates = sorted({r["target_date"] for r in selected})
    for day in dates:
        write_observation_rows(parse_iso_date(day), [r for r in selected if r["target_date"] == day])
    write_json(os.path.join(report_dir(), "import_observations_summary.json"), {
        **report, "source_file": path, "uploaded_rows": len(raw), "rows_imported": len(selected), "dates": dates,
        "stations_file": getattr(args, "stations_file", None), "qc_archive": directory,
        "selected_station_ids": sorted({str(r.get(f + "_station_id")) for r in selected for f in _OBS_FIELDS if r.get(f + "_station_id")})})
    if not rows:
        raise ValueError("Tidak ada timestamp observasi valid; rincian penolakan disimpan di " + directory)
    return selected


def sentinel_load_observations_by_key(args=None):
    index = {}
    slug = str(getattr(args, "location_slug", "") or "") if args is not None else ""
    for path in (observation_master_file(), path_output(AETHER_FEEDBACK_FILENAME)):
        for raw in read_dict_csv(path):
            row = normalize_observation_row(raw)
            if row and (not slug or not row.get("location_slug") or row["location_slug"] == slug):
                key = (row["target_date"], row["jam"])
                index[key] = _quality_merge_observation(index.get(key), row)
    return index


def sentinel_observed_rain_event(row):
    rain = _quality_observed_value(row, "rain_mm")
    if rain is not None:
        return rain > 0.1
    cat = str(_quality_observed_value(row, "category") or "").lower()
    if "hujan" in cat or "gerimis" in cat:
        return True
    if cat in {"cerah", "cerah berawan", "berawan", "kabut", "udara kabur"}:
        return False
    return None


def extract_archive_observations(target_date, payload, tz_name):
    """Referensi model (reanalysis arsip). BUKAN pengukuran stasiun; tidak masuk pelatihan."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    arrays = {k: hourly.get(k) or [] for k in ("temperature_2m", "relative_humidity_2m", "precipitation", "weather_code", "wind_speed_10m")}

    def at(name, idx):
        arr = arrays[name]
        return arr[idx] if idx < len(arr) else None

    candidates = []
    for idx, text in enumerate(times):
        dt_local = parse_open_meteo_time(text, tz_name)
        if dt_local.date() != target_date:
            continue
        temp, rh, rain, wind, _ = validate_point_values(safe_float(at("temperature_2m", idx)), safe_float(at("relative_humidity_2m", idx)),
                                                        safe_float(at("precipitation", idx)), safe_float(at("wind_speed_10m", idx)))
        candidates.append({"dt": dt_local, "temp_c": temp, "rh_pct": rh, "rain_mm": rain, "wind_kmh": wind, "weather_code": at("weather_code", idx)})
    rows = []
    for jam in TARGET_TIMES:
        target_dt = parse_local_hour_string(target_date, jam, tz_name)
        match = next((c for c in candidates if c["dt"] == target_dt), None) or nearest_candidate(candidates, target_dt, max_gap_hours=2)
        if not match:
            continue
        rows.append({"tanggal": target_date.strftime("%d-%m-%Y"), "jam": jam, "observed_datetime": match["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                     "temp_c": round_or_blank(match["temp_c"]), "rh_pct": round_or_blank(match["rh_pct"]), "rain_mm": round_or_blank(match["rain_mm"]),
                     "wind_kmh": round_or_blank(match["wind_kmh"]), "weather_code": match["weather_code"],
                     "category": category_from_wmo_code(match["weather_code"], match["rain_mm"], match["rh_pct"]),
                     "observation_type": "model_reference", "source_id": "open_meteo_archive", "qc": "reference_only",
                     "available_at": now_local(tz_name).isoformat()})
    return rows


def fetch_archive_observations(target_date, args):
    params = {"latitude": args.latitude, "longitude": args.longitude, "timezone": args.timezone, "start_date": target_date.isoformat(),
              "end_date": target_date.isoformat(), "hourly": ",".join(AETHER_BASIC_OPEN_METEO_VARIABLES)}
    url = build_url(OBSERVATION_ARCHIVE_URL, params)
    payload, _, _ = fetch_json_with_retry(url, source_id="OBSERVATION_ARCHIVE", timeout=args.http_timeout, max_retry=args.max_retry_http)
    return url, payload, extract_archive_observations(target_date, payload, args.timezone)


def sync_model_references(args):
    end_date = parse_iso_date(args.end_date) if args.end_date else now_local(args.timezone).date() - timedelta(days=5)
    start_date = parse_iso_date(args.start_date) if args.start_date else end_date - timedelta(days=args.lookback_days - 1)
    if start_date > end_date:
        raise ValueError("start_date tidak boleh lebih besar dari end_date")
    summary_rows = []
    for target_date in iter_dates(start_date, end_date):
        url, payload, rows = fetch_archive_observations(target_date, args)
        write_observation_rows(target_date, rows)
        if args.save_raw_payloads:
            doc = {"request_url": url, "target_date": target_date.isoformat(), "payload": payload}
            payload_path = os.path.join(observation_dir(), f"archive_payload_{target_date:%Y%m%d}{_raw_payload_ext(args)}")
            (write_json_gz if args.compress_raw_payloads else write_json)(payload_path, doc)
        summary_rows.append({"target_date": target_date.isoformat(), "rows_saved": len(rows)})
        log_info("Observasi tersimpan untuk", target_date.isoformat(), f"({len(rows)} rows)")
    write_json(os.path.join(report_dir(), "observation_sync_summary.json"),
               {"generated_at": now_local(args.timezone).isoformat(), "location_slug": args.location_slug, "location_name": args.location_name, "rows": summary_rows})
    return summary_rows


def sync_observations(args):
    """Ambil pengukuran stasiun + referensi satelit lewat modul forecast_tools (bila tersedia).
    Jika modul tidak ada di repo, jatuh kembali ke arsip referensi model dengan peringatan jelas."""
    try:
        from forecast_tools.observations import sync_location
    except ImportError:
        log_warning("forecast_tools.observations tidak ditemukan; memakai sync-references (referensi model, bukan stasiun).")
        return sync_model_references(args)
    import copy
    result = sync_location(args, ACTIVE_OUTPUT_DIR)
    if result["station"]["rows"]:
        imported_args = copy.copy(args)
        imported_args.observations_csv = os.path.join(ACTIVE_OUTPUT_DIR, "observations", "station_incoming.csv")
        import_external_observations(imported_args)
    if not result["reference"]["available_hours"]:
        log_info("WARNING: Tidak ada referensi satelit yang dapat diambil; status kekosongan tetap diarsipkan.")
    return result["reference"]["records"]


def aether_feedback_for_location(args):
    row = normalize_observation_row({
        "target_date": args.feedback_date or args.target_date or now_local(args.timezone).date().isoformat(), "jam": args.feedback_time,
        "category": args.feedback_category or "", "rain_mm": args.feedback_rain_mm, "temp_c": args.feedback_temp_c,
        "observation_type": "manual_observation", "source_id": "manual_feedback", "available_at": now_local(args.timezone).isoformat(),
        "note": args.feedback_note or ""})
    if not row or all(row.get(f) in (None, "") for f in _OBS_FIELDS):
        raise ValueError("Feedback membutuhkan tanggal, jam, dan minimal satu nilai observasi valid")
    existing = read_dict_csv(path_output(AETHER_FEEDBACK_FILENAME))
    write_dict_csv(path_output(AETHER_FEEDBACK_FILENAME), rows=existing + [row])
    write_observation_rows(parse_iso_date(row["target_date"]), [row])
    return row


# ---------------------------------------------------------------------------
# Ensemble nyata (anggota individual) + model hujan dua tahap
# ---------------------------------------------------------------------------
V61_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
V61_ENSEMBLE_MODELS = {
    "ncep_gefs_seamless": {"family": "GFS", "native_hours": 3},
    "ncep_gefs025": {"family": "GFS", "native_hours": 3},
    "ncep_gefs05": {"family": "GFS", "native_hours": 3},
    "ecmwf_ifs025_ensemble": {"family": "ECMWF", "native_hours": 3},
    "ecmwf_aifs025_ensemble": {"family": "ECMWF", "native_hours": 6},
    "icon_global_eps": {"family": "ICON", "native_hours": 1},
    "icon_seamless_eps": {"family": "ICON", "native_hours": 1},
    "gem_global_ensemble": {"family": "GEM", "native_hours": 3},
    "ukmo_global_ensemble_20km": {"family": "UKMO", "native_hours": 1},
}
V61_ENSEMBLE_ALIASES = {
    "gfs_seamless": "ncep_gefs_seamless", "gfs025": "ncep_gefs025", "gfs05": "ncep_gefs05",
    "ecmwf_ifs025": "ecmwf_ifs025_ensemble", "ecmwf_aifs025": "ecmwf_aifs025_ensemble",
    "icon_global": "icon_global_eps", "icon_seamless": "icon_seamless_eps", "gem_global": "gem_global_ensemble",
}
V61_ENSEMBLE_VARIABLES = {
    "temperature_2m": ("temp_c", "°C", -80.0, 60.0),
    "relative_humidity_2m": ("rh_pct", "%", 0.0, 100.0),
    "precipitation": ("rain_mm", "mm", 0.0, 1000.0),
    "wind_speed_10m": ("wind_kmh", "km/h", 0.0, 500.0),
}
V61_ICON_GLOBAL_RESOLUTION_SCHEDULE = [
    {"through_lead_hours": 48, "native_step_hours": 1}, {"through_lead_hours": 72, "native_step_hours": 3},
    {"through_lead_hours": 120, "native_step_hours": 6}, {"through_lead_hours": 180, "native_step_hours": 12},
]
_V61_ENSEMBLE_RUNTIME_CACHE = {}


def v61_weighted_quantile(members, quantile):
    pairs = []
    for m in members:
        value, weight = safe_float(m.get("value")), safe_float(m.get("weight", 1.0))
        if value is not None and weight is not None and weight > 0:
            pairs.append((value, weight))
    if not pairs:
        return None
    pairs.sort()
    cut = max(0.0, min(1.0, float(quantile))) * sum(w for _, w in pairs)
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= cut:
            return value
    return pairs[-1][0]


def v61_family_balanced_members(members):
    """Satu suara per famili, lalu per model, lalu per anggota."""
    groups, unique = {}, {}
    for m in members:
        value = safe_float(m.get("value"))
        if value is None:
            continue
        key = (str(m.get("family") or "unknown"), str(m.get("model") or "unknown"), str(m.get("member") or "unknown"))
        if key in unique:
            continue
        item = dict(m, value=value)
        unique[key] = item
        groups.setdefault(key[0], {}).setdefault(key[1], []).append(item)
    out = []
    for family, models in sorted(groups.items()):
        for model, items in sorted(models.items()):
            weight = 1.0 / (len(groups) * len(models) * len(items))
            out.extend(dict(i, weight=weight) for i in items)
    return out


def v61_distribution_summary(members):
    balanced = v61_family_balanced_members(members)
    if not balanced:
        return {"members": [], "member_count": 0, "family_count": 0, "mean": None, "quantiles": {}}
    return {"members": balanced, "member_count": len(balanced), "family_count": len({m["family"] for m in balanced}),
            "model_count": len({m["model"] for m in balanced}), "mean": sum(m["value"] * m["weight"] for m in balanced),
            "quantiles": {"p%02d" % p: v61_weighted_quantile(balanced, p / 100.0) for p in (5, 10, 25, 50, 75, 90, 95)}}


def v61_rain_two_stage_summary(members, interval_minutes=60):
    dist = v61_distribution_summary(members)
    if not dist["members"] or interval_minutes != 60:
        return {"available": False, "reason": "missing_members_or_interval_not_one_hour", "interval_minutes": interval_minutes}
    values = dist["members"]
    wet = [m for m in values if m["value"] > 0.1]
    wet_weight = sum(m["weight"] for m in wet)
    wet_mean = sum(m["value"] * m["weight"] for m in wet) / wet_weight if wet_weight else 0.0
    wet_q = {"p%02d" % p: v61_weighted_quantile(wet, p / 100.0) for p in (10, 50, 90, 95)} if wet else {}
    bins = (("dry", 0.0, 0.1), ("light", 0.1, 5.0), ("moderate", 5.0, 10.0), ("heavy", 10.0, 20.0), ("extreme", 20.0, None))
    probs = {}
    for name, low, high in bins:
        if name == "dry":
            sel = [m for m in values if m["value"] <= high]
        else:
            sel = [m for m in values if (m["value"] > low if name == "light" else m["value"] >= low) and (high is None or m["value"] < high)]
        probs[name] = 100.0 * sum(m["weight"] for m in sel)
    exceed = {str(t): 100.0 * sum(m["weight"] for m in values if (m["value"] > t if t == 0.1 else m["value"] >= t)) for t in (0.1, 5.0, 10.0, 20.0)}
    return {"available": True, "occurrence_threshold_mm": 0.1, "interval_minutes": 60, "probability_percent": 100.0 * wet_weight,
            "conditional_wet_mean_mm": wet_mean, "conditional_wet_quantiles_mm": wet_q, "expected_rain_mm": wet_weight * wet_mean,
            "all_member_mean_mm": dist["mean"], "bin_probabilities_percent": probs, "exceedance_probabilities_percent": exceed,
            "calibration_status": "RAW_UNCALIBRATED", "method": "family_balanced_real_member_empirical_distribution"}


def v61_parse_ensemble_payload(payload, model, target_date, args, fetched_at=None):
    """Validasi kolom anggota individual tanpa mengarang anggota yang hilang. Anggota disimpan ringkas."""
    info = V61_ENSEMBLE_MODELS.get(model)
    if not info:
        raise ValueError("Model ensemble tidak didukung: " + str(model))
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("Respons ensemble bukan payload data yang valid")
    hourly, units = payload.get("hourly") or {}, payload.get("hourly_units") or {}
    times = hourly.get("time") or []
    parsed_times = []
    for stamp in times:
        try:
            dt = parse_open_meteo_time(stamp, args.timezone)
            parsed_times.append(dt if dt.tzinfo is not None else None)
        except (TypeError, ValueError, OverflowError):
            parsed_times.append(None)
    valid_times = [d for d in parsed_times if d is not None]
    steps = [(b - a).total_seconds() / 60.0 for a, b in zip(valid_times, valid_times[1:]) if b > a]
    step_minutes = min(steps) if steps else None
    flags = []
    if len(valid_times) != len(times):
        flags.append("invalid_timestamp")
    if len(set(valid_times)) != len(valid_times) or any(b <= a for a, b in zip(valid_times, valid_times[1:])):
        raise ValueError("Ensemble hourly timestamps must be unique and strictly increasing")
    if step_minutes is None:
        flags.append("unverifiable_interval")
    elif any(s != step_minutes for s in steps):
        flags.append("irregular_interval")
    out, rejected = {}, 0
    for base, (field, unit, lower, upper) in V61_ENSEMBLE_VARIABLES.items():
        columns, perturbed = {}, False
        for key, values in hourly.items():
            match = re.fullmatch(re.escape(base) + r"_member(\d+)", key)
            if match:
                member_id = int(match.group(1))
                columns[member_id] = (key, values)
                perturbed = perturbed or member_id > 0
        if perturbed and base in hourly and 0 not in columns:
            columns[0] = (base, hourly[base])
        if not perturbed:
            continue
        for member_id, (key, values) in sorted(columns.items()):
            if not isinstance(values, list) or len(values) != len(times):
                flags.append(field + ":member_length_mismatch")
                continue
            accepted = {unit, "celsius"} if field == "temp_c" else ({unit, "kmh", "km h-1"} if field == "wind_kmh" else {unit})
            if units.get(key, units.get(base)) not in accepted:
                flags.append(field + ":unit_mismatch")
                continue
            for idx, dt in enumerate(parsed_times):
                if dt is None or dt.date() != target_date:
                    continue
                raw = values[idx]
                value = None if isinstance(raw, bool) else safe_float(raw)
                if value is None or value < lower or value > upper:
                    rejected += 1
                    continue
                if field == "rain_mm" and step_minutes != 60:
                    continue
                iso = dt.isoformat()
                slot = out.setdefault(iso, {"valid_time": iso, "variables": {}})
                dist = slot["variables"].setdefault(field, {"members": [], "unit": unit, "interval_minutes": 60 if field == "rain_mm" else None,
                    "interval_start": (dt - timedelta(hours=1)).isoformat() if field == "rain_mm" else None,
                    "interval_end": iso if field == "rain_mm" else None})
                dist["members"].append({"model": model, "family": info["family"], "member": "m%02d" % member_id, "value": round(value, 3)})
    usable = sum(len(s["variables"].get("rain_mm", {}).get("members", [])) for s in out.values())
    min_members = max(2, int(getattr(args, "ensemble_min_members", 5)))
    sufficient = sum(len(s["variables"].get("rain_mm", {}).get("members", [])) >= min_members for s in out.values())
    return {"model": model, "family": info["family"], "success": sufficient > 0,
            "status": "usable" if sufficient else ("limited" if out else "unavailable"),
            "fetched_at": fetched_at or now_local(args.timezone).isoformat(), "model_run_at": payload.get("model_run_at") or payload.get("run"),
            "native_resolution_hours": None, "documented_initial_resolution_hours": info["native_hours"],
            "documented_global_schedule": V61_ICON_GLOBAL_RESOLUTION_SCHEDULE if info["family"] == "ICON" else None,
            "provider_resolution_scope": "documented_initial_step; exact_run_and_horizon_step_unverified",
            "api_resolution_minutes": step_minutes, "provider_may_interpolate": True, "api_interpolated": None,
            "qc_flags": sorted(set(flags)), "rejected_member_values": rejected, "valid_rain_member_values": usable,
            "sufficient_rain_hours": sufficient, "hourly": out,
            "grid_latitude": safe_float(payload.get("latitude")), "grid_longitude": safe_float(payload.get("longitude")),
            "api_elevation_m": safe_float(payload.get("elevation")),
            "elevation_basis": "api_target_downscaling_elevation_not_verified_native_grid_elevation",
            "provenance": "real_individual_ensemble_api_members; hourly values may be interpolated by provider"}


def v61_fetch_ensembles(target_date, args):
    """Satu URL per lokasi per siklus (jendela tanggal konstan), sehingga memo HTTP melayani semua tanggal."""
    if getattr(args, "disable_ensemble", False):
        return {"enabled": False, "status": "disabled", "models": {}, "hourly": {}}
    requested = str(getattr(args, "ensemble_models", "ncep_gefs_seamless,ecmwf_ifs025_ensemble,icon_global_eps"))
    models = list(dict.fromkeys(V61_ENSEMBLE_ALIASES.get(x.strip(), x.strip()) for x in requested.split(",") if x.strip()))
    window_start = getattr(args, "forecast_window_start", None) or target_date.isoformat()
    window_end = getattr(args, "forecast_window_end", None) or target_date.isoformat()
    issued = _quality_issued(args).isoformat()
    cache_key = (issued, str(getattr(args, "location_slug", "")), args.latitude, args.longitude, target_date.isoformat(), tuple(models))
    if cache_key in _V61_ENSEMBLE_RUNTIME_CACHE:
        return _V61_ENSEMBLE_RUNTIME_CACHE[cache_key]
    result = {"enabled": True, "issued_at": issued, "target_date": target_date.isoformat(), "models": {}, "hourly": {},
              "endpoint": V61_ENSEMBLE_URL, "calibration_status": "RAW_UNCALIBRATED", "requested_model_count": len(models)}

    def fetch_model(model):
        if model not in V61_ENSEMBLE_MODELS:
            return {"model": model, "success": False, "status": "unsupported_model", "error": "Model ensemble global tidak didukung", "hourly": {}}
        params = {"latitude": args.latitude, "longitude": args.longitude, "models": model, "hourly": ",".join(V61_ENSEMBLE_VARIABLES),
                  "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm", "timezone": args.timezone,
                  "start_date": window_start, "end_date": window_end, "temporal_resolution": "hourly"}
        url = build_url(V61_ENSEMBLE_URL, params)
        try:
            payload, status, duration = fetch_json_with_retry(url, headers=DEFAULT_HTTP_HEADERS, source_id="ENSEMBLE:" + model,
                                                              timeout=getattr(args, "http_timeout", HTTP_TIMEOUT_SECONDS), max_retry=getattr(args, "max_retry_http", MAX_RETRY_HTTP))
            parsed = v61_parse_ensemble_payload(payload, model, target_date, args)
            parsed.update({"http_status": status, "duration_ms": duration, "url": url})
            return parsed
        except Exception as exc:
            return {"model": model, "family": V61_ENSEMBLE_MODELS[model]["family"], "success": False, "status": "failed",
                    "error": type(exc).__name__ + ": " + str(exc), "url": url, "hourly": {}}

    with ThreadPoolExecutor(max_workers=max(1, min(3, len(models)))) as executor:
        futures = {executor.submit(fetch_model, m): m for m in models}
        for future in as_completed(futures):
            parsed, model = future.result(), futures[future]
            result["models"][model] = {k: v for k, v in parsed.items() if k != "hourly"}
            for iso, slot in parsed["hourly"].items():
                out = result["hourly"].setdefault(iso, {"valid_time": iso, "variables": {}})
                for field, dist in slot["variables"].items():
                    merged = out["variables"].setdefault(field, {k: v for k, v in dist.items() if k != "members"})
                    merged.setdefault("members", []).extend(dist["members"])
    minimum = max(2, int(getattr(args, "ensemble_min_members", 5)))
    for slot in result["hourly"].values():
        for field, dist in slot["variables"].items():
            dist["members"].sort(key=lambda m: (m["model"], m["member"]))
            counts = {}
            for m in dist["members"]:
                counts[m["model"]] = counts.get(m["model"], 0) + 1
            usable = [m for m in dist["members"] if counts[m["model"]] >= minimum]
            dist["excluded_sparse_models"] = [m for m, n in counts.items() if n < minimum]
            dist["raw_member_count"] = len(dist["members"])
            dist.update(v61_distribution_summary(usable))
            dist["quantile_status"] = "RAW_NOT_VALIDATED_AS_CONFIDENCE_INTERVAL"
    result["successful_model_count"] = sum(bool(m.get("success")) for m in result["models"].values())
    result["status"] = "usable" if result["successful_model_count"] == len(models) and models else ("partial" if result["successful_model_count"] else "unavailable")
    _V61_ENSEMBLE_RUNTIME_CACHE[cache_key] = result
    return result


def v61_apply_two_stage(probability_percent, conditional_wet_mean_mm, candidate):
    candidate = (candidate or {}).get("candidate", candidate or {})
    probability, wet_mean = safe_float(probability_percent), safe_float(conditional_wet_mean_mm)
    if probability is None or wet_mean is None:
        return {"probability_percent": probability, "conditional_wet_mean_mm": wet_mean, "expected_rain_mm": None}
    bins = (candidate.get("occurrence") or {}).get("bins") or []
    if bins:
        sb = sorted(bins, key=lambda b: b["center"])
        if sb[0]["center"] <= probability <= sb[-1]["center"]:
            if len(sb) == 1:
                probability = sb[0]["probability_percent"]
            else:
                for left, right in zip(sb, sb[1:]):
                    if left["center"] <= probability <= right["center"]:
                        span = right["center"] - left["center"]
                        frac = (probability - left["center"]) / span if span else 0.0
                        probability = left["probability_percent"] + frac * (right["probability_percent"] - left["probability_percent"])
                        break
    scale = safe_float(candidate.get("wet_amount_scale")) or 1.0
    wet_mean = max(0.0, wet_mean * max(0.5, min(2.0, scale)))
    probability = max(0.0, min(100.0, probability))
    return {"probability_percent": probability, "conditional_wet_mean_mm": wet_mean, "expected_rain_mm": probability / 100.0 * wet_mean}


def v61_enrich_probabilistic_rows(rows, ensemble_payload, args):
    """Lampirkan anggota nyata; POP per jam hanya diganti bila famili/anggota memadai."""
    hourly = (ensemble_payload or {}).get("hourly") or {}
    by_instant = {}
    for stamp, slot in hourly.items():
        dt = _quality_datetime(stamp, args.timezone)
        if dt is not None:
            by_instant[dt] = slot
    for row in rows:
        dt = _quality_datetime(row.get("valid_time") or row.get("target_datetime"), args.timezone)
        distributions = (by_instant.get(dt) or {}).get("variables") or {}
        row["ensemble_distribution"] = distributions
        row["ensemble_status"] = (ensemble_payload or {}).get("status", "unavailable")
        row["ensemble_quantile_status"] = "RAW_NOT_VALIDATED_AS_CONFIDENCE_INTERVAL"
        for field, prefix in (("temp_c", "temp"), ("rh_pct", "rh"), ("rain_mm", "rain"), ("wind_kmh", "wind")):
            for q, value in (distributions.get(field) or {}).get("quantiles", {}).items():
                row["ensemble_" + prefix + "_" + q] = value
        rain_dist = distributions.get("rain_mm") or {}
        row["ensemble_member_count"] = rain_dist.get("member_count", 0)
        row["ensemble_family_count"] = rain_dist.get("family_count", 0)
        adequate = (rain_dist.get("member_count", 0) >= max(2, int(getattr(args, "ensemble_min_members", 5)))
                    and rain_dist.get("family_count", 0) >= max(1, int(getattr(args, "ensemble_min_families", 2)))
                    and rain_dist.get("interval_minutes") == 60)
        if not adequate:
            row["probability_basis"] = row.get("probability_basis") or "deterministic_source_heuristic"
            row["rain_two_stage"] = {"available": False, "reason": "insufficient_real_members_or_families"}
            continue
        rain = v61_rain_two_stage_summary(rain_dist["members"], 60)
        row["rain_two_stage_raw"] = dict(rain)
        model = (_QUALITY_SKILL or {}).get("rain_two_stage")
        if model and model.get("accepted") is True:
            raw_exceed = dict(rain["exceedance_probabilities_percent"])
            rain.update(v61_apply_two_stage(rain["probability_percent"], rain["conditional_wet_mean_mm"], model))
            scale = safe_float(model.get("wet_amount_scale")) or 1.0
            wet_members = [m for m in rain_dist["members"] if m["value"] > 0.1]
            wet_weight = sum(m["weight"] for m in wet_members)
            occurrence = rain["probability_percent"] / 100.0
            rain["conditional_wet_quantiles_mm"] = {k: v * scale for k, v in rain["conditional_wet_quantiles_mm"].items()}
            rain["raw_exceedance_probabilities_percent"] = raw_exceed
            rain["exceedance_probabilities_percent"] = {"0.1": rain["probability_percent"]}
            for t in (5.0, 10.0, 20.0):
                mass = sum(m["weight"] for m in wet_members if m["value"] * scale >= t)
                rain["exceedance_probabilities_percent"][str(t)] = 100.0 * occurrence * mass / wet_weight if wet_weight else 0.0
            ex = rain["exceedance_probabilities_percent"]
            rain["bin_probabilities_percent"] = {"dry": 100.0 - rain["probability_percent"], "light": rain["probability_percent"] - ex["5.0"],
                                                 "moderate": ex["5.0"] - ex["10.0"], "heavy": ex["10.0"] - ex["20.0"], "extreme": ex["20.0"]}
            rain["calibration_status"] = "VALIDATED_CANDIDATE_APPLIED_RECHECK_REQUIRED"
            rain["method"] = "family_balanced_real_members_plus_frozen_two_stage_model"
        row["rain_two_stage"] = rain
        row["prob_rain_heuristic"] = row.get("prob_rain_heuristic", row.get("prob_rain"))
        row["prob_rain_raw"] = row["rain_two_stage_raw"]["probability_percent"]
        row["prob_rain"] = rain["probability_percent"]
        row["probability_basis"] = rain["method"]
        row["prob_heavy_rain"] = rain["exceedance_probabilities_percent"]["10.0"]
        row["prob_moderate_heavy_rain"] = rain["exceedance_probabilities_percent"]["5.0"]
        row["rain_mm_expected"] = rain["expected_rain_mm"]
        row["rain_mm_est"] = rain_dist["quantiles"].get("p50")
        for q, value in rain_dist["quantiles"].items():
            row["rain_" + q] = value
        row["rain_interval_start"], row["rain_interval_end"], row["rain_interval_minutes"] = rain_dist["interval_start"], rain_dist["interval_end"], 60
        row["calibration_status"] = rain["calibration_status"]
    return rows


def v61_crps(members, observation):
    observation = safe_float(observation)
    if observation is None:
        return None
    pairs = sorted((v, w) for v, w in ((safe_float(m.get("value")), safe_float(m.get("weight", 1.0))) for m in members) if v is not None and w is not None and w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    absolute = sum(w * abs(v - observation) for v, w in pairs) / total
    acc_w = acc_v = half = 0.0
    for value, weight in pairs:
        half += weight * (value * acc_w - acc_v)
        acc_w += weight
        acc_v += weight * value
    return max(0.0, absolute - half / (total * total))


def _v61_observation_lookup(observations):
    if isinstance(observations, dict):
        return observations
    return {(sentinel_obs_date_to_iso(str(o.get("date") or o.get("target_date") or o.get("tanggal") or "")), str(o.get("time") or o.get("jam") or "")): o for o in observations or []}


def _v61_same_observed_interval(obs, distribution, valid, args):
    minutes = safe_float(_quality_first(obs, "rain_mm_interval_minutes", "rain_interval_minutes", "interval_minutes"))
    end = _quality_datetime(_quality_first(obs, "rain_mm_interval_end", "rain_interval_end", "interval_end"), args.timezone)
    start = _quality_datetime(_quality_first(obs, "rain_mm_interval_start", "rain_interval_start", "interval_start"), args.timezone)
    if end is None or start is None:
        return False
    exp_end = _quality_datetime(distribution.get("interval_end"), args.timezone)
    exp_start = _quality_datetime(distribution.get("interval_start"), args.timezone)
    return minutes == 60 and end == exp_end == valid and start == exp_start and (end - start).total_seconds() == 3600


def _v61_observed_location_distance(obs, field, target=None):
    reported = safe_float(obs.get(field + "_station_distance_km"))
    if reported is not None:
        return reported if reported >= 0 else None
    variable_station, global_station = obs.get(field + "_station_id"), obs.get("station_id")
    other = bool(variable_station and global_station and variable_station != global_station)
    lat_keys, lon_keys = [field + "_station_latitude"], [field + "_station_longitude"]
    if not other:
        lat_keys += ["station_latitude", "latitude"]
        lon_keys += ["station_longitude", "longitude"]
    lat, lon = safe_float(_quality_first(obs, *lat_keys)), safe_float(_quality_first(obs, *lon_keys))
    if isinstance(target, dict):
        t_lat, t_lon = safe_float(_quality_first(target, "site_latitude", "latitude")), safe_float(_quality_first(target, "site_longitude", "longitude"))
    else:
        t_lat, t_lon = safe_float(getattr(target, "latitude", None)), safe_float(getattr(target, "longitude", None))
    if all(v is not None for v in (lat, lon, t_lat, t_lon)):
        if not (-90 <= lat <= 90 and -90 <= t_lat <= 90 and -180 <= lon <= 180 and -180 <= t_lon <= 180):
            return None
        return _v61_distance_km(lat, lon, t_lat, t_lon)
    if not other:
        g = safe_float(obs.get("station_distance_km"))
        if g is not None and g >= 0:
            return g
    return None


_RAIN_BIN_EDGES = (("dry", None, 0.1), ("light", 0.1, 5), ("moderate", 5, 10), ("heavy", 10, 20), ("extreme", 20, None))


def _rain_bin(value):
    return "dry" if value <= 0.1 else "light" if value < 5 else "moderate" if value < 10 else "heavy" if value < 20 else "extreme"


def v61_probabilistic_verification(rows, observations, args):
    """Verifikasi hanya prakiraan tak-berubah yang terbit sebelum kejadian dan observasi QC lolos."""
    observed = _v61_observation_lookup(observations)
    eval_at = now_local(args.timezone)
    buckets, unique = {}, {}
    rejected = {"not_pre_event": 0, "missing_qc_observation": 0, "rain_interval_mismatch": 0, "unknown_observation_location": 0, "observation_outside_local_radius": 0}
    for row in rows:
        valid = _quality_datetime(row.get("valid_time") or row.get("target_datetime"), args.timezone)
        if valid is None:
            continue
        issued = _quality_datetime(row.get("issued_at"), args.timezone)
        if issued is None or issued > valid - timedelta(hours=1) or valid > eval_at:
            rejected["not_pre_event"] += 1
            continue
        obs = observed.get((valid.date().isoformat(), valid.strftime("%H:%M"))) or observed.get(valid.isoformat())
        if not obs:
            rejected["missing_qc_observation"] += 1
            continue
        lead = (valid - issued).total_seconds() / 3600.0
        lead_bin = "0-24h" if lead <= 24 else ("24-48h" if lead <= 48 else "48h+")
        key = (str(row.get("location_slug") or ""), valid, lead_bin)
        prior = unique.get(key)
        if prior is None or issued < prior[0]:
            unique[key] = (issued, row, obs, valid, lead_bin)
    limits = {"rain_mm": 2.0, "temp_c": 10.0, "rh_pct": 10.0, "wind_kmh": 5.0}
    for issued, row, obs, valid, lead_bin in unique.values():
        for field, dist in (row.get("ensemble_distribution") or {}).items():
            value = _quality_observed_value(obs, field)
            available = _quality_datetime(obs.get(field + "_available_at") or obs.get("available_at"), args.timezone)
            if value is None or available is None or available > eval_at:
                rejected["missing_qc_observation"] += 1
                continue
            distance = _v61_observed_location_distance(obs, field, args)
            if distance is None:
                rejected["unknown_observation_location"] += 1
                continue
            if distance > limits.get(field, 2.0):
                rejected["observation_outside_local_radius"] += 1
                continue
            if field == "rain_mm" and not _v61_same_observed_interval(obs, dist, valid, args):
                rejected["rain_interval_mismatch"] += 1
                continue
            members = dist.get("members") or []
            score = v61_crps(members, value)
            if score is None:
                continue
            p10, p90 = v61_weighted_quantile(members, 0.1), v61_weighted_quantile(members, 0.9)
            item = {"date": valid.date().isoformat(), "lead_bin": lead_bin, "crps": score, "covered": p10 <= value <= p90, "width": p90 - p10}
            if field == "rain_mm":
                stage = v61_rain_two_stage_summary(members, 60)
                item["brier"] = {t: (p / 100.0 - (1.0 if (value > float(t) if float(t) == 0.1 else value >= float(t)) else 0.0)) ** 2
                                 for t, p in stage["exceedance_probabilities_percent"].items()}
                item["rain_bin"] = _rain_bin(value)
            buckets.setdefault(field, []).append(item)
    result = {"schema": "langit-probabilistic-verification-1", "generated_at": eval_at.isoformat(),
              "status": "evaluated" if buckets else "insufficient_matching_observations", "nominal_interval_coverage": 0.8,
              "calibration_status": "NOT_CERTIFIED", "distribution": "raw_family_balanced_individual_members",
              "issue_rule": "earliest pre-event issue per valid_time and lead bin, minimum one-hour lead",
              "local_observation_radius_km": limits, "rejected": rejected, "variables": {}}

    def summarize(group, field):
        s = {"n": len(group), "distinct_days": len({i["date"] for i in group}), "crps": sum(i["crps"] for i in group) / len(group),
             "p10_p90_coverage": sum(i["covered"] for i in group) / len(group), "p10_p90_mean_width": sum(i["width"] for i in group) / len(group)}
        if field == "rain_mm":
            s["brier_by_threshold_mm_per_hour"] = {t: sum(i["brier"][t] for i in group) / len(group) for t in ("0.1", "5.0", "10.0", "20.0")}
            s["observed_intensity_counts"] = {b: sum(i["rain_bin"] == b for i in group) for b in ("dry", "light", "moderate", "heavy", "extreme")}
        return s

    for field, items in buckets.items():
        result["variables"][field] = summarize(items, field)
        result["variables"][field]["by_lead"] = {l: summarize([i for i in items if i["lead_bin"] == l], field) for l in sorted({i["lead_bin"] for i in items})}
        if field == "rain_mm":
            result["variables"][field]["by_observed_intensity"] = {n: summarize([i for i in items if i["rain_bin"] == n], field) for n in sorted({i["rain_bin"] for i in items})}
    return result


def _v61_rain_case(case):
    row = case.get("row") or case.get("forecast") or case
    obs = case.get("obs") or case.get("observation") or {}
    rain = _quality_observed_value(obs, "rain_mm")
    if rain is None:
        return None
    raw_stage = row.get("rain_two_stage_raw") or row.get("rain_two_stage") or {}
    valid = case.get("valid")
    if not isinstance(valid, datetime):
        valid = _quality_datetime(row.get("valid_time") or row.get("target_datetime"))
    end = _quality_datetime(_quality_first(obs, "rain_mm_interval_end", "rain_interval_end", "interval_end"))
    start = _quality_datetime(_quality_first(obs, "rain_mm_interval_start", "rain_interval_start", "interval_start"))
    minutes = safe_float(_quality_first(obs, "rain_mm_interval_minutes", "rain_interval_minutes", "interval_minutes"))
    if valid is None or end != valid or start != valid - timedelta(hours=1) or minutes != 60:
        return None
    dist_distance = _v61_observed_location_distance(obs, "rain_mm", row)
    if dist_distance is None or dist_distance > 2:
        return None
    dist = (row.get("ensemble_distribution") or {}).get("rain_mm") or {}
    if (dist.get("interval_minutes") or raw_stage.get("interval_minutes") or row.get("rain_interval_minutes")) != 60:
        return None
    probability = safe_float(raw_stage.get("probability_percent"))
    if probability is None:
        probability = safe_float(row.get("prob_rain_raw"))
    if probability is None:
        probability = safe_float(row.get("prob_rain"))
    wet_mean = safe_float(raw_stage.get("conditional_wet_mean_mm"))
    if wet_mean is None and dist.get("members"):
        wet_mean = safe_float(v61_rain_two_stage_summary(dist["members"], dist.get("interval_minutes")).get("conditional_wet_mean_mm"))
    if wet_mean is None:
        wet_mean = safe_float(row.get("rain_mm_est"))
        if wet_mean is None:
            wet_mean = safe_float(row.get("rain_p50"))
    if probability is None or not 0 <= probability <= 100 or wet_mean is None or wet_mean < 0:
        return None
    return {"p": probability, "wet_mean": wet_mean, "rain": rain, "date": str(case.get("date") or row.get("date") or str(case.get("valid") or "")[:10]),
            "intensity_basis": "conditional_real_members" if raw_stage.get("available") or dist.get("members") else "deterministic_intensity_proxy"}


def v61_score_rain_two_stage(cases, candidate=None):
    samples = [i for i in (_v61_rain_case(c) for c in cases) if i is not None and i["intensity_basis"] == "conditional_real_members"]
    wet_errors, brier = [], []
    bins = {n: [] for n in ("dry", "light", "moderate", "heavy", "extreme")}
    for item in samples:
        pred = v61_apply_two_stage(item["p"], item["wet_mean"], candidate)
        event = item["rain"] > 0.1
        brier.append((pred["probability_percent"] / 100.0 - float(event)) ** 2)
        if event:
            err = abs(pred["conditional_wet_mean_mm"] - item["rain"])
            wet_errors.append(err)
            bins[_rain_bin(item["rain"])].append(err)
    return {"n": len(samples), "distinct_days": len({s["date"] for s in samples if s["date"]}), "n_wet": sum(s["rain"] > 0.1 for s in samples),
            "n_dry": sum(s["rain"] <= 0.1 for s in samples), "brier": sum(brier) / len(brier) if brier else None,
            "wet_mae": sum(wet_errors) / len(wet_errors) if wet_errors else None,
            "intensity_mae_by_bin": {n: {"n": len(e), "mae": sum(e) / len(e) if e else None} for n, e in bins.items()},
            "conditional_member_samples": len(samples)}


def v61_fit_rain_two_stage(train_cases, validation_cases):
    train = [i for i in (_v61_rain_case(c) for c in train_cases) if i is not None and i["intensity_basis"] == "conditional_real_members"]
    before = v61_score_rain_two_stage(validation_cases)
    wet = [s for s in train if s["rain"] > 0.1 and s["wet_mean"] > 0.1]
    dry = [s for s in train if s["rain"] <= 0.1]
    evidence = {"n_train": len(train), "distinct_training_days": len({s["date"] for s in train if s["date"]}), "n_wet_train": len(wet),
                "n_dry_train": len(dry), "n_validation": before["n"], "n_wet_validation": before["n_wet"], "brier_before": before["brier"],
                "wet_mae_before": before["wet_mae"], "fit_uses_validation": False, "test_set_used": False}
    if len(train) < 60 or len(wet) < 20 or len(dry) < 20 or evidence["distinct_training_days"] < 10:
        evidence["reason"] = "insufficient_training_days_or_event_classes"
        return {"candidate": None, "accepted": False, "evidence": evidence}
    pbins = []
    for lower in range(0, 100, 20):
        group = [s for s in train if lower <= s["p"] < lower + 20 or (lower == 80 and s["p"] == 100)]
        if len(group) < 10:
            continue
        center = sum(s["p"] for s in group) / len(group)
        target = (100.0 * sum(s["rain"] > 0.1 for s in group) + 20.0 * center) / (len(group) + 20.0)
        pbins.append({"center": center, "probability_percent": target, "n": len(group)})
    if len(pbins) < 3:
        evidence["reason"] = "insufficient_probability_support"
        return {"candidate": None, "accepted": False, "evidence": evidence}
    blocks = []
    for b in pbins:
        blocks.append([b])
        while len(blocks) >= 2:
            left = sum(x["probability_percent"] * x["n"] for x in blocks[-2]) / sum(x["n"] for x in blocks[-2])
            right = sum(x["probability_percent"] * x["n"] for x in blocks[-1]) / sum(x["n"] for x in blocks[-1])
            if left <= right:
                break
            blocks[-2].extend(blocks.pop())
    for block in blocks:
        pooled = sum(x["probability_percent"] * x["n"] for x in block) / sum(x["n"] for x in block)
        for b in block:
            b["probability_percent"] = pooled
    mean_log = sum(math.log(s["rain"] / s["wet_mean"]) for s in wet) / len(wet)
    scale = max(0.5, min(2.0, math.exp(mean_log * len(wet) / (len(wet) + 50.0))))
    candidate = {"schema": "langit-two-stage-rain-1", "threshold_mm": 0.1, "interval_minutes": 60,
                 "occurrence": {"method": "shrunken_monotonic_bins_no_extrapolation", "bins": pbins}, "wet_amount_scale": scale,
                 "wet_scale_method": "regularized_mean_log_ratio", "status": "candidate_requires_rolling_and_frozen_test_acceptance",
                 "training_case_count": len(train), "training_probability_basis": "real_individual_members", "training_intensity_basis": "conditional_real_members"}
    after = v61_score_rain_two_stage(validation_cases, candidate)
    evidence.update({"brier_after": after["brier"], "wet_mae_after": after["wet_mae"], "validation_before": before, "validation_after": after})
    accepted = (before["n"] >= 20 and before["n_wet"] >= 10 and before["n_dry"] >= 10 and before["distinct_days"] >= 3 and before["brier"] is not None
                and after["brier"] < before["brier"] and before["wet_mae"] is not None and after["wet_mae"] < before["wet_mae"])
    evidence["reason"] = "joint_validation_improvement" if accepted else "validation_absent_insufficient_or_not_jointly_improved"
    return {"candidate": candidate, "accepted": bool(accepted), "evidence": evidence}


# ---------------------------------------------------------------------------
# Arsip prakiraan, skill, pengayaan baris
# ---------------------------------------------------------------------------
def aether_db_path():
    return path_output(AETHER_DB_FILENAME)


def aether_connect_db():
    ensure_directory(ACTIVE_OUTPUT_DIR)
    conn = sqlite3.connect(aether_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def aether_init_db(conn):
    # payload_json kini hanya berisi metadata ringan; payload penuh ada di forecast_archive/*.json.gz.
    conn.execute("CREATE TABLE IF NOT EXISTS quality_runs (run_id TEXT PRIMARY KEY, issued_at TEXT NOT NULL, target_date TEXT NOT NULL, payload_json TEXT NOT NULL)")
    conn.commit()


def _quality_archive_dir():
    directory = path_output(ARCHIVE_DIRNAME)
    ensure_directory(directory)
    return directory


def _quality_archive_documents():
    """Generator: satu dokumen per waktu; tidak memuat seluruh arsip sekaligus."""
    entries = {}
    for entry in os.scandir(_quality_archive_dir()):
        if not entry.is_file() or not entry.name.endswith((".json", ".json.gz")):
            continue
        identity = entry.name.removesuffix(".gz")
        if identity not in entries or entry.name.endswith(".json"):
            entries[identity] = entry.path
    for identity in sorted(entries):
        doc = read_json(entries[identity], {})
        if isinstance(doc, dict) and doc:
            yield doc


def _strip_members(row):
    """Salinan baris tanpa daftar anggota ensemble (untuk JSON/CSV publik)."""
    out = {k: v for k, v in row.items() if k != "ensemble_models"}
    dists = out.get("ensemble_distribution")
    if isinstance(dists, dict):
        out["ensemble_distribution"] = {f: {k: v for k, v in d.items() if k != "members"} for f, d in dists.items() if isinstance(d, dict)}
    return out


def _slim_archive_row(row):
    out = {k: v for k, v in row.items() if k != "ensemble_models"}
    dists = out.get("ensemble_distribution")
    if isinstance(dists, dict):
        slim = {}
        for field, d in dists.items():
            if not isinstance(d, dict):
                continue
            nd = dict(d)
            nd["members"] = [{"model": m["model"], "family": m["family"], "member": m["member"], "value": m["value"], "weight": round(m.get("weight", 0.0), 8)}
                             for m in d.get("members", [])]
            slim[field] = nd
        out["ensemble_distribution"] = slim
    return out


def _quality_archive_run(run_id, target_date, results, rows, args):
    issued = _quality_issued(args)
    source_points = [_quality_jsonable({**asdict(p), "weight": point_weight(p)}) for r in results for p in r.points.values()]
    payload = {"schema_version": QUALITY_SCHEMA, "run_id": run_id, "issued_at": issued.isoformat(), "target_date": target_date.isoformat(),
               "location_slug": args.location_slug, "location_name": args.location_name, "latitude": args.latitude, "longitude": args.longitude,
               "skill_config": _QUALITY_SKILL, "hourly": [_slim_archive_row(r) for r in rows], "source_points": source_points,
               "activity_plans": v61_activity_plans(rows, args, now=issued),
               "source_availability": read_json(path_output("source_availability.json"), {}),
               "ensemble_status": {k: v for k, v in _V61_ENSEMBLES.items() if k != "hourly"},
               "site_elevation_m": getattr(args, "site_elevation_m", None), "terrain_class": getattr(args, "terrain_class", "unknown")}
    archive = os.path.join(_quality_archive_dir(), run_id + ".json.gz")
    if os.path.exists(archive) or os.path.exists(archive.removesuffix(".gz")):
        raise ValueError("Forecast run already archived: " + run_id)
    write_json_gz(archive, payload)
    conn = aether_connect_db()
    try:
        aether_init_db(conn)
        conn.execute("INSERT INTO quality_runs VALUES (?,?,?,?)", (run_id, issued.isoformat(), target_date.isoformat(),
                                                                  json.dumps({"archive": os.path.basename(archive), "hours": len(rows)})))
        conn.commit()
    finally:
        conn.close()
    return archive


def _quality_historical_rows(observations=None):
    """Generator baris historis. Dengan `observations`, baris tanpa pasangan observasi dilewati."""
    for doc in _quality_archive_documents():
        for row in doc.get("hourly") or []:
            if observations is not None and (row.get("target_date"), row.get("jam")) not in observations:
                continue
            yield {**row, "issued_at": doc.get("issued_at"), "run_id": doc.get("run_id"), "location_slug": doc.get("location_slug")}


def _quality_forecast_pairs(rows, observations, args):
    selected = {}
    now = now_local(args.timezone)
    for row in rows or []:
        date, hour = row.get("target_date"), _lg_hour(row.get("jam"), "")
        issued = _quality_datetime(row.get("issued_at"), args.timezone)
        valid = _quality_datetime(row.get("target_datetime"), args.timezone)
        if issued is None or valid is None or valid > now or issued > valid - timedelta(hours=1) or row.get("data_quality") not in {"usable", "limited", "stale"}:
            continue
        observation = observations.get((date, hour))
        if observation is None:
            continue
        lead = (valid - issued).total_seconds() / 3600
        key = (date, hour, aether_lead_bucket(lead))
        previous = selected.get(key)
        if previous is None or issued < previous[0]:
            selected[key] = (issued, row, observation, lead)
    return [selected[k] for k in sorted(selected)]


def _quality_map_probability(probability, mapping):
    for left, right in zip(mapping, mapping[1:]):
        if left[0] <= probability <= right[0] and right[0] > left[0]:
            return left[1] + (probability - left[0]) / (right[0] - left[0]) * (right[1] - left[1])
    return probability


def _quality_apply_probability(probability, valid_time):
    probability = _lg_prob(probability)
    if probability is None:
        return None
    mapping = _quality_skill_section("prob_rain", valid_time, _RUN_ISSUED_AT).get("mapping") or []
    for left, right in zip(mapping, mapping[1:]):
        if left[0] <= probability <= right[0] and right[0] > left[0]:
            return round(left[1] + (probability - left[0]) / (right[0] - left[0]) * (right[1] - left[1]), 3)
    return probability


def _quality_load_skill(args):
    global _QUALITY_SKILL
    doc = read_json(path_output("quality_skill_config.json"), {}) or {}
    fitted = _quality_datetime(doc.get("fitted_at"), args.timezone)
    _QUALITY_SKILL = doc if doc.get("location_slug") == args.location_slug and fitted and fitted <= _quality_issued(args) else {}


def _data_quality_thresholds():
    families = {_quality_family(c["source_id"]) for c in ACTIVE_SOURCE_CONFIGS}
    return len(families), max(3, math.ceil(len(families) * 0.7))


def _quality_enrich_rows(rows, points, target_date, args):
    issued = _quality_issued(args)
    expected_families, usable_families = _data_quality_thresholds()
    grouped = {}
    for p in points:
        grouped.setdefault(p.target_time, []).append(p)
    for row in rows:
        bucket = grouped.get(row["jam"], [])
        actual = [p for p in bucket if any(getattr(p, k) is not None for k in ("temp_c", "rh_pct", "rain_mm", "wind_kmh"))]
        families = {_quality_family(p.source_id) for p in actual}
        row.update({"issued_at": issued.isoformat(), "site_latitude": args.latitude, "site_longitude": args.longitude,
                    "site_elevation_m": getattr(args, "site_elevation_m", None), "terrain_class": getattr(args, "terrain_class", "unknown"),
                    "rain_interval_minutes": 60, "model_families": sorted(families), "independent_families_used": len(families),
                    "variable_coverage": {k: sum(getattr(p, k) is not None for p in bucket) for k in ("temp_c", "rh_pct", "rain_mm", "wind_kmh")},
                    "quality_flags": sorted({f for p in bucket for f in p.quality_flags}),
                    "is_cached": any(p.is_cached for p in actual), "cache_age_hours": max((p.cache_age_hours or 0 for p in actual), default=0)})
        coverage = safe_float(row.get("coverage_fraction")) or 0
        if len(families) < 3 or not actual or safe_float(row.get("prob_rain")) is None or coverage < 0.5:
            row["data_quality"] = "unavailable"
        elif row["is_cached"]:
            row["data_quality"] = "stale"
        elif len(families) < usable_families:
            row["data_quality"] = "limited"
        else:
            row["data_quality"] = "usable"
        row["expected_families"] = expected_families
        if row["data_quality"] == "unavailable":
            row["operational_status"], row["trust_level"] = "BLACK", "DO_NOT_TRUST"
            for field in ("prob_rain", "prob_heavy_rain", "prob_moderate_heavy_rain", "rain_threat_score", "heavy_rain_threat_score", "direct_hit_risk", "nearby_rain_risk"):
                row[field] = None
            row["decision_recommendation"] = "Data cuaca belum memadai untuk rekomendasi aktivitas."
        row["probability_unit"] = "percent"
        row["risk_score_basis"] = "heuristic_not_observation_certified"
        row["rain_interval_start"] = (parse_local_hour_string(target_date, row["jam"], args.timezone) - timedelta(hours=1)).isoformat()
        row["rain_interval_end"] = row["target_datetime"]
        row["rain_quantile_basis"] = "family_balanced_real_members_raw_not_calibrated_interval" if row.get("rain_two_stage", {}).get("available") else "inter_model_quantile_not_calibrated_interval"
    return rows


# ---------------------------------------------------------------------------
# Evaluasi bergulir beku (frozen rolling evaluation) + koreksi lokal empiris
# ---------------------------------------------------------------------------
_V61_NUMERIC_VARIABLES = ("temp_c", "rh_pct", "rain_mm", "wind_kmh")
_V61_STATION_METADATA = ("station_latitude", "station_longitude", "station_elevation_m", "station_distance_km", "station_id", "latitude", "longitude",
                         "elevation_m", "terrain_class", "interval_start", "interval_end", "interval_minutes", "rain_interval_start", "rain_interval_end",
                         "rain_interval_minutes", "rain_mm_interval_start", "rain_mm_interval_end", "rain_mm_interval_minutes")
_V61_FIELD_STATION_METADATA = tuple(f + "_station_" + s for f in _OBS_FIELDS for s in ("latitude", "longitude", "elevation_m", "distance_km", "id"))


def _v61_matching_rain_interval(obs, valid):
    start = _quality_datetime(obs.get("rain_mm_interval_start") or obs.get("rain_interval_start") or obs.get("interval_start"))
    end = _quality_datetime(obs.get("rain_mm_interval_end") or obs.get("rain_interval_end") or obs.get("interval_end"))
    minutes = safe_float(obs.get("rain_mm_interval_minutes", obs.get("rain_interval_minutes", obs.get("interval_minutes"))))
    if start is None or end is None:
        return False
    actual = (end - start).total_seconds() / 60
    return abs(actual - 60) < 1e-6 and (minutes is None or abs(minutes - 60) < 1e-6) and abs((end - valid).total_seconds()) < 1 and abs((start - (valid - timedelta(hours=1))).total_seconds()) < 1


def _v61_station_value(obs, variable, suffix):
    for key in (variable + "_station_" + suffix, "station_" + suffix, suffix):
        value = safe_float(obs.get(key))
        if value is not None:
            return value
    return None


def _quality_training_cases(args):
    """Kasus pelatihan: prakiraan pra-kejadian + observasi lolos QC yang tersedia saat itu dan berada dalam radius lokal."""
    observations = sentinel_load_observations_by_key(args)
    if not observations:
        return []
    now = now_local(args.timezone)
    limits = {"rain_mm": 2.0, "wind_kmh": 5.0, "temp_c": 10.0, "rh_pct": 10.0, "category": 2.0}
    raw_cases = []
    for doc in _quality_archive_documents():
        issued = _quality_datetime(doc.get("issued_at"), args.timezone)
        if issued is None or (doc.get("location_slug") and doc["location_slug"] != args.location_slug):
            continue
        points_by_hour = {}
        for p in doc.get("source_points") or []:
            points_by_hour.setdefault(p.get("target_time"), []).append(p)
        for row in doc.get("hourly") or []:
            valid = _quality_datetime(row.get("target_datetime"), args.timezone)
            if valid is None or valid > now or issued > valid - timedelta(hours=1):
                continue
            raw = observations.get((row.get("target_date"), row.get("jam")))
            if raw is None:
                continue
            obs = {}
            for key in _V61_STATION_METADATA + _V61_FIELD_STATION_METADATA:
                if raw.get(key) not in (None, ""):
                    obs[key] = raw[key]
            for field in _OBS_FIELDS:
                value = _quality_observation_available(raw, field, now)
                if value is None:
                    continue
                obs[field] = value
                for suffix, fb in (("type", "observation_type"), ("qc", "qc"), ("available_at", "available_at"), ("source", "source_id")):
                    obs[field + "_" + suffix] = raw.get(field + "_" + suffix) or raw.get(fb)
            if "rain_mm" in obs and not _v61_matching_rain_interval(obs, valid):
                obs.pop("rain_mm", None)
            for field, maximum in limits.items():
                base_field = field if field != "category" else "rain_mm"
                lat, lon = _v61_station_value(obs, base_field, "latitude"), _v61_station_value(obs, base_field, "longitude")
                distance = _v61_distance_km(getattr(args, "latitude", None), getattr(args, "longitude", None), lat, lon)
                if distance is None:
                    distance = _v61_station_value(obs, base_field, "distance_km")
                if distance is None or distance < 0 or distance > maximum:
                    obs.pop(field, None)
                else:
                    obs[field + "_station_distance_km"] = distance
            if not any(f in obs for f in _OBS_FIELDS):
                continue
            raw_cases.append({"date": valid.date().isoformat(), "valid": valid, "issued": issued, "row": row, "obs": obs, "points": points_by_hour.get(row.get("jam"), [])})
    selected = {}
    for c in sorted(raw_cases, key=lambda c: c["issued"]):
        key = (c["date"], c["valid"].strftime("%H:%M"), aether_lead_bucket((c["valid"] - c["issued"]).total_seconds() / 3600))
        selected.setdefault(key, c)
    return list(selected.values())


def _quality_predict_variable(case, variable, multipliers=None):
    pairs = []
    for p in case["points"]:
        value = safe_float(p.get(variable))
        if value is None:
            continue
        weight = (safe_float(p.get("weight")) or SOURCE_BASE_WEIGHTS.get(p.get("source_id"), 1)) * (multipliers or {}).get(p.get("source_id"), 1)
        pairs.append((value, weight))
    return aether_weighted_quantile(pairs, 0.5)


def aether_weighted_quantile(weighted_pairs, q):
    valid = sorted((float(v), float(w)) for v, w in weighted_pairs if v is not None and w is not None and w > 0)
    if not valid:
        return None
    total = sum(w for _, w in valid)
    if total <= 0:
        return None
    threshold, cumulative = q * total, 0.0
    for value, weight in valid:
        cumulative += weight
        if cumulative >= threshold:
            return round(value, 3)
    return round(valid[-1][0], 3)


def _v61_asof_cases(cases, cutoff):
    output = []
    for case in cases:
        if case["valid"] >= cutoff:
            continue
        clean = {k: v for k, v in case["obs"].items() if k in _V61_STATION_METADATA + _V61_FIELD_STATION_METADATA or k == "station_distance_km"}
        for field in _OBS_FIELDS:
            value = _quality_observation_available(case["obs"], field, cutoff)
            if value is not None:
                clean[field] = value
                for suffix in ("type", "qc", "available_at", "source"):
                    clean[field + "_" + suffix] = case["obs"].get(field + "_" + suffix)
        if any(f in clean for f in _OBS_FIELDS):
            output.append(dict(case, obs=clean))
    return output


def _v61_scope_cases(cases):
    scopes = {"all": cases}
    for case in cases:
        for name, value in _quality_context(case["valid"], case["issued"]).items():
            scopes.setdefault(name + ":" + value, []).append(case)
    return scopes


def _v61_frozen_cohort(cases, args, ledger):
    pending = next((c for c in ledger.get("cohorts", []) if c.get("status") == "reserved"), None)
    if pending:
        return pending, None
    used_cutoff = max((c["data_cutoff_date"] for c in ledger.get("cohorts", [])), default="")
    fresh = [c for c in cases if c["date"] > used_cutoff]
    days = sorted({c["date"] for c in fresh})
    min_days = max(20, int(getattr(args, "evaluation_min_days", 30)))
    test_days = max(3, int(getattr(args, "evaluation_test_days", 7)))
    folds_count = max(2, min(5, int(getattr(args, "evaluation_folds", 3))))
    if len(days) < max(min_days, test_days + 13) or len(fresh) < int(getattr(args, "learning_min_samples", 60)):
        return None, {"status": "insufficient_new_data", "new_days": len(days), "new_samples": len(fresh), "required_days": max(min_days, test_days + 13), "previous_cutoff": used_cutoff or None}
    dev_days, final_days = days[:-test_days], days[-test_days:]
    initial = max(10, len(dev_days) // 2)
    val_days = dev_days[initial:]
    if len(val_days) < 3 * folds_count:
        folds_count = max(2, len(val_days) // 3)
    if len(val_days) < 6:
        return None, {"status": "insufficient_rolling_validation_days"}
    folds = []
    for i in range(folds_count):
        left, right = initial + i * len(val_days) // folds_count, initial + (i + 1) * len(val_days) // folds_count
        block = dev_days[left:right]
        folds.append({"fold": i + 1, "train_dates": dev_days[:left], "validation_dates": block,
                      "fit_cutoff": parse_local_hour_string(datetime.fromisoformat(block[0]).date(), "00:00", args.timezone).isoformat()})
    digest = hashlib.sha256(json.dumps(sorted((c["date"], c["valid"].isoformat(), c["issued"].isoformat()) for c in fresh), separators=(",", ":")).encode()).hexdigest()
    cohort = {"cohort_id": uuid.uuid4().hex, "created_at": now_local(args.timezone).isoformat(), "location_slug": args.location_slug, "cohort_digest": digest,
              "data_cutoff_date": days[-1], "development_dates": dev_days, "final_test_dates": final_days, "folds": folds,
              "final_fit_cutoff": parse_local_hour_string(datetime.fromisoformat(final_days[0]).date(), "00:00", args.timezone).isoformat(),
              "policy": "predeclared chronological expanding folds; final test used once for reporting; all final-test dates permanently excluded from learning",
              "status": "reserved"}
    return cohort, None


def _v61_source_multipliers(cases, variable):
    errors = {}
    for case in cases:
        if variable == "rain_mm" and not _v61_matching_rain_interval(case["obs"], case["valid"]):
            continue
        observed = safe_float(case["obs"].get(variable))
        if observed is None:
            continue
        for p in case["points"]:
            value = safe_float(p.get(variable))
            if value is not None:
                errors.setdefault(p["source_id"], []).append(abs(value - observed))
    means = {s: sum(v) / len(v) for s, v in errors.items() if len(v) >= 20}
    if len(means) < 2:
        return None
    overall = sum(means.values()) / len(means)
    mult = {s: 1 + len(errors[s]) / (len(errors[s]) + 100) * (clamp((overall + .1) / (mae + .1), .5, 2) - 1) for s, mae in means.items()}
    return {"accepted": True, "multipliers": mult, "training_source_samples": {s: len(v) for s, v in errors.items()}, "method": "shrunk_inverse_training_mae"}


def _v61_error_summary(pairs):
    if not pairs:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    errs = [p - o for p, o in pairs]
    return {"n": len(errs), "mae": sum(abs(e) for e in errs) / len(errs), "rmse": math.sqrt(sum(e * e for e in errs) / len(errs)), "bias": sum(errs) / len(errs)}


def _v61_variable_pairs(cases, variable, section=None, bias=0, local=None, args=None):
    out = []
    for case in cases:
        if variable == "rain_mm" and not _v61_matching_rain_interval(case["obs"], case["valid"]):
            continue
        observed = safe_float(case["obs"].get(variable))
        prediction = _quality_predict_variable(case, variable, (section or {}).get("multipliers"))
        if observed is None or prediction is None:
            continue
        prediction += bias
        if local and args:
            features, _ = _v61_local_features(case, args, local.get("station_reference"), variable)
            if features is not None and _v61_local_in_range(features, local):
                prediction += _v61_local_delta(features, local, variable)
        out.append((prediction, observed))
    return out


def _v61_accept_folds(folds, minimum_cases=12):
    eligible = [f for f in folds if f["baseline"]["n"] >= minimum_cases and f["candidate"]["n"] == f["baseline"]["n"] and f["baseline"]["mae"] is not None]
    if len(eligible) < 2:
        return False, {"status": "insufficient_independent_folds", "folds": folds}
    total = sum(f["baseline"]["n"] for f in eligible)
    baseline = sum(f["baseline"]["mae"] * f["baseline"]["n"] for f in eligible) / total
    candidate = sum(f["candidate"]["mae"] * f["candidate"]["n"] for f in eligible) / total
    wins = sum(f["candidate"]["mae"] < f["baseline"]["mae"] - 1e-9 for f in eligible)
    accepted = wins >= math.ceil(2 * len(eligible) / 3) and candidate < baseline - max(1e-6, baseline * .005)
    return accepted, {"status": "accepted" if accepted else "no_consistent_rolling_improvement", "n": total, "baseline_mae": baseline,
                      "candidate_mae": candidate, "improved_folds": wins, "eligible_folds": len(eligible), "folds": folds}


def _v61_fit_bias(cases, variable, section=None):
    residuals = [o - p for p, o in _v61_variable_pairs(cases, variable, section)]
    if len(residuals) < 30:
        return None
    limit = 3 if variable == "temp_c" else 10
    return clamp(sum(residuals) / (len(residuals) + 100), -limit, limit)


def _v61_station_reference(cases, variable="temp_c"):
    keys = {"station_latitude": "latitude", "station_longitude": "longitude", "station_elevation_m": "elevation_m"}
    ref = {}
    for field, fb in keys.items():
        values = sorted(v for c in cases for v in [_v61_station_value(c["obs"], variable, fb)] if v is not None)
        if not values:
            return None
        ref[field] = values[len(values) // 2]
    return ref


_TERRAIN_LEVELS = ("urban", "valley", "coastal", "mountain", "rural", "forest", "highland", "lowland", "urban_highland", "valley_highland")


def _v61_local_features(case, args, station_reference=None, variable="temp_c"):
    row, obs = case.get("row", {}), station_reference or case.get("obs", {})
    site_lat = safe_float(row.get("site_latitude", getattr(args, "latitude", None)))
    site_lon = safe_float(row.get("site_longitude", getattr(args, "longitude", None)))
    site_elev = safe_float(row.get("site_elevation_m", getattr(args, "site_elevation_m", None)))
    st_lat, st_lon, st_elev = (_v61_station_value(obs, variable, s) for s in ("latitude", "longitude", "elevation_m"))
    model_elevs = [v for v in (safe_float(p.get("model_elevation_m")) for p in case.get("points", [])) if v is not None]
    if any(v is None for v in (site_lat, site_lon, site_elev, st_lat, st_lon, st_elev)) or not model_elevs:
        return None, "missing_site_station_or_model_elevation_metadata"
    distance = _v61_distance_km(site_lat, site_lon, st_lat, st_lon)
    if distance is None or distance > 5:
        return None, "station_outside_local_correction_radius"
    model_elev = sum(model_elevs) / len(model_elevs)
    terrain = str(row.get("terrain_class") or getattr(args, "terrain_class", "unknown") or "unknown").lower()
    features = [1.0, (site_elev - model_elev) / 1000, (site_elev - st_elev) / 1000, distance] + [1.0 if terrain == n else 0.0 for n in _TERRAIN_LEVELS]
    return features, {"station_distance_km": distance, "terrain_class": terrain, "model_elevation_basis": "api_downscaled_elevation_or_source_declared; empirical_feature_only"}


def _v61_ridge_solve(features, targets, penalty=20):
    width = len(features[0])
    m = [[sum(r[i] * r[j] for r in features) + (penalty if i == j else 0) for j in range(width)] + [sum(r[i] * t for r, t in zip(features, targets))] for i in range(width)]
    for i in range(width):
        pivot = max(range(i, width), key=lambda k: abs(m[k][i]))
        m[i], m[pivot] = m[pivot], m[i]
        d = m[i][i]
        if abs(d) < 1e-10:
            return None
        m[i] = [v / d for v in m[i]]
        for o in range(width):
            if o != i:
                f = m[o][i]
                m[o] = [a - f * b for a, b in zip(m[o], m[i])]
    return [r[-1] for r in m]


def _v61_fit_local(cases, variable, args, section=None, bias=0):
    reference = _v61_station_reference(cases, variable)
    if reference is None:
        return None, "missing_station_metadata"
    features, targets = [], []
    for case in cases:
        row, _ = _v61_local_features(case, args, variable=variable)
        observed = safe_float(case["obs"].get(variable))
        prediction = _quality_predict_variable(case, variable, (section or {}).get("multipliers"))
        if row is not None and observed is not None and prediction is not None:
            features.append(row)
            targets.append(observed - prediction - bias)
    if len(features) < 60:
        return None, "insufficient_local_metadata_samples"
    coef = _v61_ridge_solve(features, targets)
    if coef is None:
        return None, "singular_regularized_fit"
    return {"accepted": True, "coefficients": coef, "station_reference": reference, "training_samples": len(features),
            "feature_names": ["intercept", "site_minus_api_elevation_km", "site_minus_station_elevation_km", "station_distance_km"] + list(_TERRAIN_LEVELS),
            "feature_min": [min(r[i] for r in features) for i in range(len(coef))], "feature_max": [max(r[i] for r in features) for i in range(len(coef))],
            "penalty": 20, "method": "regularized_empirical_residual_fit; no_named_location_offset"}, None


def _v61_local_in_range(features, section):
    lo, hi = section.get("feature_min", []), section.get("feature_max", [])
    if len(features) != len(lo) or len(features) != len(hi):
        return False
    return all(l - (.15 if i < 4 else 0) <= v <= h + (.15 if i < 4 else 0) for i, (v, l, h) in enumerate(zip(features, lo, hi)))


def _v61_local_delta(features, section, variable):
    delta = sum(c * f for c, f in zip(section.get("coefficients", []), features))
    return clamp(delta, -3 if variable == "temp_c" else -10, 3 if variable == "temp_c" else 10)


def v61_local_adjustment(value, variable, valid_time, args, points=None):
    """Kembalikan (nilai, provenans); koreksi hanya diterapkan jika kandidat lokal diterima dan metadata dalam rentang pelatihan."""
    meta = {"status": "unavailable", "reason": "no_validated_local_correction", "variable": variable}
    if value is None or variable not in {"temp_c", "rh_pct"}:
        return value, meta
    section = _quality_skill_section("local_" + variable, valid_time, _quality_issued(args))
    if not section:
        return value, meta
    case = {"row": {}, "obs": {}, "points": [asdict(p) if hasattr(p, "__dataclass_fields__") else dict(p) for p in (points or [])]}
    features, details = _v61_local_features(case, args, section.get("station_reference"), variable)
    if features is None:
        meta["reason"] = details
        return value, meta
    if not _v61_local_in_range(features, section):
        meta["reason"] = "metadata_outside_validated_training_range"
        return value, meta
    delta = _v61_local_delta(features, section, variable)
    adjusted = clamp(value + delta, 0, 100) if variable == "rh_pct" else value + delta
    return adjusted, {"status": "applied", "variable": variable, "delta": delta, "cohort_id": section.get("cohort_id"), "details": details}


def _v61_selected_scope(variables, variable, case, cohort_id=None):
    sections = variables.get(variable, {})
    ctx = _quality_context(case["valid"], case["issued"])
    eligible = [sections[k] for k in ("lead:" + ctx["lead"], "hour:" + ctx["hour"], "month:" + ctx["month"], "all") if sections.get(k) and sections[k].get("accepted")]
    return next((s for s in eligible if cohort_id and s.get("cohort_id") == cohort_id), eligible[0] if eligible else {})


def _v61_configuration_metrics(cases, config, args):
    variables = config.get("variables", {})
    metrics = {}
    for variable in _V61_NUMERIC_VARIABLES:
        pairs = []
        for case in cases:
            section = _v61_selected_scope(variables, variable, case, config.get("cohort_id"))
            bias_name = "temp_bias" if variable == "temp_c" else "rh_bias" if variable == "rh_pct" else ""
            bias = safe_float(_v61_selected_scope(variables, bias_name, case, config.get("cohort_id")).get("bias")) or 0
            local = _v61_selected_scope(variables, "local_" + variable, case, config.get("cohort_id"))
            pairs.extend(_v61_variable_pairs([case], variable, section, bias, local, args))
        metrics[variable] = _v61_error_summary(pairs)
    brier, events = [], []
    for case in cases:
        p = _lg_prob(case["row"].get("prob_rain_raw", case["row"].get("prob_rain")))
        event = sentinel_observed_rain_event(case["obs"]) if _v61_matching_rain_interval(case["obs"], case["valid"]) else None
        if p is None or event is None:
            continue
        p = _quality_map_probability(p, _v61_selected_scope(variables, "prob_rain", case, config.get("cohort_id")).get("mapping", []))
        brier.append((p / 100 - int(event)) ** 2)
        events.append(int(event))
    metrics["rain_probability"] = {"n": len(brier), "brier": sum(brier) / len(brier) if brier else None, "wet_cases": sum(events), "dry_cases": len(events) - sum(events)}
    metrics["rain_two_stage"] = v61_score_rain_two_stage(cases, config.get("rain_two_stage"))
    return metrics


def _v61_per_lead_metrics(cases, config, args):
    leads = {}
    for case in cases:
        leads.setdefault(aether_lead_bucket((case["valid"] - case["issued"]).total_seconds() / 3600), []).append(case)
    return {l: {"samples": len(s), "days": len({c["date"] for c in s}), "metrics": _v61_configuration_metrics(s, config, args)} for l, s in leads.items()}


def _v61_fit_rolling_scope(development, scope, cohort, previous, args):
    if len(development) < max(60, int(getattr(args, "learning_min_samples", 60))):
        return {}, {"status": "insufficient_scope_samples", "samples": len(development)}
    fold_data = []
    for fold in cohort["folds"]:
        train = _v61_asof_cases([c for c in development if c["date"] in fold["train_dates"]], _quality_datetime(fold["fit_cutoff"], args.timezone))
        fold_data.append((fold, train, [c for c in development if c["date"] in fold["validation_dates"]]))
    final_training = _v61_asof_cases(development, _quality_datetime(cohort["final_fit_cutoff"], args.timezone))
    sections, evidence = {}, {"status": "evaluated", "samples": len(development), "asof_fit_samples": len(final_training), "variables": {}}
    prev_fit = _quality_datetime(previous.get("fitted_at"), args.timezone)
    cohort_start = parse_local_hour_string(datetime.fromisoformat(cohort["development_dates"][0]).date(), "00:00", args.timezone)
    incumbent_ok = prev_fit is not None and prev_fit < cohort_start
    for variable in _V61_NUMERIC_VARIABLES:
        prior = (previous.get("variables") or {}).get(variable) or {}
        incumbent = prior.get(scope) or prior.get("all") or {}
        if incumbent and not incumbent_ok:
            evidence["variables"][variable] = {"status": "incumbent_temporally_ineligible; preserved_until_fresh_cohort"}
            continue
        folds, candidates = [], []
        for fold, train, validation in fold_data:
            cand = _v61_source_multipliers(train, variable)
            candidates.append(cand)
            if cand:
                folds.append({"fold": fold["fold"], "train_samples": len(train), "baseline": _v61_error_summary(_v61_variable_pairs(validation, variable, incumbent)),
                              "candidate": _v61_error_summary(_v61_variable_pairs(validation, variable, cand))})
        accepted, detail = _v61_accept_folds(folds)
        evidence["variables"][variable] = detail
        selected = _v61_source_multipliers(final_training, variable) if accepted else incumbent
        if accepted and selected:
            sections[variable] = dict(selected, rolling_validation=detail, cohort_id=cohort["cohort_id"], selection_basis="rolling_validation_only")
        if variable not in {"temp_c", "rh_pct"}:
            continue
        bias_name = "temp_bias" if variable == "temp_c" else "rh_bias"
        prior_bias = (previous.get("variables") or {}).get(bias_name) or {}
        inc_bias_sec = prior_bias.get(scope) or prior_bias.get("all") or {}
        inc_bias = safe_float(inc_bias_sec.get("bias")) or 0
        if inc_bias_sec and not incumbent_ok:
            evidence["variables"][bias_name] = {"status": "incumbent_temporally_ineligible; preserved_until_fresh_cohort"}
            continue
        bias_folds, fold_biases = [], []
        for (fold, train, validation), cand in zip(fold_data, candidates):
            weights = cand if accepted else incumbent
            fb = _v61_fit_bias(train, variable, weights)
            fold_biases.append(fb)
            if fb is not None:
                bias_folds.append({"fold": fold["fold"], "baseline": _v61_error_summary(_v61_variable_pairs(validation, variable, weights, inc_bias)),
                                   "candidate": _v61_error_summary(_v61_variable_pairs(validation, variable, weights, fb))})
        bias_ok, bias_ev = _v61_accept_folds(bias_folds)
        evidence["variables"][bias_name] = bias_ev
        final_bias = _v61_fit_bias(final_training, variable, selected) if bias_ok else inc_bias
        if bias_ok and final_bias is not None:
            sections[bias_name] = {"accepted": True, "bias": final_bias, "rolling_validation": bias_ev, "cohort_id": cohort["cohort_id"], "selection_basis": "rolling_validation_only"}
        local_name = "local_" + variable
        prior_local = (previous.get("variables") or {}).get(local_name) or {}
        local_inc = prior_local.get(scope) or prior_local.get("all") or {}
        if local_inc and not incumbent_ok:
            evidence["variables"][local_name] = {"status": "incumbent_temporally_ineligible; preserved_until_fresh_cohort"}
            continue
        local_folds, reasons = [], []
        for (fold, train, validation), cand, fb in zip(fold_data, candidates, fold_biases):
            weights = cand if accepted else incumbent
            chosen = fb if bias_ok and fb is not None else inc_bias
            local, reason = _v61_fit_local(train, variable, args, weights, chosen)
            if local is None:
                reasons.append(reason)
                continue
            comparable = [c for c in validation if _v61_local_features(c, args, local.get("station_reference"), variable)[0] is not None]
            local_folds.append({"fold": fold["fold"], "baseline": _v61_error_summary(_v61_variable_pairs(comparable, variable, weights, chosen, local_inc, args)),
                                "candidate": _v61_error_summary(_v61_variable_pairs(comparable, variable, weights, chosen, local, args))})
        local_ok, local_ev = _v61_accept_folds(local_folds)
        local_ev["unavailable_reasons"] = sorted(set(reasons))
        evidence["variables"][local_name] = local_ev
        if local_ok:
            fitted, _ = _v61_fit_local(final_training, variable, args, selected, final_bias or 0)
            if fitted:
                sections[local_name] = dict(fitted, rolling_validation=local_ev, cohort_id=cohort["cohort_id"], selection_basis="rolling_validation_only")
    return sections, evidence


def _v61_fit_rolling_rain(development, cohort, previous, args):
    incumbent = previous.get("rain_two_stage")
    fitted_at = _quality_datetime(previous.get("fitted_at"), args.timezone)
    if incumbent and (not fitted_at or fitted_at >= parse_local_hour_string(datetime.fromisoformat(cohort["development_dates"][0]).date(), "00:00", args.timezone)):
        return None, {"status": "incumbent_temporally_ineligible; preserved_until_fresh_cohort"}
    folds = []
    for fold in cohort["folds"]:
        train = _v61_asof_cases([c for c in development if c["date"] in fold["train_dates"]], _quality_datetime(fold["fit_cutoff"], args.timezone))
        validation = [c for c in development if c["date"] in fold["validation_dates"]]
        cand = v61_fit_rain_two_stage(train, validation).get("candidate")
        if cand is None:
            folds.append({"fold": fold["fold"], "status": "insufficient_data"})
            continue
        folds.append({"fold": fold["fold"], "baseline": v61_score_rain_two_stage(validation, incumbent), "candidate": v61_score_rain_two_stage(validation, cand)})
    eligible = []
    for f in folds:
        b, a = f.get("baseline", {}), f.get("candidate", {})
        vals = (b.get("brier"), a.get("brier"), b.get("wet_mae"), a.get("wet_mae"))
        if all(v is not None for v in vals):
            eligible.append(vals)
    wins = sum(nb < ob - 1e-9 and nw < ow - 1e-9 for ob, nb, ow, nw in eligible)
    accepted = (len(eligible) >= 2 and wins >= math.ceil(2 * len(eligible) / 3) and sum(r[1] for r in eligible) < sum(r[0] for r in eligible) - 1e-9
                and sum(r[3] for r in eligible) < sum(r[2] for r in eligible) - 1e-9)
    evidence = {"status": "accepted" if accepted else "insufficient_data_or_no_joint_rolling_improvement", "folds": folds, "eligible_folds": len(eligible), "joint_improved_folds": wins}
    if not accepted:
        return None, evidence
    final_train = _v61_asof_cases(development, _quality_datetime(cohort["final_fit_cutoff"], args.timezone))
    cand = v61_fit_rain_two_stage(final_train, []).get("candidate")
    if cand:
        cand = dict(cand, accepted=True, cohort_id=cohort["cohort_id"], rolling_validation=evidence, selection_basis="rolling_validation_only")
    return cand, evidence


def _v61_write_prospective(cases, previous, args, cutoff):
    fitted_at = _quality_datetime(previous.get("fitted_at"), args.timezone)
    prospective = [c for c in cases if c["date"] > cutoff and fitted_at is not None and c["issued"] >= fitted_at]
    report = {"status": "prospective_observations_available" if prospective else "awaiting_new_post_fit_forecasts_and_observations", "fitted_at": previous.get("fitted_at"),
              "samples": len(prospective), "days": len({c["date"] for c in prospective}), "policy": "report only; no automatic weight changes based on this report",
              "metrics": _v61_configuration_metrics(prospective, previous, args), "by_lead": _v61_per_lead_metrics(prospective, previous, args)}
    write_json(os.path.join(report_dir(), "quality_prospective_evaluation.json"), report)
    return report


def evaluate_historical_performance(args):
    """Satu putaran pemilihan model per kohort beku; test akhir tidak pernah dipakai untuk seleksi."""
    cases = _quality_training_cases(args)
    if getattr(args, "start_date", None):
        cases = [c for c in cases if c["date"] >= args.start_date]
    if getattr(args, "end_date", None):
        cases = [c for c in cases if c["date"] <= args.end_date]
    config_path = path_output("quality_skill_config.json")
    previous = read_json(config_path, {}) or {}
    if previous.get("location_slug") != args.location_slug:
        previous = {}
    plan_path = path_output("quality_evaluation_plan.json")
    ledger = read_json(plan_path, {}) or {}
    if ledger.get("location_slug") != args.location_slug:
        ledger = {"schema_version": "LANGIT.v61.frozen-evaluation", "location_slug": args.location_slug, "cohorts": []}
    cohort, shortage = _v61_frozen_cohort(cases, args, ledger)
    cutoff = max((e["data_cutoff_date"] for e in ledger.get("cohorts", [])), default="")
    prospective = _v61_write_prospective(cases, previous, args, cutoff)
    summary_path = os.path.join(report_dir(), "quality_learning_evaluation.json")
    if cohort is None:
        summary = {"status": (shortage or {}).get("status", "insufficient_data"), "samples": len(cases), "accepted_sections": 0, "retained_previous_configuration": bool(previous),
                   "requirements": shortage, "prospective": prospective,
                   "local_correction": "unavailable_until_station_site_and_model_elevation_metadata_and_rolling_validation_are_present"}
        write_json(summary_path, summary)
        return {"source_score_rows": [], "detail_rows": cases, "summary_path": summary_path, "weights_path": config_path, "learning_status": summary["status"]}
    snapshot_path = path_output("quality_evaluation_cohort_" + cohort["cohort_id"] + ".json")
    if not any(e["cohort_id"] == cohort["cohort_id"] for e in ledger["cohorts"]):
        ledger["cohorts"].append(cohort)
        snap_cases = [c for c in cases if c["date"] in cohort["development_dates"] or c["date"] in cohort["final_test_dates"]]
        cohort["case_snapshot_digest"] = hashlib.sha256(json.dumps(_quality_jsonable(snap_cases), sort_keys=True).encode()).hexdigest()
        write_json(snapshot_path, {"cohort_id": cohort["cohort_id"], "case_snapshot_digest": cohort["case_snapshot_digest"], "incumbent_configuration": previous, "cases": snap_cases})
        write_json(plan_path, ledger)
    else:
        snapshot = read_json(snapshot_path, {}) or {}
        restored = []
        for raw in snapshot.get("cases", []):
            case = dict(raw)
            case["valid"], case["issued"] = _quality_datetime(case.get("valid"), args.timezone), _quality_datetime(case.get("issued"), args.timezone)
            if case["valid"] is not None and case["issued"] is not None:
                restored.append(case)
        if not restored:
            raise ValueError("Reserved evaluation cohort has no immutable case snapshot; restore the snapshot before evaluation.")
        digest = hashlib.sha256(json.dumps(_quality_jsonable(snapshot.get("cases", [])), sort_keys=True).encode()).hexdigest()
        if snapshot.get("case_snapshot_digest") and digest != snapshot["case_snapshot_digest"]:
            raise ValueError("Reserved evaluation snapshot digest changed; restore the original frozen snapshot.")
        previous, cases = snapshot.get("incumbent_configuration") or {}, restored
    development = [c for c in cases if c["date"] in cohort["development_dates"]]
    final_test = [c for c in cases if c["date"] in cohort["final_test_dates"]]
    variables = json.loads(json.dumps(previous.get("variables") or {}))
    evidence, accepted = {}, 0
    for scope, subset in _v61_scope_cases(development).items():
        sections, details = _v61_fit_rolling_scope(subset, scope, cohort, previous, args)
        evidence[scope] = details
        for variable, section in sections.items():
            variables.setdefault(variable, {})[scope] = section
            accepted += 1
    config = dict(previous)
    config.update({"schema_version": "LANGIT.v61.rolling-validated", "location_slug": args.location_slug, "fitted_at": cohort["created_at"], "variables": variables,
                   "training_policy": cohort["policy"], "cohort_id": cohort["cohort_id"], "development_cutoff": cohort["development_dates"][-1],
                   "reserved_test_dates": cohort["final_test_dates"], "model_families": _SOURCE_FAMILIES, "legacy_sections_preserved": bool(previous)})
    rain_candidate, rain_evidence = _v61_fit_rolling_rain(development, cohort, previous, args)
    if rain_candidate:
        config["rain_two_stage"] = rain_candidate
        config["variables"].setdefault("rain_two_stage", {})["all"] = rain_candidate
        accepted += 1
    digest = hashlib.sha256(json.dumps(_quality_jsonable(config), sort_keys=True).encode()).hexdigest()
    candidate_path = path_output("quality_evaluation_candidate_" + cohort["cohort_id"] + ".json")
    artifact = read_json(candidate_path, {}) or {}
    if artifact:
        if artifact.get("configuration_digest") != digest:
            raise ValueError("Frozen evaluation candidate differs from the pre-test selection; restore the reserved cohort inputs.")
    else:
        write_json(candidate_path, {"configuration_digest": digest, "cohort_id": cohort["cohort_id"], "configuration": config, "selection_excluded_test": True})
    test_report = {"status": "retrospective_untouched_final_test", "cohort_id": cohort["cohort_id"], "configuration_digest": digest,
                   "test_dates": cohort["final_test_dates"], "samples": len(final_test), "days": len({c["date"] for c in final_test}), "selection_excluded_test": True,
                   "baseline_metrics": _v61_configuration_metrics(final_test, previous, args), "candidate_metrics": _v61_configuration_metrics(final_test, config, args),
                   "by_lead": _v61_per_lead_metrics(final_test, config, args),
                   "limitations": "final-test results are reported without retuning; prospective accuracy still requires forecasts issued after fit"}
    test_path = os.path.join(report_dir(), "quality_final_test_" + cohort["cohort_id"] + ".json")
    existing = read_json(test_path, {}) or {}
    if existing:
        if existing.get("configuration_digest") != digest:
            raise ValueError("Completed final test is immutable and its selection digest differs; restore the reserved cohort inputs.")
        test_report = existing
    else:
        write_json(test_path, test_report)
    freeze = bool(getattr(args, "freeze_weights", False))
    deployed = bool(accepted and not freeze)
    if deployed:
        write_json(config_path, config)
    cohort.update({"status": "completed", "completed_at": now_local(args.timezone).isoformat(), "accepted_sections": accepted, "deployed": deployed,
                   "candidate_path": candidate_path, "final_test_path": test_path, "configuration_digest": digest})
    write_json(plan_path, ledger)
    summary = {"status": "rolling_improvements_deployed" if deployed else "frozen_candidate_reported" if accepted and freeze else "no_consistent_rolling_improvement; previous_configuration_retained",
               "samples": len(cases), "accepted_sections": accepted, "cohort": cohort, "scopes": evidence, "rain_two_stage": rain_evidence, "candidate_path": candidate_path,
               "final_test_path": test_path, "final_test": test_report, "prospective": prospective, "test_not_used_for_selection": True}
    write_json(summary_path, summary)
    return {"source_score_rows": [{"variable": v, "accepted_scopes": len(s)} for v, s in variables.items()], "detail_rows": cases,
            "summary_path": summary_path, "weights_path": config_path, "learning_status": summary["status"]}


# ---------------------------------------------------------------------------
# Baris sumber, status, ensemble tertimbang, keluaran CSV
# ---------------------------------------------------------------------------
def flatten_points(results):
    return [r.points[j] for r in results for j in TARGET_TIMES if r.points.get(j) is not None]


def build_source_rows(points, target_date):
    display = target_date.strftime("%d-%m-%Y")
    return [[display, p.source_id, p.provider, p.target_time, p.source_datetime.strftime("%Y-%m-%d %H:%M:%S"), round_or_blank(p.temp_c),
             round_or_blank(p.rh_pct), round_or_blank(p.rain_mm), round_or_blank(p.wind_kmh), round_or_blank(p.gap_minutes),
             round_or_blank(point_weight(p), 4), p.category, p.raw_condition] for p in points]


def build_status_rows(results, target_date):
    display = target_date.strftime("%d-%m-%Y")
    rows = []
    for r in results:
        hf = source_health_factor(r.source_id)
        rows.append({"tanggal": display, "source_id": r.source_id, "provider": r.provider, "success": "yes" if r.success else "no",
                     "base_weight": r.base_weight, "health_factor": hf, "effective_base_weight": round(r.base_weight * hf, 4),
                     "points_collected": len(r.points), "target_points": len(TARGET_TIMES),
                     "http_status": r.http_status if r.http_status is not None else "", "duration_ms": r.duration_ms if r.duration_ms is not None else "",
                     "error_content_type": r.error_content_type or "", "error_snippet": r.error_snippet or "",
                     "payload_saved_path": r.payload_saved_path, "error": r.error, "availability_reason": source_reason(r),
                     "live_fetch_success": r.live_fetch_success, "is_cached": r.is_cached, "is_backup": r.is_backup,
                     "model_identifier": r.model_identifier or r.source_id, "original_fetched_at": _quality_jsonable(r.original_fetched_at),
                     "cache_age_hours": r.cache_age_hours})
    return rows


def build_bmkg_rows(results, target_date):
    bmkg = next((r for r in results if r.source_id == "BMKG"), None)
    if not bmkg:
        return []
    display = target_date.strftime("%d-%m-%Y")
    return [[display, j, round_or_blank(p.temp_c), p.raw_condition, round_or_blank(p.rh_pct), round_or_blank(p.wind_kmh)]
            for j in TARGET_TIMES if (p := bmkg.points.get(j)) is not None]


def build_ensemble_rows(points):
    _quality_set_families(points)
    grouped = {j: [] for j in TARGET_TIMES}
    for p in points:
        grouped.setdefault(p.target_time, []).append(p)
    rows = []
    for jam in TARGET_TIMES:
        bucket = grouped.get(jam) or []
        cat_w, temp_v, rh_v, rain_v, hi_v, ids, total = {}, [], [], [], [], [], 0.0
        for p in bucket:
            w = point_weight(p)
            ids.append(p.source_id)
            cat_w[p.category] = cat_w.get(p.category, 0.0) + w
            total += w
            if p.temp_c is not None:
                temp_v.append((p.temp_c, point_variable_weight(p, "temp_c")))
            if p.rh_pct is not None:
                rh_v.append((p.rh_pct, point_variable_weight(p, "rh_pct")))
            if p.rain_mm is not None:
                rain_v.append((p.rain_mm, point_variable_weight(p, "rain_mm")))
            hi = heat_index(p.temp_c, p.rh_pct)
            if hi is not None:
                hi_v.append((hi, w))
        temp_v, rh_v, hi_v = filter_weighted_pairs(temp_v), filter_weighted_pairs(rh_v), filter_weighted_pairs(hi_v)
        probs = {c: round(cat_w.get(c, 0.0) / total * 100, 1) if total else 0.0 for c in CUACA_ORDER}
        dominant = max(cat_w, key=cat_w.get) if cat_w else ""
        temp_m, temp_s = weighted_mean_std(temp_v)
        rh_m, rh_s = weighted_mean_std(rh_v)
        rain_m, rain_s = weighted_mean_std(rain_v)
        hi_m, hi_s = weighted_mean_std(hi_v)
        score, band = compute_confidence(bucket, total, cat_w.get(dominant, 0.0) if dominant else 0.0, temp_s, rh_s, rain_s)
        expected = max(len(ACTIVE_SOURCE_CONFIGS), 1)
        gaps = [p.gap_minutes for p in bucket if p.gap_minutes is not None]
        rows.append([jam, len(bucket), round_or_blank(total, 4), "cukup" if len(bucket) >= MIN_SOURCE_SUCCESS_FOR_RUN else "terbatas",
                     ",".join(sorted(set(ids))), dominant, score, band, round_or_blank(temp_m), f"+/-{temp_s}" if temp_s is not None else "",
                     round_or_blank(rh_m), f"+/-{rh_s}" if rh_s is not None else "", round_or_blank(rain_m), f"+/-{rain_s}" if rain_s is not None else "",
                     round_or_blank(hi_m), f"+/-{hi_s}" if hi_s is not None else "", probs["Cerah"], probs["Cerah Berawan"], probs["Berawan"],
                     probs["Hujan Ringan"], probs["Hujan Sedang"], probs["Hujan Lebat"], round_or_blank(temp_s), round_or_blank(rh_s),
                     round_or_blank(rain_s), round_or_blank(hi_s), expected, round_or_blank(round(len(bucket) / expected, 4), 4),
                     round_or_blank(round(sum(gaps) / len(gaps), 2) if gaps else None), round_or_blank(round(max(gaps), 2) if gaps else None)])
    return rows


FORECAST_HEADERS = ["tanggal", "source_id", "provider", "target_jam", "source_datetime", "suhu_C", "RH_%", "rain_mm", "wind_kmh", "gap_minutes", "point_weight", "kategori", "raw_condition"]
STATUS_HEADERS = ["tanggal", "source_id", "provider", "success", "base_weight", "health_factor", "effective_base_weight", "points_collected", "target_points",
                  "http_status", "duration_ms", "error_content_type", "error_snippet", "payload_saved_path", "error", "availability_reason", "live_fetch_success",
                  "is_cached", "is_backup", "model_identifier", "original_fetched_at", "cache_age_hours"]
ENSEMBLE_HEADERS = ["jam", "sources_used", "weight_total", "coverage_status", "source_list", "dominant_category", "confidence_score", "confidence_label",
                    "temp_mean", "temp_error", "rh_mean", "rh_error", "rain_mean", "rain_error", "heat_index_mean", "heat_index_error", "%cerah", "%cerah_berawan",
                    "%berawan", "%hujan_ringan", "%hujan_sedang", "%hujan_lebat", "temp_std", "rh_std", "rain_std", "heat_index_std", "sources_expected",
                    "coverage_fraction", "gap_mean_minutes", "gap_max_minutes"]


def build_canva_row(ensemble_rows, target_date, args):
    row = {"tanggal_target": target_date.strftime("%d-%m-%Y"), "lokasi": args.location_name}
    names = ["jam", "jumlah_sumber", "bobot_total", "coverage", "sumber", "dominant", "confidence_score", "confidence_label", "temp"]
    for idx, d in enumerate(ensemble_rows, start=1):
        for k, name in enumerate(names):
            row[f"{name}{idx}"] = d[k]
        row[f"rh{idx}"], row[f"rain{idx}"], row[f"hi{idx}"] = d[10], d[12], f"{d[14]} {d[15]}".strip()
        for name, k in (("cerah", 16), ("cerah_berawan", 17), ("berawan", 18), ("hujan_ringan", 19), ("hujan_sedang", 20), ("hujan_lebat", 21)):
            row[f"{name}{idx}"] = d[k]
    return row


def save_outputs(target_date, results, args):
    stamp = target_date.strftime("%Y%m%d")
    points = flatten_points(results)
    source_rows = build_source_rows(points, target_date)
    status_rows = build_status_rows(results, target_date)
    bmkg_rows = build_bmkg_rows(results, target_date)
    ensemble_rows = build_ensemble_rows(points)
    canva_row = build_canva_row(ensemble_rows, target_date, args)
    sentinel_payload = sentinel_x_save_artifacts(target_date, results, args, source_rows, status_rows, ensemble_rows)
    for suffix in ("", f"_{stamp}"):
        write_csv(path_output(f"forecast{suffix}.csv"), FORECAST_HEADERS, source_rows)
        write_dict_csv(path_output(f"source_status{suffix}.csv"), STATUS_HEADERS, status_rows)
        write_csv(path_output(f"ensemble{suffix}.csv"), ENSEMBLE_HEADERS, ensemble_rows)
        if bmkg_rows:
            write_csv(path_output(f"bmkg{suffix}.csv"), ["tanggal", "jam", "suhu_C", "cuaca", "RH_%", "wind_kmh"], bmkg_rows)
        write_dict_csv(path_output(f"canva{suffix}.csv"), list(canva_row.keys()), [canva_row])
    low = [r[0] for r in ensemble_rows if r[3] != "cukup"]
    minimum = getattr(args, "min_success_sources", None) or len(ACTIVE_SOURCE_CONFIGS)
    ok_count = sum(r.success for r in results)
    status = "error" if not ok_count else "warning" if low or any(r.is_cached for r in results) or ok_count < minimum else "ok"
    summary = {"schema_version": OUTPUT_SCHEMA_VERSION, "generated_at": now_local(args.timezone).isoformat(), "location_slug": args.location_slug,
               "location_name": args.location_name, "bmkg_point_name": args.bmkg_point_name, "area_level": args.area_level,
               "is_proxy_bmkg": args.is_proxy_bmkg, "location_note": getattr(args, "location_note", ""), "adm4": args.adm4,
               "latitude": args.latitude, "longitude": args.longitude, "timezone": args.timezone, "target_date": target_date.isoformat(),
               "sources_total": len(results), "sources_success": ok_count, "points_total": len(points),
               "sources_active": [i["source_id"] for i in ACTIVE_SOURCE_CONFIGS], "target_hours": list(TARGET_TIMES),
               "weights_file": path_output(WEIGHTS_FILENAME), "health_file": path_output(HEALTH_FILENAME), "output_dir": ACTIVE_OUTPUT_DIR,
               "retention_days": args.retention_days, "low_coverage_slots": low, "run_status": status, "sentinel_x": sentinel_payload}
    write_json(path_output("run_summary.json"), summary)
    write_json(path_output(f"run_summary_{stamp}.json"), summary)
    return summary


# ---------------------------------------------------------------------------
# Intelijen risiko Sentinel (heuristik; label jujur)
# ---------------------------------------------------------------------------
aether_value = safe_float


def aether_round(value, digits=2):
    try:
        return "" if value is None else round(float(value), digits)
    except Exception:
        return ""


def aether_weighted_mean(pairs):
    valid = [(float(v), float(w)) for v, w in pairs if v is not None and w is not None and w > 0]
    total = sum(w for _, w in valid)
    return round(sum(v * w for v, w in valid) / total, 3) if valid and total > 0 else None


def aether_get_weighted_attr(bucket, attr):
    return [(safe_float(getattr(p, attr, None)), point_variable_weight(p, attr)) for p in bucket if safe_float(getattr(p, attr, None)) is not None]


def aether_microclimate_profile(args):
    explicit = getattr(args, "microclimate", "auto") or "auto"
    return explicit if explicit != "auto" else "unspecified"


def aether_microclimate_adjustment(profile, hour, temp_c, rh_pct):
    temp_bias = rh_bias = 0
    if _QUALITY_TARGET_DT is not None:
        temp_bias = safe_float(_quality_skill_section("temp_bias", _QUALITY_TARGET_DT, _RUN_ISSUED_AT).get("bias")) or 0
        rh_bias = safe_float(_quality_skill_section("rh_bias", _QUALITY_TARGET_DT, _RUN_ISSUED_AT).get("bias")) or 0
    return (temp_c + temp_bias if temp_c is not None else None, clamp(rh_pct + rh_bias, 0, 100) if rh_pct is not None else None, 0, temp_bias, rh_bias)


def aether_lead_hours(target_date, jam, args):
    valid = parse_local_hour_string(target_date, jam, args.timezone)
    return round(max(0, (valid - _quality_issued(args)).total_seconds() / 3600), 2)


def aether_risk_label(score):
    return "unknown" if score is None else "very_high" if score >= 80 else "high" if score >= 60 else "medium" if score >= 35 else "low"


def aether_trust_level(sources_used, confidence_score, uncertainty_score, coverage_fraction, source_health_mean):
    if sources_used < 3 or coverage_fraction < 0.25:
        return "DO_NOT_TRUST"
    confidence_score = 0 if confidence_score is None else confidence_score
    uncertainty_score = 80 if uncertainty_score is None else uncertainty_score
    source_health_mean = 0.7 if source_health_mean is None else source_health_mean
    score = 0.45 * confidence_score + 0.25 * (100 - uncertainty_score) + 0.20 * coverage_fraction * 100 + 0.10 * source_health_mean * 100
    return "HIGHLY_TRUSTED" if score >= 82 else "TRUSTED" if score >= 67 else "USABLE" if score >= 45 else "EXPERIMENTAL"


def aether_operational_status(trust, rain_risk, uncertainty, sources_used):
    if trust == "DO_NOT_TRUST" or sources_used < 3:
        return "BLACK"
    if (rain_risk is not None and rain_risk >= 80) or (uncertainty is not None and uncertainty >= 82):
        return "RED"
    if trust in {"EXPERIMENTAL", "USABLE"} or (rain_risk is not None and rain_risk >= 55) or (uncertainty is not None and uncertainty >= 60):
        return "YELLOW"
    return "GREEN"


def aether_source_state_rows(results):
    rows = []
    for r in results:
        h = SOURCE_HEALTH.get(r.source_id) or {}
        ema = aether_value(h.get("ema_success"))
        cf = int(h.get("consecutive_failures", 0) or 0)
        if cf >= 5:
            state = "QUARANTINED"
        elif cf >= 2 or (ema is not None and ema < 0.55):
            state = "DEGRADED"
        elif cf == 1:
            state = "RECOVERING"
        else:
            state = "ACTIVE" if r.success else "DEGRADED"
        rows.append({"source_id": r.source_id, "provider": r.provider, "state": state, "success": r.success, "points": len(r.points),
                     "ema_success": aether_round(ema, 4), "ema_completeness": h.get("ema_completeness", ""), "consecutive_failures": cf,
                     "http_status": r.http_status if r.http_status is not None else "", "duration_ms": r.duration_ms if r.duration_ms is not None else "", "error": r.error})
    return rows


def sentinel_location_personality(args):
    return {"personality": "unspecified_local_conditions", "convective_sensitivity": "unknown", "rain_displacement_risk": "unknown",
            "morning_humidity_memory": "unknown", "forecast_difficulty": "unknown",
            "notes": "Karakter lokal belum diverifikasi dengan pengamatan; skor risiko memakai aturan heuristik umum."}


def sentinel_predictability(stress, uncertainty, displacement):
    s = max(aether_value(stress) or 0, aether_value(uncertainty) or 0, aether_value(displacement) or 0)
    return "VERY_LOW" if s >= 80 else "LOW" if s >= 65 else "MEDIUM" if s >= 45 else "HIGH"


def sentinel_atmospheric_mode(hour, prob_rain, prob_heavy, rh_p50, cloud_p50, uncertainty, cape_p50):
    pr, ph, rh = aether_value(prob_rain) or 0, aether_value(prob_heavy) or 0, aether_value(rh_p50) or 0
    cloud = aether_value(cloud_p50) if aether_value(cloud_p50) is not None else 65
    unc, cape = aether_value(uncertainty) or 0, aether_value(cape_p50) or 0
    if unc >= 78:
        return "HIGH_UNCERTAINTY_ATMOSPHERE"
    if ph >= 28 or (pr >= 70 and cape >= 700):
        return "HEAVY_RAIN_WATCH"
    if 12 <= hour <= 19 and rh >= 78 and (pr >= 45 or cloud >= 72):
        return "HUMID_CONVECTIVE_AFTERNOON"
    if 19 <= hour <= 23 and pr >= 45:
        return "EVENING_RAIN_RESIDUAL"
    if 0 <= hour <= 8 and rh >= 86 and pr < 35:
        return "HUMID_STABLE_MORNING"
    if pr <= 25 and cloud <= 45 and unc <= 45:
        return "STABLE_LOW_RAIN"
    return "RAIN_TRANSITION" if pr >= 50 else "MIXED_LOCAL_ATMOSPHERE"


def sentinel_failure_mode(mode, prob_rain, uncertainty, displacement, rain_p90, category_disagreement):
    pr, unc, disp = aether_value(prob_rain) or 0, aether_value(uncertainty) or 0, aether_value(displacement) or 0
    p90, disag = aether_value(rain_p90) or 0, aether_value(category_disagreement) or 0
    if mode in {"HUMID_CONVECTIVE_AFTERNOON", "HEAVY_RAIN_WATCH"} and disp >= 60:
        return "rain_cell_displacement"
    if pr >= 45 and unc >= 55:
        return "rain_timing_error"
    if p90 >= 8 and pr < 55:
        return "underestimated_intensity_tail"
    if disag >= 60:
        return "category_disagreement_error"
    return "low_failure_risk_non_rain" if pr < 30 and p90 < 3 else "general_uncertainty"


def sentinel_multi_reality(prob_rain, prob_heavy, displacement, uncertainty, rain_p90):
    pr, ph = clamp(aether_value(prob_rain) or 0, 0, 100), clamp(aether_value(prob_heavy) or 0, 0, 100)
    disp, unc = clamp(aether_value(displacement) or 0, 0, 100), clamp(aether_value(uncertainty) or 0, 0, 100)
    p90 = aether_value(rain_p90) or 0
    vals = [clamp((100 - pr) * 0.55 + disp * 0.25 + unc * 0.10, 2, 70), clamp(pr * (disp / 100.0) * 0.55 + unc * 0.18, 3, 55),
            clamp(pr * (1 - ph / 120.0) * 0.45, 3, 60), clamp(pr * 0.18 + max(p90 - 4, 0) * 2.0, 1, 45),
            clamp(ph * 0.40 + max(p90 - 9, 0) * 2.8 + unc * 0.08, 0, 35)]
    total = sum(vals) or 1
    norm = [round(v / total * 100, 1) for v in vals]
    norm[2] = round(norm[2] + round(100 - sum(norm), 1), 1)
    return dict(zip(["dry_miss", "nearby_rain_only", "direct_light_rain", "direct_moderate_rain", "convective_burst"], norm))


def sentinel_threat_level(score):
    return "VERY_HIGH" if score >= 80 else "HIGH" if score >= 60 else "MEDIUM" if score >= 35 else "LOW"


def sentinel_activity_score(base, penalty):
    return int(round(clamp(base - penalty, 0, 10)))


def sentinel_decision(row, args):
    mission = getattr(args, "mission", "safety_first") or "safety_first"
    rain, heavy = aether_value(row.get("rain_threat_score")) or 0, aether_value(row.get("heavy_rain_threat_score")) or 0
    failure = aether_value(row.get("forecast_failure_risk")) or 0
    hour = int(str(row.get("jam", "00:00")).split(":")[0])
    threshold = aether_value(getattr(args, "decision_risk_threshold", 55.0)) or 55.0
    if mission in {"avoid_rain", "commute", "safety_first"}:
        threshold = min(threshold, 45)
    elif mission in {"outdoor_event", "sport", "photography"}:
        threshold = min(threshold, 50)
    elif mission == "laundry":
        threshold = 35
    if max(rain, heavy, failure * 0.65) >= threshold:
        return {"laundry": "Jangan andalkan jemur luar ruang pada jam ini; pilih window yang lebih kering atau indoor backup.",
                "fieldwork": "Fieldwork sebaiknya dilakukan sebelum risk peak; siapkan opsi berhenti/berlindung saat hujan lokal terbentuk.",
                "outdoor_event": "Event outdoor perlu plan B indoor/teduh; risiko utama adalah hujan lokal dan timing yang bisa bergeser.",
                "sport": "Olahraga outdoor kurang ideal jika mendekati risk peak; pantau update 1–3 jam sebelumnya.",
                "photography": "Fotografi outdoor masih mungkin, tetapi siapkan perubahan rute karena nearby rain dan cloud build-up.",
                "research": "Tandai jam ini sebagai kasus high-value untuk verifikasi; uncertainty dan failure risk layak dianalisis."}.get(
            mission, "Bawa payung/jas hujan dan gunakan interpretasi konservatif, terutama jika keluar setelah siang.")
    if 7 <= hour <= 11:
        return "Window relatif baik untuk aktivitas luar ruang, tetapi tetap cek update menjelang siang."
    return "Risiko relatif terkendali; keputusan normal masih masuk akal dengan pemantauan berkala."


def sentinel_expert_council(row, args):
    pr, ph = aether_value(row.get("prob_rain")) or 0, aether_value(row.get("prob_heavy_rain")) or 0
    unc, disp, p90 = aether_value(row.get("uncertainty_score")) or 0, aether_value(row.get("rain_displacement_risk")) or 0, aether_value(row.get("rain_p90")) or 0
    items = ["Local Signal Analyst: sinyal hujan cukup kuat untuk mengaktifkan rain-risk window." if pr >= 55 else
             "Local Signal Analyst: sinyal hujan belum dominan, tetapi tetap membaca tail-risk dari P90/P95.",
             "Uncertainty Auditor: disagreement/spread cukup tinggi; forecast tidak boleh dipakai sebagai kepastian tunggal." if unc >= 60 else
             "Uncertainty Auditor: agreement relatif masih dapat diterima untuk keputusan umum."]
    if disp >= 60:
        items.append("Rain Displacement Analyst: hujan sekitar lokasi lebih mungkin daripada direct-hit yang presisi.")
    if ph >= 25 or p90 >= 8:
        items.append("Risk Officer: worst-case hujan sedang/lebat perlu tetap ditampilkan walaupun skenario utama lebih ringan.")
    if row.get("atmospheric_mode") in {"HUMID_CONVECTIVE_AFTERNOON", "HEAVY_RAIN_WATCH"}:
        items.append("Tropical Rain Specialist: gunakan mode konservatif karena hujan konvektif lokal rawan salah timing/posisi.")
    items.append(f"Decision Judge: mission={getattr(args, 'mission', 'safety_first')} → {row.get('decision_recommendation', '')}")
    return " | ".join(items)


def sentinel_counterfactual(row):
    f = []
    if (aether_value(row.get("rh_p50")) or 0) >= 82:
        f.append("Jika RH turun ±10%, rain support kemungkinan turun signifikan.")
    if (aether_value(row.get("category_disagreement")) or 0) >= 50:
        f.append("Jika kategori antar-source lebih sepakat, trust level akan naik.")
    if (aether_value(row.get("rain_p90")) or 0) >= 8:
        f.append("Jika rain P90 turun di bawah 5 mm, worst-case scenario akan melemah.")
    if (aether_value(row.get("rain_displacement_risk")) or 0) >= 55:
        f.append("Jika grid/nearby signal mendukung direct-hit, dry-miss probability akan turun.")
    return " ".join(f[:3]) or "Faktor pengubah utama belum dominan; forecast relatif dikendalikan oleh consensus source."


def sentinel_forecast_contract_summary(row):
    return {"BLACK": "Jangan percaya output untuk keputusan penting; data/source coverage tidak layak.",
            "RED": "Gunakan sebagai warning/risk signal, bukan timing presisi.",
            "YELLOW": "Boleh dipakai untuk keputusan umum dengan plan B dan update berkala."}.get(
        row.get("operational_status"), "Boleh dipakai untuk rencana umum; tetap bukan peringatan resmi cuaca ekstrem.")


def sentinel_build_explanation(row):
    r = []
    if row.get("atmospheric_mode"):
        r.append(f"mode atmosfer {row['atmospheric_mode']}")
    if row.get("sources_used"):
        r.append(f"{row['sources_used']} source aktif")
    if (aether_value(row.get("prob_rain")) or 0) >= 55:
        r.append(f"probabilitas hujan {row['prob_rain']}%")
    if (aether_value(row.get("rain_p90")) or 0) >= 7:
        r.append(f"rain P90 {row['rain_p90']} mm")
    if (aether_value(row.get("rain_displacement_risk")) or 0) >= 60:
        r.append("risiko displacement hujan tinggi")
    if (aether_value(row.get("forecast_failure_risk")) or 0) >= 60:
        r.append(f"failure risk {row['forecast_failure_risk']}/100")
    return "Sentinel memilih interpretasi ini karena " + "; ".join(r or ["sinyal atmosfer relatif netral"]) + "."


def aether_build_rows(points, ensemble_rows, target_date, args):
    global _QUALITY_TARGET_DT
    grouped = {j: [] for j in TARGET_TIMES}
    for p in points:
        grouped.setdefault(p.target_time, []).append(p)
    ens_by_jam = {r[0]: r for r in ensemble_rows}
    micro_profile = aether_microclimate_profile(args)
    personality = sentinel_location_personality(args)
    rows = []
    for jam in TARGET_TIMES:
        _QUALITY_TARGET_DT = parse_local_hour_string(target_date, jam, args.timezone)
        bucket = grouped.get(jam) or []
        ens = ens_by_jam.get(jam)
        hour = int(jam.split(":")[0]) if jam and ":" in jam else 0
        source_ids = sorted({p.source_id for p in bucket})
        sources_used = len(bucket)
        weights = [(p, point_weight(p)) for p in bucket]
        weight_total = sum(w for _, w in weights)
        expected_sources = max(len(ACTIVE_SOURCE_CONFIGS), 1)
        coverage_fraction = round(clamp(sources_used / expected_sources, 0, 1), 4)
        temp_pairs = [(p.temp_c, point_variable_weight(p, "temp_c")) for p, _ in weights if p.temp_c is not None]
        rh_pairs = [(p.rh_pct, point_variable_weight(p, "rh_pct")) for p, _ in weights if p.rh_pct is not None]
        rain_pairs = [(p.rain_mm, point_variable_weight(p, "rain_mm")) for p, _ in weights if p.rain_mm is not None]
        wind_pairs = [(p.wind_kmh, point_variable_weight(p, "wind_kmh")) for p, _ in weights if p.wind_kmh is not None]
        gust_pairs = aether_get_weighted_attr(bucket, "wind_gusts_kmh")
        hi_pairs = [(heat_index(p.temp_c, p.rh_pct), w) for p, w in weights if heat_index(p.temp_c, p.rh_pct) is not None]
        cloud_pairs = aether_get_weighted_attr(bucket, "cloud_cover_pct")
        pprob_pairs = aether_get_weighted_attr(bucket, "precip_prob_pct")
        cape_pairs = aether_get_weighted_attr(bucket, "cape_jkg")
        vis_pairs = aether_get_weighted_attr(bucket, "visibility_m")

        cat_w = {}
        for p, w in weights:
            if p.category in CUACA_ORDER:
                cat_w[p.category] = cat_w.get(p.category, 0.0) + w
        cat_total = sum(cat_w.values())
        cat_probs = {c: (cat_w.get(c, 0.0) / cat_total * 100.0 if cat_total else 0.0) for c in CUACA_ORDER}
        dominant = max(cat_w, key=cat_w.get) if cat_w else ""
        dominant_prob = cat_probs.get(dominant, 0.0) if dominant else 0.0
        category_disagreement = round(100.0 - dominant_prob, 2) if bucket else 100.0
        prob_rain_cat = sum(cat_probs.get(c, 0.0) for c in ("Hujan Ringan", "Hujan Sedang", "Hujan Lebat"))
        prob_heavy_cat = cat_probs.get("Hujan Lebat", 0.0)
        prob_mod_heavy_cat = sum(cat_probs.get(c, 0.0) for c in ("Hujan Sedang", "Hujan Lebat"))
        pprob_mean = aether_weighted_mean(pprob_pairs)
        prob_rain = (prob_rain_cat if cat_total else None) if pprob_mean is None else (0.62 * prob_rain_cat + 0.38 * pprob_mean if cat_total else pprob_mean)
        heavy_signal = sum(w for p, w in weights if p.rain_mm is not None and p.rain_mm >= 10.0) / weight_total * 100.0 if weight_total else 0.0
        mod_signal = sum(w for p, w in weights if p.rain_mm is not None and p.rain_mm >= 5.0) / weight_total * 100.0 if weight_total else 0.0
        prob_heavy = max(prob_heavy_cat, 0.55 * prob_heavy_cat + 0.45 * heavy_signal)
        prob_mod_heavy = max(prob_mod_heavy_cat, 0.55 * prob_mod_heavy_cat + 0.45 * mod_signal)

        q = aether_weighted_quantile
        temp_p05, temp_p10, temp_p25, temp_p50, temp_p75, temp_p90, temp_p95 = [q(temp_pairs, x) for x in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)]
        rh_p10, rh_p50, rh_p90 = [q(rh_pairs, x) for x in (0.10, 0.50, 0.90)]
        rain_p05, rain_p10, rain_p25, rain_p50, rain_p75, rain_p90, rain_p95 = [q(rain_pairs, x) for x in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)]
        wind_p50, wind_p90 = [q(wind_pairs, x) for x in (0.50, 0.90)]
        gust_p90 = q(gust_pairs, 0.90)
        hi_p50, hi_p90 = [q(hi_pairs, x) for x in (0.50, 0.90)]
        cape_p50, visibility_p10, cloud_p50 = q(cape_pairs, 0.50), q(vis_pairs, 0.10), q(cloud_pairs, 0.50)

        prob_rain_raw = prob_rain
        prob_rain = _quality_apply_probability(prob_rain, _QUALITY_TARGET_DT)
        prob_rain_heuristic = prob_rain
        signal = {"target_datetime": _QUALITY_TARGET_DT.isoformat(), "prob_rain": prob_rain, "prob_rain_raw": prob_rain_raw}
        v61_enrich_probabilistic_rows([signal], _V61_ENSEMBLES, args)
        ensemble_fields = {k: v for k, v in signal.items() if k.startswith("ensemble_") or k in ("rain_two_stage", "rain_two_stage_raw", "probability_basis", "calibration_status", "rain_mm_expected", "rain_mm_est")}
        if signal.get("rain_two_stage", {}).get("available"):
            prob_rain, prob_rain_raw = signal["prob_rain"], signal["prob_rain_raw"]
            prob_heavy, prob_mod_heavy = signal["prob_heavy_rain"], signal["prob_moderate_heavy_rain"]
            rain_p05, rain_p10, rain_p25, rain_p50, rain_p75, rain_p90, rain_p95 = [signal.get("rain_" + k) for k in ("p05", "p10", "p25", "p50", "p75", "p90", "p95")]
            prob_rain_heuristic = signal.get("prob_rain_heuristic", prob_rain_heuristic)

        temp_micro, rh_micro, fog_bonus, temp_adj, rh_adj = aether_microclimate_adjustment(micro_profile, hour, temp_p50, rh_p50)
        temp_micro, local_temp_meta = v61_local_adjustment(temp_micro, "temp_c", _QUALITY_TARGET_DT, args, points=bucket)
        rh_micro, local_rh_meta = v61_local_adjustment(rh_micro, "rh_pct", _QUALITY_TARGET_DT, args, points=bucket)
        hi_p50 = heat_index(temp_micro, rh_micro)
        hi_p90 = max((v for v in (hi_p90, hi_p50) if v is not None), default=None)
        confidence_score = aether_value(ens[6]) if ens else None
        health_vals = [source_health_factor(p.source_id) for p in bucket]
        health_mean = round(sum(health_vals) / len(health_vals), 4) if health_vals else None
        gaps = [p.gap_minutes for p in bucket if p.gap_minutes is not None]
        gap_mean = round(sum(gaps) / len(gaps), 2) if gaps else None
        freshness_unc = clamp((gap_mean or 0.0) / 180.0 * 100.0, 0.0, 100.0)
        health_unc = 100.0 - (health_mean * 100.0 if health_mean is not None else 60.0)
        lead_hours = aether_lead_hours(target_date, jam, args)
        lead_unc = clamp((lead_hours or 24.0) / 72.0 * 100.0, 5.0, 100.0)
        spread_unc = clamp(((rain_p90 or 0.0) - (rain_p10 or 0.0)) / 15.0 * 100.0, 0.0, 100.0)
        displacement_prior = 50  # prior generik tanpa validasi; tidak ada koreksi berbasis nama lokasi
        displacement = round(clamp(0.32 * category_disagreement + 0.25 * spread_unc + 0.20 * displacement_prior + 0.15 * (prob_rain or 0) + 0.08 * lead_unc, 0, 100), 1)
        spatial_unc = round(clamp(0.62 * displacement + 0.38 * category_disagreement, 0, 100), 1)
        uncertainty = round(clamp(0.28 * category_disagreement + 0.22 * spread_unc + 0.18 * freshness_unc + 0.14 * health_unc + 0.10 * lead_unc + 0.08 * spatial_unc, 0, 100), 1)
        mode = sentinel_atmospheric_mode(hour, prob_rain, prob_heavy, rh_p50, cloud_p50, uncertainty, cape_p50)
        rain_threat = round(clamp(0.50 * (prob_rain or 0) + 0.22 * min((rain_p90 or 0) / 12.0 * 100.0, 100) + 0.18 * (rh_p50 or 0) + 0.10 * spatial_unc, 0, 100), 1)
        heavy_threat = round(clamp(0.42 * (prob_heavy or 0) + 0.38 * min((rain_p95 or rain_p90 or 0) / 18.0 * 100.0, 100) + 0.10 * uncertainty + 0.10 * (cape_p50 or 0) / 1500.0 * 100.0, 0, 100), 1)
        wind_threat = round(clamp(max(wind_p90 or 0, gust_p90 or 0) / 45.0 * 100.0, 0, 100), 1)
        heat_disc = round(clamp(((hi_p90 or hi_p50 or temp_p90 or temp_p50 or 26) - 27.0) / 9.0 * 100.0 + max((rh_p50 or 70) - 75, 0), 0, 100), 1)
        low_vis = round(clamp((10000 - (visibility_p10 if visibility_p10 is not None else 10000)) / 10000.0 * 100.0 + fog_bonus, 0, 100), 1)
        thunder = round(clamp(0.42 * (prob_heavy or 0) + 0.28 * (cape_p50 or 0) / 1500.0 * 100.0 + 0.20 * (rh_p50 or 0) + 0.10 * (cloud_p50 or 0), 0, 100), 1)
        direct_hit = round(clamp(rain_threat * (1 - displacement / 165.0), 0, 100), 1)
        nearby = round(clamp(0.70 * rain_threat + 0.45 * displacement, 0, 100), 1)
        stress = round(clamp(0.30 * uncertainty + 0.25 * displacement + 0.18 * spread_unc + 0.12 * lead_unc + 0.10 * category_disagreement + 0.05 * (100 - coverage_fraction * 100), 0, 100), 1)
        failure = round(clamp(0.35 * stress + 0.25 * uncertainty + 0.20 * displacement + 0.10 * (prob_rain or 0) + 0.10 * (100 - coverage_fraction * 100), 0, 100), 1)
        self_doubt = round(clamp(0.50 * failure + 0.30 * uncertainty + 0.20 * category_disagreement, 0, 100), 1)
        failure_mode = sentinel_failure_mode(mode, prob_rain, uncertainty, displacement, rain_p90, category_disagreement)
        predictability = sentinel_predictability(stress, uncertainty, displacement)
        scenarios = sentinel_multi_reality(prob_rain, prob_heavy, displacement, uncertainty, rain_p90)
        trust = aether_trust_level(sources_used, confidence_score, uncertainty, coverage_fraction, health_mean)
        op = aether_operational_status(trust, rain_threat, uncertainty, sources_used)
        if failure >= 82 and op != "BLACK":
            op = "RED"
        elif failure >= 62 and op == "GREEN":
            op = "YELLOW"
        if mode == "HEAVY_RAIN_WATCH" and op == "GREEN":
            op = "YELLOW"
        if mode in {"HUMID_CONVECTIVE_AFTERNOON", "HEAVY_RAIN_WATCH"}:
            route = "conservative_convective_risk_route"
        elif failure >= 70:
            route = "failure_aware_scenario_route"
        elif getattr(args, "mission", "") in {"outdoor_event", "fieldwork", "commute", "avoid_rain"}:
            route = "mission_decision_route"
        else:
            route = "sentinel_probabilistic_risk_route"
        r = aether_round
        row = {
            "local_temp_correction": local_temp_meta, "local_rh_correction": local_rh_meta, "prob_rain_heuristic": prob_rain_heuristic, "prob_rain_raw": prob_rain_raw,
            "target_date": target_date.isoformat(), "jam": jam, "target_datetime": _QUALITY_TARGET_DT.isoformat(), "lead_hours": r(lead_hours),
            "lead_bucket": aether_lead_bucket(lead_hours), "mission": getattr(args, "mission", "safety_first"),
            "location_personality": personality.get("personality"), "microclimate_profile": micro_profile, "atmospheric_mode": mode,
            "predictability": predictability, "dominant_category": dominant, "dominant_category_probability": r(dominant_prob, 1),
            "category_disagreement": r(category_disagreement, 1), "sources_used": sources_used, "source_ids": "+".join(source_ids),
            "coverage_fraction": r(coverage_fraction, 3),
            "temp_p05": r(temp_p05), "temp_p10": r(temp_p10), "temp_p25": r(temp_p25), "temp_p50": r(temp_p50), "temp_p75": r(temp_p75), "temp_p90": r(temp_p90), "temp_p95": r(temp_p95),
            "temp_micro_p50": r(temp_micro), "temp_micro_adjustment": r(temp_adj),
            "rh_p10": r(rh_p10), "rh_p50": r(rh_p50), "rh_p90": r(rh_p90), "rh_micro_p50": r(rh_micro), "rh_micro_adjustment": r(rh_adj),
            "rain_p05": r(rain_p05), "rain_p10": r(rain_p10), "rain_p25": r(rain_p25), "rain_p50": r(rain_p50), "rain_p75": r(rain_p75), "rain_p90": r(rain_p90), "rain_p95": r(rain_p95),
            "prob_rain": r(prob_rain, 1), "prob_moderate_heavy_rain": r(prob_mod_heavy, 1), "prob_heavy_rain": r(prob_heavy, 1),
            "wind_p50": r(wind_p50), "wind_p90": r(wind_p90), "gust_p90": r(gust_p90),
            "heat_index_p50": r(hi_p50), "heat_index_p90": r(hi_p90), "cloud_p50": r(cloud_p50), "cape_p50": r(cape_p50), "visibility_p10": r(visibility_p10),
            "direct_hit_risk": r(direct_hit, 1), "nearby_rain_risk": r(nearby, 1), "rain_displacement_risk": r(displacement, 1), "spatial_uncertainty": r(spatial_unc, 1),
            "rain_threat_score": r(rain_threat, 1), "rain_threat_level": sentinel_threat_level(rain_threat),
            "heavy_rain_threat_score": r(heavy_threat, 1), "heavy_rain_threat_level": sentinel_threat_level(heavy_threat),
            "wind_threat_score": r(wind_threat, 1), "wind_threat_level": sentinel_threat_level(wind_threat),
            "heat_discomfort_threat_score": r(heat_disc, 1), "heat_discomfort_threat_level": sentinel_threat_level(heat_disc),
            "low_visibility_threat_score": r(low_vis, 1), "low_visibility_threat_level": sentinel_threat_level(low_vis),
            "thunderstorm_proxy_threat_score": r(thunder, 1), "thunderstorm_proxy_threat_level": sentinel_threat_level(thunder),
            "uncertainty_score": r(uncertainty, 1), "forecast_stress_index": r(stress, 1), "forecast_failure_risk": r(failure, 1), "self_doubt_score": r(self_doubt, 1),
            "main_failure_mode": failure_mode,
            "scenario_dry_miss": r(scenarios["dry_miss"], 1), "scenario_nearby_rain_only": r(scenarios["nearby_rain_only"], 1),
            "scenario_direct_light_rain": r(scenarios["direct_light_rain"], 1), "scenario_direct_moderate_rain": r(scenarios["direct_moderate_rain"], 1),
            "scenario_convective_burst": r(scenarios["convective_burst"], 1),
            "walking_score": sentinel_activity_score(9, rain_threat / 13 + heat_disc / 25), "motorbike_score": sentinel_activity_score(9, rain_threat / 11 + wind_threat / 20),
            "outdoor_event_score": sentinel_activity_score(10, rain_threat / 9 + failure / 18),
            "fieldwork_score": sentinel_activity_score(10, rain_threat / 10 + heat_disc / 30 + failure / 25),
            "laundry_score": sentinel_activity_score(10, rain_threat / 7 + (cloud_p50 or 70) / 18),
            "sport_score": sentinel_activity_score(9, rain_threat / 11 + heat_disc / 18),
            "trust_level": trust, "operational_status": op, "autopilot_route": route, "confidence_score_base": r(confidence_score, 1),
            "source_health_mean": r(health_mean, 3), "gap_mean_minutes": r(gap_mean, 1),
        }
        row.update(ensemble_fields)
        row["decision_recommendation"] = sentinel_decision(row, args)
        row["expert_council"] = sentinel_expert_council(row, args)
        row["counterfactual_summary"] = sentinel_counterfactual(row)
        row["forecast_contract_summary"] = sentinel_forecast_contract_summary(row)
        row["explanation"] = sentinel_build_explanation(row)
        rows.append(row)
    return rows


def _v8_weighted_attr(points, attr):
    return aether_weighted_mean([(safe_float(getattr(p, attr, None)), point_weight(p)) for p in points or [] if getattr(p, attr, None) is not None])


def _weighted_wind_direction(points):
    """Rata-rata arah melingkar: 359° dan 1° menghasilkan utara."""
    x = y = total = 0.0
    for p in points or []:
        d, w, speed = safe_float(getattr(p, "wind_direction_deg", None)), point_weight(p), safe_float(getattr(p, "wind_kmh", None))
        if d is None or not 0 <= d <= 360 or w <= 0 or speed == 0:
            continue
        a = math.radians(d % 360)
        x += w * math.cos(a)
        y += w * math.sin(a)
        total += w
    if not total or math.hypot(x, y) < total * 1e-10:
        return None
    return math.degrees(math.atan2(y, x)) % 360


def _v8_enrich_rows_with_source_variables(points, rows):
    grouped = {j: [] for j in TARGET_TIMES}
    for p in points or []:
        grouped.setdefault(p.target_time, []).append(p)
    for row in rows or []:
        bucket = grouped.get(row.get("jam"), [])
        row["dew_point_c"] = aether_round(_v8_weighted_attr(bucket, "dew_point_c"), 1)
        row["apparent_temperature_c"] = aether_round(_v8_weighted_attr(bucket, "apparent_temp_c"), 1)
        row["pressure_msl_hpa"] = aether_round(_v8_weighted_attr(bucket, "pressure_msl_hpa"), 1)
        row["surface_pressure_hpa"] = aether_round(_v8_weighted_attr(bucket, "surface_pressure_hpa"), 1)
        direction = _weighted_wind_direction(bucket)
        row["wind_direction_deg"] = round(direction) % 360 if direction is not None else None
        row["uv_index"] = aether_round(_v8_weighted_attr(bucket, "uv_index"), 1)
        if row.get("apparent_temperature_c") in (None, ""):
            row["apparent_temperature_c"] = row.get("heat_index_p50", "")
    return rows


def sentinel_daily_status(statuses):
    for s in ("BLACK", "RED", "YELLOW"):
        if s in statuses:
            return s
    return "GREEN"


def aether_daily_summary(rows, args):
    date = rows[0]["target_date"] if rows else now_local(args.timezone).date().isoformat()
    day = _lg_norm_day({"date": date, "hours": [{**r, "hour": r.get("jam"), "rain_probability": r.get("prob_rain"),
                                                  "temp_c": _quality_first(r, "temp_micro_p50", "temp_p50"), "humidity_pct": _quality_first(r, "rh_micro_p50", "rh_p50"),
                                                  "heat_index_c": r.get("heat_index_p50"), "wind_kmh": r.get("wind_p50"), "rain_risk_score": r.get("rain_threat_score")} for r in rows]},
                       0, args.location_name, args)
    peak = max((r for r in rows if r.get("data_quality") != "unavailable"), key=lambda r: safe_float(r.get("rain_threat_score")) or 0, default={})
    return {"generated_at": _quality_issued(args).isoformat(), "issued_at": _quality_issued(args).isoformat(), "aether_version": QUALITY_VERSION,
            "location": args.location_name, "mission": getattr(args, "mission", "safety_first"),
            "daily_operational_status": sentinel_daily_status([r.get("operational_status", "BLACK") for r in rows]) if rows else "BLACK",
            "data_quality": day["data_quality"], "best_general_activity_window": day["best_activity_window"], "peak_rain_threat_hour": peak.get("jam"),
            "peak_rain_threat_score": peak.get("rain_threat_score"), "peak_heavy_rain_threat_hour": peak.get("jam"),
            "peak_heavy_rain_threat_score": peak.get("heavy_rain_threat_score"), "summary_text": day["decision_sentence"], "risk_window": [],
            "location_personality": sentinel_location_personality(args),
            "dominant_atmospheric_mode": "UNAVAILABLE" if day["data_quality"] == "unavailable" else peak.get("atmospheric_mode", "UNKNOWN")}


# ---------------------------------------------------------------------------
# Rencana aktivitas berbasis ambang (bukan jaminan keselamatan)
# ---------------------------------------------------------------------------
V61_ACTIVITY_PROFILES = {
    "motor": {"label": "Berkendara / motor", "duration_hours": 1, "rain_probability": 35, "rain_p90_mm": 2, "wind_kmh": 25, "gust_kmh": 35, "heat_index_c": 38, "window_rain_upper_bound": 60},
    "walking": {"label": "Jalan kaki", "duration_hours": 1, "rain_probability": 30, "rain_p90_mm": 1, "wind_kmh": 25, "gust_kmh": 35, "heat_index_c": 34, "window_rain_upper_bound": 50},
    "sport": {"label": "Olahraga", "duration_hours": 2, "rain_probability": 25, "rain_p90_mm": 1, "wind_kmh": 20, "gust_kmh": 30, "heat_index_c": 32, "window_rain_upper_bound": 60},
    "drying": {"label": "Menjemur", "duration_hours": 3, "rain_probability": 15, "rain_p90_mm": 0.1, "wind_kmh": 25, "gust_kmh": 35, "heat_index_c": 42, "window_rain_upper_bound": 40},
    "outdoor_event": {"label": "Acara outdoor", "duration_hours": 3, "rain_probability": 25, "rain_p90_mm": 1, "wind_kmh": 20, "gust_kmh": 30, "heat_index_c": 33, "window_rain_upper_bound": 65},
    "photography": {"label": "Fotografi", "duration_hours": 2, "rain_probability": 30, "rain_p90_mm": 1, "wind_kmh": 20, "gust_kmh": 30, "heat_index_c": 35, "window_rain_upper_bound": 70},
}


def v61_activity_profiles(args=None):
    profiles = json.loads(json.dumps(V61_ACTIVITY_PROFILES))
    path = getattr(args, "activity_profiles_file", None) if args else None
    if path:
        custom = read_json(path, {})
        if not isinstance(custom, dict):
            raise ValueError("Activity profiles must be a JSON object")
        for name, values in custom.items():
            if name not in profiles or not isinstance(values, dict):
                raise ValueError("Unknown activity profile: " + name)
            for key, value in values.items():
                if key == "label":
                    continue
                if key not in profiles[name] or safe_float(value) is None or safe_float(value) < 0:
                    raise ValueError("Invalid activity threshold: " + name + "." + key)
                profiles[name][key] = safe_float(value)
            if not 1 <= profiles[name]["duration_hours"] <= 8:
                raise ValueError("Activity duration must be between 1 and 8 hours")
    return profiles


def v61_activity_hour(hour):
    heat = max((v for k in ("heat_index_p90", "heat_index_c", "heat_index_p50") if (v := safe_float(hour.get(k))) is not None), default=None)
    return {**hour, "hour": _quality_first(hour, "hour", "jam"), "rain_probability": _quality_first(hour, "rain_probability", "prob_rain"), "heat_index_c": heat,
            "wind_kmh": _quality_first(hour, "wind_p90", "wind_kmh", "wind_p50"), "rain_p90_mm": _quality_first(hour, "rain_p90_mm", "rain_p90"),
            "gust_kmh": _quality_first(hour, "gust_kmh", "gust_p90")}


def v61_activity_plans(hours, args=None, now=None):
    now = now or (_quality_issued(args) if args else now_local(DEFAULT_TIMEZONE))
    normalized = sorted((v61_activity_hour(h) for h in hours or []), key=lambda h: h.get("target_datetime") or "")
    plans = []
    for name, profile in v61_activity_profiles(args).items():
        valid, rejects = [], {}
        for h in normalized:
            dt = _quality_datetime(h.get("target_datetime"))
            if dt is None or dt < now or not 6 <= dt.hour <= 18:
                continue
            reasons = []
            if h.get("data_quality") not in {"usable", "limited"} or h.get("operational_status") == "BLACK":
                reasons.append("data_not_usable")
            for key in ("rain_probability", "rain_p90_mm", "wind_kmh", "heat_index_c"):
                value = safe_float(h.get(key))
                if value is None:
                    reasons.append(key + "_missing")
                elif value > profile[key]:
                    reasons.append(key + "_above_limit")
            gust = safe_float(h.get("gust_kmh"))
            if gust is not None and gust > profile["gust_kmh"]:
                reasons.append("gust_above_limit")
            if reasons:
                for rs in reasons:
                    rejects[rs] = rejects.get(rs, 0) + 1
            else:
                valid.append((dt, h))
        windows = []
        for i, (start, h) in enumerate(valid):
            chain = [(start, h)]
            for nxt in valid[i + 1:]:
                if nxt[0] - chain[-1][0] != timedelta(hours=1):
                    break
                chain.append(nxt)
                if (chain[-1][0] - start).total_seconds() / 3600 >= profile["duration_hours"]:
                    break
            if (chain[-1][0] - start).total_seconds() / 3600 < profile["duration_hours"]:
                continue
            event_hours = chain[1:]
            upper = min(100, sum(safe_float(it[1]["rain_probability"]) or 0 for it in event_hours))  # batas atas union, tanpa asumsi independensi
            if upper > profile["window_rain_upper_bound"]:
                continue
            windows.append({"start": start.isoformat(), "end": chain[-1][0].isoformat(), "hours": [it[1]["hour"] for it in chain],
                            "any_rain_probability_upper_bound_pct": round(upper, 2),
                            "max_rain_probability_pct": max(safe_float(it[1]["rain_probability"]) or 0 for it in event_hours),
                            "evaluation_hours": [it[1]["hour"] for it in event_hours],
                            "limiting_gust_data_missing": any(safe_float(it[1].get("gust_kmh")) is None for it in chain)})
        windows.sort(key=lambda w: (w["any_rain_probability_upper_bound_pct"], w["start"]))
        sel = windows[0] if windows else None
        plans.append({"activity_id": name, "activity": profile["label"], "profile": profile, "window": sel, "issued_at": now.isoformat(),
                      "status": "Rentang risiko relatif lebih rendah" if sel else "Rekomendasi belum tersedia",
                      "advice": "Sesuaikan dengan kondisi setempat; batas ini belum merupakan jaminan keselamatan." if sel else "Tidak ada rentang masa depan yang memenuhi data dan batas aktivitas.",
                      "priority_hour": (sel["hours"][0] + "–" + sel["hours"][-1]) if sel else "—", "risk_class": "watch" if sel else "unavailable", "rejection_counts": rejects})
    return plans


# ---------------------------------------------------------------------------
# Verifikasi (dijalankan sekali per lokasi di akhir siklus)
# ---------------------------------------------------------------------------
def sentinel_compute_verification(rows, args, observations=None):
    observations = observations if observations is not None else sentinel_load_observations_by_key(args)
    now = now_local(args.timezone)
    pairs = []
    bins = {f"{i:02d}-{i + 10:02d}": [] for i in range(0, 100, 10)}
    for issued, row, obs, lead in _quality_forecast_pairs(rows, observations, args):
        obs = dict(obs)
        for field in _OBS_FIELDS:
            if _quality_observation_available(obs, field, now) is None:
                obs[field] = None
        event = sentinel_observed_rain_event(obs)
        prob = _lg_prob(row.get("prob_rain"))
        pred_temp = safe_float(_quality_first(row, "temp_micro_p50", "temp_p50"))
        temp = _quality_observed_value(obs, "temp_c")
        pair = {"target_date": row["target_date"], "jam": row["jam"], "issued_at": issued.isoformat(), "run_id": row.get("run_id"), "lead_hours": lead,
                "lead_bucket": aether_lead_bucket(lead), "pred_prob_rain": prob, "observed_rain": int(event) if event is not None else None,
                "pred_rain_event": int(prob >= 50) if prob is not None else None,
                "brier": (prob / 100 - int(event)) ** 2 if prob is not None and event is not None else None, "pred_temp": pred_temp, "observed_temp": temp,
                "temp_abs_error": abs(temp - pred_temp) if temp is not None and pred_temp is not None else None}
        observed_cat = _quality_observed_value(obs, "category")
        pair["category_match"] = int(row.get("dominant_category") == observed_cat) if observed_cat and row.get("dominant_category") else None
        pairs.append(pair)
        if pair["brier"] is not None:
            lower = min(90, int(prob // 10) * 10)
            bins[f"{lower:02d}-{lower + 10:02d}"].append(pair)
    rain_pairs = [p for p in pairs if p["brier"] is not None]
    temp_pairs = [p for p in pairs if p["temp_abs_error"] is not None]
    cats = [p["category_match"] for p in pairs if p["category_match"] is not None]
    hits = sum(p["observed_rain"] == 1 and p["pred_rain_event"] == 1 for p in rain_pairs)
    misses = sum(p["observed_rain"] == 1 and p["pred_rain_event"] == 0 for p in rain_pairs)
    false = sum(p["observed_rain"] == 0 and p["pred_rain_event"] == 1 for p in rain_pairs)
    negatives = len(rain_pairs) - hits - misses - false

    # Klimatologi sekuensial tanpa lookahead: sapuan kronologis, O(P x jumlah_hari) bukan O(P x N).
    events = []
    for (d, _), o in observations.items():
        ev = sentinel_observed_rain_event(o)
        avail = _quality_datetime(o.get("rain_mm_available_at") or o.get("category_available_at") or o.get("available_at"), args.timezone)
        if ev is not None and avail is not None:
            events.append((avail, d, int(ev)))
    events.sort()
    per_date, ptr, baseline_errors = {}, 0, []
    for p in sorted(rain_pairs, key=lambda p: p["issued_at"]):
        issued = _quality_datetime(p["issued_at"], args.timezone)
        while ptr < len(events) and events[ptr][0] <= issued:
            c, t = per_date.get(events[ptr][1], (0, 0))
            per_date[events[ptr][1]] = (c + 1, t + events[ptr][2])
            ptr += 1
        n = s = 0
        for d, (c, t) in per_date.items():
            if d < p["target_date"]:
                n += c
                s += t
        if n:
            baseline_errors.append(((p["pred_prob_rain"] / 100 - p["observed_rain"]) ** 2, (s / n - p["observed_rain"]) ** 2))
    forecast_brier = sum(p["brier"] for p in rain_pairs) / len(rain_pairs) if rain_pairs else None
    baseline = sum(b for _, b in baseline_errors) / len(baseline_errors) if baseline_errors else None
    comparable = sum(f for f, _ in baseline_errors) / len(baseline_errors) if baseline_errors else None
    skill = 1 - comparable / baseline if baseline is not None and baseline > 0 else None
    days = len({p["target_date"] for p in rain_pairs})
    enough = len(rain_pairs) >= int(getattr(args, "verification_min_cases", 30)) and days >= 10 and hits + misses >= 5 and negatives + false >= 5
    reliability = [{"probability_bin": label, "n": len(v), "mean_forecast_probability": sum(p["pred_prob_rain"] for p in v) / len(v) if v else None,
                    "observed_rain_frequency": sum(p["observed_rain"] for p in v) / len(v) * 100 if v else None} for label, v in bins.items()]
    lead_metrics = {}
    for bucket in sorted({p["lead_bucket"] for p in rain_pairs}):
        subset = [p for p in rain_pairs if p["lead_bucket"] == bucket]
        lead_metrics[bucket] = {"samples": len(subset), "brier": sum(p["brier"] for p in subset) / len(subset)}
    summary = {"generated_at": now.isoformat(), "location_slug": args.location_slug, "location_name": args.location_name, "matched_cases": len(pairs),
               "rain_valid_cases": len(rain_pairs), "temperature_valid_cases": len(temp_pairs), "independent_days": days,
               "verification_min_cases": getattr(args, "verification_min_cases", 30), "sample_status": "ENOUGH_TO_EVALUATE" if enough else "INSUFFICIENT_DATA",
               "calibration_status": "UNCALIBRATED", "skill_status": "POSITIVE_VS_BASELINE" if enough and skill is not None and skill > 0 else "NOT_DEMONSTRATED",
               "rain_brier_score": forecast_brier, "baseline_brier_score": baseline, "baseline_comparable_cases": len(baseline_errors), "rain_brier_skill_score": skill,
               "temperature_mae_c": sum(p["temp_abs_error"] for p in temp_pairs) / len(temp_pairs) if temp_pairs else None,
               "category_accuracy": sum(cats) / len(cats) if cats else None, "rain_hits": hits, "rain_misses": misses, "rain_false_alarms": false,
               "rain_correct_negatives": negatives, "rain_pod": hits / (hits + misses) if hits + misses else None,
               "rain_far": false / (hits + false) if hits + false else None, "rain_csi": hits / (hits + misses + false) if hits + misses + false else None,
               "lead_time_metrics": lead_metrics,
               "scientific_note": "Skor risiko/trust/skenario tetap heuristik. Jumlah sampel bukan bukti kalibrasi. Hanya prakiraan pra-kejadian dan observasi QC-lolos yang dinilai."}
    return summary, pairs, reliability


def v61_activity_verification(args, plan_docs, observations):
    counts = {n: {"recommended_windows": 0, "evaluated_windows": 0, "disrupted_windows": 0, "unknown_windows": 0} for n in V61_ACTIVITY_PROFILES}
    seen = set()
    now = now_local(args.timezone)
    for doc in sorted(plan_docs, key=lambda d: d.get("issued_at") or ""):
        issued = _quality_datetime(doc.get("issued_at"))
        if issued is None:
            continue
        for plan in doc.get("activity_plans") or []:
            name, window = plan.get("activity_id"), plan.get("window")
            if name not in counts or not window:
                continue
            start, end = _quality_datetime(window.get("start")), _quality_datetime(window.get("end"))
            if start is None or end is None or start <= issued or end > now:
                continue
            ident = (name, window["start"], window["end"])
            if ident in seen:
                continue
            seen.add(ident)
            counts[name]["recommended_windows"] += 1
            unknown = disrupted = False
            profile = plan["profile"]
            for hour in window.get("evaluation_hours") or []:
                row = observations.get((start.date().isoformat(), hour)) or {}
                valid = parse_local_hour_string(start.date(), hour, args.timezone)
                vals = {f: _quality_observation_available(row, f, now) for f in ("rain_mm", "wind_kmh", "temp_c", "rh_pct")}
                if not _v61_matching_rain_interval(row, valid):
                    vals["rain_mm"] = None
                for field, maximum in {"rain_mm": 2, "wind_kmh": 5, "temp_c": 10, "rh_pct": 10}.items():
                    dist = _v61_distance_km(args.latitude, args.longitude, _v61_station_value(row, field, "latitude"), _v61_station_value(row, field, "longitude"))
                    if dist is None or dist > maximum:
                        vals[field] = None
                if any(v is None for v in vals.values()):
                    unknown = True
                    continue
                hi = heat_index(vals["temp_c"], vals["rh_pct"])
                if vals["rain_mm"] > profile["rain_p90_mm"] or vals["wind_kmh"] > profile["wind_kmh"] or (hi is not None and hi > profile["heat_index_c"]):
                    disrupted = True
            if unknown:
                counts[name]["unknown_windows"] += 1
            else:
                counts[name]["evaluated_windows"] += 1
                counts[name]["disrupted_windows"] += int(disrupted)
    for c in counts.values():
        c["disruption_rate"] = c["disrupted_windows"] / c["evaluated_windows"] if c["evaluated_windows"] else None
    return {"activities": counts, "gust_verification": "Unavailable unless observed gust data is supplied; rates do not certify gust safety.",
            "policy": "Only complete QC-passed local observation windows scored. Unknown is not counted as success."}


def _quality_load_history(observations):
    """Satu kali lewat arsip: baris yang punya observasi + dokumen rencana aktivitas. Tanpa observasi, arsip tidak dibaca."""
    rows, plans = [], []
    if not observations:
        return rows, plans
    for doc in _quality_archive_documents():
        plans.append({"issued_at": doc.get("issued_at"), "activity_plans": doc.get("activity_plans")})
        for row in doc.get("hourly") or []:
            if (row.get("target_date"), row.get("jam")) in observations:
                rows.append({**row, "issued_at": doc.get("issued_at"), "run_id": doc.get("run_id"), "location_slug": doc.get("location_slug")})
    return rows, plans


def sentinel_write_verification_artifacts(rows, args):
    summary_path = path_output("sentinel_x_verification_summary.json")
    if getattr(args, "defer_verification", False):
        cached = read_json(summary_path, {}) or {}
        cached.setdefault("calibration_status", "UNCALIBRATED")
        cached.setdefault("rain_valid_cases", 0)
        return cached
    observations = sentinel_load_observations_by_key(args)
    history, plan_docs = _quality_load_history(observations)
    summary, pairs, reliability = sentinel_compute_verification(history, args, observations)
    probabilistic = v61_probabilistic_verification(history, observations, args)
    activity = v61_activity_verification(args, plan_docs, observations)
    write_json(path_output("probabilistic_verification.json"), probabilistic)
    write_json(path_output("activity_verification.json"), activity)
    summary.update({"probabilistic_verification": probabilistic, "activity_verification": activity, "matched_pairs": pairs[-500:], "reliability_bins": reliability})
    write_json(summary_path, summary)
    write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), rows=pairs or [{"target_date": "", "jam": "", "note": "No matched forecast-observation pairs yet."}])
    write_dict_csv(path_output("sentinel_x_reliability.csv"), ["probability_bin", "n", "mean_forecast_probability", "observed_rain_frequency"], reliability)
    doc = _lg_accuracy_html(summary, args)
    for name in ("akurasi_data.html", "sentinel_x_accuracy_public.html"):
        atomic_write_text(path_output(name), lambda f: f.write(doc))
    return summary


def sentinel_x_save_artifacts(target_date, results, args, source_rows, status_rows, ensemble_rows):
    for r in results:
        _quality_result_metadata(r, args)
    points = flatten_points(results)
    _quality_set_families(points)
    rows = aether_build_rows(points, ensemble_rows, target_date, args)
    _v8_enrich_rows_with_source_variables(points, rows)
    _quality_enrich_rows(rows, points, target_date, args)
    run_id = f"{args.location_slug}_{target_date:%Y%m%d}_{uuid.uuid4().hex}"
    for row in rows:
        row["run_id"] = run_id
    archive = _quality_archive_run(run_id, target_date, results, rows, args)
    verification = sentinel_write_verification_artifacts(rows, args)  # no-op ringan saat --defer-verification
    for row in rows:
        row["calibration_status"] = verification.get("calibration_status", "UNCALIBRATED")
        row["verification_cases"] = verification.get("rain_valid_cases", 0)
        row["official_warning_disclaimer"] = LANGIT_DISCLAIMER
    public_rows = [_strip_members(r) for r in rows]
    nested = {"ensemble_distribution", "ensemble_models", "rain_two_stage", "rain_two_stage_raw"}
    csv_rows = [{k: v for k, v in r.items() if k not in nested} for r in public_rows]
    write_dict_csv(path_output(AETHER_CSV_FILENAME), rows=csv_rows)
    write_dict_csv(path_output(f"sentinel_x_{target_date:%Y%m%d}.csv"), rows=csv_rows)
    states = aether_source_state_rows(results)
    for state, result in zip(states, results):
        state.update({"is_cached": result.is_cached, "live_fetch_success": result.live_fetch_success, "original_fetched_at": _quality_jsonable(result.original_fetched_at),
                      "cache_age_hours": result.cache_age_hours, "model_family": result.model_family})
    write_dict_csv(path_output(AETHER_SOURCE_STATE_FILENAME), rows=states)
    daily = aether_daily_summary(rows, args)
    daily["activity_plans"] = v61_activity_plans(rows, args)
    daily["peak_rain_probability"] = max((safe_float(r.get("prob_rain")) for r in rows if safe_float(r.get("prob_rain")) is not None), default=None)
    payload = {"schema_version": QUALITY_SCHEMA, "version": APP_VERSION, "issued_at": _quality_issued(args).isoformat(), "daily": daily, "hourly": public_rows,
               "source_states": states, "source_availability": read_json(path_output("source_availability.json"), {}),
               "ensemble_status": {k: v for k, v in _V61_ENSEMBLES.items() if k != "hourly"}}
    write_json(path_output(AETHER_JSON_FILENAME), payload)
    write_json(path_output(f"sentinel_x_{target_date:%Y%m%d}.json"), payload)
    aether_write_contract(daily, args)
    sentinel_write_constitution(args)
    if not getattr(args, "defer_public_pages", False):
        anemos_write_multiday_public_pages(args, [target_date])
        sentinel_write_publish_manifest(args)
    return {"version": APP_VERSION, "run_id": run_id, "csv": path_output(AETHER_CSV_FILENAME), "json": path_output(AETHER_JSON_FILENAME),
            "contract": path_output(AETHER_CONTRACT_FILENAME), "archive": archive, "daily_operational_status": daily.get("daily_operational_status")}


def aether_write_contract(daily, args):
    write_json(path_output(AETHER_CONTRACT_FILENAME), {
        "version": APP_VERSION, "generated_at": now_local(args.timezone).isoformat(), "location": getattr(args, "location_name", ""),
        "official_warning_disclaimer": LANGIT_DISCLAIMER,
        "allowed_uses": ["rencana aktivitas umum", "estimasi risiko kehujanan", "pendukung keputusan non-kritis", "edukasi/riset pribadi"],
        "not_allowed_uses": ["peringatan bencana resmi", "penerbangan", "operasi keselamatan kritis", "keputusan banjir resmi", "pengganti informasi BMKG"],
        "known_weaknesses": ["timing hujan konvektif lokal", "intensitas hujan ekstrem", "posisi tepat sel hujan", "wilayah dengan observasi historis minim"],
        "calibration_policy": "Skor risiko berlabel heuristik sampai pasangan prakiraan-observasi memenuhi ambang verifikasi.",
        "daily_summary": daily, "constitution": SENTINEL_CONSTITUTION})
    return path_output(AETHER_CONTRACT_FILENAME)


def sentinel_write_constitution(args):
    write_json(path_output("sentinel_constitution.json"), {"version": APP_VERSION, "generated_at": now_local(args.timezone).isoformat(), "constitution": SENTINEL_CONSTITUTION})
    lines = [f"# {APP_VERSION} — Forecast Constitution", ""] + [f"{i}. {r}" for i, r in enumerate(SENTINEL_CONSTITUTION, 1)]
    atomic_write_text(path_output("sentinel_constitution.md"), lambda f: f.write("\n".join(lines)))


# ===========================================================================
# LANGIT — lapisan produk publik (satu definisi per fungsi)
# ===========================================================================
def _lg_extract_rain(row):
    return _lg_prob(_quality_first(row, "rain_probability", "prob_rain", "precip_prob_pct", "rain_prob_pct", "precipitation_probability"))


def _lg_class(score):
    v = _lg_float(score)
    return "unavailable" if v is None else "danger" if v >= 75 else "rain" if v >= 52 else "watch" if v >= 28 else "safe"


def _lg_label(score):
    return {"unavailable": "Data belum memadai", "safe": "Risiko relatif rendah", "watch": "Perlu diperhatikan", "rain": "Waspada", "danger": "Berpotensi signifikan"}[_lg_class(score)]


def _lg_risk_score(prob=None, heat=None, rh=None, wind=None, condition=None, confidence_penalty=0, cloud=None):
    p, c = _lg_prob(prob, 0.0), _lg_text(condition).lower()
    heat_v, rh_v, wind_v, cloud_v = _lg_float(heat), _lg_float(rh), _lg_float(wind), _lg_float(cloud)
    cond = 15 if any(k in c for k in ("kuat", "sedang", "petir")) else 8 if ("hujan" in c or "gerimis" in c) else 4 if ("potensi" in c or "mendung" in c) else 0
    score = (p * 0.74 + max(0, (heat_v or 27) - 31.0) * 4.0 + max(0, (rh_v or 70) - 84.0) * 0.42 + max(0, (wind_v or 0) - 18.0) * 0.55
             + max(0, (cloud_v or 50) - 78.0) * 0.18 + cond + (_lg_float(confidence_penalty, 0) or 0))
    return round(clamp(score, 0, 100), 1)


def _lg_hour_note(prob, score, condition=None):
    p = _lg_prob(prob, 0)
    s = _lg_float(score, p) or p
    cond = _lg_text(condition, "kondisi").lower()
    if s >= 75 or p >= 75:
        return "Kondisi cuaca dapat mengganggu aktivitas. Batasi kegiatan di luar ruang."
    if s >= 52 or p >= 50:
        return "Potensi hujan terpantau. Siapkan perlengkapan hujan sebelum beraktivitas."
    if s >= 28 or p >= 28:
        return "Kondisi secara umum kondusif, tetap pantau potensi hujan lokal."
    if "panas" in cond:
        return "Kondisi cuaca cerah. Kurangi aktivitas terpapar sinar matahari langsung."
    if "lembap" in cond:
        return "Tingkat kelembapan terpantau cukup tinggi. Tetap berhati-hati."
    return "Kondisi cuaca mendukung aktivitas luar ruang."


def _lg_condition_smart(row, hour, temp, rh, rain, wind):
    raw_low = _lg_text(row.get("condition") or row.get("category") or row.get("cuaca") or row.get("raw_condition"), "").lower()
    code = row.get("weather_code")
    if code is None and "wmo:" in raw_low:
        try:
            code = int(raw_low.split("wmo:", 1)[1].split()[0].strip())
        except Exception:
            code = None
    try:
        code = int(code) if code is not None and str(code).strip() != "" else None
    except Exception:
        code = None
    hh = _lg_hour_int(hour, 12)
    cloud = _lg_float(row.get("cloud_cover_pct") or row.get("cloud_cover"))
    radiation = _lg_float(row.get("shortwave_radiation_wm2") or row.get("shortwave_radiation"))
    cape = _lg_float(row.get("cape_jkg") or row.get("cape"), 0) or 0
    rain, rh, temp = _lg_prob(rain, 0), _lg_float(rh), _lg_float(temp)
    if any(k in raw_low for k in ("petir", "thunder", "badai")):
        return "Hujan petir lokal"
    if any(k in raw_low for k in ("lebat", "heavy")):
        return "Hujan kuat"
    if any(k in raw_low for k in ("hujan", "rain", "gerimis", "drizzle")):
        return "Hujan lokal" if rain >= 35 else "Gerimis lokal"
    if code is not None:
        if code == 0:
            return "Cerah"
        if code in (1, 2):
            return "Cerah berawan"
        if code in (3, 45, 48):
            return "Berawan tebal" if (rh or 0) >= 82 else "Berawan"
        if code in (51, 53, 55, 56, 57, 61, 80):
            return "Hujan lokal"
        if code in (63, 65, 66, 67, 81, 82, 95, 96, 99):
            return "Hujan kuat"
    if rain >= 70:
        return "Hujan lokal kuat"
    if rain >= 50:
        return "Hujan lokal"
    if rain >= 35:
        return "Awan tumbuh, potensi hujan"
    if rain >= 20 and 11 <= hh <= 18:
        return "Awan tumbuh"
    if rh is not None and rh >= 90 and hh <= 8:
        return "Lembap pagi"
    if rh is not None and rh >= 88 and (hh >= 20 or hh <= 5):
        return "Lembap malam"
    if temp is not None and temp >= 30 and rh is not None and rh >= 65:
        return "Panas lembap"
    if cloud is not None:
        if cloud <= 25 and 8 <= hh <= 15:
            return "Cerah"
        if cloud <= 55:
            return "Cerah berawan"
        if cloud >= 82:
            return "Berawan tebal"
    if radiation is not None and radiation >= 450 and 8 <= hh <= 15:
        return "Cerah berawan"
    if cape >= 800 and 12 <= hh <= 18:
        return "Awan konvektif"
    return "Cerah berawan" if 10 <= hh <= 15 else "Berawan sore" if 16 <= hh <= 18 else "Berawan dipantau"


def _lg_icon(condition):
    c = str(condition or "").lower()
    if any(k in c for k in ("petir", "badai", "thunder")):
        return "▧"
    if any(k in c for k in ("hujan kuat", "lebat", "kuat")):
        return "▥"
    if any(k in c for k in ("hujan", "gerimis")):
        return "◒"
    if "cerah" in c:
        return "◉"
    if any(k in c for k in ("lembap", "kabut")):
        return "≋"
    if any(k in c for k in ("awan tumbuh", "konvektif")):
        return "◌"
    return "☁"


def _lg_norm_hour(row, args=None, day_index=0):
    row = dict(row) if isinstance(row, dict) else {}
    hour = _lg_hour(_quality_first(row, "hour", "jam", "target_time"), "—")
    temp = _lg_float(_quality_first(row, "temp_c", "temperature_c", "temperature"))
    rh = _lg_float(_quality_first(row, "humidity_pct", "rh_pct", "relative_humidity"))
    heat = _lg_float(_quality_first(row, "heat_index_c", "apparent_temp_c", "apparent_temperature_c"), temp)
    wind = _lg_float(_quality_first(row, "wind_kmh", "wind_speed_kmh"))
    rain = _lg_extract_rain(row)
    sources, coverage = safe_float(row.get("sources_used")), safe_float(row.get("coverage_fraction"))
    state = row.get("data_quality") or "legacy_unclassified"
    usable = (state in {"usable", "stale", "limited"} and sources is not None and sources >= 3 and coverage is not None and coverage >= 0.5
              and rain is not None and row.get("operational_status") != "BLACK")
    if not usable:
        state, rain = "unavailable", None
    raw = _quality_first(row, "condition", "category", "cuaca", "raw_condition", default="")
    condition = _lg_condition_smart(row, hour, temp, rh, rain if rain is not None else 0, wind) if usable else "Data cuaca belum memadai"
    score = _lg_float(row.get("rain_risk_score")) if usable else None
    if score is None and usable:
        score = _lg_risk_score(rain, heat, rh, wind, condition)
    advice = _lg_hour_note(rain, score, condition) if usable else "Prakiraan belum layak untuk rekomendasi aktivitas; tunggu pembaruan data."
    return {**row, "hour": hour, "raw_condition": raw, "condition": condition, "icon": _lg_icon(condition), "temp_c": temp, "humidity_pct": rh,
            "heat_index_c": heat, "rain_probability": rain, "probability_unit": "percent", "wind_kmh": wind, "risk_score": score,
            "risk_class": _lg_class(score), "risk_label": _lg_label(score), "advice": advice, "data_quality": state, "recommendation_eligible": usable}


def _lg_sort_hours(hours):
    def key(row):
        h = _lg_hour(row.get("hour") or row.get("jam") or row.get("target_time"), "99:99")
        try:
            return int(h[:2]) * 60 + int(h[3:5])
        except Exception:
            return 9999
    return sorted((h for h in hours if isinstance(h, dict)), key=key)


def _lg_average(values):
    vals = [v for v in (_lg_float(x) for x in values) if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _lg_raw_hours(day):
    for key in ("key_hours", "hours", "hourly", "timeline", "forecast_hours"):
        val = day.get(key) if isinstance(day, dict) else None
        if isinstance(val, list):
            return [h for h in val if isinstance(h, dict)]
    return []


def _lg_best_window(hours, duration_hours=2, now=None):
    now = now or now_local(DEFAULT_TIMEZONE)
    eligible = []
    for row in _lg_sort_hours(hours or []):
        if row.get("data_quality") not in {"usable", "limited"} or not row.get("recommendation_eligible"):
            continue
        stamp = _quality_datetime(row.get("target_datetime"), str(getattr(now.tzinfo, "key", DEFAULT_TIMEZONE)))
        if stamp is None:
            continue
        p, heat = _lg_prob(row.get("rain_probability")), _lg_float(row.get("heat_index_c"))
        risk, wind = _lg_float(row.get("risk_score")), _lg_float(row.get("wind_kmh"))
        if stamp >= now and 6 <= stamp.hour <= 18 and p is not None and p <= 40 and heat is not None and heat <= 33 and risk is not None and risk <= 45 and wind is not None and wind <= 25:
            eligible.append((stamp, row, p + max(0, heat - 30) * 5))
    windows, duration = [], timedelta(hours=max(1, duration_hours))
    for i in range(len(eligible)):
        chain = [eligible[i]]
        for cand in eligible[i + 1:]:
            if cand[0] - chain[-1][0] != timedelta(hours=1):
                break
            chain.append(cand)
            if chain[-1][0] - chain[0][0] >= duration:
                break
        if chain[-1][0] - chain[0][0] >= duration:
            windows.append((max(c[2] for c in chain), [c[1]["hour"] for c in chain]))
    return min(windows, key=lambda x: x[0])[1] if windows else []


def _lg_best_window_text(values):
    out = []
    for v in values or []:
        s = _lg_hour(v, "")
        if s and s != "—" and s not in out:
            out.append(s)
    return " · ".join(out) if out else "Belum ada window aman"


def _lg_decision_sentence(day, loc):
    if day.get("data_quality") == "unavailable":
        return f"{loc}: data prakiraan belum lengkap atau sudah usang. Rekomendasi waktu aktivitas belum tersedia."
    p = _lg_prob(day.get("peak_rain_probability"))
    best, peak = _lg_best_window_text(day.get("best_activity_window")), _lg_hour(day.get("peak_rain_hour"))
    note = " (cakupan sumber terbatas)" if day.get("data_quality") == "limited" else ""
    if p is None:
        return f"{loc}: peluang hujan belum tersedia."
    if not day.get("best_activity_window"):
        return f"{loc}: belum ada rentang aktivitas yang memenuhi batas risiko; pantau pembaruan. Peluang hujan per jam tertinggi {p:g}% sekitar {peak}{note}."
    return f"{loc}: peluang hujan per jam tertinggi {p:g}% sekitar {peak}. Rentang dengan risiko relatif lebih rendah: {best}. Tetap pantau kondisi setempat{note}."


def _lg_nowcast(day, args=None):
    now = now_local(getattr(args, "timezone", DEFAULT_TIMEZONE))
    future = [h for h in day.get("key_hours", []) if h.get("recommendation_eligible") and (dt := _quality_datetime(h.get("target_datetime"), str(getattr(now.tzinfo, "key", DEFAULT_TIMEZONE)))) and now <= dt <= now + timedelta(hours=6)]
    if not future:
        return {"window": "0–6 jam", "status": "Belum tersedia", "method": "model_forecast_subset", "summary": "Prakiraan model untuk enam jam berikutnya belum tersedia.", "peak_hour": "—", "peak_probability": None}
    peak = max(future, key=lambda h: h["rain_probability"])
    return {"window": "0–6 jam", "status": peak["risk_label"], "method": "model_forecast_subset",
            "summary": "Ringkasan model enam jam berikutnya. Belum menggunakan radar atau pengamatan nowcast.", "peak_hour": peak["hour"], "peak_probability": peak["rain_probability"]}


def _lg_microclimate(args):
    accepted = any(s.get("accepted") is True for n, scopes in ((_QUALITY_SKILL or {}).get("variables") or {}).items() if n.startswith("local_") for s in scopes.values())
    return {"profile": getattr(args, "terrain_class", "unknown"),
            "note": "Koreksi lokal diterapkan per variabel hanya jika kandidat diterima melalui evaluasi bergulir." if accepted else "Belum tersedia koreksi lokal yang diterima melalui evaluasi pengamatan.",
            "zone_count": 0, "engine": "validated_empirical_local_correction" if accepted else "awaiting_observations", "site_elevation_m": getattr(args, "site_elevation_m", None)}


def _lg_cloud_proxy(day):
    rains = [_lg_prob(h.get("rain_probability"), 0) for h in _lg_sort_hours(day.get("key_hours") or [])]
    trend = "stabil"
    if len(rains) >= 5:
        morning, afternoon, evening = max(rains[:4] or [0]), max(rains[4:8] or [0]), max(rains[8:] or [0])
        if max(afternoon, evening) - morning >= 20:
            trend = "meningkat setelah pemanasan siang"
        elif morning - max(afternoon, evening) >= 20:
            trend = "menurun setelah pagi"
    return {"method": "proxy dari peluang hujan per jam", "rain_signal_trend": trend,
            "summary": f"Sinyal hujan {trend}; tetap validasi dengan kondisi langit dan radar/BMKG."}


def _lg_source_court():
    rows = read_dict_csv(path_output(AETHER_SOURCE_STATE_FILENAME))
    if not rows:
        return {"status": "Belum tersedia", "agreement": "Belum diukur", "summary": "Status sumber belum tersedia. Ketersediaan API tidak membuktikan akurasi prakiraan.", "sources": []}
    sources = []
    for row in rows:
        sid = row.get("source_id") or row.get("source") or "UNKNOWN"
        cached = str(row.get("is_cached")).lower() in {"true", "1"}
        live = str(row.get("live_fetch_success")).lower() in {"true", "1"}
        sources.append({"source_id": sid, "verdict": "Cache" if cached else "Aktif" if live else "Gagal / tidak tersedia",
                        "points": _quality_first(row, "points_collected", "point_count", "points", default=0), "http_status": row.get("http_status", ""),
                        "latency_ms": row.get("duration_ms", ""), "model_family": _quality_family(sid), "is_cached": cached,
                        "original_fetched_at": row.get("original_fetched_at"), "cache_age_hours": safe_float(row.get("cache_age_hours"))})
    live_n, cached_n = sum(s["verdict"] == "Aktif" for s in sources), sum(s["is_cached"] for s in sources)
    return {"status": "Data tersedia" if live_n or cached_n else "Belum cukup data", "agreement": "Belum diukur",
            "summary": f"{live_n} sumber live; {cached_n} sumber cache. Status ini menunjukkan ketersediaan, bukan akurasi atau kesepakatan model.", "sources": sources}


def _lg_period_summary(hours, label, start, end):
    sel = [h for h in hours or [] if h.get("recommendation_eligible") and ((start <= _lg_hour_int(h.get("hour")) <= end) if start <= end else (_lg_hour_int(h.get("hour")) >= start or _lg_hour_int(h.get("hour")) <= end))]
    if not sel:
        return {"name": label, "condition": "Data belum memadai", "attention_hour": "—", "temp_c": None, "rain_probability": None, "risk_score": None, "risk_class": "unavailable", "risk_label": "Data belum memadai"}
    peak = max(sel, key=lambda h: h.get("risk_score") if h.get("risk_score") is not None else -1)
    return {"name": label, "condition": peak["condition"], "attention_hour": peak["hour"], "temp_c": _lg_average([h.get("temp_c") for h in sel]),
            "rain_probability": max(h["rain_probability"] for h in sel), "risk_score": peak["risk_score"], "risk_class": peak["risk_class"], "risk_label": peak["risk_label"]}


def _lg_activity_row(item, fallback_priority=""):
    base = {"activity": "Aktivitas", "status": "Dipantau", "advice": "Pantau kondisi lokal sebelum beraktivitas.", "priority_hour": fallback_priority or "—", "risk_class": "watch"}
    if isinstance(item, dict):
        normalized = {k: _lg_text(item.get(k), base[k]) for k in base}
        return {**item, **normalized}
    if isinstance(item, (list, tuple)):
        return {k: _lg_text(item[i] if len(item) > i else None, base[k]) for i, k in enumerate(base)}
    return {**base, "activity": _lg_text(item, "Aktivitas")}


def _lg_activity_rows(items, fallback_priority=""):
    return [_lg_activity_row(i, fallback_priority) for i in (items or [])]


def _lg_activity_matrix(day):
    return day.get("activity_matrix") or v61_activity_plans(day.get("key_hours") or [])


def _lg_norm_day(raw, idx, loc, args):
    raw = dict(raw or {})
    hours = [_lg_norm_hour(h, args, idx) for h in _lg_sort_hours(_lg_raw_hours(raw))]
    valid = [h for h in hours if h.get("recommendation_eligible")]
    complete = len(valid) == len(hours) and bool(hours)
    state = "usable" if complete and all(h.get("data_quality") == "usable" for h in hours) else "limited" if valid else "unavailable"
    peak_rain = max(valid, key=lambda h: h["rain_probability"], default={})
    peak_risk = max(valid, key=lambda h: h.get("risk_score") or 0, default={})
    now = now_local(getattr(args, "timezone", DEFAULT_TIMEZONE))
    date = raw.get("date")
    try:
        d = parse_iso_date(date)
        tag = {0: "Hari ini", 1: "Besok", 2: "Lusa"}.get((d - now.date()).days, format_date_id(d))
    except (ValueError, TypeError):
        tag = "Tanggal belum tersedia"
    day = {**raw, "day_tag": tag, "date_label": raw.get("date_label") or date or "—", "key_hours": hours,
           "condition": peak_risk.get("condition", "Data cuaca belum memadai"),
           "avg_temperature_c": _lg_average([h.get("temp_c") for h in valid]), "avg_humidity_pct": _lg_average([h.get("humidity_pct") for h in valid]),
           "max_heat_index_c": max((h["heat_index_c"] for h in valid if h.get("heat_index_c") is not None), default=None),
           "max_wind_kmh": max((h["wind_kmh"] for h in valid if h.get("wind_kmh") is not None), default=None),
           "peak_rain_probability": peak_rain.get("rain_probability"), "peak_rain_hour": peak_rain.get("hour", "—"),
           "rain_risk_score": peak_risk.get("risk_score"), "risk_score": peak_risk.get("risk_score"), "risk_class": _lg_class(peak_risk.get("risk_score")),
           "risk_label": _lg_label(peak_risk.get("risk_score")), "data_quality": state, "usable_hours": len(valid), "total_hours": len(hours),
           "best_activity_window": _lg_best_window(hours, now=now)}
    day["decision_sentence"] = _lg_decision_sentence(day, loc)
    day["nowcast"] = _lg_nowcast(day, args)
    day["cloud_motion_proxy"] = _lg_cloud_proxy(day)
    day["periods"] = [_lg_period_summary(hours, n, s, e) for n, s, e in (("Pagi", 5, 10), ("Siang", 11, 14), ("Sore", 15, 18), ("Malam", 19, 4))]
    day["activity_matrix"] = v61_activity_plans(hours, args)
    return day


def _lg_build_api(args, forecast_dates=None):
    dates = forecast_dates or _forecast_dates(args)
    days, issued = [], []
    last_payload = {}
    for d in dates:
        payload = read_json(path_output(f"sentinel_x_{d:%Y%m%d}.json"), {}) or {}
        if payload:
            last_payload = payload
        daily = payload.get("daily") or {}
        rows = [{**r, "date": r.get("target_date") or d.isoformat(), "hour": r.get("jam"), "temp_c": _quality_first(r, "temp_micro_p50", "temp_p50"),
                 "humidity_pct": _quality_first(r, "rh_micro_p50", "rh_p50"), "heat_index_c": r.get("heat_index_p50"), "rain_probability": r.get("prob_rain"),
                 "wind_kmh": r.get("wind_p50"), "rain_risk_score": r.get("rain_threat_score"), "condition": r.get("dominant_category"),
                 "cloud_cover_pct": r.get("cloud_p50")} for r in payload.get("hourly") or []]
        day = _lg_norm_day({"date": d.isoformat(), "date_label": format_date_id(d), "hours": rows, "issued_at": payload.get("issued_at") or daily.get("issued_at")}, len(days), args.location_name, args)
        days.append(day)
        dt = _quality_datetime(day.get("issued_at"), args.timezone)
        if dt:
            issued.append(dt)
    stamp = max(issued) if issued else None
    return {"brand": LANGIT_BRAND_NAME, "product": LANGIT_PRODUCT_NAME, "version": APP_VERSION, "ui_version": SENTINEL_PUBLIC_UI_VERSION,
            "schema_version": QUALITY_SCHEMA, "location_name": args.location_name, "location_slug": sanitize_filename(getattr(args, "location_slug", "") or args.location_name.lower()),
            "generated_at": stamp.isoformat() if stamp else None, "updated_label": pretty_dt(stamp) if stamp else "Waktu penerbitan belum tersedia",
            "rendered_at": now_local(args.timezone).isoformat(), "days": days, "microclimate": _lg_microclimate(args), "source_court": _lg_source_court(),
            "source_states": last_payload.get("source_states") or [], "map_layers": ["Model risk estimates", "Time selector"], "probability_unit": "percent",
            "source_availability": read_json(path_output("source_availability.json"), {}),
            "ensemble_status": {k: v for k, v in (read_json(path_output("ensemble_member_status.json"), {}) or {}).items() if k != "hourly"},
            "scientific_status": "Awaiting local observation verification"}


def _lg_today(api):
    days = api.get("days") or []
    return days[0] if days else {}


def _lg_css():
    return r"""
:root{--bg:#07101f;--panel:rgba(12,27,48,.76);--panel2:rgba(255,255,255,.07);--ink:#f5f8ff;--muted:#9fb3d1;--line:rgba(170,203,255,.18);--safe:#22c55e;--watch:#f59e0b;--rain:#fb7185;--danger:#e11d48;--shadow:0 30px 80px rgba(0,0,0,.32)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.45;background:radial-gradient(circle at 12% -8%,rgba(34,211,238,.28),transparent 380px),radial-gradient(circle at 84% 8%,rgba(59,130,246,.34),transparent 440px),linear-gradient(180deg,#06101e 0,#0b1729 36%,#0e2035 100%)}
a{text-decoration:none;color:inherit}.wrap{width:min(1180px,calc(100% - 44px));margin:auto}.topbar{position:sticky;top:0;z-index:60;background:rgba(5,14,27,.78);backdrop-filter:blur(22px);border-bottom:1px solid var(--line)}.top{min-height:72px;display:flex;align-items:center;justify-content:space-between;gap:18px}.brand{display:flex;align-items:center;gap:12px}.mark{width:42px;height:42px;border-radius:16px;background:conic-gradient(from 220deg,#03142c,#1d4ed8,#38bdf8,#16a34a,#03142c);box-shadow:0 18px 42px rgba(37,99,235,.45)}.brand b{font-size:17px}.brand small{display:block;color:var(--muted);font-size:11px}.nav{display:flex;gap:9px;flex-wrap:wrap}.btn{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:8px 13px;border:1px solid rgba(147,197,253,.28);background:rgba(255,255,255,.06);border-radius:999px;color:#dbeafe;font-size:12px;font-weight:850}.btn.active,.btn.primary{background:linear-gradient(135deg,#2563eb,#06b6d4);color:#fff;border-color:transparent}
.hero{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:18px;margin:28px 0 0}.hero-main{position:relative;overflow:hidden;min-height:226px;border-radius:34px;padding:34px 38px;background:linear-gradient(135deg,rgba(13,34,66,.98),rgba(19,72,147,.98) 58%,rgba(14,165,233,.92));box-shadow:var(--shadow);border:1px solid rgba(255,255,255,.16)}.chips{position:relative;z-index:2;display:flex;gap:8px;flex-wrap:wrap}.chip{padding:6px 11px;border:1px solid rgba(255,255,255,.26);background:rgba(255,255,255,.11);border-radius:999px;font-size:10px;font-weight:950;text-transform:uppercase}.hero h1{position:relative;z-index:2;max-width:820px;margin:22px 0 11px;font-size:clamp(34px,4.6vw,62px);line-height:.95;letter-spacing:-.06em}.hero p{position:relative;z-index:2;max-width:760px;margin:0;color:#eaf4ff;font-size:16px}
.weather-tile,.panel,.kpi,.card,.day,.period,.activity,.hour{border:1px solid var(--line);background:var(--panel);backdrop-filter:blur(18px);box-shadow:var(--shadow)}.weather-tile{border-radius:28px;padding:22px;display:grid;align-content:center;min-height:142px}.weather-tile .wx{font-size:36px}.weather-tile .temp{font-size:44px;font-weight:950;letter-spacing:-.06em}.weather-tile small,.muted{color:var(--muted)}.side-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.notice{margin:14px 0 18px;border:1px solid rgba(245,158,11,.34);background:rgba(245,158,11,.11);color:#fde68a;border-radius:16px;padding:10px 14px;font-size:12px;font-weight:850}.panel{border-radius:26px;padding:22px;margin:18px 0}.head{display:flex;align-items:end;justify-content:space-between;gap:18px;margin-bottom:14px}.head h2{margin:0;font-size:23px}.head p{margin:0;color:var(--muted);font-size:12px}.grid{display:grid;gap:14px}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.g4{grid-template-columns:repeat(4,minmax(0,1fr))}
.decision{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr);gap:16px;margin:18px 0}.decision-card{border-radius:30px;padding:28px 30px;background:linear-gradient(135deg,rgba(4,18,38,.94),rgba(6,32,67,.92));border:1px solid rgba(125,211,252,.18);box-shadow:var(--shadow)}.decision-card h2{margin:12px 0 10px;font-size:clamp(26px,3.2vw,46px);line-height:1;letter-spacing:-.05em}.decision-card p{color:#cfe7ff}.badge{display:inline-flex;border-radius:999px;padding:6px 10px;font-size:11px;font-weight:950;border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.08)}.badge.safe{color:#bbf7d0}.badge.watch{color:#fde68a}.badge.rain,.badge.danger{color:#fecdd3}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:12px}.kpi{border-radius:22px;padding:16px;background:var(--panel2)}.kpi span,.focus{display:block;color:#93c5fd;text-transform:uppercase;letter-spacing:.1em;font-size:10px;font-weight:950}.kpi b{display:block;margin-top:8px;font-size:23px;letter-spacing:-.04em;overflow-wrap:anywhere}.kpi small{color:var(--muted)}
.day,.period,.activity,.card{border-radius:22px;padding:18px;background:rgba(255,255,255,.055)}.day{border-top:4px solid var(--safe)}.day.watch,.period.watch,.activity.watch,.hour.watch{border-color:rgba(245,158,11,.55)}.day.rain,.period.rain,.activity.rain,.hour.rain{border-color:rgba(251,113,133,.65)}.day.danger,.period.danger,.activity.danger,.hour.danger{border-color:rgba(225,29,72,.72)}.day h3,.period h3,.activity h3{margin:8px 0;font-size:21px}.day p,.activity p,.period p{color:#cbdcf3;font-size:13px}.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:13px}.mini span,.hbox{background:rgba(15,39,68,.72);border:1px solid var(--line);border-radius:14px;padding:10px}.mini span{color:var(--muted);font-size:10px}.mini b{display:block;color:var(--ink);font-size:15px;margin-top:4px}.hour{display:grid;grid-template-columns:78px minmax(220px,1fr) repeat(5,minmax(88px,110px));gap:10px;align-items:center;border-radius:20px;padding:12px;margin:9px 0;border-left:4px solid var(--safe);background:rgba(255,255,255,.055)}.time{font-size:17px;font-weight:950}.cond b{display:block}.cond small{display:block;color:var(--muted);font-size:11px}.hbox b{display:block}.hbox small{color:var(--muted);font-size:10px}.mapbox{height:430px;border-radius:24px;overflow:hidden;border:1px solid var(--line);background:#0b1729}.mapbox iframe{width:100%;height:100%;border:0}
.rainchart{height:190px;display:flex;align-items:end;gap:10px}.rainchart div{flex:1;text-align:center}.rainchart b{font-size:11px}.bar{display:block;border-radius:12px 12px 4px 4px;margin:5px 0;background:linear-gradient(#34d399,#059669)}.bar.watch{background:linear-gradient(#fbbf24,#f59e0b)}.bar.rain{background:linear-gradient(#fb923c,#fb7185)}.bar.danger{background:linear-gradient(#fb7185,#e11d48)}.rainchart small{font-size:10px;color:var(--muted)}.share textarea{width:100%;min-height:150px;border:1px solid var(--line);border-radius:16px;background:#06101e;color:#dbeafe;padding:14px}table{width:100%;border-collapse:collapse}.table-wrap{overflow:auto}th,td{border-bottom:1px solid var(--line);padding:10px;text-align:left;font-size:12px}th{color:#93c5fd;text-transform:uppercase}.footer{text-align:center;color:var(--muted);font-size:12px;margin:28px 0 48px}.empty{border:1px dashed rgba(147,197,253,.38);padding:22px;border-radius:20px;color:#cbdcf3}.progress{height:12px;background:rgba(255,255,255,.12);border-radius:99px;overflow:hidden}.progress i{display:block;height:100%;background:linear-gradient(90deg,#2563eb,#06b6d4)}
@media(max-width:980px){.hero,.decision{grid-template-columns:1fr}.g4,.g3,.g2{grid-template-columns:1fr}.hour{grid-template-columns:70px minmax(160px,1fr) repeat(2,1fr)}.hour .hbox:nth-of-type(n+4){display:none}.wrap{width:min(100% - 24px,1180px)}}
@media(max-width:560px){.top{align-items:flex-start;flex-direction:column;padding:12px 0}.hero h1{font-size:36px}.hero-main{padding:28px 24px}.metrics{grid-template-columns:1fr}.hour{grid-template-columns:1fr}}
"""


def _lg_kpi(label, value, note="", cls=""):
    return (f"<article class='kpi {html.escape(str(cls or ''), quote=True)}'><span>{_lg_esc(label)}</span><b>{_lg_esc(_lg_clean_join(value))}</b><small>{_lg_esc(note)}</small></article>")


def _lg_topbar(api, args, active="today"):
    loc = _lg_text(api.get("location_name"), getattr(args, "location_name", "Lokasi"))
    links = [("Hari ini", "langit_app.html", "today"), ("3 hari", "langit_3day.html", "3day"), ("Aktivitas", "langit_activity.html", "activity"),
             ("Peta", "langit_map_room.html", "map"), ("Model", "keandalan_data.html", "court"), ("Akurasi", "akurasi_data.html", "accuracy"), ("Lokasi", "../index.html", "location")]
    nav = "".join(f"<a class='btn {'active' if k == active else ''}' href='{h}'>{l}</a>" for l, h, k in links)
    return (f"<header class='topbar'><div class='top wrap'><a class='brand' href='langit_app.html'><span class='mark'></span><span><b>{LANGIT_BRAND_NAME}</b>"
            f"<small>{_lg_esc(loc)} · {APP_VERSION}</small></span></a><nav class='nav'>{nav}</nav></div></header>")


def _lg_hero(api, args, page="today"):
    d = _lg_today(api)
    loc = _lg_text(api.get("location_name"), getattr(args, "location_name", "Lokasi"))
    cond = _lg_text(d.get("condition"), "Cuaca lokal")
    updated = _lg_text(api.get("updated_label") or api.get("generated_at"), "baru diperbarui")
    titles = {"today": f"Prakiraan {loc}", "3day": "Prakiraan 3 hari", "activity": "Saran aktivitas", "map": "Map Room", "court": "Model Court", "accuracy": "Status akurasi", "portal": "Cuaca lokal yang langsung bisa dipakai"}
    subs = {"today": d.get("decision_sentence"), "3day": f"Bandingkan risiko {loc}: hari ini, besok, dan lusa.",
            "activity": f"Rekomendasi praktis untuk motor, jalan kaki, jemur, olahraga, outdoor, dan foto di {loc}.",
            "map": "Peta lokasi dan estimasi risiko model per jam.", "court": "Ringkasan sumber data, latensi, dan sinyal model.",
            "accuracy": "Akurasi hanya dibaca setelah pasangan prakiraan dan observasi cukup.", "portal": "Pilih lokasi, lihat ringkasan risiko, peta, dan data publik."}
    side = ""
    if page != "portal":
        side = (f"<aside><div class='weather-tile'><div class='wx'>{_lg_icon(cond)}</div><div class='temp'>{_lg_num(d.get('avg_temperature_c'), '°C', 1)}</div><small>{_lg_esc(cond)}</small></div>"
                f"<div class='side-grid'>{_lg_kpi('Hujan', _lg_pct(d.get('peak_rain_probability')), 'puncak')}{_lg_kpi('Status', _lg_text(d.get('risk_label'), 'Dipantau'), 'ringkasan')}</div></aside>")
    return (f"<section class='hero'><article class='hero-main'><div class='chips'><span class='chip'>{LANGIT_BRAND_NAME}</span><span class='chip'>{APP_VERSION}</span>"
            f"<span class='chip'>Diperbarui {_lg_esc(updated)}</span></div><h1>{_lg_esc(titles.get(page, titles['today']))}</h1><p>{_lg_esc(_lg_text(subs.get(page), LANGIT_TAGLINE))}</p></article>{side}</section>"
            f"<div class='notice'><b>{LANGIT_DISCLAIMER}</b></div>")


def _lg_decision(api):
    d = _lg_today(api)
    cls = _lg_text(d.get("risk_class"), "watch")
    acc = str((read_json(path_output("sentinel_x_verification_summary.json"), {}) or {}).get("matched_cases", 0))
    return (f"<section class='decision'><article class='decision-card'><span class='badge {cls}'>{_lg_esc(d.get('risk_label'))}</span><h2>{_lg_esc(d.get('decision_sentence'))}</h2>"
            f"<p>Nowcast: {_lg_esc((d.get('nowcast') or {}).get('summary', 'Pantau kondisi sekitar.'))}</p></article><div class='metrics'>"
            f"{_lg_kpi('Risk score', _lg_num(d.get('risk_score'), '/100', 0), d.get('risk_label'), cls)}"
            f"{_lg_kpi('Puncak hujan', _lg_pct(d.get('peak_rain_probability')), 'sekitar ' + _lg_hour(d.get('peak_rain_hour')), cls)}"
            f"{_lg_kpi('Window aman', _lg_best_window_text(d.get('best_activity_window')), 'aktivitas')}{_lg_kpi('Akurasi', acc, 'pasangan data')}</div></section>")


def _lg_day_cards(days):
    cards = []
    for d in (days or [])[:3]:
        cls = _lg_text(d.get("risk_class"), _lg_class(d.get("risk_score")))
        cards.append(f"<article class='day {cls}'><span class='focus'>{_lg_esc(d.get('day_tag'))} · {_lg_esc(d.get('date_label') or d.get('date'))}</span><h3>{_lg_esc(d.get('risk_label'))}</h3>"
                     f"<p>{_lg_esc(d.get('decision_sentence'))}</p><div class='mini'><span>Hujan<b>{_lg_pct(d.get('peak_rain_probability'))}</b></span>"
                     f"<span>Jam<b>{_lg_hour(d.get('peak_rain_hour'))}</b></span><span>Window<b>{_lg_esc(_lg_best_window_text(d.get('best_activity_window')))}</b></span></div></article>")
    return "".join(cards) or "<p class='muted'>Data 3 hari belum tersedia.</p>"


def _lg_metric_strip(day):
    return ("<section class='panel'><div class='head'><h2>Kondisi utama</h2><p>Variabel untuk keputusan harian.</p></div><div class='grid g4'>"
            f"{_lg_kpi('Suhu', _lg_num(day.get('avg_temperature_c'), '°C', 1), 'rata-rata')}{_lg_kpi('Terasa', _lg_num(day.get('max_heat_index_c'), '°C', 1), 'heat index')}"
            f"{_lg_kpi('RH', _lg_pct(day.get('avg_humidity_pct')), 'kelembapan')}{_lg_kpi('Angin', _lg_num(day.get('max_wind_kmh'), ' km/jam', 1), 'maksimum')}</div></section>")


def _lg_periods(day):
    cards = []
    for p in day.get("periods", []):
        cls = _lg_text(p.get("risk_class"), "safe")
        cards.append(f"<article class='period {cls}'><h3>{_lg_icon(p.get('condition'))} {_lg_esc(p.get('name'))}</h3><b>{_lg_esc(p.get('condition'))}</b>"
                     f"<p class='muted'>Jam perhatian: {_lg_hour(p.get('attention_hour'))}</p><div class='mini'><span>Suhu<b>{_lg_num(p.get('temp_c'), '°C', 1)}</b></span>"
                     f"<span>Hujan<b>{_lg_pct(p.get('rain_probability'))}</b></span><span>Risiko<b>{_lg_esc(p.get('risk_label'))}</b></span></div></article>")
    return f"<section class='panel'><div class='head'><h2>Pagi, siang, sore, malam</h2><p>Ringkasan cepat.</p></div><div class='grid g4'>{''.join(cards)}</div></section>"


def _lg_activity_section(day):
    rows = _lg_activity_rows((day or {}).get("activity_matrix") or _lg_activity_matrix(day), fallback_priority=_lg_hour((day or {}).get("peak_rain_hour"), "—"))
    cards = [f"<article class='activity {_lg_text(a.get('risk_class'), 'safe')}'><h3>{_lg_esc(a.get('activity'))}</h3><b>{_lg_esc(a.get('status'))}</b><p>{_lg_esc(a.get('advice'))}</p>"
             f"<span class='focus'>Fokus: {_lg_esc(a.get('priority_hour'))}</span></article>" for a in rows]
    return f"<section class='panel'><div class='head'><h2>Saran aktivitas</h2><p>Berbasis ambang yang dapat diubah; bukan jaminan keselamatan.</p></div><div class='grid g3'>{''.join(cards)}</div></section>"


def _lg_hours_section(day, title="Jam penting", risky_only=False):
    hours = day.get("key_hours", [])
    if risky_only:
        risky = [h for h in hours if (_lg_float(h.get("risk_score"), 0) or 0) >= 25 or _lg_prob(h.get("rain_probability"), 0) >= 25]
        hours = risky or sorted(hours, key=lambda h: _lg_float(h.get("risk_score"), 0) or 0, reverse=True)[:4]
    rows = []
    for h in hours:
        cls = _lg_text(h.get("risk_class"), "safe")
        rows.append(f"<article class='hour {cls}'><div class='time'>{_lg_hour(h.get('hour'))}</div><div class='cond'><b>{_lg_icon(h.get('condition'))} {_lg_esc(h.get('condition'))}</b>"
                    f"<small>{_lg_esc(h.get('advice'))}</small></div><div class='hbox'><b>{_lg_num(h.get('temp_c'), '°C', 1)}</b><small>Suhu</small></div>"
                    f"<div class='hbox'><b>{_lg_pct(h.get('humidity_pct'))}</b><small>RH</small></div><div class='hbox'><b>{_lg_num(h.get('heat_index_c'), '°C', 1)}</b><small>Terasa</small></div>"
                    f"<div class='hbox'><b>{_lg_pct(h.get('rain_probability'))}</b><small>Hujan</small></div><div class='hbox'><b>{_lg_esc(h.get('risk_label'))}</b><small>Risiko</small></div></article>")
    return (f"<section class='panel'><div class='head'><h2>{_lg_esc(title)}</h2><p>{'Dipilih dari jam utama dan jam rawan.' if risky_only else 'Kondisi, angka, dan risiko dipisah agar tidak menumpuk.'}</p></div>"
            f"{''.join(rows) or '<p class=muted>Data jam belum tersedia.</p>'}</section>")


def _lg_rain_chart(day):
    bars = []
    for h in day.get("key_hours", []):
        p = _lg_prob(h.get("rain_probability"), 0)
        cls = _lg_text(h.get("risk_class"), _lg_class(h.get("risk_score")))
        bars.append(f"<div><b>{round(p)}%</b><span class='bar {cls}' style='height:{max(4, p)}px'></span><small>{_lg_hour(h.get('hour'))}</small></div>")
    return f"<section class='panel'><div class='head'><h2>Peluang hujan</h2><p>Warna menunjukkan jam yang perlu diperhatikan.</p></div><div class='rainchart'>{''.join(bars)}</div></section>"


def _lg_share_text(api):
    d = _lg_today(api)
    return (f"{LANGIT_BRAND_NAME} · {_lg_text(api.get('location_name'), 'Lokasi')}\n{_lg_text(d.get('date_label'), _lg_text(d.get('date')))}\n{_lg_text(d.get('decision_sentence'))}\n"
            f"Peluang hujan tertinggi: {_lg_pct(d.get('peak_rain_probability'))} sekitar {_lg_hour(d.get('peak_rain_hour'))}.\nBukan peringatan resmi; untuk cuaca ekstrem ikuti BMKG.")


def _lg_share(api):
    return (f"<section class='grid g2'><article class='panel share'><div class='head'><h2>Share singkat</h2><p>Teks siap disalin.</p></div><textarea readonly>{_lg_esc(_lg_share_text(api))}</textarea></article>"
            "<article class='panel'><h2>Catatan penggunaan</h2><ul><li>Prakiraan ini panduan aktivitas, bukan peringatan resmi.</li><li>Hujan lokal bisa bergeser beberapa kilometer atau berubah beberapa jam.</li>"
            "<li>Untuk cuaca ekstrem, ikuti informasi BMKG dan kondisi setempat.</li></ul></article></section>")


def _lg_intel_sections(api):
    mc, sc = api.get("microclimate") or {}, api.get("source_court") or {}
    rows = "".join(f"<tr><td>{_lg_esc(s.get('source_id'))}</td><td>{_lg_esc(s.get('verdict'))}</td><td>{_lg_esc(s.get('points'))}</td><td>{_lg_esc(s.get('http_status'))}</td><td>{_lg_esc(s.get('latency_ms'))}</td></tr>"
                   for s in sc.get("sources", [])) or "<tr><td colspan='5'>Status sumber belum tersedia.</td></tr>"
    return ("<section class='panel'><div class='head'><h2>Weather Brain</h2><p>Nowcast, microclimate, dan status sumber.</p></div>"
            f"<div class='grid g2'><article class='card'><span class='focus'>Microclimate</span><h3>{_lg_esc(mc.get('profile'))}</h3><p class='muted'>{_lg_esc(mc.get('note'))}</p></article>"
            f"<article class='card'><span class='focus'>Source court</span><h3>{_lg_esc(sc.get('status'))}</h3><p class='muted'>{_lg_esc(sc.get('summary'))}</p></article></div>"
            f"<div class='table-wrap' style='margin-top:16px'><table><thead><tr><th>Model</th><th>Putusan</th><th>Point</th><th>HTTP</th><th>ms</th></tr></thead><tbody>{rows}</tbody></table></div></section>")


def _lg_geojson_center_feature(api, args):
    day = _lg_today(api)
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [args.longitude, args.latitude]},
            "properties": {"layer_type": "center", "brand": LANGIT_BRAND_NAME, "location_name": api.get("location_name") or getattr(args, "location_name", ""),
                           "risk_score": day.get("risk_score"), "risk_class": day.get("risk_class", "unavailable"), "status": day.get("risk_label", "Data belum memadai"),
                           "rain_probability": day.get("peak_rain_probability"), "peak_hour": day.get("peak_rain_hour"), "temperature_c": day.get("avg_temperature_c"),
                           "decision": day.get("decision_sentence"), "data_quality": day.get("data_quality", "unavailable")}}


def _lg_geojson_layers(api, args):
    # Hanya estimasi model di titik lokasi; offset tak terukur tidak boleh menghasilkan risiko geografis.
    return {"type": "FeatureCollection", "features": [_lg_geojson_center_feature(api, args)]}


def _lg_leaflet_html(title, geo, back="langit_app.html"):
    if not (isinstance(geo, dict) and geo.get("type") == "FeatureCollection"):
        geo = {"type": "FeatureCollection", "features": geo or []}
    data = json.dumps(_quality_jsonable(geo), ensure_ascii=False).replace("</", "<\\/")
    template = """<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>
html,body,#map{height:100%;margin:0}body{font-family:Inter,system-ui,Arial,sans-serif;background:#07101f;color:#eef6ff}.panel{position:absolute;z-index:999;left:18px;top:18px;max-width:390px;background:rgba(5,14,27,.82);backdrop-filter:blur(18px);border:1px solid rgba(147,197,253,.28);border-radius:24px;padding:18px}.panel h1{font-size:22px;margin:0 0 8px}.panel p{margin:0 0 12px;color:#b7c9e5;font-size:13px}.btn{display:inline-flex;background:linear-gradient(135deg,#2563eb,#06b6d4);color:#fff;text-decoration:none;border-radius:999px;padding:9px 14px;font-weight:850;font-size:12px}.legend{position:absolute;z-index:999;right:18px;bottom:18px;background:rgba(5,14,27,.82);border:1px solid rgba(147,197,253,.24);border-radius:18px;padding:12px 14px;font-size:12px}.dot{display:inline-block;width:9px;height:9px;border-radius:99px;margin-right:6px}@media(max-width:760px){.panel{left:10px;right:10px;max-width:none}.legend{display:none}}
</style></head><body><div id="map"></div><div class="panel"><h1>__TITLE__</h1><p>Estimasi risiko model di titik lokasi. Peta dibatasi Indonesia.</p><a class="btn" href="__BACK__">Kembali</a></div>
<div class="legend"><div><span class="dot" style="background:#22c55e"></span>Aman</div><div><span class="dot" style="background:#f59e0b"></span>Dipantau</div><div><span class="dot" style="background:#fb7185"></span>Waspada</div><div><span class="dot" style="background:#e11d48"></span>Risiko tinggi</div></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const data=__DATA__;const color=c=>({safe:'#22c55e',watch:'#f59e0b',rain:'#fb7185',danger:'#e11d48'}[c]||'#38bdf8');
const bounds=L.latLngBounds(L.latLng(-11.2,94.5),L.latLng(6.4,141.2));
const dark=L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:19,minZoom:5,noWrap:true,bounds:bounds,attribution:'© OpenStreetMap © CARTO'});
const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,minZoom:5,noWrap:true,bounds:bounds,attribution:'© OpenStreetMap contributors'});
const map=L.map('map',{worldCopyJump:false,maxBounds:bounds,maxBoundsViscosity:1.0,minZoom:5,layers:[dark]});
L.control.layers({'Dark':dark,'OSM':osm}).addTo(map);const layer=L.layerGroup().addTo(map);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
(data.features||[]).forEach(f=>{if(!f.geometry)return;const p=f.properties||{};const ll=[f.geometry.coordinates[1],f.geometry.coordinates[0]];
L.circleMarker(ll,{radius:16,color:color(p.risk_class),fillColor:color(p.risk_class),fillOpacity:.88,weight:3}).bindPopup('<b>'+esc(p.location_name)+'</b><br>Status: '+esc(p.status)+'<br>Risk score: '+esc(p.risk_score)+'/100<br>Hujan: '+esc(p.rain_probability)+'% sekitar '+esc(p.peak_hour)+'<br><small>'+esc(p.decision)+'</small>').addTo(layer);});
try{const g=L.featureGroup(layer.getLayers());map.fitBounds(g.getBounds().pad(.32),{maxZoom:12});}catch(e){map.setView([-2.5,118],5);}
</script></body></html>"""
    return template.replace("__TITLE__", _lg_esc(title)).replace("__BACK__", html.escape(back, quote=True)).replace("__DATA__", data)


def _lg_write_maps(api, args):
    geo = _lg_geojson_layers(api, args)
    write_json(path_output("langit_location.geojson"), geo)
    write_json(path_output("langit_map_layers.json"), {"brand": LANGIT_BRAND_NAME, "version": APP_VERSION, "layers": api.get("map_layers"), "geojson": geo, "bounds": "Indonesia"})
    doc = _lg_leaflet_html(f"{LANGIT_BRAND_NAME} Map Room — {_lg_text(api.get('location_name'))}", geo)
    atomic_write_text(path_output("langit_map_room.html"), lambda f: f.write(doc))


def _lg_map_section():
    return ("<section class='panel'><div class='head'><h2>Map Room</h2><p>Estimasi risiko model dan batas Indonesia.</p></div><div class='mapbox'><iframe src='langit_map_room.html' loading='lazy'></iframe></div>"
            "<div class='nav' style='margin-top:14px'><a class='btn primary' href='langit_map_room.html'>Buka peta penuh</a><a class='btn' href='langit_location.geojson'>GeoJSON lokasi</a></div></section>")


def _lg_doc(api, args, active, page, body):
    return (f"<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{LANGIT_BRAND_NAME} — {_lg_esc(api.get('location_name'))}</title><meta name='theme-color' content='#07101f'><style>{_lg_css()}</style></head>"
            f"<body>{_lg_topbar(api, args, active)}<main class='wrap'>{_lg_hero(api, args, page)}{body}<p class='footer'>{LANGIT_BRAND_NAME} · {APP_VERSION}</p></main></body></html>")


def _lg_page(api, args, page="today"):
    d, days = _lg_today(api), api.get("days") or []
    if page == "3day":
        body = f"<section class='panel'><div class='head'><h2>Ringkasan 3 hari</h2><p>Bandingkan risiko utama.</p></div><div class='grid g3'>{_lg_day_cards(days)}</div></section>"
        for item in days[:3]:
            body += _lg_periods(item) + _lg_hours_section(item, f"Detail jam · {_lg_text(item.get('day_tag'))}")
        return _lg_doc(api, args, "3day", "3day", body)
    if page == "activity":
        return _lg_doc(api, args, "activity", "activity", _lg_decision(api) + _lg_activity_section(d) + _lg_hours_section(d, "Jam rawan untuk aktivitas", True) + _lg_share(api))
    if page == "map":
        return _lg_doc(api, args, "map", "map", _lg_map_section() + _lg_rain_chart(d) + _lg_metric_strip(d) + _lg_activity_section(d))
    if page == "court":
        return _lg_doc(api, args, "court", "court", _lg_intel_sections(api) + _lg_map_section() + _lg_share(api) + _lg_hours_section(d, "Jam penting"))
    body = (_lg_decision(api) + f"<section class='panel'><div class='head'><h2>Ringkasan 3 hari</h2><p>Fokus ke keputusan utama.</p></div><div class='grid g3'>{_lg_day_cards(days)}</div></section>"
            + _lg_map_section() + _lg_rain_chart(d) + _lg_metric_strip(d) + _lg_periods(d) + _lg_activity_section(d) + _lg_intel_sections(api) + _lg_share(api) + _lg_hours_section(d, "Jam penting"))
    return _lg_doc(api, args, "today", "today", body)


def _lg_planner(api, args):
    d = _lg_today(api)
    return _lg_doc(api, args, "activity", "activity", _lg_decision(api) + _lg_activity_section(d) + _lg_hours_section(d, "Jam untuk planner"))


def _lg_accuracy_html(summary, args):
    matched = int(_lg_float(summary.get("matched_cases"), 0) or 0)
    target = max(1, int(getattr(args, "verification_min_cases", 30) or 30))
    pct = min(100, round(matched / target * 100))
    reliability = summary.get("reliability_bins") or []
    if matched < target:
        rel_body = "<div class='empty'><b>Tabel reliabilitas disembunyikan sementara.</b><br>Belum cukup pasangan data. Setelah minimal 30 pasangan, LANGIT menampilkan error suhu, Brier score, hit rate, false alarm, dan reliabilitas.</div>"
    else:
        rel_rows = "".join(f"<tr><td>{_lg_esc(r.get('probability_bin'))}</td><td>{_lg_esc(r.get('n'))}</td><td>{_lg_esc(r.get('mean_forecast_probability'))}</td><td>{_lg_esc(r.get('observed_rain_frequency'))}</td></tr>" for r in reliability)
        rel_body = f"<div class='table-wrap'><table><thead><tr><th>Kelompok peluang</th><th>Kasus</th><th>Rata-rata prakiraan</th><th>Hujan terjadi</th></tr></thead><tbody>{rel_rows}</tbody></table></div>"
    body = (f"<section class='decision'><article class='decision-card'><span class='badge {'safe' if matched >= target else 'watch'}'>Evaluasi</span><h2>{'Akurasi mulai bisa dibaca.' if matched >= target else 'Akurasi belum aktif.'}</h2>"
            f"<p>{'Jumlah pasangan data sudah melewati batas awal; ini belum berarti terkalibrasi.' if matched >= target else 'LANGIT masih mengumpulkan pasangan prakiraan dan observasi. Sebelum cukup, halaman ini tidak mengklaim akurasi.'}</p>"
            f"<div class='progress'><i style='width:{pct}%'></i></div></article><div class='metrics'>{_lg_kpi('Pasangan data', f'{matched}/{target}', 'prakiraan-observasi')}{_lg_kpi('Progress', f'{pct}%', 'menuju minimum')}"
            f"{_lg_kpi('Error suhu', _lg_num(summary.get('temperature_mae_c'), '°C', 1), 'aktif setelah cukup')}{_lg_kpi('Skor hujan', _lg_num(summary.get('rain_brier_score'), '', 3), 'aktif setelah cukup')}</div></section>"
            f"<section class='panel'><div class='head'><h2>Bukti peluang hujan</h2><p>Ditampilkan penuh setelah data cukup.</p></div>{rel_body}</section>")
    dummy = {"location_name": getattr(args, "location_name", "Lokasi"), "days": [], "updated_label": pretty_dt(_quality_datetime(summary.get("generated_at"), args.timezone))}
    return _lg_doc(dummy, args, "accuracy", "accuracy", body)


def _lg_quality_gate(api):
    checks = []
    for p in ("langit_app.html", "langit_3day.html", "langit_activity.html", "keandalan_data.html", "langit_map_room.html"):
        path = path_output(p)
        ok, detail = os.path.exists(path), path
        if ok:
            try:
                text = open(path, "r", encoding="utf-8").read()
                if p != "langit_map_room.html" and ("<br>" in text or "&lt;br" in text):  # peta memakai <br> sah di string JS
                    ok, detail = False, "raw_br_detected"
            except OSError as exc:
                ok, detail = False, str(exc)
        checks.append({"check": f"page_{p}", "ok": bool(ok), "detail": detail})
    for d in api.get("days", []):
        conds = [h.get("condition") for h in d.get("key_hours", [])]
        checks.append({"check": f"condition_variety_{d.get('day_tag')}", "ok": len(set(conds)) >= min(3, max(1, len(conds))), "detail": f"unique_conditions={len(set(conds))}"})
    payload = {"brand": LANGIT_BRAND_NAME, "version": APP_VERSION, "generated_at": now_local(DEFAULT_TIMEZONE).isoformat(), "checks": checks, "passed": sum(c["ok"] for c in checks), "total": len(checks)}
    write_json(path_output("langit_quality_gate.json"), payload)
    return payload


def _lg_write_outputs(args, forecast_dates=None):
    api = _lg_build_api(args, forecast_dates)
    for d in api.get("days", []):
        d["activity_matrix"] = _lg_activity_rows(d.get("activity_matrix") or _lg_activity_matrix(d), fallback_priority=_lg_hour(d.get("peak_rain_hour"), "—"))
    _lg_write_maps(api, args)
    write_json(path_output("langit_api_v1.json"), api)
    write_json(path_output("langit_intelligence.json"), {"brand": LANGIT_BRAND_NAME, "version": APP_VERSION, "nowcast": _lg_today(api).get("nowcast"),
                                                          "microclimate": api.get("microclimate"), "source_court": api.get("source_court"), "map_layers": api.get("map_layers")})
    daily_rows, hourly_rows, activity_rows = [], [], []
    for d in api.get("days", []):
        daily_rows.append({"date": d.get("date"), "day_tag": d.get("day_tag"), "risk_score": d.get("risk_score"), "risk_label": d.get("risk_label"),
                           "peak_rain_probability": d.get("peak_rain_probability"), "peak_rain_hour": d.get("peak_rain_hour"),
                           "best_activity_window": _lg_best_window_text(d.get("best_activity_window")), "summary": d.get("decision_sentence")})
        for h in d.get("key_hours", []):
            hourly_rows.append({"date": d.get("date"), "day_tag": d.get("day_tag"), **{k: h.get(k) for k in ("hour", "condition", "temp_c", "humidity_pct", "heat_index_c", "rain_probability", "wind_kmh", "risk_score", "risk_label", "risk_class")}})
        for a in d.get("activity_matrix", []):
            activity_rows.append({"date": d.get("date"), "day_tag": d.get("day_tag"), **a})
    write_dict_csv(path_output("langit_daily_outlook.csv"), ["date", "day_tag", "risk_score", "risk_label", "peak_rain_probability", "peak_rain_hour", "best_activity_window", "summary"], daily_rows)
    write_dict_csv(path_output("langit_hourly_intelligence.csv"), ["date", "day_tag", "hour", "condition", "temp_c", "humidity_pct", "heat_index_c", "rain_probability", "wind_kmh", "risk_score", "risk_label", "risk_class"], hourly_rows)
    write_dict_csv(path_output("langit_activity_matrix.csv"), ["date", "day_tag", "activity", "status", "advice", "priority_hour", "risk_class"], activity_rows)
    today_doc = _lg_page(api, args, "today")
    pages = {"langit_app.html": today_doc, AETHER_DASHBOARD_FILENAME: today_doc, AETHER_REPORT_HTML_FILENAME: today_doc, "anemos_app.html": today_doc,
             "langit_3day.html": _lg_page(api, args, "3day"), "langit_activity.html": _lg_page(api, args, "activity"),
             "keandalan_data.html": _lg_page(api, args, "court"), "langit_planner.html": _lg_planner(api, args)}
    for name, doc in pages.items():
        atomic_write_text(path_output(name), lambda f, doc=doc: f.write(doc))
    d0 = _lg_today(api)
    md = [f"# Ringkasan cuaca — {api.get('location_name')}", "", f"Diperbarui: {api.get('updated_label')}", "", d0.get("decision_sentence", ""), "", f"> {LANGIT_DISCLAIMER}", "",
          "Versi HTML: `langit_app.html`."]
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write("\n".join(md)))
    atomic_write_text(path_output("langit_whatsapp_brief.txt"), lambda f: f.write(_lg_share_text(api)))
    manifest = {"brand": LANGIT_BRAND_NAME, "version": APP_VERSION, "generated_at": api.get("generated_at"), "location": api.get("location_name"),
                "files": list(pages) + ["langit_api_v1.json", "langit_location.geojson", "langit_map_layers.json", "langit_quality_gate.json"]}
    write_json(path_output("langit_manifest.json"), manifest)
    gate = _lg_quality_gate(api)
    return {"brand": LANGIT_BRAND_NAME, "version": APP_VERSION, "dashboard": "langit_app.html", "map": "langit_map_room.html", "quality_gate": gate, "days": len(api.get("days", []))}


def anemos_write_multiday_public_pages(args, forecast_dates=None, source_state_rows=None):
    return _lg_write_outputs(args, forecast_dates)


def _lg_portal_card(loc, base_url=""):
    slug = sanitize_filename(loc.slug)
    api = read_json(os.path.join(root_output_dir(), slug, "langit_api_v1.json"), {}) or {}
    d = _lg_today(api)
    prefix = f"{base_url}/{slug}/" if base_url else f"{slug}/"
    cls = _lg_text(d.get("risk_class"), "watch")
    summary = d.get("decision_sentence") or "Prakiraan belum tersedia untuk lokasi ini."
    return (f"<article class='day {cls}'><h3>{_lg_icon(d.get('condition'))} {_lg_esc(loc.location_name)}</h3><p>{_lg_esc(summary)}</p>"
            f"<div class='mini'><span>Hujan<b>{_lg_pct(d.get('peak_rain_probability'))}</b></span><span>Jam<b>{_lg_hour(d.get('peak_rain_hour'))}</b></span><span>Score<b>{_lg_num(d.get('risk_score'), '', 0)}</b></span></div>"
            f"<div class='nav' style='margin-top:14px'><a class='btn primary' href='{prefix}langit_app.html'>Buka</a><a class='btn' href='{prefix}langit_3day.html'>3 hari</a>"
            f"<a class='btn' href='{prefix}langit_activity.html'>Aktivitas</a><a class='btn' href='{prefix}langit_map_room.html'>Peta</a></div></article>")


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    now = now_local(getattr(args, "timezone", DEFAULT_TIMEZONE))
    updated = pretty_dt(now)
    features = []
    for loc in locations:
        slug = sanitize_filename(loc.slug)
        api = read_json(os.path.join(root_output_dir(), slug, "langit_api_v1.json"), {}) or {}
        fake = type("A", (), {"location_name": loc.location_name, "latitude": loc.latitude, "longitude": loc.longitude})()
        features.append(_lg_geojson_center_feature(api or {"location_name": loc.location_name, "days": []}, fake))
    cards = "".join(_lg_portal_card(loc, base_url) for loc in locations)
    atomic_write_text(root_output_path("langit_portal_map.html"), lambda f: f.write(_lg_leaflet_html(f"{LANGIT_BRAND_NAME} Portal Map", {"type": "FeatureCollection", "features": features}, "index.html")))
    write_json(root_output_path("langit_all_locations.geojson"), {"type": "FeatureCollection", "features": features})
    hero = _lg_hero({"location_name": "Portal", "days": [{}], "updated_label": updated}, args, "portal")
    doc = (f"<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{LANGIT_BRAND_NAME} Portal</title>"
           f"<meta name='theme-color' content='#07101f'><style>{_lg_css()}</style></head><body><main class='wrap'>{hero}"
           f"<section class='panel'><div class='head'><h2>Pilih lokasi</h2><p>Ringkasan cepat untuk tiap wilayah.</p></div><div class='grid g3'>{cards}</div></section>"
           "<section class='panel'><div class='head'><h2>Peta lokasi</h2><p>Estimasi risiko model per lokasi.</p></div><div class='mapbox'><iframe src='langit_portal_map.html' loading='lazy'></iframe></div>"
           "<div class='nav' style='margin-top:14px'><a class='btn primary' href='langit_portal_map.html'>Buka peta penuh</a><a class='btn' href='langit_all_locations.geojson'>GeoJSON semua lokasi</a></div></section>"
           "<section class='panel'><div class='head'><h2>Data publik</h2><p>Untuk analisis, arsip, dan integrasi.</p></div><nav class='nav'><a class='btn' href='ensemble_all_locations.csv'>Ensemble CSV</a>"
           "<a class='btn' href='forecast_all_locations.csv'>Forecast CSV</a><a class='btn' href='source_status_all_locations.csv'>Status sumber</a><a class='btn' href='forecast_batch_summary.json'>Batch summary</a></nav></section>"
           f"<p class='footer'>{LANGIT_BRAND_NAME} · {APP_VERSION} · {_lg_esc(updated)}</p></main></body></html>")
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    manifest = {"brand": LANGIT_BRAND_NAME, "version": APP_VERSION, "generated_at": now.isoformat(), "locations": [l.slug for l in locations], "index": "index.html",
                "map": "langit_portal_map.html", "geojson": "langit_all_locations.geojson", "disclaimer": LANGIT_DISCLAIMER}
    write_json(root_output_path("langit_portal_manifest.json"), manifest)
    return "index.html"


def sentinel_write_publish_manifest(args):
    public = ["langit_app.html", "langit_3day.html", "langit_activity.html", "keandalan_data.html", "langit_map_room.html", "akurasi_data.html", AETHER_DASHBOARD_FILENAME,
              AETHER_REPORT_HTML_FILENAME, AETHER_CSV_FILENAME, AETHER_JSON_FILENAME, AETHER_CONTRACT_FILENAME, "sentinel_x_verification_summary.json", "sentinel_x_reliability.csv", "sentinel_constitution.md"]
    internal = ["*.sqlite", "*.db", "raw_payloads/", "logs/", "forecast_archive/", "source_health.json", "observations/qc_archive/"]
    write_json(path_output("sentinel_x_publish_manifest.json"), {"generated_at": now_local(args.timezone).isoformat(), "public_version": APP_VERSION, "location": getattr(args, "location_name", ""),
                                                                  "public_files": public, "internal_or_do_not_publish": internal, "public_disclaimer": LANGIT_DISCLAIMER})
    atomic_write_text(root_output_path(".public_gitignore_template"), lambda f: f.write("\n".join(["*.sqlite", "*.db", "**/logs/", "**/raw_payloads/", "**/forecast_archive/", "**/source_health.json", "**/*.corrupt-*"])))
    return path_output("sentinel_x_publish_manifest.json")


# ---------------------------------------------------------------------------
# Lokasi
# ---------------------------------------------------------------------------
def build_location_config(slug, payload):
    return LocationConfig(slug=sanitize_filename(slug.lower()), location_name=payload["location_name"], adm4=payload["adm4"],
                          latitude=float(payload["latitude"]), longitude=float(payload["longitude"]), timezone=payload.get("timezone", DEFAULT_TIMEZONE),
                          bmkg_point_name=payload.get("bmkg_point_name", payload["location_name"]), area_level=payload.get("area_level", "adm4"),
                          is_proxy_bmkg=bool(payload.get("is_proxy_bmkg", False)), note=payload.get("note", ""))


def embedded_location_presets():
    return {slug: build_location_config(slug, p) for slug, p in DEFAULT_LOCATION_PRESET_DATA.items()}


LOCATION_PRESETS = embedded_location_presets()


def resolve_locations_file_path(locations_file=None):
    if not locations_file:
        return path_config(LOCATIONS_FILENAME)
    return locations_file if os.path.isabs(locations_file) else os.path.join(BASE_DIR, locations_file)


def load_location_presets(locations_file=None):
    path = resolve_locations_file_path(locations_file)
    if not os.path.exists(path):
        if locations_file:
            raise ValueError(f"locations file tidak ditemukan: {path}")
        return embedded_location_presets(), list(DEFAULT_MULTI_LOCATION_SLUGS), ""
    payload = read_json(path, default=None)
    if not isinstance(payload, dict):
        raise ValueError(f"Isi locations file tidak valid: {path}")
    raw = payload.get("locations")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"Field 'locations' wajib ada dan tidak boleh kosong: {path}")
    presets = {sanitize_filename(s.lower()): build_location_config(s, i) for s, i in raw.items()}
    defaults = []
    for slug in payload.get("default_multi_locations") or DEFAULT_MULTI_LOCATION_SLUGS:
        clean = sanitize_filename(str(slug).lower())
        if clean not in presets:
            raise ValueError(f"default_multi_locations memuat slug yang tidak ada di locations file: {clean}")
        if clean not in defaults:
            defaults.append(clean)
    return presets, defaults, path


def refresh_location_presets(locations_file=None):
    global LOCATION_PRESETS, ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS, ACTIVE_LOCATIONS_FILE
    LOCATION_PRESETS, ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS, ACTIVE_LOCATIONS_FILE = load_location_presets(locations_file)


def validate_location_config(location):
    if not -90 <= location.latitude <= 90:
        raise ValueError(f"latitude tidak valid untuk lokasi {location.slug}")
    if not -180 <= location.longitude <= 180:
        raise ValueError(f"longitude tidak valid untuk lokasi {location.slug}")
    parts = location.adm4.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise ValueError(f"adm4 tidak valid untuk lokasi {location.slug}: {location.adm4}")
    ZoneInfo(location.timezone)


def clone_args_for_location(args, location):
    data = vars(args).copy()
    data.update({"location_slug": location.slug, "location_name": location.location_name, "adm4": location.adm4, "latitude": location.latitude,
                 "longitude": location.longitude, "timezone": location.timezone, "bmkg_point_name": location.bmkg_point_name,
                 "area_level": location.area_level, "is_proxy_bmkg": location.is_proxy_bmkg, "location_note": location.note})
    return argparse.Namespace(**data)


def is_default_single_location_args(args):
    return (args.location_name == DEFAULT_LOCATION_NAME and args.adm4 == DEFAULT_ADM4 and abs(args.latitude - DEFAULT_LATITUDE) < 1e-9
            and abs(args.longitude - DEFAULT_LONGITUDE) < 1e-9 and args.timezone == DEFAULT_TIMEZONE)


def resolve_requested_locations(args):
    if not args.locations:
        if args.mode != "import-observations" and is_default_single_location_args(args):
            return [LOCATION_PRESETS[s] for s in ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS]
        return [LocationConfig(slug=sanitize_filename(args.location_name.lower()), location_name=args.location_name, adm4=args.adm4, latitude=args.latitude,
                               longitude=args.longitude, timezone=args.timezone, bmkg_point_name=args.location_name, area_level="custom",
                               note="Custom location from CLI arguments")]
    tokens = [t.strip().lower() for t in args.locations.split(",") if t.strip()]
    if not tokens:
        raise ValueError("--locations tidak boleh kosong")
    if tokens == ["all"]:
        return [LOCATION_PRESETS[s] for s in ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS]
    selected, seen = [], set()
    for slug in tokens:
        if slug not in LOCATION_PRESETS:
            raise ValueError(f"Lokasi preset tidak dikenali: {slug}")
        if slug not in seen:
            selected.append(LOCATION_PRESETS[slug])
            seen.add(slug)
    return selected


def print_available_locations():
    print("Lokasi preset tersedia:")
    print("Sumber config lokasi:", ACTIVE_LOCATIONS_FILE or "(embedded defaults)")
    ordered = list(ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS) + sorted(s for s in LOCATION_PRESETS if s not in ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS)
    for slug in ordered:
        l = LOCATION_PRESETS[slug]
        print(f"- {l.slug}: {l.location_name} | adm4={l.adm4} | lat={l.latitude} | lon={l.longitude} | bmkg_point={l.bmkg_point_name} | proxy={l.is_proxy_bmkg}")
        if l.note:
            print(f"  note: {l.note}")


# ---------------------------------------------------------------------------
# Logging per lokasi, pembersihan, validasi
# ---------------------------------------------------------------------------
def setup_logging(args):
    log_dir = path_output(LOG_DIRNAME)
    ensure_directory(log_dir)
    log_path = os.path.join(log_dir, f"{sanitize_filename(args.mode)}_{now_local(args.timezone):%Y%m%d_%H%M%S}.log")
    for h in list(LOGGER.handlers):
        h.close()
        LOGGER.removeHandler(h)
    level = logging.DEBUG if args.debug else logging.INFO
    LOGGER.setLevel(level)
    LOGGER.propagate = False
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    return log_path


def cleanup_old_files_in_directory(directory_path, retention_days):
    if not retention_days or retention_days <= 0 or not os.path.isdir(directory_path):
        return 0
    cutoff, deleted = time.time() - retention_days * 86400, 0
    for entry in os.scandir(directory_path):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                deleted += 1
        except OSError:
            continue
    return deleted


def cleanup_to_max_size_mb(directory_path, max_mb):
    try:
        max_mb = float(max_mb)
    except Exception:
        return 0
    if max_mb <= 0 or not os.path.isdir(directory_path):
        return 0
    max_bytes, files, total = int(max_mb * 1024 * 1024), [], 0
    for entry in os.scandir(directory_path):
        try:
            if entry.is_file():
                st = entry.stat()
                total += st.st_size
                files.append((st.st_mtime, st.st_size, entry.path))
        except OSError:
            continue
    deleted = 0
    for _, size, path in sorted(files):
        if total <= max_bytes:
            break
        try:
            os.remove(path)
            total -= size
            deleted += 1
        except OSError:
            continue
    return deleted


def cleanup_old_outputs(args):
    deleted = 0
    for folder in (RAW_PAYLOAD_DIRNAME, LOG_DIRNAME, REPORT_DIRNAME):
        directory = path_output(folder)
        deleted += cleanup_old_files_in_directory(directory, args.retention_days)
        deleted += cleanup_to_max_size_mb(directory, args.retention_max_mb)
    # Arsip prakiraan = data pelatihan; retensi lebih panjang, tetapi tidak tanpa batas.
    deleted += cleanup_old_files_in_directory(os.path.join(ACTIVE_OUTPUT_DIR, ARCHIVE_DIRNAME), getattr(args, "archive_retention_days", 0))
    return deleted


_VERSIONED_BASES = ("ensemble_all_locations", "ensemble_long_all_locations", "forecast_all_locations", "source_status_all_locations", "ensemble_fact_all_locations")


def cleanup_versioned_root_files(keep):
    if not keep or keep <= 0:
        return 0
    root, deleted = root_output_dir(), 0
    for base in _VERSIONED_BASES:
        files = [e for e in os.scandir(root) if e.is_file() and e.name.startswith(base + "_") and e.name.endswith(".csv")
                 and re.fullmatch(r"[\d_]+", e.name[len(base) + 1:-4] or "x")]
        for entry in sorted(files, key=lambda e: e.stat().st_mtime, reverse=True)[keep:]:
            try:
                os.remove(entry.path)
                deleted += 1
            except OSError:
                pass
    return deleted


def validate_common_args(args):
    if args.lookback_days <= 0:
        raise ValueError("lookback_days harus lebih besar dari 0")
    if args.retention_days <= 0:
        raise ValueError("retention_days harus lebih besar dari 0")
    hour, minute = [int(p) for p in args.run_time.split(":")]
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("run_time harus memakai format HH:MM")
    for name in ("max_inflight_per_host", "max_workers", "max_retry_http", "http_timeout"):
        if getattr(args, name) <= 0:
            raise ValueError(f"{name} harus > 0")
    if getattr(args, "min_success_sources", None) is not None and not 1 <= args.min_success_sources <= 12:
        raise ValueError("min-success-sources must be 1..12")
    if args.station_max_distance_km <= 0:
        raise ValueError("station-max-distance-km must be positive")
    if args.ensemble_min_members < 2 or args.ensemble_min_families < 1:
        raise ValueError("ensemble-min-members minimal 2 dan ensemble-min-families minimal 1")
    if not 0 <= args.rollover_hour <= 24:
        raise ValueError("rollover-hour harus 0..24 (24 = nonaktif)")
    if args.forecast_range_days < 1:
        raise ValueError("forecast-range-days harus >= 1")
    unknown = [n.strip() for n in str(args.backup_models).split(",") if n.strip() and n.strip() not in V61_BACKUP_CONFIGS]
    if unknown:
        raise ValueError("Unknown backup model(s): " + ",".join(unknown))
    v61_activity_profiles(args)


def prepare_location_context(args):
    set_active_output_dir(args.location_slug)
    log_path = setup_logging(args)
    log_info("Log file:", log_path)
    log_info("Output dir:", ACTIVE_OUTPUT_DIR)
    log_info("Metadata lokasi:", f"slug={args.location_slug}", f"adm4={args.adm4}", f"bmkg_point={args.bmkg_point_name}", f"area_level={args.area_level}", f"proxy_bmkg={args.is_proxy_bmkg}")
    log_info("Koordinat:", f"lat={args.latitude}", f"lon={args.longitude}", f"timezone={args.timezone}")
    if getattr(args, "location_note", ""):
        log_info("Catatan lokasi:", args.location_note)
    cleanup_old_outputs(args)
    return log_path


def execute_mode_for_location(base_args, location, runner):
    location_args = clone_args_for_location(base_args, location)
    validate_location_config(location)
    log_path = prepare_location_context(location_args)
    return location_args, runner(location_args), log_path


# ---------------------------------------------------------------------------
# Satu run (lokasi x tanggal)
# ---------------------------------------------------------------------------
def _forecast_dates(args):
    if getattr(args, "target_date", None):
        start = parse_iso_date(args.target_date)
    else:
        now = now_local(args.timezone)
        start = now.date()
        if getattr(args, "rollover_hour", DEFAULT_ROLLOVER_HOUR) <= now.hour <= 23:
            start += timedelta(days=1)  # sudah malam: hari ini praktis habis
    days = 1 if getattr(args, "single_day_public", False) else int(getattr(args, "forecast_range_days", 3) or 3)
    return [start + timedelta(days=i) for i in range(max(1, min(days, 5)))]


def run_once(args):
    global _RUN_ISSUED_AT
    _RUN_ISSUED_AT = now_local(args.timezone)
    target_date = parse_iso_date(args.target_date) if args.target_date else (now_local(args.timezone) + timedelta(days=1)).date()
    stamp = target_date.strftime("%Y%m%d")
    existing = path_output(f"forecast_{stamp}.csv")
    if args.skip_existing and os.path.exists(existing) and not args.force:
        log_info("skip-existing aktif: forecast sudah ada:", existing)
        return {"generated_at": now_local(args.timezone).isoformat(), "location_slug": args.location_slug, "location_name": args.location_name,
                "target_date": target_date.isoformat(), "output_dir": ACTIVE_OUTPUT_DIR, "run_status": "skipped", "reason": "existing_output"}
    load_weight_config(getattr(args, "use_legacy_weights", False))
    load_health_config()
    _quality_load_skill(args)
    log_info("Mulai proses untuk lokasi", args.location_name)
    log_info("Target date:", target_date.isoformat())
    log_info("Target hours:", ", ".join(TARGET_TIMES))
    log_info("Sumber aktif:", ", ".join(i["source_id"] for i in ACTIVE_SOURCE_CONFIGS))
    active_ids = {i["source_id"] for i in ACTIVE_SOURCE_CONFIGS}
    log_info("Bobot aktif:", ", ".join(f"{s}={round(w, 3)}" for s, w in sorted(ACTIVE_SOURCE_WEIGHTS.items()) if s in active_ids))
    results = collect_all_sources(target_date, args)
    summary = save_outputs(target_date, results, args)
    save_health_config(results, args, target_date=target_date)
    ok = sum(r.success for r in results)
    log_info("Selesai.")
    log_info("Sumber sukses:", f"{ok}/{len(results)}")
    log_info("Total forecast point:", sum(len(r.points) for r in results))
    if ok < MIN_SOURCE_SUCCESS_FOR_RUN:
        log_warning("Jumlah sumber sukses di bawah ambang minimum:", ok, "<", MIN_SOURCE_SUCCESS_FOR_RUN)
    if summary["low_coverage_slots"]:
        log_warning("Coverage terbatas pada jam:", ", ".join(summary["low_coverage_slots"]))
    for r in results:
        meta = ([f"http={r.http_status}"] if r.http_status is not None else []) + ([f"t={r.duration_ms}ms"] if r.duration_ms is not None else [])
        log_info(f"{r.source_id}:", "OK" if r.success else "FAIL", f"({len(r.points)}/{len(TARGET_TIMES)} point)", " ".join(meta), r.error or "")
    return summary


def run_once_for_date(args, target_date):
    args.target_date = target_date.isoformat()
    return run_once(args)


def finalize_location(args, dates, skip_verification=False):
    """Verifikasi sekali per lokasi + halaman publik multi-hari."""
    args.defer_verification = skip_verification
    args.defer_public_pages = False
    try:
        sentinel_write_verification_artifacts([], args)
    except Exception as exc:
        log_warning("Verifikasi gagal (halaman publik tetap ditulis):", exc)
        traceback.print_exc()
    result = anemos_write_multiday_public_pages(args, dates)
    sentinel_write_publish_manifest(args)
    return result


# ---------------------------------------------------------------------------
# CSV gabungan & artefak BI
# ---------------------------------------------------------------------------
LOCATION_FIELDS = ["location_slug", "location_name", "target_date", "adm4", "bmkg_point_name", "area_level", "is_proxy_bmkg", "latitude", "longitude", "timezone", "location_note"]
ENS_COMBINED_FIELDS = ENSEMBLE_HEADERS[:22]
ENS_LONG_FIELDS = [h.replace("%", "pct_") for h in ENSEMBLE_HEADERS]
STATUS_COMBINED_FIELDS = STATUS_HEADERS[:15]


def combined_location_metadata(la, target_date):
    return {"location_slug": la.location_slug, "location_name": la.location_name, "target_date": target_date, "adm4": la.adm4, "bmkg_point_name": la.bmkg_point_name,
            "area_level": la.area_level, "is_proxy_bmkg": "yes" if la.is_proxy_bmkg else "no", "latitude": la.latitude, "longitude": la.longitude,
            "timezone": la.timezone, "location_note": getattr(la, "location_note", "")}


def _collect_combined(la, target_date, output_dir, filename, fields, rename=None):
    meta = combined_location_metadata(la, target_date)
    rename = rename or {}
    return [{**meta, **{f: row.get(rename.get(f, f)) or "" for f in fields}} for row in read_dict_csv(os.path.join(output_dir, filename))]


def write_combined_csv(base_filename, fieldnames, rows):
    if not rows:
        return None, None
    updated_locations = {r.get("location_slug") for r in rows}
    updated_dates = {r.get("target_date") for r in rows}
    previous = read_dict_csv(root_output_path(f"{base_filename}.csv"))
    rows = list(rows) + [r for r in previous if r.get("location_slug") not in updated_locations and r.get("target_date") in updated_dates]
    unique_dates = sorted({r["target_date"] for r in rows if r.get("target_date")})
    run_stamp = now_local(DEFAULT_TIMEZONE).strftime("%Y%m%d_%H%M%S")
    stamp = f"{unique_dates[0].replace('-', '')}_{run_stamp}" if len(unique_dates) == 1 else run_stamp
    latest, versioned = root_output_path(f"{base_filename}.csv"), root_output_path(f"{base_filename}_{stamp}.csv")
    write_dict_csv(versioned, fieldnames, rows)
    try:
        write_dict_csv(latest, fieldnames, rows)
    except PermissionError as exc:
        batch_warning(f"File {os.path.basename(latest)} sedang dipakai atau terkunci, hanya file versi waktu yang ditulis:", exc)
        latest = None
    return latest, versioned


def write_dim_locations(locations, base_args):
    rows = [{"location_slug": l.slug, "location_name": l.location_name, "adm4": l.adm4, "bmkg_point_name": l.bmkg_point_name, "area_level": l.area_level,
             "is_proxy_bmkg": "yes" if l.is_proxy_bmkg else "no", "latitude": l.latitude, "longitude": l.longitude, "timezone": l.timezone, "note": l.note} for l in locations]
    path = root_output_path("dim_locations.csv")
    updated = {r["location_slug"] for r in rows}
    rows.extend(r for r in read_dict_csv(path) if r.get("location_slug") not in updated)
    rows.sort(key=lambda r: r["location_slug"])
    write_dict_csv(path, ["location_slug", "location_name", "adm4", "bmkg_point_name", "area_level", "is_proxy_bmkg", "latitude", "longitude", "timezone", "note"], rows)
    return path, len(rows)


def write_dim_sources():
    rows = sorted(({"source_id": c.get("source_id") or "", "provider": c.get("provider") or "", "kind": c.get("kind") or "", "endpoint": c.get("endpoint") or "",
                    "models": c.get("models") or "", "base_weight": source_base_weight(c.get("source_id") or "")} for c in ALL_SOURCE_CONFIGS), key=lambda r: r["source_id"])
    path = root_output_path("dim_sources.csv")
    write_dict_csv(path, ["source_id", "provider", "kind", "endpoint", "models", "base_weight"], rows)
    return path, len(rows)


def write_ensemble_fact_from_long(long_rows):
    if not long_rows:
        return None, 0
    fields = ["location_slug", "target_date", "jam", "dominant_category", "confidence_score", "confidence_label", "sources_used", "sources_expected", "coverage_fraction",
              "weight_total", "temp_mean", "temp_std", "rh_mean", "rh_std", "rain_mean", "rain_std", "heat_index_mean", "heat_index_std", "gap_mean_minutes",
              "gap_max_minutes", "pct_cerah", "pct_cerah_berawan", "pct_berawan", "pct_hujan_ringan", "pct_hujan_sedang", "pct_hujan_lebat"]
    rows = [{k: r.get(k, "") for k in fields} for r in long_rows]
    latest, versioned = write_combined_csv("ensemble_fact_all_locations", fields, rows)
    return latest or versioned, len(rows)


def write_batch_summary(mode, rows, extra_payload=None):
    payload = {"generated_at": now_local(DEFAULT_TIMEZONE).isoformat(), "mode": mode, "locations_file": ACTIVE_LOCATIONS_FILE or "",
               "default_multi_locations": list(ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS), "locations_total": len(rows), "locations": rows}
    if extra_payload:
        payload.update(extra_payload)
    write_json(root_output_path(f"{mode}_batch_summary.json"), payload)


def run_forecast_for_locations(base_args, locations):
    """Urutan: tanggal -> lokasi. Bila anggaran waktu habis, semua lokasi tetap punya hari pertama."""
    started = time.monotonic()
    budget = float(getattr(base_args, "time_budget_minutes", 0) or 0) * 60
    dates = _forecast_dates(base_args)
    today = now_local(base_args.timezone).date()
    base_args.forecast_window_start, base_args.forecast_window_end = dates[0].isoformat(), dates[-1].isoformat()
    base_args.forecast_days_request = int(clamp((dates[-1] - today).days + 2, 3, 7))
    base_args.defer_verification = True
    base_args.defer_public_pages = True
    _V61_ENSEMBLE_RUNTIME_CACHE.clear()
    clear_transport_cache()
    state = {l.slug: {"args": None, "runs": [], "error": ""} for l in locations}
    combined = {"ens": [], "long": [], "fc": [], "st": []}
    for d in dates:
        for loc in locations:
            st = state[loc.slug]
            if budget and time.monotonic() - started > budget:
                st["runs"].append({"target_date": d.isoformat(), "run_status": "skipped_time_budget"})
                batch_warning(f"Anggaran waktu habis; {loc.location_name} {d.isoformat()} dilewati.")
                continue
            try:
                la, summary, log_path = execute_mode_for_location(base_args, loc, lambda a, dd=d: run_once_for_date(a, dd))
                st["args"] = la
                out_dir, td = summary.get("output_dir") or ACTIVE_OUTPUT_DIR, summary["target_date"]
                combined["ens"] += _collect_combined(la, td, out_dir, "ensemble.csv", ENS_COMBINED_FIELDS)
                combined["long"] += _collect_combined(la, td, out_dir, "ensemble.csv", ENS_LONG_FIELDS, {f: f.replace("pct_", "%") for f in ENS_LONG_FIELDS})
                combined["fc"] += _collect_combined(la, td, out_dir, "forecast.csv", FORECAST_HEADERS)
                combined["st"] += _collect_combined(la, td, out_dir, "source_status.csv", STATUS_COMBINED_FIELDS)
                st["runs"].append({"target_date": td, "run_status": summary.get("run_status", "ok"), "sources_success": summary.get("sources_success"),
                                   "sources_total": summary.get("sources_total"), "low_coverage_slots": summary.get("low_coverage_slots"), "log_file": log_path})
            except Exception as exc:
                st["error"] = str(exc)
                batch_warning(f"{loc.location_name} gagal untuk {d.isoformat()}:", exc)
                traceback.print_exc()
                st["runs"].append({"target_date": d.isoformat(), "run_status": "error", "error": str(exc)})
    skip_verification = bool(budget and time.monotonic() - started > budget + 120)
    for loc in locations:
        st = state[loc.slug]
        if st["args"] is None:
            continue
        try:
            execute_mode_for_location(base_args, loc, lambda a: finalize_location(a, dates, skip_verification))
        except Exception as exc:
            st["error"] = str(exc)
            batch_warning(f"Gagal finalisasi {loc.location_name}:", exc)
            traceback.print_exc()
    rows = []
    for loc in locations:
        st = state[loc.slug]
        ok_days = sum(r.get("run_status") not in ("error", "skipped_time_budget") for r in st["runs"])
        bad = any(r.get("run_status") in ("warning", "skipped_time_budget") for r in st["runs"])
        rows.append({"location_slug": loc.slug, "location_name": loc.location_name, "bmkg_point_name": loc.bmkg_point_name, "area_level": loc.area_level,
                     "is_proxy_bmkg": loc.is_proxy_bmkg, "target_date": ",".join(d.isoformat() for d in dates),
                     "run_status": "error" if not ok_days else "warning" if (bad or st["error"]) else "ok", "days_ok": ok_days, "days_total": len(dates),
                     "output_dir": os.path.join(root_output_dir(), sanitize_filename(loc.slug)), "daily_runs": st["runs"], "error": st["error"]})
    outputs = {}
    for base, label, fields, payload in (("ensemble_all_locations", "Ensemble", LOCATION_FIELDS + ENS_COMBINED_FIELDS, combined["ens"]),
                                         ("ensemble_long_all_locations", "Ensemble long (BI)", LOCATION_FIELDS + ENS_LONG_FIELDS, combined["long"]),
                                         ("forecast_all_locations", "Forecast raw", LOCATION_FIELDS + FORECAST_HEADERS, combined["fc"]),
                                         ("source_status_all_locations", "Source status", LOCATION_FIELDS + STATUS_COMBINED_FIELDS, combined["st"])):
        latest, versioned = (None, None) if base_args.no_combined else write_combined_csv(base, fields, payload)
        outputs[base] = {"latest_path": latest or "", "versioned_path": versioned or "", "rows": len(payload)}
        if latest:
            batch_info(f"{label} gabungan:", latest)
    if not base_args.no_combined:
        try:
            src_path, src_n = write_dim_sources()
            loc_path, loc_n = write_dim_locations(locations, base_args)
            fact_path, fact_n = write_ensemble_fact_from_long(combined["long"])
            write_json(root_output_path("bi_artifacts_summary.json"), {"generated_at": now_local(DEFAULT_TIMEZONE).isoformat(), "schema_version": OUTPUT_SCHEMA_VERSION,
                       "dim_sources": {"path": src_path, "rows": src_n}, "dim_locations": {"path": loc_path, "rows": loc_n}, "ensemble_fact": {"path": fact_path or "", "rows": fact_n}})
        except Exception as exc:
            batch_warning("Gagal menulis BI artifacts:", exc)
        cleanup_versioned_root_files(getattr(base_args, "keep_versioned_files", DEFAULT_KEEP_VERSIONED_FILES))
    write_batch_summary("forecast", rows, {"brand": LANGIT_BRAND_NAME, "forecast_range_days": len(dates), "forecast_dates": [d.isoformat() for d in dates],
                                           "elapsed_seconds": round(time.monotonic() - started, 1), "time_budget_seconds": budget or None,
                                           "locations_ok": sum(r["run_status"] != "error" for r in rows), "combined_outputs": outputs})
    try:
        sentinel_write_root_public_index(locations, rows, base_args)
    except Exception as exc:
        batch_warning("Gagal menulis portal publik:", exc)
    clear_transport_cache()
    return rows


def sync_observations_for_locations(base_args, locations):
    return _batch_mode("sync-observations", base_args, locations, sync_observations, lambda la, res: {"days_processed": len(res)})


def evaluate_for_locations(base_args, locations):
    return _batch_mode("evaluate", base_args, locations, evaluate_historical_performance,
                       lambda la, res: {"evaluated_sources": len(res["source_score_rows"]), "evaluated_rows": len(res["detail_rows"]), "weights_file": res["weights_path"], "learning_status": res["learning_status"]})


def _batch_mode(name, base_args, locations, runner, extra):
    rows = []
    for loc in locations:
        try:
            la, res, log_path = execute_mode_for_location(base_args, loc, runner)
            rows.append({"location_slug": la.location_slug, "location_name": la.location_name, "bmkg_point_name": la.bmkg_point_name, "area_level": la.area_level,
                         "is_proxy_bmkg": la.is_proxy_bmkg, **extra(la, res), "output_dir": ACTIVE_OUTPUT_DIR, "log_file": log_path})
        except Exception as exc:
            batch_warning(f"{name} gagal untuk {loc.location_name}:", exc)
            traceback.print_exc()
            rows.append({"location_slug": loc.slug, "location_name": loc.location_name, "status": "error", "error": str(exc)})
    write_batch_summary(name, rows)
    return rows


def import_observations_for_location(base_args, location):
    la, imported, log_path = execute_mode_for_location(base_args, location, import_external_observations)
    rows = [{"location_slug": la.location_slug, "location_name": la.location_name, "rows_imported": len(imported), "output_dir": ACTIVE_OUTPUT_DIR, "log_file": log_path}]
    write_batch_summary("import-observations", rows)
    return rows


def seconds_until_run(run_time_text, tz_name):
    now = now_local(tz_name)
    hour, minute = [int(p) for p in run_time_text.split(":")]
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return int((nxt - now).total_seconds()), nxt


def loop_daily(base_args, locations):
    tz = locations[0].timezone if locations else base_args.timezone
    batch_info("Mode loop harian aktif.", "Jadwal harian:", base_args.run_time, "Lokasi:", ", ".join(l.location_name for l in locations))
    if base_args.run_immediately_on_start:
        run_forecast_for_locations(base_args, locations)
    while True:
        try:
            left, nxt = seconds_until_run(base_args.run_time, tz)
            batch_info("Menunggu run berikutnya pada", nxt.strftime("%Y-%m-%d %H:%M:%S %Z"), f"({left} detik lagi)")
            while left > 0:
                nap = min(base_args.sleep_seconds, left)
                time.sleep(nap)
                left -= nap
            run_forecast_for_locations(base_args, locations)
        except Exception as exc:
            batch_warning("ERROR loop_daily:", exc)
            traceback.print_exc()
            time.sleep(60)


# ---------------------------------------------------------------------------
# Mode pendukung: doctor, red-team, autopsy, skill-league, serve, self-test
# ---------------------------------------------------------------------------
def aether_doctor_for_location(args):
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": "yes" if ok else "no", "detail": str(detail)})

    ensure_directory(ACTIVE_OUTPUT_DIR)
    add("output_dir_writable", os.access(ACTIVE_OUTPUT_DIR, os.W_OK), ACTIVE_OUTPUT_DIR)
    try:
        conn = aether_connect_db()
        aether_init_db(conn)
        conn.close()
        add("sqlite_ledger", True, aether_db_path())
    except Exception as exc:
        add("sqlite_ledger", False, exc)
    add("constitution_rules", len(SENTINEL_CONSTITUTION) >= 10, len(SENTINEL_CONSTITUTION))
    for name in ("langit_app.html", "akurasi_data.html", "sentinel_x_verification_summary.json", "langit_quality_gate.json"):
        add(name + "_exists", os.path.exists(path_output(name)), path_output(name))
    for c in ACTIVE_SOURCE_CONFIGS:
        add("preview_url_" + c["source_id"], True, preview_request_url(c, args))
    write_dict_csv(path_output("doctor_sentinel_x.csv"), ["check", "ok", "detail"], checks)
    write_json(path_output("doctor_sentinel_x.json"), {"checks": checks, "generated_at": now_local(args.timezone).isoformat(), "public_version": APP_VERSION})
    return checks


def sentinel_red_team_for_location(args):
    """Invarian keselamatan yang benar-benar dieksekusi (bukan hasil statis)."""
    tests = [("satu_sumber_tidak_dipercaya", aether_trust_level(1, 90, 10, 1.0, 1.0) == "DO_NOT_TRUST"),
             ("cakupan_buruk_menjadi_BLACK", aether_operational_status("DO_NOT_TRUST", 10, 10, 5) == "BLACK"),
             ("ketidakpastian_tinggi_menjadi_RED", aether_operational_status("TRUSTED", 10, 90, 8) == "RED"),
             ("ekor_hujan_tetap_terlihat", sentinel_multi_reality(10, 5, 50, 50, 15)["convective_burst"] > 0),
             ("probabilitas_di_luar_0_100_ditolak", _lg_prob(120) is None and _lg_prob(-1) is None),
             ("disclaimer_resmi_ada", "Bukan informasi resmi" in LANGIT_DISCLAIMER)]
    rows = [{"scenario": n, "result": "PASS" if ok else "FAIL"} for n, ok in tests]
    write_dict_csv(path_output("sentinel_x_red_team.csv"), ["scenario", "result"], rows)
    return {"red_team_csv": path_output("sentinel_x_red_team.csv"), "passed": sum(ok for _, ok in tests), "total": len(tests)}


def sentinel_autopsy_for_location(args):
    observations = sentinel_load_observations_by_key(args)
    history = list(_quality_historical_rows(observations)) if observations else []
    summary, pairs, _ = sentinel_compute_verification(history, args, observations)
    finding = "Belum cukup pasangan forecast-observasi untuk autopsy yang kuat."
    if pairs:
        l = pairs[-1]
        finding = ("Latest matched case: MISS hujan." if l["observed_rain"] == 1 and l["pred_rain_event"] == 0 else
                   "Latest matched case: FALSE ALARM." if l["observed_rain"] == 0 and l["pred_rain_event"] == 1 else
                   "Latest matched case: HIT hujan." if l["observed_rain"] == 1 else "Latest matched case: correct negative.")
    write_json(path_output("forecast_autopsy_latest.json"), {"generated_at": now_local(args.timezone).isoformat(), "summary": summary, "finding": finding, "latest_pairs_checked": pairs[-10:]})
    return {"autopsy_json": path_output("forecast_autopsy_latest.json"), "matched_cases": summary.get("matched_cases", 0)}


def sentinel_skill_league_for_location(args):
    observations = sentinel_load_observations_by_key(args)
    history = list(_quality_historical_rows(observations)) if observations else []
    summary, _, _ = sentinel_compute_verification(history, args, observations)
    rows = [{"rank": 1, "system": APP_VERSION, "metric": "rain_brier_score", "score": summary.get("rain_brier_score", ""), "cases": summary.get("matched_cases", 0),
             "note": "Skor agregat; turnamen per-sumber membutuhkan ledger verifikasi tingkat sumber."}]
    write_dict_csv(path_output("sentinel_x_skill_league.csv"), ["rank", "system", "metric", "score", "cases", "note"], rows)
    return {"skill_league": path_output("sentinel_x_skill_league.csv"), "cases": summary.get("matched_cases", 0)}


def aether_local_server(args):
    root, port = root_output_dir(), int(getattr(args, "serve_port", 8000))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _newest(self, name):
            found = [os.path.join(d, name) for d, _, fs in os.walk(root) if name in fs]
            return max(found, key=os.path.getmtime) if found else None

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in {"/", "/status"}:
                p = root_output_path("forecast_batch_summary.json")
                self._send(200, json.dumps(read_json(p, {}) if os.path.exists(p) else {"message": "No batch summary yet"}, ensure_ascii=False, indent=2), "application/json; charset=utf-8")
            elif path in {"/dashboard", "/sentinel.json"}:
                name = "langit_app.html" if path == "/dashboard" else AETHER_JSON_FILENAME
                f = self._newest(name)
                if not f:
                    self._send(404, "Belum tersedia. Jalankan --mode forecast dulu.")
                else:
                    with open(f, "r", encoding="utf-8") as fh:
                        self._send(200, fh.read(), "text/html; charset=utf-8" if path == "/dashboard" else "application/json; charset=utf-8")
            else:
                self._send(404, "Not found")

    print(f"[LANGIT] Local server: http://localhost:{port}/dashboard")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def run_self_tests(args):
    sample_point = ForecastPoint("BMKG", "BMKG", "10:00", parse_local_hour_string(parse_iso_date("2026-04-27"), "10:00", args.timezone), 28.0, 75.0, 0.0, 10.0,
                                 "Cerah Berawan", "Cerah Berawan", 0.0)
    payload = {"data": [{"cuaca": [[{"local_datetime": "2026-04-27 10:00:00", "t": 28, "hu": 75, "weather_desc": "Cerah Berawan", "ws": 10}]]}]}
    assert bmkg_to_kategori("Hujan Ringan") == "Hujan Ringan"
    assert bmkg_rain_proxy_mm("Hujan Sedang") > 0
    assert category_from_wmo_code(0, 0, 50) == "Cerah" and category_from_wmo_code(63, 4, 90) == "Hujan Sedang"
    assert extract_bmkg_points(parse_iso_date("2026-04-27"), payload, args)["10:00"].category == "Cerah Berawan"
    assert point_weight(sample_point) > 0 and confidence_label(85) == "Tinggi"
    assert round(heat_index(32.0, 70.0), 2) >= 32.0
    assert len(filter_weighted_pairs([(10, 1), (11, 1), (12, 1), (100, 1)])) < 4
    score, label = compute_confidence([sample_point] * 5, 5.0, 4.0, 1.0, 5.0, 0.5)
    assert score >= 0 and label in {"Tinggi", "Sedang", "Rendah"}
    assert all(c["source_id"] != "KMA" for c in ALL_SOURCE_CONFIGS) and OPTIONAL_SOURCE_CONFIGS[0]["models"] == "kma_seamless"
    assert next(c for c in ALL_SOURCE_CONFIGS if c["source_id"] == "UKMO")["models"] == "ukmo_seamless"
    fake_cli = argparse.Namespace(locations="all", location_name=DEFAULT_LOCATION_NAME, adm4=DEFAULT_ADM4, latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE, timezone=DEFAULT_TIMEZONE, mode="forecast")
    assert [l.slug for l in resolve_requested_locations(fake_cli)] == ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS
    assert _weighted_wind_direction([]) is None
    assert abs(sum(sentinel_multi_reality(60, 20, 70, 65, 10).values()) - 100) < 0.2
    assert _lg_prob(0.4) == 0.4 and _lg_prob(101) is None
    assert aether_lead_bucket(2) == "lead_0_3h" and sentinel_threat_level(85) == "VERY_HIGH"
    members = [{"model": "a", "family": "A", "member": "m%d" % i, "value": float(i)} for i in range(10)] + [{"model": "b", "family": "B", "member": "m%d" % i, "value": 0.0} for i in range(10)]
    stage = v61_rain_two_stage_summary(members)
    assert stage["available"] and abs(stage["probability_percent"] - 45.0) < 1e-6
    assert v61_crps([{"value": 1.0, "weight": 1.0}], 1.0) == 0.0
    tmp = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"default_multi_locations": ["customtown"], "locations": {"customtown": {"location_name": "Custom Town", "adm4": "32.73.02.1004", "latitude": -6.9, "longitude": 107.6,
                       "bmkg_point_name": "Custom BMKG", "area_level": "test", "is_proxy_bmkg": True, "note": "custom"}}}, f)
            tmp = f.name
        presets, defaults, _ = load_location_presets(tmp)
        assert defaults == ["customtown"] and presets["customtown"].is_proxy_bmkg is True
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    log_info("Self-test selesai. Semua assertion lulus.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser():
    p = argparse.ArgumentParser(description="Multi-location multi-source weather ensemble (single file).")
    modes = ["forecast", "sync-observations", "sync-references", "evaluate", "import-observations", "self-test", "doctor", "dashboard", "report", "feedback",
             "red-team", "autopsy", "skill-league", "constitution", "verify-public", "public-index", "serve", "dry-run"]
    p.add_argument("--mode", choices=modes, default="forecast")
    p.add_argument("--locations", help="Preset lokasi dipisah koma, atau 'all'.")
    p.add_argument("--list-locations", action="store_true")
    p.add_argument("--locations-file")
    p.add_argument("--location-name", default=DEFAULT_LOCATION_NAME)
    p.add_argument("--adm4", default=DEFAULT_ADM4)
    p.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    p.add_argument("--longitude", type=float, default=DEFAULT_LONGITUDE)
    p.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    p.add_argument("--target-date", help="YYYY-MM-DD; menjadi hari pertama prakiraan.")
    p.add_argument("--start-date")
    p.add_argument("--end-date")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_EVALUATION_DAYS)
    p.add_argument("--observations-csv")
    p.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    p.add_argument("--retention-max-mb", type=int, default=DEFAULT_RETENTION_MAX_MB)
    p.add_argument("--archive-retention-days", type=int, default=DEFAULT_ARCHIVE_RETENTION_DAYS, help="Retensi forecast_archive (0 = simpan selamanya).")
    p.add_argument("--keep-versioned-files", type=int, default=DEFAULT_KEEP_VERSIONED_FILES, help="Jumlah CSV gabungan berversi waktu yang disimpan per jenis.")
    p.add_argument("--run-daily", action="store_true", default=RUN_DAILY)
    p.add_argument("--run-time", default=RUN_TIME)
    p.add_argument("--run-immediately-on-start", action="store_true", default=RUN_IMMEDIATELY_ON_START)
    p.add_argument("--no-run-immediately-on-start", action="store_false", dest="run_immediately_on_start")
    p.add_argument("--sleep-seconds", type=int, default=SLEEP_INTERVAL_SECONDS)
    p.add_argument("--save-raw-payloads", action="store_true", default=SAVE_RAW_PAYLOADS)
    p.add_argument("--no-save-raw-payloads", action="store_false", dest="save_raw_payloads")
    p.add_argument("--compress-raw-payloads", action="store_true", default=COMPRESS_RAW_PAYLOADS)
    p.add_argument("--no-compress-raw-payloads", action="store_false", dest="compress_raw_payloads")
    p.add_argument("--auto-compress-raw-payloads", action="store_true", default=True)
    p.add_argument("--no-auto-compress-raw-payloads", action="store_false", dest="auto_compress_raw_payloads")
    p.add_argument("--debug", action="store_true", default=DEBUG)
    p.add_argument("--no-debug", action="store_false", dest="debug")
    p.add_argument("--csv-delimiter", default=",")
    p.add_argument("--http-timeout", type=int, default=HTTP_TIMEOUT_SECONDS)
    p.add_argument("--max-retry-http", type=int, default=MAX_RETRY_HTTP)
    p.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    p.add_argument("--max-inflight-per-host", type=int, default=DEFAULT_MAX_INFLIGHT_PER_HOST)
    p.add_argument("--skip-existing", action="store_true", default=False)
    p.add_argument("--force", action="store_true", default=False)
    p.add_argument("--sources", default="", help="Batasi sumber, mis. BMKG,GFS,METNO. KMA hanya aktif bila disebut eksplisit.")
    p.add_argument("--targets", default="")
    p.add_argument("--per-hour", action="store_true", default=False)
    p.add_argument("--target-step-minutes", type=int, default=60)
    p.add_argument("--dry-run", action="store_true", default=False)
    p.add_argument("--no-combined", action="store_true", default=False)
    p.add_argument("--strict-exit-code", action="store_true", default=False)
    p.add_argument("--enable-circuit-breaker", action="store_true", default=True)
    p.add_argument("--disable-circuit-breaker", action="store_false", dest="enable_circuit_breaker")
    p.add_argument("--circuit-base-seconds", type=int, default=20)
    p.add_argument("--circuit-max-backoff-seconds", type=int, default=15 * 60)
    p.add_argument("--disable-health", action="store_true", default=False)
    p.add_argument("--freeze-weights", action="store_true", default=False)
    p.add_argument("--use-legacy-weights", action="store_true", default=False, help="Pakai source_weights.json lama (tanpa validasi holdout).")
    p.add_argument("--metno-user-agent", default="")
    p.add_argument("--bmkg-user-agent", default="", help="Override User-Agent BMKG bila UA default ditolak.")
    p.add_argument("--aether-extra-vars", action="store_true", default=True)
    p.add_argument("--no-aether-extra-vars", action="store_false", dest="aether_extra_vars")
    p.add_argument("--microclimate", default="auto", choices=["auto", "generic_local", "valley_highland", "urban_highland", "lowland_agriculture", "coastal"])
    p.add_argument("--umbrella-threshold", type=float, default=25.0)
    p.add_argument("--mission", default="safety_first", choices=["safety_first", "avoid_rain", "outdoor_event", "fieldwork", "commute", "photography", "sport", "laundry", "research", "public_warning"])
    p.add_argument("--decision-risk-threshold", type=float, default=55.0)
    p.add_argument("--disable-sentinel-command-center", action="store_true", default=False)
    p.add_argument("--disable-grid-sampling", action="store_true", default=False, help="Deprecated: diabaikan (sampling grid dihapus).")
    p.add_argument("--grid-radius-km", type=float, default=3.0, help="Deprecated: diabaikan.")
    p.add_argument("--verification-min-cases", type=int, default=30)
    p.add_argument("--public-base-url", default="")
    p.add_argument("--public-disclaimer", default=LANGIT_DISCLAIMER, help="Deprecated: disclaimer kini konstan.")
    p.add_argument("--feedback-date")
    p.add_argument("--feedback-time")
    p.add_argument("--feedback-category")
    p.add_argument("--feedback-rain-mm", type=float)
    p.add_argument("--feedback-temp-c", type=float)
    p.add_argument("--feedback-note", default="")
    p.add_argument("--serve-port", type=int, default=8000)
    p.add_argument("--forecast-range-days", type=int, default=int(os.environ.get("ANEMOS_FORECAST_DAYS", "3")))
    p.add_argument("--single-day-public", action="store_true", default=False)
    p.add_argument("--rollover-hour", type=int, default=DEFAULT_ROLLOVER_HOUR, help="Setelah jam lokal ini hari pertama = besok (24 = nonaktif).")
    p.add_argument("--time-budget-minutes", type=float, default=DEFAULT_TIME_BUDGET_MINUTES, help="Anggaran waktu forecast; 0 = nonaktif.")
    p.add_argument("--defer-verification", action="store_true", default=False)
    p.add_argument("--cache-max-age-hours", type=float, default=6.0)
    p.add_argument("--stations-file")
    p.add_argument("--station-max-distance-km", type=float, default=30)
    p.add_argument("--observation-jump-temp-c", type=float, default=8)
    p.add_argument("--observation-stuck-hours", type=int, default=8)
    p.add_argument("--evaluation-min-days", type=int, default=30)
    p.add_argument("--evaluation-test-days", type=int, default=7)
    p.add_argument("--evaluation-folds", type=int, default=3)
    p.add_argument("--learning-min-samples", type=int, default=60)
    p.add_argument("--site-elevation-m", type=float)
    p.add_argument("--terrain-class", default="unknown")
    p.add_argument("--ensemble-models", default="ncep_gefs_seamless,ecmwf_ifs025_ensemble,icon_global_eps")
    p.add_argument("--disable-ensemble", action="store_true")
    p.add_argument("--ensemble-min-members", type=int, default=5)
    p.add_argument("--ensemble-min-families", type=int, default=2)
    p.add_argument("--backup-models", default="jma_seamless,cmc_gem_gdps,bom_access_global")
    p.add_argument("--min-success-sources", type=int)
    p.add_argument("--activity-profiles-file")
    return p


def _apply_runtime_overrides(args):
    global TARGET_TIMES, ACTIVE_SOURCE_CONFIGS
    if args.targets:
        parsed = []
        for t in (x.strip() for x in args.targets.split(",") if x.strip()):
            if len(t) != 5 or t[2] != ":" or not (t[:2].isdigit() and t[3:].isdigit()) or not (0 <= int(t[:2]) <= 23 and 0 <= int(t[3:]) <= 59):
                raise ValueError(f"--targets invalid time: {t}")
            parsed.append(t)
        if not parsed:
            raise ValueError("--targets tidak boleh kosong")
        TARGET_TIMES = parsed
    elif args.per_hour:
        TARGET_TIMES = build_hourly_targets(int(args.target_step_minutes))
    if args.sources:
        allowed = {s.strip().upper() for s in args.sources.split(",") if s.strip()}
        pool = ALL_SOURCE_CONFIGS + OPTIONAL_SOURCE_CONFIGS
        missing = sorted(allowed - {c["source_id"].upper() for c in pool})
        if missing:
            raise ValueError(f"--sources berisi source_id tidak dikenal: {', '.join(missing)}")
        ACTIVE_SOURCE_CONFIGS = [c for c in pool if c["source_id"].upper() in allowed]
    else:
        ACTIVE_SOURCE_CONFIGS = list(ALL_SOURCE_CONFIGS)
    if args.auto_compress_raw_payloads and args.save_raw_payloads and not args.compress_raw_payloads and len(TARGET_TIMES) >= 24:
        args.compress_raw_payloads = True
        batch_info("auto-compress aktif: raw payload akan disimpan sebagai .json.gz")


def main():
    global DEBUG, CSV_DELIMITER
    args = build_arg_parser().parse_args()
    DEBUG = args.debug
    refresh_location_presets(args.locations_file)
    CSV_DELIMITER = args.csv_delimiter or ","
    if CSV_DELIMITER not in {",", ";", "\t", "|"}:
        raise ValueError("--csv-delimiter hanya mendukung: ',', ';', '\\t', '|'")
    if args.list_locations:
        print_available_locations()
        return
    if args.mode == "dry-run":
        args.dry_run, args.mode = True, "forecast"
    _apply_runtime_overrides(args)
    validate_common_args(args)
    locations = resolve_requested_locations(args)
    for l in locations:
        validate_location_config(l)

    if args.dry_run:
        batch_info("dry-run aktif: menampilkan URL request tanpa fetch.")
        batch_info("Target hours:", ", ".join(TARGET_TIMES))
        batch_info("Sumber aktif:", ", ".join(i["source_id"] for i in ACTIVE_SOURCE_CONFIGS))
        for l in locations:
            batch_info("Lokasi:", l.location_name, f"(slug={l.slug})")
            la = clone_args_for_location(args, l)
            for c in ACTIVE_SOURCE_CONFIGS:
                print("-", c["source_id"], preview_request_url(c, la))
        return
    if args.mode == "serve":
        aether_local_server(args)
        return

    simple = {"doctor", "dashboard", "report", "feedback", "red-team", "autopsy", "skill-league", "constitution", "verify-public", "public-index"}
    if args.mode in simple:
        rows = []
        for l in locations:
            la = clone_args_for_location(args, l)
            validate_location_config(l)
            prepare_location_context(la)
            meta = {"location_slug": l.slug, "location_name": l.location_name}
            if args.mode == "doctor":
                checks = aether_doctor_for_location(la)
                rows.append({**meta, "checks_ok": sum(c["ok"] == "yes" for c in checks), "checks_total": len(checks)})
            elif args.mode in {"dashboard", "report"}:
                rows.append({**meta, **anemos_write_multiday_public_pages(la, _forecast_dates(la))})
            elif args.mode == "feedback":
                rows.append({**meta, **{k: v for k, v in aether_feedback_for_location(la).items() if k in ("target_date", "jam", "rain_mm", "temp_c", "category")}})
            elif args.mode == "red-team":
                rows.append({**meta, **sentinel_red_team_for_location(la)})
            elif args.mode == "autopsy":
                rows.append({**meta, **sentinel_autopsy_for_location(la)})
            elif args.mode == "skill-league":
                rows.append({**meta, **sentinel_skill_league_for_location(la)})
            elif args.mode == "constitution":
                sentinel_write_constitution(la)
                rows.append({**meta, "written": True})
            elif args.mode == "verify-public":
                la.defer_verification = False
                s = sentinel_write_verification_artifacts([], la)
                rows.append({**meta, "matched_cases": s.get("matched_cases", 0), "calibration_status": s.get("calibration_status", "")})
            elif args.mode == "public-index":
                rows.append({**meta, "manifest": sentinel_write_publish_manifest(la)})
        write_batch_summary(args.mode, rows)
        return

    if args.mode == "forecast":
        if args.run_daily:
            loop_daily(args, locations)
            return
        rows = run_forecast_for_locations(args, locations)
        if any(r.get("run_status") == "error" for r in rows):
            sys.exit(3)
        if any(r.get("run_status") == "warning" for r in rows):
            if args.strict_exit_code:
                sys.exit(2)
            batch_warning("Forecast selesai dengan warning coverage/source/waktu; exit code tetap 0 agar Pages tetap ter-deploy. Gunakan --strict-exit-code untuk gagal pada warning.")
    elif args.mode == "sync-observations":
        sync_observations_for_locations(args, locations)
    elif args.mode == "sync-references":
        for l in locations:
            execute_mode_for_location(args, l, sync_model_references)
    elif args.mode == "evaluate":
        evaluate_for_locations(args, locations)
    elif args.mode == "import-observations":
        if len(locations) != 1:
            raise ValueError("Mode import-observations hanya mendukung satu lokasi per run.")
        import_observations_for_location(args, locations[0])
    elif args.mode == "self-test":
        _batch_mode("self-test", args, locations, run_self_tests, lambda la, res: {"status": "ok"})
        batch_info("LANGIT self-test selesai.")
    else:
        raise ValueError(f"Mode tidak dikenali: {args.mode}")


if __name__ == "__main__":
    main()
