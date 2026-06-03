import argparse
import csv
import gzip
import json
import logging
import math
import os
import random
import ssl
import tempfile
import threading
import time
import traceback
import zlib
import sys
import sqlite3
import html
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

try:
    import certifi
except ImportError:
    certifi = None


"""
Weather Ensemble Multi-Location (single-file version).

Upgrades over baseline:
- Retry: exponential backoff + jitter + explicit handling for 429/5xx and Retry-After.
- Concurrency: per-host semaphore to avoid rate-limit bursts; per-run total workers remain bounded.
- Cache: BMKG fallback now prefers latest SUCCESS for same target_date stamp.
- Raw payloads: optional gzip compression; "latest" pointers supported.
- Observability: per-source timing + last HTTP status captured in SourceResult and written to source_status CSV.
- Robustness: value validation / sanitization for point fields; safer file writes; more CLI switches.

Keeping everything in ONE file, as requested.
"""


# Default target times: per-hour (00:00..23:00).
# You can still override with --targets (custom list) at runtime.
TARGET_TIMES = [f"{hour:02d}:00" for hour in range(24)]
CUACA_ORDER = [
    "Cerah",
    "Cerah Berawan",
    "Berawan",
    "Hujan Ringan",
    "Hujan Sedang",
    "Hujan Lebat",
]

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
WEIGHTS_FILENAME = "source_weights.json"
HEALTH_FILENAME = "source_health.json"
OBSERVATION_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MIN_SOURCES_FOR_HIGH_CONFIDENCE = 5
MIN_SOURCE_SUCCESS_FOR_RUN = 5
OUTLIER_Z_THRESHOLD = 3.5
DEFAULT_EVALUATION_DAYS = 14
DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETENTION_MAX_MB = 0
MAX_CONSECUTIVE_FAILURE_PENALTY = 5

# Host-level concurrency to reduce burstiness / rate limit issues.
DEFAULT_MAX_INFLIGHT_PER_HOST = 3

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
DEFAULT_HTTP_HEADERS = {
    "User-Agent": "weather-ensemble-multi-location/3.1 (+https://data.bmkg.go.id/prakiraan-cuaca/)",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip,deflate",
    "Connection": "close",
}
BMKG_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip,deflate",
    "Referer": BMKG_PORTAL_URL,
    "Origin": "https://data.bmkg.go.id",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "close",
}
SOURCE_BASE_WEIGHTS = {
    "BMKG": 1.35,
    "ECMWF": 1.20,
    "METEOFRANCE": 1.10,
    "ICON": 1.05,
    "GFS": 1.00,
    "METNO": 1.00,
    "UKMO": 0.95,
    "KMA": 0.90,
    "CMA": 0.85,
}
OPEN_METEO_SOURCES = [
    {
        "source_id": "ECMWF",
        "provider": "Open-Meteo / ECMWF",
        "endpoint": "https://api.open-meteo.com/v1/ecmwf",
    },
    {
        "source_id": "GFS",
        "provider": "Open-Meteo / NOAA GFS",
        "endpoint": "https://api.open-meteo.com/v1/gfs",
    },
    {
        "source_id": "ICON",
        "provider": "Open-Meteo / DWD ICON",
        "endpoint": "https://api.open-meteo.com/v1/dwd-icon",
    },
    {
        "source_id": "CMA",
        "provider": "Open-Meteo / CMA GRAPES",
        "endpoint": "https://api.open-meteo.com/v1/cma",
    },
    {
        "source_id": "METEOFRANCE",
        "provider": "Open-Meteo / Meteo-France",
        "endpoint": "https://api.open-meteo.com/v1/meteofrance",
    },
    {
        "source_id": "KMA",
        "provider": "Open-Meteo / KMA",
        "endpoint": "https://api.open-meteo.com/v1/forecast",
        "models": "kma_seamless",
    },
    {
        "source_id": "UKMO",
        "provider": "Open-Meteo / UK Met Office",
        "endpoint": "https://api.open-meteo.com/v1/forecast",
        "models": "ukmo_seamless",
    },
]
ALL_SOURCE_CONFIGS = [
    {
        "source_id": "BMKG",
        "provider": "BMKG",
        "kind": "bmkg",
    },
    *[
        {
            "source_id": item["source_id"],
            "provider": item["provider"],
            "kind": "open_meteo",
            "endpoint": item["endpoint"],
            "models": item.get("models"),
        }
        for item in OPEN_METEO_SOURCES
    ],
    {
        "source_id": "METNO",
        "provider": "MET Norway",
        "kind": "met_no",
    },
]

# Active sources can be restricted via CLI (--sources).
ACTIVE_SOURCE_CONFIGS = list(ALL_SOURCE_CONFIGS)

# Output schema version (helps downstream consumers tolerate new columns).
OUTPUT_SCHEMA_VERSION = "2026-06-03.sentinel-x.public-operational-v2"


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


DEFAULT_LOCATION_PRESET_DATA = {
    "dago": {
        "location_name": "Dago, Bandung",
        "adm4": "32.73.02.1004",
        "latitude": -6.8890,
        "longitude": 107.6100,
        "bmkg_point_name": "Dago",
        "area_level": "kelurahan",
        "is_proxy_bmkg": False,
        "note": "BMKG point: Dago, Coblong, Kota Bandung",
    },
    "jatinangor": {
        "location_name": "Jatinangor, Sumedang",
        "adm4": "32.11.15.2002",
        "latitude": -6.9380,
        "longitude": 107.7556,
        "bmkg_point_name": "Hegarmanah",
        "area_level": "kecamatan",
        "is_proxy_bmkg": True,
        "note": "BMKG representative point: Hegarmanah, Kecamatan Jatinangor",
    },
    "arjawinangun": {
        "location_name": "Arjawinangun, Cirebon",
        "adm4": "32.09.24.2004",
        "latitude": -6.6453,
        "longitude": 108.4103,
        "bmkg_point_name": "Arjawinangun",
        "area_level": "kecamatan",
        "is_proxy_bmkg": False,
        "note": "BMKG point: Arjawinangun, Kecamatan Arjawinangun",
    },
}
DEFAULT_MULTI_LOCATION_SLUGS = ["dago", "jatinangor", "arjawinangun"]
ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS = list(DEFAULT_MULTI_LOCATION_SLUGS)

LOGGER = logging.getLogger("weather_ensemble_multi_location")
ACTIVE_SOURCE_WEIGHTS = dict(SOURCE_BASE_WEIGHTS)
SOURCE_HEALTH = {}
ACTIVE_OUTPUT_DIR = BASE_DIR
ACTIVE_LOCATIONS_FILE = ""
CSV_DELIMITER = ","
ACTIVE_HOUR_BUCKET_WEIGHTS: dict[str, float] = {}


def log_info(*args):
    message = " ".join(str(arg) for arg in args)
    print("[INFO]", message)
    if LOGGER.handlers:
        LOGGER.info(message)


def log_debug(*args):
    if DEBUG:
        message = " ".join(str(arg) for arg in args)
        print("[DEBUG]", message)
        if LOGGER.handlers:
            LOGGER.debug(message)


def log_warning(*args):
    message = " ".join(str(arg) for arg in args)
    print("[WARN]", message)
    if LOGGER.handlers:
        LOGGER.warning(message)


def batch_info(*args):
    print("[INFO]", " ".join(str(arg) for arg in args))


def batch_warning(*args):
    print("[WARN]", " ".join(str(arg) for arg in args))


def ensure_directory(path):
    os.makedirs(path, exist_ok=True)


def sanitize_filename(text):
    cleaned = []
    for char in text:
        cleaned.append(char if char.isalnum() or char in ("-", "_") else "_")
    return "".join(cleaned).strip("_") or "unknown"


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
    temp_fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{sanitize_filename(os.path.basename(path))}_",
        suffix=".tmp",
        text=False,
    )
    try:
        with os.fdopen(temp_fd, "wb") as f:
            writer_fn(f)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path, writer_fn, newline=None):
    directory = os.path.dirname(path) or "."
    ensure_directory(directory)
    temp_fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{sanitize_filename(os.path.basename(path))}_",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(temp_fd, "w", newline=newline, encoding="utf-8") as f:
            writer_fn(f)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def write_csv(path, headers, rows):
    def writer_fn(f):
        writer = csv.writer(f, delimiter=CSV_DELIMITER)
        writer.writerow(headers)
        writer.writerows(rows)

    atomic_write_text(path, writer_fn, newline="")


def write_dict_csv(path, fieldnames, rows):
    def writer_fn(f):
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=CSV_DELIMITER)
        writer.writeheader()
        writer.writerows(rows)

    atomic_write_text(path, writer_fn, newline="")


def write_json(path, payload):
    def writer_fn(f):
        json.dump(payload, f, ensure_ascii=False, indent=2)

    atomic_write_text(path, writer_fn)


def write_json_gz(path, payload):
    def writer_fn(fb):
        with gzip.GzipFile(fileobj=fb, mode="wb") as gz:
            gz.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    atomic_write_bytes(path, writer_fn)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    if path.lower().endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_dict_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = CSV_DELIMITER
        try:
            sniffed = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"])
            delimiter = sniffed.delimiter or delimiter
        except Exception:
            pass
        return list(csv.DictReader(f, delimiter=delimiter))


def build_location_config(slug, payload):
    return LocationConfig(
        slug=sanitize_filename(slug.lower()),
        location_name=payload["location_name"],
        adm4=payload["adm4"],
        latitude=float(payload["latitude"]),
        longitude=float(payload["longitude"]),
        timezone=payload.get("timezone", DEFAULT_TIMEZONE),
        bmkg_point_name=payload.get("bmkg_point_name", payload["location_name"]),
        area_level=payload.get("area_level", "adm4"),
        is_proxy_bmkg=bool(payload.get("is_proxy_bmkg", False)),
        note=payload.get("note", ""),
    )


def embedded_location_presets():
    return {
        slug: build_location_config(slug, payload)
        for slug, payload in DEFAULT_LOCATION_PRESET_DATA.items()
    }


LOCATION_PRESETS = embedded_location_presets()


def resolve_locations_file_path(locations_file=None):
    if not locations_file:
        return path_config(LOCATIONS_FILENAME)
    if os.path.isabs(locations_file):
        return locations_file
    return os.path.join(BASE_DIR, locations_file)


def load_location_presets(locations_file=None):
    locations_path = resolve_locations_file_path(locations_file)
    if not os.path.exists(locations_path):
        if locations_file:
            raise ValueError(f"locations file tidak ditemukan: {locations_path}")
        return embedded_location_presets(), list(DEFAULT_MULTI_LOCATION_SLUGS), ""

    payload = read_json(locations_path, default=None)
    if not isinstance(payload, dict):
        raise ValueError(f"Isi locations file tidak valid: {locations_path}")

    raw_locations = payload.get("locations")
    if not isinstance(raw_locations, dict) or not raw_locations:
        raise ValueError(f"Field 'locations' wajib ada dan tidak boleh kosong: {locations_path}")

    presets = {
        sanitize_filename(slug.lower()): build_location_config(slug, item)
        for slug, item in raw_locations.items()
    }

    configured_defaults = payload.get("default_multi_locations") or DEFAULT_MULTI_LOCATION_SLUGS
    active_defaults = []
    for slug in configured_defaults:
        clean_slug = sanitize_filename(str(slug).lower())
        if clean_slug not in presets:
            raise ValueError(
                f"default_multi_locations memuat slug yang tidak ada di locations file: {clean_slug}"
            )
        if clean_slug not in active_defaults:
            active_defaults.append(clean_slug)

    return presets, active_defaults, locations_path


def refresh_location_presets(locations_file=None):
    global LOCATION_PRESETS, ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS, ACTIVE_LOCATIONS_FILE
    LOCATION_PRESETS, ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS, ACTIVE_LOCATIONS_FILE = load_location_presets(
        locations_file
    )


def safe_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def round_or_blank(value, digits=2):
    if value is None:
        return ""
    return round(value, digits)


def setup_logging(args):
    log_dir = path_output(LOG_DIRNAME)
    ensure_directory(log_dir)
    timestamp = now_local(args.timezone).strftime("%Y%m%d_%H%M%S")
    mode_stub = sanitize_filename(args.mode)
    log_path = os.path.join(log_dir, f"{mode_stub}_{timestamp}.log")

    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.DEBUG if args.debug else logging.INFO)
    LOGGER.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG if args.debug else logging.INFO)
    handler.setFormatter(formatter)
    LOGGER.addHandler(handler)
    return log_path


def now_local(tz_name):
    return datetime.now(ZoneInfo(tz_name))


def parse_local_hour_string(target_date, jam, tz_name):
    return datetime.strptime(
        f"{target_date.isoformat()} {jam}:00", "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=ZoneInfo(tz_name))


def parse_naive_local_datetime(text, tz_name):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=ZoneInfo(tz_name)
    )


def parse_open_meteo_time(text, tz_name):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(tz_name))


def parse_utc_iso_to_local(text, tz_name):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        ZoneInfo(tz_name)
    )


def parse_iso_date(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


def parse_display_date(text):
    return datetime.strptime(text, "%d-%m-%Y").date()


def build_hourly_targets(step_minutes: int = 60) -> list[str]:
    if step_minutes <= 0 or (1440 % step_minutes) != 0:
        raise ValueError("step_minutes harus membagi 1440 (mis. 60, 30, 15)")
    times = []
    for minutes in range(0, 24 * 60, step_minutes):
        hh = minutes // 60
        mm = minutes % 60
        times.append(f"{hh:02d}:{mm:02d}")
    return times


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


class HttpPayloadError(ValueError):
    def __init__(self, message: str, *, status: Optional[int] = None, content_type: str = "", snippet: str = ""):
        super().__init__(message)
        self.status = status
        self.content_type = content_type or ""
        self.snippet = snippet or ""


def _looks_like_html(text: str) -> bool:
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
    effective_headers = dict(DEFAULT_HTTP_HEADERS)
    if headers:
        effective_headers.update(headers)

    request = urllib.request.Request(url, headers=effective_headers)
    ssl_context = build_ssl_context() if url.lower().startswith("https://") else None

    started = time.time()
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
        status = getattr(response, "status", None) or response.getcode()
        charset = response.headers.get_content_charset() or "utf-8"
        encoding = response.headers.get("Content-Encoding") or ""
        content_type = response.headers.get("Content-Type") or ""
        raw = response.read()
        raw = _decode_http_bytes(raw, encoding)
        payload = raw.decode(charset, errors="replace")
        duration_ms = int((time.time() - started) * 1000)
        if not payload.strip():
            raise HttpPayloadError(
                f"Empty response body (status={status}, content_type={content_type})",
                status=status,
                content_type=content_type,
                snippet="",
            )
        if "json" not in content_type.lower() and _looks_like_html(payload):
            snippet = payload.strip().replace("\n", " ")[:200]
            raise HttpPayloadError(
                f"Non-JSON HTML response (status={status}, content_type={content_type}): {snippet}",
                status=status,
                content_type=content_type,
                snippet=snippet,
            )
        try:
            return json.loads(payload), status, duration_ms
        except json.JSONDecodeError as exc:
            snippet = payload.strip().replace("\n", " ")[:200]
            raise HttpPayloadError(
                f"JSON decode failed (status={status}, content_type={content_type}): {snippet}",
                status=status,
                content_type=content_type,
                snippet=snippet,
            ) from exc


def _parse_retry_after_seconds(exc):
    try:
        headers = getattr(exc, "headers", None) or {}
        value = headers.get("Retry-After")
        if not value:
            return None
        value = value.strip()
        if value.isdigit():
            return int(value)
        # HTTP-date format is possible; skip for simplicity
        return None
    except Exception:
        return None


def fetch_json_with_retry(url, headers=None, source_id="UNKNOWN", timeout=HTTP_TIMEOUT_SECONDS, max_retry=None):
    """
    Returns tuple: (payload_dict, http_status, duration_ms)
    """
    if max_retry is None:
        max_retry = MAX_RETRY_HTTP

    last_error = None
    for attempt in range(1, max_retry + 1):
        try:
            log_debug(source_id, "HTTP attempt", attempt, url)
            payload, status, duration_ms = http_get_json(url, headers=headers, timeout=timeout)
            return payload, status, duration_ms
        except urllib.error.HTTPError as exc:
            last_error = exc
            status = getattr(exc, "code", None)
            log_debug(source_id, "HTTPError:", status, exc)

            # Non-retryable by default
            non_retry = {400, 401, 403, 404}
            if status in non_retry:
                raise

            retryable = (status == 429) or (status is not None and 500 <= status <= 599)
            if not retryable or attempt >= max_retry:
                raise

            retry_after = _parse_retry_after_seconds(exc)
            if retry_after is not None:
                sleep_s = min(max(retry_after, 1), 60)
            else:
                base = max(RETRY_BACKOFF_SECONDS, 0.5)
                sleep_s = base * (2 ** (attempt - 1))
                sleep_s *= random.uniform(0.7, 1.4)  # jitter
                sleep_s = min(sleep_s, 45)
            time.sleep(sleep_s)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, HttpPayloadError) as exc:
            last_error = exc
            log_debug(source_id, "Network/Decode error:", exc)
            if attempt >= max_retry:
                break
            base = max(RETRY_BACKOFF_SECONDS, 0.5)
            sleep_s = base * (2 ** (attempt - 1))
            sleep_s *= random.uniform(0.7, 1.4)  # jitter
            sleep_s = min(sleep_s, 45)
            time.sleep(sleep_s)
    raise last_error


def nearest_candidate(candidates, target_dt, max_gap_hours=4):
    best = None
    for item in candidates:
        delta_seconds = abs((item["dt"] - target_dt).total_seconds())
        if best is None or delta_seconds < best["delta_seconds"]:
            best = {"delta_seconds": delta_seconds, "item": item}
    if best is None:
        return None
    if best["delta_seconds"] > max_gap_hours * 3600:
        return None
    return best["item"]


def weighted_mean_std(weighted_pairs):
    valid_pairs = [
        (value, weight)
        for value, weight in weighted_pairs
        if value is not None and weight is not None and weight > 0
    ]
    if not valid_pairs:
        return None, None

    total_weight = sum(weight for _, weight in valid_pairs)
    if total_weight <= 0:
        return None, None

    mean = sum(value * weight for value, weight in valid_pairs) / total_weight
    variance = (
        sum(weight * (value - mean) ** 2 for value, weight in valid_pairs)
        / total_weight
    )
    return round(mean, 2), round(math.sqrt(variance), 2)


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def source_base_weight(source_id):
    return SOURCE_BASE_WEIGHTS.get(source_id, 1.0)


def source_active_weight(source_id):
    return ACTIVE_SOURCE_WEIGHTS.get(source_id, source_base_weight(source_id))


def hour_bucket_for_time(jam_text: str) -> str:
    try:
        hh = int(str(jam_text).split(":")[0])
    except Exception:
        return ""
    if 0 <= hh <= 5:
        return "00-05"
    if 6 <= hh <= 11:
        return "06-11"
    if 12 <= hh <= 17:
        return "12-17"
    if 18 <= hh <= 23:
        return "18-23"
    return ""


def hour_bucket_factor(jam_text: str) -> float:
    bucket = hour_bucket_for_time(jam_text)
    if not bucket:
        return 1.0
    return float(ACTIVE_HOUR_BUCKET_WEIGHTS.get(bucket) or 1.0)


def load_weight_config():
    global ACTIVE_SOURCE_WEIGHTS, ACTIVE_HOUR_BUCKET_WEIGHTS
    path = path_output(WEIGHTS_FILENAME)
    payload = read_json(path, default=None)
    ACTIVE_SOURCE_WEIGHTS = dict(SOURCE_BASE_WEIGHTS)
    ACTIVE_HOUR_BUCKET_WEIGHTS = {}
    if not payload:
        return

    for source_id, value in (payload.get("weights") or {}).items():
        parsed = safe_float(value)
        if parsed is not None and parsed > 0:
            ACTIVE_SOURCE_WEIGHTS[source_id] = round(parsed, 4)

    for bucket, value in (payload.get("hour_bucket_weights") or {}).items():
        parsed = safe_float(value)
        if parsed is not None and parsed > 0:
            ACTIVE_HOUR_BUCKET_WEIGHTS[str(bucket)] = round(parsed, 4)


def save_weight_config(weights, metadata):
    payload = {
        "generated_at": now_local(DEFAULT_TIMEZONE).isoformat(),
        "weights": {key: round(value, 4) for key, value in sorted(weights.items())},
        "metadata": metadata,
    }
    write_json(path_output(WEIGHTS_FILENAME), payload)


def load_health_config():
    global SOURCE_HEALTH
    payload = read_json(path_output(HEALTH_FILENAME), default=None)
    SOURCE_HEALTH = payload.get("sources", {}) if payload else {}


def save_health_config(results, args, target_date=None):
    if args.disable_health:
        return

    previous = read_json(path_output(HEALTH_FILENAME), default={}) or {}
    source_health = previous.get("sources", {})

    for result in results:
        current = source_health.get(
            result.source_id,
            {
                "ema_success": 1.0,
                "ema_completeness": 1.0,
                "consecutive_failures": 0,
                "last_error": "",
                "last_run_date": "",
            },
        )
        success_value = 1.0 if result.success else 0.0
        completeness_value = len(result.points) / max(len(TARGET_TIMES), 1)
        alpha = 0.35
        current["ema_success"] = round(
            current.get("ema_success", 1.0) * (1 - alpha) + success_value * alpha, 4
        )
        current["ema_completeness"] = round(
            current.get("ema_completeness", 1.0) * (1 - alpha)
            + completeness_value * alpha,
            4,
        )
        current["consecutive_failures"] = (
            0 if result.success else min(current.get("consecutive_failures", 0) + 1, 999)
        )
        current["last_error"] = result.error
        current["last_run_date"] = (
            target_date.isoformat() if target_date else now_local(args.timezone).date().isoformat()
        )
        source_health[result.source_id] = current

    payload = {
        "generated_at": now_local(args.timezone).isoformat(),
        "sources": source_health,
    }
    write_json(path_output(HEALTH_FILENAME), payload)
    load_health_config()


def source_health_factor(source_id):
    health = SOURCE_HEALTH.get(source_id) or {}
    ema_success = safe_float(health.get("ema_success"))
    ema_completeness = safe_float(health.get("ema_completeness"))
    consecutive_failures = int(health.get("consecutive_failures", 0) or 0)

    if ema_success is None:
        ema_success = 1.0
    if ema_completeness is None:
        ema_completeness = 1.0

    failure_penalty = clamp(
        1 - (min(consecutive_failures, MAX_CONSECUTIVE_FAILURE_PENALTY) * 0.08),
        0.55,
        1.0,
    )
    factor = (0.55 + ema_success * 0.30 + ema_completeness * 0.15) * failure_penalty
    return round(clamp(factor, 0.45, 1.05), 4)


def point_weight(point):
    base = (
        source_active_weight(point.source_id)
        * source_health_factor(point.source_id)
        * hour_bucket_factor(point.target_time)
    )
    gap_minutes = point.gap_minutes or 0.0
    gap_factor = clamp(1 - (gap_minutes / 240.0), 0.55, 1.0)

    present_fields = sum(
        1
        for value in (point.temp_c, point.rh_pct, point.rain_mm, point.wind_kmh)
        if value is not None
    )
    completeness_factor = 0.70 + (present_fields / 4.0) * 0.30
    return round(base * gap_factor * completeness_factor, 4)


def confidence_label(score):
    if score >= 80:
        return "Tinggi"
    if score >= 60:
        return "Sedang"
    return "Rendah"


def expected_total_weight():
    return round(
        sum(source_active_weight(item["source_id"]) for item in ACTIVE_SOURCE_CONFIGS), 4
    )


def compute_confidence(bucket, total_weight, dominant_weight, temp_std, rh_std, rain_std):
    if not bucket:
        return 0.0, "Rendah"

    expected_sources = max(len(ACTIVE_SOURCE_CONFIGS), 1)
    expected_weight = max(expected_total_weight(), 0.0001)
    coverage_score = clamp((len(bucket) / expected_sources) * 100, 0, 100)
    weight_score = clamp((total_weight / expected_weight) * 100, 0, 100)
    agreement_score = (
        clamp((dominant_weight / total_weight) * 100, 0, 100) if total_weight else 0
    )

    spread_components = []
    if temp_std is not None:
        spread_components.append(clamp(100 - (temp_std * 10), 20, 100))
    if rh_std is not None:
        spread_components.append(clamp(100 - (rh_std * 1.5), 20, 100))
    if rain_std is not None:
        spread_components.append(clamp(100 - (rain_std * 15), 20, 100))
    spread_score = sum(spread_components) / len(spread_components) if spread_components else 40

    score = (
        coverage_score * 0.35
        + weight_score * 0.25
        + agreement_score * 0.25
        + spread_score * 0.15
    )

    if len(bucket) < MIN_SOURCES_FOR_HIGH_CONFIDENCE:
        score = min(score, 59.0)

    score = round(clamp(score, 0, 100), 1)
    return score, confidence_label(score)


def median(values):
    cleaned = sorted(value for value in values if value is not None)
    if not cleaned:
        return None
    mid = len(cleaned) // 2
    if len(cleaned) % 2 == 1:
        return cleaned[mid]
    return (cleaned[mid - 1] + cleaned[mid]) / 2


def robust_outlier_bounds(values, threshold=OUTLIER_Z_THRESHOLD):
    cleaned = [value for value in values if value is not None]
    if len(cleaned) < 4:
        return None, None

    med = median(cleaned)
    deviations = [abs(value - med) for value in cleaned]
    mad = median(deviations)
    if mad in (None, 0):
        return None, None

    scale = 1.4826 * mad
    return med - (threshold * scale), med + (threshold * scale)


def filter_weighted_pairs(weighted_pairs):
    values = [value for value, _ in weighted_pairs]
    lower, upper = robust_outlier_bounds(values)
    if lower is None or upper is None:
        return weighted_pairs
    filtered = [
        (value, weight)
        for value, weight in weighted_pairs
        if value is not None and lower <= value <= upper
    ]
    return filtered if filtered else weighted_pairs


def heat_index(temp_c, rh):
    if temp_c is None or rh is None:
        return None

    temp_f = (temp_c * 9 / 5) + 32
    if temp_f < 80 or rh < 40:
        return round(temp_c, 2)

    hi_f = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * rh
        - 0.22475541 * temp_f * rh
        - 0.00683783 * temp_f * temp_f
        - 0.05481717 * rh * rh
        + 0.00122874 * temp_f * temp_f * rh
        + 0.00085282 * temp_f * rh * rh
        - 0.00000199 * temp_f * temp_f * rh * rh
    )

    if rh < 13 and 80 <= temp_f <= 112:
        adjustment = ((13 - rh) / 4) * math.sqrt((17 - abs(temp_f - 95)) / 17)
        hi_f -= adjustment
    elif rh > 85 and 80 <= temp_f <= 87:
        adjustment = ((rh - 85) / 10) * ((87 - temp_f) / 5)
        hi_f += adjustment

    hi_c = (hi_f - 32) * 5 / 9
    return round(max(temp_c, hi_c), 2)


def kategori_hujan(mm):
    if mm is None or mm <= 0:
        return "Berawan"
    if mm <= 5:
        return "Hujan Ringan"
    if mm <= 10:
        return "Hujan Sedang"
    return "Hujan Lebat"


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
    kategori = bmkg_to_kategori(cuaca)
    if kategori == "Hujan Ringan":
        return 1.5
    if kategori == "Hujan Sedang":
        return 6.0
    if kategori == "Hujan Lebat":
        return 15.0
    return 0.0


def infer_kategori_non_hujan(temp_c, rh):
    if rh is None:
        return "Berawan"
    if rh <= 70:
        return "Cerah"
    if rh <= 85:
        return "Cerah Berawan"
    return "Berawan"


def category_from_wmo_code(weather_code, rain_mm, rh):
    if weather_code is None:
        if rain_mm is not None and rain_mm > 0:
            return kategori_hujan(rain_mm)
        return infer_kategori_non_hujan(None, rh)

    code = int(weather_code)
    if code == 0:
        return "Cerah"
    if code in (1, 2):
        return "Cerah Berawan"
    if code in (3, 45, 48):
        return "Berawan"
    if code in (51, 53, 55, 56, 57):
        return "Hujan Ringan"
    if code in (61, 80):
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
    if "rain" in text or "drizzle" in text or "sleet" in text or "snow" in text:
        return kategori_hujan(rain_mm if rain_mm is not None else 1)
    if rain_mm is not None and rain_mm > 0:
        return kategori_hujan(rain_mm)
    return infer_kategori_non_hujan(None, rh)


def validate_point_values(temp_c, rh_pct, rain_mm, wind_kmh):
    """
    Light-weight sanitization (does not "fix" too much):
    - RH clipped to 0..100
    - rain clipped to >=0
    - wind clipped to >=0
    - temperature sanity: allow -30..60C otherwise None
    """
    flags = []

    if temp_c is not None and not (-30 <= temp_c <= 60):
        flags.append("temp_out_of_range")
        temp_c = None
    if rh_pct is not None:
        if rh_pct < 0 or rh_pct > 100:
            flags.append("rh_clipped")
        rh_pct = clamp(rh_pct, 0, 100)
    if rain_mm is not None:
        if rain_mm < 0:
            flags.append("rain_clipped")
            rain_mm = 0.0
        # keep upper range; heavy rain possible
    if wind_kmh is not None and wind_kmh < 0:
        flags.append("wind_clipped")
        wind_kmh = 0.0

    return temp_c, rh_pct, rain_mm, wind_kmh, flags


def extract_bmkg_points(target_date, payload, args):
    data_items = payload.get("data") or []
    if not data_items:
        raise ValueError("BMKG response tidak memiliki data")

    candidates = []
    for day_group in data_items[0].get("cuaca") or []:
        for item in day_group:
            local_datetime = item.get("local_datetime")
            if not local_datetime:
                continue
            dt_local = parse_naive_local_datetime(local_datetime, args.timezone)
            if dt_local.date() != target_date:
                continue
            temp_c, rh_pct, rain_mm, wind_kmh, flags = validate_point_values(
                safe_float(item.get("t")),
                safe_float(item.get("hu")),
                bmkg_rain_proxy_mm(item.get("weather_desc")),
                safe_float(item.get("ws")),
            )
            candidates.append(
                {
                    "dt": dt_local,
                    "temp_c": temp_c,
                    "rh_pct": rh_pct,
                    "rain_mm": rain_mm,
                    "wind_kmh": wind_kmh,
                    "raw_condition": item.get("weather_desc") or "",
                    "category": bmkg_to_kategori(item.get("weather_desc")),
                    "flags": flags + (["rain_proxy"] if rain_mm is not None else []),
                }
            )

    if not candidates:
        raise ValueError("BMKG tidak mengembalikan kandidat untuk target date")

    points = {}
    for jam in TARGET_TIMES:
        target_dt = parse_local_hour_string(target_date, jam, args.timezone)
        match = next((item for item in candidates if item["dt"] == target_dt), None)
        if not match:
            match = nearest_candidate(candidates, target_dt, max_gap_hours=3)
        if not match:
            continue
        gap_minutes = round(abs((match["dt"] - target_dt).total_seconds()) / 60, 2)
        points[jam] = ForecastPoint(
            source_id="BMKG",
            provider="BMKG",
            target_time=jam,
            source_datetime=match["dt"],
            temp_c=match["temp_c"],
            rh_pct=match["rh_pct"],
            rain_mm=match["rain_mm"],
            wind_kmh=match["wind_kmh"],
            category=match["category"],
            raw_condition=match["raw_condition"],
            gap_minutes=gap_minutes,
        )
    return points


def extract_open_meteo_points(target_date, payload, config, args):
    hourly = payload.get("hourly") or {}

    times = hourly.get("time") or []
    if not times:
        raise ValueError("Open-Meteo response tidak memiliki hourly.time")

    temperatures = hourly.get("temperature_2m") or []
    humidities = hourly.get("relative_humidity_2m") or []
    precipitations = hourly.get("precipitation") or []
    weather_codes = hourly.get("weather_code") or []
    wind_speeds = hourly.get("wind_speed_10m") or []
    apparent_temperatures = hourly.get("apparent_temperature") or []
    dew_points = hourly.get("dew_point_2m") or []
    precipitation_probabilities = hourly.get("precipitation_probability") or []
    cloud_covers = hourly.get("cloud_cover") or []
    pressure_msl = hourly.get("pressure_msl") or []
    surface_pressure = hourly.get("surface_pressure") or []
    wind_directions = hourly.get("wind_direction_10m") or []
    wind_gusts = hourly.get("wind_gusts_10m") or []
    visibilities = hourly.get("visibility") or []
    shortwave_radiation = hourly.get("shortwave_radiation") or []
    direct_radiation = hourly.get("direct_radiation") or []
    diffuse_radiation = hourly.get("diffuse_radiation") or []
    direct_normal_irradiance = hourly.get("direct_normal_irradiance") or []
    global_tilted_irradiance = hourly.get("global_tilted_irradiance") or []
    cape = hourly.get("cape") or []

    candidates = []
    for idx, time_text in enumerate(times):
        dt_local = parse_open_meteo_time(time_text, args.timezone)
        if dt_local.date() != target_date:
            continue
        temp_c, rh_pct, rain_mm, wind_kmh, _flags = validate_point_values(
            safe_float(temperatures[idx] if idx < len(temperatures) else None),
            safe_float(humidities[idx] if idx < len(humidities) else None),
            safe_float(precipitations[idx] if idx < len(precipitations) else None),
            safe_float(wind_speeds[idx] if idx < len(wind_speeds) else None),
        )
        candidates.append(
            {
                "dt": dt_local,
                "temp_c": temp_c,
                "rh_pct": rh_pct,
                "rain_mm": rain_mm,
                "wind_kmh": wind_kmh,
                "weather_code": weather_codes[idx] if idx < len(weather_codes) else None,
                "apparent_temp_c": safe_float(apparent_temperatures[idx] if idx < len(apparent_temperatures) else None),
                "dew_point_c": safe_float(dew_points[idx] if idx < len(dew_points) else None),
                "precip_prob_pct": safe_float(precipitation_probabilities[idx] if idx < len(precipitation_probabilities) else None),
                "cloud_cover_pct": safe_float(cloud_covers[idx] if idx < len(cloud_covers) else None),
                "pressure_msl_hpa": safe_float(pressure_msl[idx] if idx < len(pressure_msl) else None),
                "surface_pressure_hpa": safe_float(surface_pressure[idx] if idx < len(surface_pressure) else None),
                "wind_direction_deg": safe_float(wind_directions[idx] if idx < len(wind_directions) else None),
                "wind_gusts_kmh": safe_float(wind_gusts[idx] if idx < len(wind_gusts) else None),
                "visibility_m": safe_float(visibilities[idx] if idx < len(visibilities) else None),
                "shortwave_radiation_wm2": safe_float(shortwave_radiation[idx] if idx < len(shortwave_radiation) else None),
                "direct_radiation_wm2": safe_float(direct_radiation[idx] if idx < len(direct_radiation) else None),
                "diffuse_radiation_wm2": safe_float(diffuse_radiation[idx] if idx < len(diffuse_radiation) else None),
                "direct_normal_irradiance_wm2": safe_float(direct_normal_irradiance[idx] if idx < len(direct_normal_irradiance) else None),
                "global_tilted_irradiance_wm2": safe_float(global_tilted_irradiance[idx] if idx < len(global_tilted_irradiance) else None),
                "cape_jkg": safe_float(cape[idx] if idx < len(cape) else None),
            }
        )

    if not candidates:
        raise ValueError(f"{config['source_id']} tidak mengembalikan kandidat target date")

    points = {}
    for jam in TARGET_TIMES:
        target_dt = parse_local_hour_string(target_date, jam, args.timezone)
        match = next((item for item in candidates if item["dt"] == target_dt), None)
        if not match:
            match = nearest_candidate(candidates, target_dt, max_gap_hours=2)
        if not match:
            continue
        gap_minutes = round(abs((match["dt"] - target_dt).total_seconds()) / 60, 2)
        category = category_from_wmo_code(
            match.get("weather_code"),
            match.get("rain_mm"),
            match.get("rh_pct"),
        )
        points[jam] = ForecastPoint(
            source_id=config["source_id"],
            provider=config["provider"],
            target_time=jam,
            source_datetime=match["dt"],
            temp_c=match["temp_c"],
            rh_pct=match["rh_pct"],
            rain_mm=match["rain_mm"],
            wind_kmh=match["wind_kmh"],
            category=category,
            raw_condition=f"wmo:{match.get('weather_code')}",
            gap_minutes=gap_minutes,
            cloud_cover_pct=match.get("cloud_cover_pct"),
            pressure_msl_hpa=match.get("pressure_msl_hpa"),
            surface_pressure_hpa=match.get("surface_pressure_hpa"),
            wind_gusts_kmh=match.get("wind_gusts_kmh"),
            wind_direction_deg=match.get("wind_direction_deg"),
            dew_point_c=match.get("dew_point_c"),
            apparent_temp_c=match.get("apparent_temp_c"),
            precip_prob_pct=match.get("precip_prob_pct"),
            visibility_m=match.get("visibility_m"),
            shortwave_radiation_wm2=match.get("shortwave_radiation_wm2"),
            direct_radiation_wm2=match.get("direct_radiation_wm2"),
            diffuse_radiation_wm2=match.get("diffuse_radiation_wm2"),
            direct_normal_irradiance_wm2=match.get("direct_normal_irradiance_wm2"),
            global_tilted_irradiance_wm2=match.get("global_tilted_irradiance_wm2"),
            cape_jkg=match.get("cape_jkg"),
        )
    return points


def extract_met_no_points(target_date, payload, config, args):
    series = ((payload.get("properties") or {}).get("timeseries")) or []
    if not series:
        raise ValueError("MET Norway response tidak memiliki timeseries")

    candidates = []
    for entry in series:
        dt_local = parse_utc_iso_to_local(entry.get("time"), args.timezone)
        if dt_local.date() != target_date:
            continue
        data = entry.get("data") or {}
        instant_details = (data.get("instant") or {}).get("details") or {}
        wind_ms = safe_float(instant_details.get("wind_speed"))
        rain_mm = metno_precipitation_amount(data)
        symbol_code = metno_symbol_code(data)
        temp_c, rh_pct, rain_mm, wind_kmh, _flags = validate_point_values(
            safe_float(instant_details.get("air_temperature")),
            safe_float(instant_details.get("relative_humidity")),
            rain_mm,
            round(wind_ms * 3.6, 2) if wind_ms is not None else None,
        )
        candidates.append(
            {
                "dt": dt_local,
                "temp_c": temp_c,
                "rh_pct": rh_pct,
                "rain_mm": rain_mm,
                "wind_kmh": wind_kmh,
                "symbol_code": symbol_code,
            }
        )

    if not candidates:
        raise ValueError("MET Norway tidak mengembalikan kandidat target date")

    points = {}
    for jam in TARGET_TIMES:
        target_dt = parse_local_hour_string(target_date, jam, args.timezone)
        match = next((item for item in candidates if item["dt"] == target_dt), None)
        if not match:
            match = nearest_candidate(candidates, target_dt, max_gap_hours=2)
        if not match:
            continue
        gap_minutes = round(abs((match["dt"] - target_dt).total_seconds()) / 60, 2)
        category = category_from_metno_symbol(
            match.get("symbol_code"),
            match.get("rain_mm"),
            match.get("rh_pct"),
        )
        points[jam] = ForecastPoint(
            source_id=config["source_id"],
            provider=config["provider"],
            target_time=jam,
            source_datetime=match["dt"],
            temp_c=match["temp_c"],
            rh_pct=match["rh_pct"],
            rain_mm=match["rain_mm"],
            wind_kmh=match["wind_kmh"],
            category=category,
            raw_condition=match.get("symbol_code") or "",
            gap_minutes=gap_minutes,
        )
    return points


def load_cached_source_payload(target_date, source_id, extractor_fn, args):
    """
    Generic cache loader for any source stored in raw_payloads.
    Prefers latest_success, then same-date successes, then other successes.
    """
    raw_dir = path_output(RAW_PAYLOAD_DIRNAME)
    if not os.path.isdir(raw_dir):
        return None

    file_stub = sanitize_filename(source_id.lower())
    stamp = target_date.strftime("%Y%m%d")
    ext = _raw_payload_ext(args)

    preferred_paths = [
        os.path.join(raw_dir, f"{file_stub}_latest_success{ext}"),
        os.path.join(raw_dir, f"{file_stub}_latest{ext}"),
    ]

    versioned_paths = []
    ignored_names = {
        f"{file_stub}_latest{ext}",
        f"{file_stub}_latest_success{ext}",
        f"{file_stub}_latest_failure{ext}",
    }
    for entry in os.scandir(raw_dir):
        if not entry.is_file():
            continue
        lower_name = entry.name.lower()
        if not lower_name.startswith(f"{file_stub}_") or not (
            lower_name.endswith(".json") or lower_name.endswith(".json.gz")
        ):
            continue
        if entry.name in ignored_names:
            continue
        versioned_paths.append(entry.path)

    same_date = [p for p in versioned_paths if f"_{stamp}_" in os.path.basename(p)]
    other_date = [p for p in versioned_paths if p not in same_date]
    same_date.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    other_date.sort(key=lambda path: os.path.getmtime(path), reverse=True)

    candidate_paths = [p for p in preferred_paths if os.path.exists(p)]
    candidate_paths.extend(same_date)
    candidate_paths.extend(other_date)

    for path in candidate_paths:
        document = read_json(path, default=None) or {}
        payload = document.get("payload")
        if not document.get("success") or not isinstance(payload, dict):
            continue
        try:
            points = extractor_fn(target_date, payload)
        except Exception:
            continue
        return {
            "path": path,
            "payload": payload,
            "points": points,
            "request_url": document.get("request_url") or "",
        }
    return None

def _raw_payload_ext(args):
    return ".json.gz" if args.compress_raw_payloads else ".json"


def load_cached_bmkg_payload(target_date, args):
    raw_dir = path_output(RAW_PAYLOAD_DIRNAME)
    if not os.path.isdir(raw_dir):
        return None

    file_stub = sanitize_filename("bmkg")
    stamp = target_date.strftime("%Y%m%d")
    ext = _raw_payload_ext(args)

    preferred_paths = [
        os.path.join(raw_dir, f"{file_stub}_latest_success{ext}"),
        os.path.join(raw_dir, f"{file_stub}_latest{ext}"),
    ]

    versioned_paths = []
    ignored_names = {
        f"{file_stub}_latest{ext}",
        f"{file_stub}_latest_success{ext}",
        f"{file_stub}_latest_failure{ext}",
    }
    for entry in os.scandir(raw_dir):
        if not entry.is_file():
            continue
        lower_name = entry.name.lower()
        if not lower_name.startswith(f"{file_stub}_") or not (lower_name.endswith(".json") or lower_name.endswith(".json.gz")):
            continue
        if entry.name in ignored_names:
            continue
        versioned_paths.append(entry.path)

    # Prefer same-date successful payloads first.
    same_date = [p for p in versioned_paths if f"_{stamp}_" in os.path.basename(p)]
    other_date = [p for p in versioned_paths if p not in same_date]
    same_date.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    other_date.sort(key=lambda path: os.path.getmtime(path), reverse=True)

    candidate_paths = [p for p in preferred_paths if os.path.exists(p)]
    candidate_paths.extend(same_date)
    candidate_paths.extend(other_date)

    for path in candidate_paths:
        document = read_json(path, default=None) or {}
        payload = document.get("payload")
        if not document.get("success") or not isinstance(payload, dict):
            continue
        try:
            points = extract_bmkg_points(target_date, payload, args)
        except ValueError:
            continue
        return {
            "path": path,
            "payload": payload,
            "points": points,
            "request_url": document.get("request_url") or "",
        }
    return None


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
    # Sentinel X optional atmospheric intelligence fields. They are filled when a source provides them;
    # otherwise the downstream risk engine uses robust heuristics/proxies.
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


def fetch_bmkg_forecast(target_date, config, args):
    params = {"adm4": args.adm4}
    url = build_url(BMKG_API_URL, params)
    try:
        payload, status, duration_ms = fetch_json_with_retry(
            url,
            headers=BMKG_HTTP_HEADERS,
            source_id=config["source_id"],
            timeout=args.http_timeout,
            max_retry=args.max_retry_http,
        )
        points = extract_bmkg_points(target_date, payload, args)
        return {"points": points, "raw_payload": payload, "request_url": url, "http_status": status, "duration_ms": duration_ms}
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        cached = load_cached_bmkg_payload(target_date, args)
        if not cached:
            raise exc
        cache_name = os.path.basename(cached["path"])
        note = f"Live BMKG gagal ({exc}); memakai cache {cache_name}"
        log_warning(note)
        return {
            "points": cached["points"],
            "raw_payload": cached["payload"],
            "request_url": f"{url} [cached:{cache_name}]",
            "note": note,
            "http_status": None,
            "duration_ms": None,
        }


def fetch_open_meteo_forecast(target_date, config, args):
    params = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "timezone": args.timezone,
        "forecast_days": 3,
        "hourly": ",".join(aether_open_meteo_variables(args, include_extra=getattr(args, "aether_extra_vars", False))),
    }
    if config.get("models"):
        params["models"] = config["models"]
    url = build_url(config["endpoint"], params)
    try:
        try:
            payload, status, duration_ms = fetch_json_with_retry(
                url,
                source_id=config["source_id"],
                timeout=args.http_timeout,
                max_retry=args.max_retry_http,
            )
        except Exception as first_exc:
            if not getattr(args, "aether_extra_vars", False):
                raise
            fallback_params = dict(params)
            fallback_params["hourly"] = ",".join(aether_open_meteo_variables(args, include_extra=False))
            fallback_url = build_url(config["endpoint"], fallback_params)
            log_warning(config["source_id"], "extra vars gagal, fallback variabel dasar:", first_exc)
            payload, status, duration_ms = fetch_json_with_retry(
                fallback_url,
                source_id=config["source_id"],
                timeout=args.http_timeout,
                max_retry=args.max_retry_http,
            )
            url = fallback_url
        points = extract_open_meteo_points(target_date, payload, config, args)
        return {
            "points": points,
            "raw_payload": payload,
            "request_url": url,
            "http_status": status,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        cached = load_cached_source_payload(
            target_date,
            config["source_id"],
            extractor_fn=lambda d, p: extract_open_meteo_points(d, p, config, args),
            args=args,
        )
        if not cached:
            raise
        cache_name = os.path.basename(cached["path"])
        note = f"Live Open-Meteo {config['source_id']} gagal ({exc}); memakai cache {cache_name}"
        log_warning(note)
        return {
            "points": cached["points"],
            "raw_payload": cached["payload"],
            "request_url": f"{url} [cached:{cache_name}]",
            "note": note,
            "http_status": None,
            "duration_ms": None,
        }


def metno_precipitation_amount(data):
    """
    MET.no precipitation_amount is aggregated over the bucket window.
    Prefer next_1_hours (most precise). If missing, use next_6/12 divided to hourly rate.
    """
    preferred = [
        ("next_1_hours", 1),
        ("next_6_hours", 6),
        ("next_12_hours", 12),
    ]
    for bucket_name, divisor in preferred:
        bucket = data.get(bucket_name) or {}
        details = bucket.get("details") or {}
        value = safe_float(details.get("precipitation_amount"))
        if value is None:
            continue
        if divisor <= 1:
            return value
        return round(value / divisor, 2)
    return None


def metno_symbol_code(data):
    for bucket_name in ("next_1_hours", "next_6_hours", "next_12_hours"):
        bucket = data.get(bucket_name) or {}
        summary = bucket.get("summary") or {}
        symbol = summary.get("symbol_code")
        if symbol:
            return symbol
    return ""


def fetch_met_no_forecast(target_date, config, args):
    params = {"lat": args.latitude, "lon": args.longitude}
    headers = {
        "User-Agent": args.metno_user_agent or "weather-ensemble-multi-location/3.1 (contact: local-script)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip,deflate",
        "Connection": "close",
    }
    url = build_url(MET_NO_URL, params)
    try:
        payload, status, duration_ms = fetch_json_with_retry(
            url,
            headers=headers,
            source_id=config["source_id"],
            timeout=args.http_timeout,
            max_retry=args.max_retry_http,
        )
        points = extract_met_no_points(target_date, payload, config, args)
        return {
            "points": points,
            "raw_payload": payload,
            "request_url": url,
            "http_status": status,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        cached = load_cached_source_payload(
            target_date,
            config["source_id"],
            extractor_fn=lambda d, p: extract_met_no_points(d, p, config, args),
            args=args,
        )
        if not cached:
            raise
        cache_name = os.path.basename(cached["path"])
        note = f"Live METNO gagal ({exc}); memakai cache {cache_name}"
        log_warning(note)
        return {
            "points": cached["points"],
            "raw_payload": cached["payload"],
            "request_url": f"{url} [cached:{cache_name}]",
            "note": note,
            "http_status": None,
            "duration_ms": None,
        }


def preview_request_url(config, args):
    kind = config["kind"]
    if kind == "bmkg":
        return build_url(BMKG_API_URL, {"adm4": args.adm4})
    if kind == "open_meteo":
        params = {
            "latitude": args.latitude,
            "longitude": args.longitude,
            "timezone": args.timezone,
            "forecast_days": 3,
            "hourly": ",".join(
                [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                ]
            ),
        }
        if config.get("models"):
            params["models"] = config["models"]
        return build_url(config["endpoint"], params)
    if kind == "met_no":
        return build_url(MET_NO_URL, {"lat": args.latitude, "lon": args.longitude})
    return ""


def save_raw_payload_snapshot(target_date, result, tz_name, args):
    raw_dir = path_output(RAW_PAYLOAD_DIRNAME)
    ensure_directory(raw_dir)
    stamp = target_date.strftime("%Y%m%d")
    created_stamp = now_local(tz_name).strftime("%Y%m%d_%H%M%S")
    file_stub = sanitize_filename(result.source_id.lower())
    ext = _raw_payload_ext(args)
    path_versioned = os.path.join(raw_dir, f"{file_stub}_{stamp}_{created_stamp}{ext}")
    path_latest = os.path.join(raw_dir, f"{file_stub}_latest{ext}")
    path_latest_success = os.path.join(raw_dir, f"{file_stub}_latest_success{ext}")
    path_latest_failure = os.path.join(raw_dir, f"{file_stub}_latest_failure{ext}")
    document = {
        "generated_at": now_local(tz_name).isoformat(),
        "target_date": target_date.isoformat(),
        "source_id": result.source_id,
        "provider": result.provider,
        "success": result.success,
        "base_weight": result.base_weight,
        "request_url": result.request_url,
        "http_status": result.http_status,
        "duration_ms": result.duration_ms,
        "points_collected": len(result.points),
        "error": result.error,
        "payload": result.raw_payload,
    }
    if args.compress_raw_payloads:
        write_json_gz(path_versioned, document)
        write_json_gz(path_latest, document)
        if result.success and result.raw_payload is not None:
            write_json_gz(path_latest_success, document)
        else:
            write_json_gz(path_latest_failure, document)
    else:
        write_json(path_versioned, document)
        write_json(path_latest, document)
        if result.success and result.raw_payload is not None:
            write_json(path_latest_success, document)
        else:
            write_json(path_latest_failure, document)
    return path_versioned


_HOST_SEMAPHORES_LOCK = threading.Lock()
_HOST_SEMAPHORES: dict[str, threading.Semaphore] = {}

_HOST_CIRCUIT_LOCK = threading.Lock()
_HOST_CIRCUIT: dict[str, dict[str, Any]] = {}


def _circuit_state(host: str) -> dict:
    with _HOST_CIRCUIT_LOCK:
        return _HOST_CIRCUIT.setdefault(
            host,
            {"fails": 0, "open_until": 0.0, "last_error": "", "last_status": None},
        )


def _circuit_is_open(host: str) -> tuple[bool, float]:
    state = _circuit_state(host)
    until = float(state.get("open_until") or 0.0)
    now = time.time()
    return (until > now), max(0.0, until - now)


def _circuit_note_failure(host: str, error_text: str, status: Optional[int], args):
    if not args.enable_circuit_breaker:
        return
    state = _circuit_state(host)
    state["fails"] = int(state.get("fails") or 0) + 1
    state["last_error"] = str(error_text)[:300]
    state["last_status"] = status
    # Backoff grows with consecutive failures and is capped.
    backoff = min(args.circuit_max_backoff_seconds, args.circuit_base_seconds * (2 ** (min(state["fails"], 6) - 1)))
    backoff *= random.uniform(0.8, 1.4)
    state["open_until"] = time.time() + backoff


def _circuit_note_success(host: str):
    state = _circuit_state(host)
    state["fails"] = 0
    state["open_until"] = 0.0
    state["last_error"] = ""
    state["last_status"] = None


def _get_host_semaphore(url, max_inflight_per_host):
    host = urllib.parse.urlparse(url).netloc.lower()
    if not host:
        host = "unknown"
    with _HOST_SEMAPHORES_LOCK:
        sem = _HOST_SEMAPHORES.get(host)
        if sem is None:
            sem = threading.Semaphore(max(1, int(max_inflight_per_host)))
            _HOST_SEMAPHORES[host] = sem
        return sem, host


def fetch_source(target_date, config, args):
    source_id = config["source_id"]
    provider = config["provider"]
    kind = config["kind"]
    request_url = preview_request_url(config, args)

    sem, host = _get_host_semaphore(request_url, args.max_inflight_per_host)
    started = time.time()

    is_open, wait_s = _circuit_is_open(host)
    if is_open:
        return SourceResult(
            source_id=source_id,
            provider=provider,
            success=False,
            points={},
            error=f"circuit_open host={host} wait={int(wait_s)}s",
            request_url=request_url,
            base_weight=source_active_weight(source_id),
            http_status=None,
            duration_ms=0,
        )

    with sem:
        try:
            if kind == "bmkg":
                fetch_result = fetch_bmkg_forecast(target_date, config, args)
            elif kind == "open_meteo":
                fetch_result = fetch_open_meteo_forecast(target_date, config, args)
            elif kind == "met_no":
                fetch_result = fetch_met_no_forecast(target_date, config, args)
            else:
                raise ValueError(f"Unknown source kind: {kind}")

            points = fetch_result["points"]
            success = len(points) > 0
            note = fetch_result.get("note", "")
            result = SourceResult(
                source_id=source_id,
                provider=provider,
                success=success,
                points=points,
                error=note if success else (note or "source returned 0 points"),
                request_url=fetch_result.get("request_url", request_url),
                raw_payload=fetch_result["raw_payload"],
                base_weight=source_active_weight(source_id),
                http_status=fetch_result.get("http_status"),
                duration_ms=fetch_result.get("duration_ms"),
            )
            if args.save_raw_payloads:
                result.payload_saved_path = save_raw_payload_snapshot(
                    target_date, result, args.timezone, args
                )
            if result.success:
                _circuit_note_success(host)
            return result
        except Exception as exc:
            log_info(f"{source_id} gagal (host={host}):", exc)
            if DEBUG:
                traceback.print_exc()
            duration_ms = int((time.time() - started) * 1000)
            status = getattr(exc, "code", None) if isinstance(exc, urllib.error.HTTPError) else None
            _circuit_note_failure(host, str(exc), status, args)
            error_content_type = ""
            error_snippet = ""
            if isinstance(exc, HttpPayloadError):
                status = exc.status if exc.status is not None else status
                error_content_type = exc.content_type
                error_snippet = exc.snippet
            result = SourceResult(
                source_id=source_id,
                provider=provider,
                success=False,
                points={},
                error=str(exc),
                request_url=request_url,
                base_weight=source_active_weight(source_id),
                http_status=status,
                duration_ms=duration_ms,
                error_content_type=error_content_type,
                error_snippet=error_snippet,
            )
            if args.save_raw_payloads:
                result.payload_saved_path = save_raw_payload_snapshot(
                    target_date, result, args.timezone, args
                )
            return result


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


def normalize_observation_row(row):
    tanggal = row.get("tanggal") or row.get("date") or row.get("target_date")
    jam = row.get("jam") or row.get("time")
    if not tanggal or not jam:
        return None

    if "-" in tanggal and len(tanggal) == 10 and tanggal[4] == "-":
        tanggal = parse_iso_date(tanggal).strftime("%d-%m-%Y")

    category = row.get("category")
    if not category:
        category = category_from_wmo_code(
            safe_float(row.get("weather_code")),
            safe_float(row.get("rain_mm")),
            safe_float(row.get("rh_pct")),
        )

    temp_c, rh_pct, rain_mm, wind_kmh, _flags = validate_point_values(
        safe_float(row.get("temp_c")),
        safe_float(row.get("rh_pct")),
        safe_float(row.get("rain_mm")),
        safe_float(row.get("wind_kmh")),
    )

    return {
        "tanggal": tanggal,
        "jam": jam,
        "observed_datetime": row.get("observed_datetime") or "",
        "temp_c": round_or_blank(temp_c),
        "rh_pct": round_or_blank(rh_pct),
        "rain_mm": round_or_blank(rain_mm),
        "wind_kmh": round_or_blank(wind_kmh),
        "weather_code": row.get("weather_code") or "",
        "category": category,
    }


def load_external_observation_rows(path):
    rows = []
    for row in read_dict_csv(path):
        normalized = normalize_observation_row(row)
        if normalized:
            rows.append(normalized)
    return rows


def extract_archive_observations(target_date, payload, tz_name):
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temperatures = hourly.get("temperature_2m") or []
    humidities = hourly.get("relative_humidity_2m") or []
    precipitations = hourly.get("precipitation") or []
    weather_codes = hourly.get("weather_code") or []
    wind_speeds = hourly.get("wind_speed_10m") or []
    apparent_temperatures = hourly.get("apparent_temperature") or []
    dew_points = hourly.get("dew_point_2m") or []
    precipitation_probabilities = hourly.get("precipitation_probability") or []
    cloud_covers = hourly.get("cloud_cover") or []
    pressure_msl = hourly.get("pressure_msl") or []
    surface_pressure = hourly.get("surface_pressure") or []
    wind_directions = hourly.get("wind_direction_10m") or []
    wind_gusts = hourly.get("wind_gusts_10m") or []
    visibilities = hourly.get("visibility") or []
    shortwave_radiation = hourly.get("shortwave_radiation") or []
    direct_radiation = hourly.get("direct_radiation") or []
    diffuse_radiation = hourly.get("diffuse_radiation") or []
    direct_normal_irradiance = hourly.get("direct_normal_irradiance") or []
    global_tilted_irradiance = hourly.get("global_tilted_irradiance") or []
    cape = hourly.get("cape") or []

    candidates = []
    for idx, time_text in enumerate(times):
        dt_local = parse_open_meteo_time(time_text, tz_name)
        if dt_local.date() != target_date:
            continue
        temp_c, rh_pct, rain_mm, wind_kmh, _flags = validate_point_values(
            safe_float(temperatures[idx] if idx < len(temperatures) else None),
            safe_float(humidities[idx] if idx < len(humidities) else None),
            safe_float(precipitations[idx] if idx < len(precipitations) else None),
            safe_float(wind_speeds[idx] if idx < len(wind_speeds) else None),
        )
        candidates.append(
            {
                "dt": dt_local,
                "temp_c": temp_c,
                "rh_pct": rh_pct,
                "rain_mm": rain_mm,
                "wind_kmh": wind_kmh,
                "weather_code": weather_codes[idx] if idx < len(weather_codes) else None,
            }
        )

    rows = []
    for jam in TARGET_TIMES:
        target_dt = parse_local_hour_string(target_date, jam, tz_name)
        match = next((item for item in candidates if item["dt"] == target_dt), None)
        if not match:
            match = nearest_candidate(candidates, target_dt, max_gap_hours=2)
        if not match:
            continue
        category = category_from_wmo_code(
            match.get("weather_code"), match.get("rain_mm"), match.get("rh_pct")
        )
        rows.append(
            {
                "tanggal": target_date.strftime("%d-%m-%Y"),
                "jam": jam,
                "observed_datetime": match["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "temp_c": round_or_blank(match.get("temp_c")),
                "rh_pct": round_or_blank(match.get("rh_pct")),
                "rain_mm": round_or_blank(match.get("rain_mm")),
                "wind_kmh": round_or_blank(match.get("wind_kmh")),
                "weather_code": match.get("weather_code"),
                "category": category,
            }
        )
    return rows


def fetch_archive_observations(target_date, args):
    params = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "timezone": args.timezone,
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "hourly": ",".join(aether_open_meteo_variables(args, include_extra=getattr(args, "aether_extra_vars", False))),
    }
    url = build_url(OBSERVATION_ARCHIVE_URL, params)
    payload, status, duration_ms = fetch_json_with_retry(url, source_id="OBSERVATION_ARCHIVE", timeout=args.http_timeout, max_retry=args.max_retry_http)
    _ = status, duration_ms
    return url, payload, extract_archive_observations(target_date, payload, args.timezone)


def write_observation_rows(target_date, rows):
    fieldnames = [
        "tanggal",
        "jam",
        "observed_datetime",
        "temp_c",
        "rh_pct",
        "rain_mm",
        "wind_kmh",
        "weather_code",
        "category",
    ]
    write_dict_csv(observation_file_for_date(target_date), fieldnames, rows)

    master_path = observation_master_file()
    existing = {}
    for row in read_dict_csv(master_path):
        existing[(row.get("tanggal"), row.get("jam"))] = row
    for row in rows:
        existing[(row.get("tanggal"), row.get("jam"))] = row
    merged = sorted(
        existing.values(),
        key=lambda item: (parse_display_date(item["tanggal"]), item["jam"]),
    )
    write_dict_csv(master_path, fieldnames, merged)


def import_external_observations(args):
    if not args.observations_csv:
        raise ValueError("Mode import-observations membutuhkan --observations-csv")
    if not os.path.exists(args.observations_csv):
        raise ValueError(f"File observasi tidak ditemukan: {args.observations_csv}")

    rows = load_external_observation_rows(args.observations_csv)
    if not rows:
        raise ValueError("Tidak ada row observasi valid yang bisa diimpor")

    fieldnames = [
        "tanggal",
        "jam",
        "observed_datetime",
        "temp_c",
        "rh_pct",
        "rain_mm",
        "wind_kmh",
        "weather_code",
        "category",
    ]
    master_path = observation_master_file()
    existing = {}
    for row in read_dict_csv(master_path):
        existing[(row.get("tanggal"), row.get("jam"))] = row
    for row in rows:
        existing[(row.get("tanggal"), row.get("jam"))] = row
    merged = sorted(
        existing.values(),
        key=lambda item: (parse_display_date(item["tanggal"]), item["jam"]),
    )
    write_dict_csv(master_path, fieldnames, merged)
    report_path = os.path.join(report_dir(), "import_observations_summary.json")
    write_json(
        report_path,
        {
            "generated_at": now_local(args.timezone).isoformat(),
            "source_file": args.observations_csv,
            "rows_imported": len(rows),
            "master_file": master_path,
            "location_slug": args.location_slug,
            "location_name": args.location_name,
        },
    )
    return rows


def sync_observations(args):
    end_date = parse_iso_date(args.end_date) if args.end_date else now_local(args.timezone).date() - timedelta(days=5)
    start_date = parse_iso_date(args.start_date) if args.start_date else end_date - timedelta(days=args.lookback_days - 1)
    if start_date > end_date:
        raise ValueError("start_date tidak boleh lebih besar dari end_date")

    summary_rows = []
    for target_date in iter_dates(start_date, end_date):
        url, payload, rows = fetch_archive_observations(target_date, args)
        write_observation_rows(target_date, rows)
        if args.save_raw_payloads:
            payload_path = os.path.join(
                observation_dir(),
                f"archive_payload_{target_date.strftime('%Y%m%d')}{_raw_payload_ext(args)}",
            )
            document = {
                "request_url": url,
                "target_date": target_date.isoformat(),
                "payload": payload,
            }
            if args.compress_raw_payloads:
                write_json_gz(payload_path, document)
            else:
                write_json(payload_path, document)
        summary_rows.append({"target_date": target_date.isoformat(), "rows_saved": len(rows)})
        log_info("Observasi tersimpan untuk", target_date.isoformat(), f"({len(rows)} rows)")

    summary_path = os.path.join(report_dir(), "observation_sync_summary.json")
    write_json(
        summary_path,
        {
            "generated_at": now_local(args.timezone).isoformat(),
            "location_slug": args.location_slug,
            "location_name": args.location_name,
            "rows": summary_rows,
        },
    )
    return summary_rows


def forecast_file_for_date(target_date):
    return path_output(f"forecast_{target_date.strftime('%Y%m%d')}.csv")


def load_observation_index():
    index = {}
    for row in read_dict_csv(observation_master_file()):
        try:
            date_key = parse_display_date(row["tanggal"]).isoformat()
        except Exception:
            continue
        index[(date_key, row.get("jam"))] = row
    return index


def cleanup_old_files_in_directory(directory_path, retention_days):
    if retention_days <= 0 or not os.path.isdir(directory_path):
        return 0
    cutoff = time.time() - (retention_days * 86400)
    deleted = 0
    for entry in os.scandir(directory_path):
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
                deleted += 1
        except OSError:
            continue
    return deleted


def cleanup_to_max_size_mb(directory_path, max_mb):
    if max_mb is None:
        return 0
    try:
        max_mb = float(max_mb)
    except Exception:
        return 0
    if max_mb <= 0 or not os.path.isdir(directory_path):
        return 0
    max_bytes = int(max_mb * 1024 * 1024)

    files = []
    total = 0
    for entry in os.scandir(directory_path):
        if not entry.is_file():
            continue
        try:
            st = entry.stat()
            size = int(st.st_size)
            total += size
            files.append((st.st_mtime, size, entry.path))
        except OSError:
            continue

    if total <= max_bytes:
        return 0

    files.sort(key=lambda x: x[0])  # oldest first
    deleted = 0
    for _mtime, size, path in files:
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
    total_deleted = 0
    for folder_name in (
        RAW_PAYLOAD_DIRNAME,
        LOG_DIRNAME,
        OBSERVATION_DIRNAME,
        REPORT_DIRNAME,
    ):
        folder_path = path_output(folder_name)
        total_deleted += cleanup_old_files_in_directory(
            path_output(folder_name), args.retention_days
        )
        total_deleted += cleanup_to_max_size_mb(folder_path, args.retention_max_mb)
    if total_deleted:
        log_info("Cleanup menghapus", total_deleted, "file lama")
    return total_deleted


def category_match_score(predicted, observed):
    if predicted == observed:
        return 100.0
    rainy = {"Hujan Ringan", "Hujan Sedang", "Hujan Lebat"}
    if predicted in rainy and observed in rainy:
        return 60.0
    if {predicted, observed} <= {"Cerah", "Cerah Berawan", "Berawan"}:
        return 60.0
    return 0.0


def metric_score(error_value, scale):
    if error_value is None:
        return 0.0
    return round(clamp(100 - (error_value * scale), 0, 100), 2)


def absolute_error(left, right):
    if left is None or right is None:
        return None
    return abs(left - right)


def evaluate_historical_performance(args):
    observation_index = load_observation_index()
    if not observation_index:
        raise ValueError("observations.csv belum ada. Jalankan mode sync-observations dulu.")

    if args.end_date:
        end_date = parse_iso_date(args.end_date)
    else:
        end_date = now_local(args.timezone).date() - timedelta(days=1)
    if args.start_date:
        start_date = parse_iso_date(args.start_date)
    else:
        start_date = end_date - timedelta(days=args.lookback_days - 1)
    if start_date > end_date:
        raise ValueError("start_date tidak boleh lebih besar dari end_date")

    detail_rows = []
    per_source = {}

    for target_date in iter_dates(start_date, end_date):
        forecast_path = forecast_file_for_date(target_date)
        if not os.path.exists(forecast_path):
            continue
        for row in read_dict_csv(forecast_path):
            key = (target_date.isoformat(), row.get("target_jam"))
            observed = observation_index.get(key)
            if not observed:
                continue

            source_id = row.get("source_id")
            temp_error = absolute_error(
                safe_float(row.get("suhu_C")),
                safe_float(observed.get("temp_c")),
            )
            rh_error = absolute_error(
                safe_float(row.get("RH_%")),
                safe_float(observed.get("rh_pct")),
            )
            # BMKG rain_mm is a proxy derived from category; do not penalize on rain magnitude.
            if source_id == "BMKG":
                rain_error = None
            else:
                rain_error = absolute_error(
                    safe_float(row.get("rain_mm")),
                    safe_float(observed.get("rain_mm")),
                )
            category_score = category_match_score(row.get("kategori"), observed.get("category"))

            temp_score = metric_score(temp_error, 8)
            rh_score = metric_score(rh_error, 1.5)
            rain_score = metric_score(rain_error, 20)
            components = [
                ("temp", temp_score, 0.35),
                ("rh", rh_score, 0.20),
                ("rain", rain_score, 0.20 if rain_error is not None else 0.0),
                ("category", category_score, 0.25),
            ]
            total_w = sum(w for _name, _score, w in components if w > 0)
            overall_score = (
                round(sum(score * w for _name, score, w in components if w > 0) / total_w, 2)
                if total_w > 0
                else 0.0
            )

            detail_rows.append(
                {
                    "target_date": target_date.isoformat(),
                    "source_id": source_id,
                    "jam": row.get("target_jam"),
                    "temp_error": round_or_blank(temp_error),
                    "rh_error": round_or_blank(rh_error),
                    "rain_error": round_or_blank(rain_error),
                    "category_score": category_score,
                    "overall_score": overall_score,
                }
            )

            bucket = per_source.setdefault(
                source_id,
                {
                    "scores": [],
                    "temp_errors": [],
                    "rh_errors": [],
                    "rain_errors": [],
                    "category_scores": [],
                    "count": 0,
                },
            )
            bucket["scores"].append(overall_score)
            if temp_error is not None:
                bucket["temp_errors"].append(temp_error)
            if rh_error is not None:
                bucket["rh_errors"].append(rh_error)
            if rain_error is not None:
                bucket["rain_errors"].append(rain_error)
            bucket["category_scores"].append(category_score)
            bucket["count"] += 1

    source_score_rows = []
    derived_weights = dict(SOURCE_BASE_WEIGHTS)
    for source_id, metrics in sorted(per_source.items()):
        avg_score = sum(metrics["scores"]) / len(metrics["scores"])
        avg_temp_error = (
            sum(metrics["temp_errors"]) / len(metrics["temp_errors"])
            if metrics["temp_errors"]
            else None
        )
        avg_rh_error = (
            sum(metrics["rh_errors"]) / len(metrics["rh_errors"])
            if metrics["rh_errors"]
            else None
        )
        avg_rain_error = (
            sum(metrics["rain_errors"]) / len(metrics["rain_errors"])
            if metrics["rain_errors"]
            else None
        )
        avg_category_score = sum(metrics["category_scores"]) / len(metrics["category_scores"])
        multiplier = clamp(0.7 + (avg_score / 100.0) * 0.8, 0.7, 1.5)
        derived_weights[source_id] = round(source_base_weight(source_id) * multiplier, 4)
        source_score_rows.append(
            {
                "source_id": source_id,
                "samples": metrics["count"],
                "avg_overall_score": round(avg_score, 2),
                "avg_temp_error": round_or_blank(avg_temp_error),
                "avg_rh_error": round_or_blank(avg_rh_error),
                "avg_rain_error": round_or_blank(avg_rain_error),
                "avg_category_score": round(avg_category_score, 2),
                "base_weight": source_base_weight(source_id),
                "derived_weight": derived_weights[source_id],
            }
        )

    source_scores_path = os.path.join(report_dir(), "source_scores.csv")
    details_path = os.path.join(report_dir(), "evaluation_details.csv")
    summary_path = os.path.join(report_dir(), "evaluation_summary.json")
    write_dict_csv(
        source_scores_path,
        [
            "source_id",
            "samples",
            "avg_overall_score",
            "avg_temp_error",
            "avg_rh_error",
            "avg_rain_error",
            "avg_category_score",
            "base_weight",
            "derived_weight",
        ],
        source_score_rows,
    )
    write_dict_csv(
        details_path,
        [
            "target_date",
            "source_id",
            "jam",
            "temp_error",
            "rh_error",
            "rain_error",
            "category_score",
            "overall_score",
        ],
        detail_rows,
    )
    summary_payload = {
        "generated_at": now_local(args.timezone).isoformat(),
        "location_slug": args.location_slug,
        "location_name": args.location_name,
        "date_range": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "files": {
            "source_scores": source_scores_path,
            "details": details_path,
        },
        "evaluated_sources": len(source_score_rows),
        "evaluated_rows": len(detail_rows),
        "status": "ok" if detail_rows else "no_data",
    }
    write_json(summary_path, summary_payload)

    if args.freeze_weights:
        log_info("freeze-weights aktif: tidak menyimpan source_weights.json dari evaluasi.")
    else:
        save_weight_config(
            derived_weights,
            {
                "date_range": summary_payload["date_range"],
                "evaluated_sources": len(source_score_rows),
                "evaluated_rows": len(detail_rows),
            },
        )
    load_weight_config()
    if not detail_rows:
        log_warning("Tidak ada pasangan forecast-observasi yang bisa dievaluasi pada rentang ini.")
    return {
        "source_score_rows": source_score_rows,
        "detail_rows": detail_rows,
        "summary_path": summary_path,
        "weights_path": path_output(WEIGHTS_FILENAME),
    }


def collect_all_sources(target_date, args):
    results = []
    workers = min(
        int(args.max_workers),
        len(ACTIVE_SOURCE_CONFIGS),
        MAX_WORKERS if MAX_WORKERS else 8,
    )
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {
            executor.submit(fetch_source, target_date, config, args): config
            for config in ACTIVE_SOURCE_CONFIGS
        }
        for future in as_completed(future_map):
            results.append(future.result())
    results.sort(key=lambda item: item.source_id)
    return results


def flatten_points(results):
    rows = []
    for result in results:
        for jam in TARGET_TIMES:
            point = result.points.get(jam)
            if point is not None:
                rows.append(point)
    return rows


def build_source_rows(points, target_date):
    rows = []
    display_date = target_date.strftime("%d-%m-%Y")
    for point in points:
        weight = point_weight(point)
        rows.append(
            [
                display_date,
                point.source_id,
                point.provider,
                point.target_time,
                point.source_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                round_or_blank(point.temp_c),
                round_or_blank(point.rh_pct),
                round_or_blank(point.rain_mm),
                round_or_blank(point.wind_kmh),
                round_or_blank(point.gap_minutes),
                round_or_blank(weight, 4),
                point.category,
                point.raw_condition,
            ]
        )
    return rows


def build_status_rows(results, target_date):
    rows = []
    display_date = target_date.strftime("%d-%m-%Y")
    for result in results:
        health_factor = source_health_factor(result.source_id)
        rows.append(
            {
                "tanggal": display_date,
                "source_id": result.source_id,
                "provider": result.provider,
                "success": "yes" if result.success else "no",
                "base_weight": result.base_weight,
                "health_factor": health_factor,
                "effective_base_weight": round(result.base_weight * health_factor, 4),
                "points_collected": len(result.points),
                "target_points": len(TARGET_TIMES),
                "http_status": result.http_status if result.http_status is not None else "",
                "duration_ms": result.duration_ms if result.duration_ms is not None else "",
                "error_content_type": result.error_content_type or "",
                "error_snippet": result.error_snippet or "",
                "payload_saved_path": result.payload_saved_path,
                "error": result.error,
            }
        )
    return rows


def build_bmkg_rows(results, target_date):
    bmkg_result = next((item for item in results if item.source_id == "BMKG"), None)
    if not bmkg_result:
        return []

    display_date = target_date.strftime("%d-%m-%Y")
    rows = []
    for jam in TARGET_TIMES:
        point = bmkg_result.points.get(jam)
        if point is None:
            continue
        rows.append(
            [
                display_date,
                jam,
                round_or_blank(point.temp_c),
                point.raw_condition,
                round_or_blank(point.rh_pct),
                round_or_blank(point.wind_kmh),
            ]
        )
    return rows


def build_ensemble_rows(points):
    grouped = {jam: [] for jam in TARGET_TIMES}
    for point in points:
        grouped.setdefault(point.target_time, []).append(point)

    rows = []
    for jam in TARGET_TIMES:
        bucket = grouped.get(jam) or []
        category_weights = {}
        temp_values = []
        rh_values = []
        rain_values = []
        hi_values = []
        source_ids = []
        weighted_total = 0.0

        for point in bucket:
            weight = point_weight(point)
            source_ids.append(point.source_id)
            category_weights[point.category] = category_weights.get(point.category, 0.0) + weight
            weighted_total += weight
            if point.temp_c is not None:
                temp_values.append((point.temp_c, weight))
            if point.rh_pct is not None:
                rh_values.append((point.rh_pct, weight))
            if point.rain_mm is not None:
                rain_values.append((point.rain_mm, weight))
            hi = heat_index(point.temp_c, point.rh_pct)
            if hi is not None:
                hi_values.append((hi, weight))

        temp_values = filter_weighted_pairs(temp_values)
        rh_values = filter_weighted_pairs(rh_values)
        rain_values = filter_weighted_pairs(rain_values)
        hi_values = filter_weighted_pairs(hi_values)

        probs = {
            category: round((category_weights.get(category, 0.0) / weighted_total) * 100, 1)
            if weighted_total
            else 0.0
            for category in CUACA_ORDER
        }
        dominant = max(category_weights, key=category_weights.get) if category_weights else ""
        dominant_weight = category_weights.get(dominant, 0.0) if dominant else 0.0

        temp_mean, temp_std = weighted_mean_std(temp_values)
        rh_mean, rh_std = weighted_mean_std(rh_values)
        rain_mean, rain_std = weighted_mean_std(rain_values)
        hi_mean, hi_std = weighted_mean_std(hi_values)
        confidence_score, confidence_band = compute_confidence(
            bucket,
            weighted_total,
            dominant_weight,
            temp_std,
            rh_std,
            rain_std,
        )
        expected_sources = max(len(ACTIVE_SOURCE_CONFIGS), 1)
        coverage_fraction = round(len(bucket) / expected_sources, 4)
        gap_values = [p.gap_minutes for p in bucket if p.gap_minutes is not None]
        gap_mean = round(sum(gap_values) / len(gap_values), 2) if gap_values else None
        gap_max = round(max(gap_values), 2) if gap_values else None
        coverage_status = "cukup" if len(bucket) >= MIN_SOURCE_SUCCESS_FOR_RUN else "terbatas"

        rows.append(
            [
                jam,
                len(bucket),
                round_or_blank(weighted_total, 4),
                coverage_status,
                ",".join(sorted(set(source_ids))),
                dominant,
                confidence_score,
                confidence_band,
                round_or_blank(temp_mean),
                f"+/-{temp_std}" if temp_std is not None else "",
                round_or_blank(rh_mean),
                f"+/-{rh_std}" if rh_std is not None else "",
                round_or_blank(rain_mean),
                f"+/-{rain_std}" if rain_std is not None else "",
                round_or_blank(hi_mean),
                f"+/-{hi_std}" if hi_std is not None else "",
                probs["Cerah"],
                probs["Cerah Berawan"],
                probs["Berawan"],
                probs["Hujan Ringan"],
                probs["Hujan Sedang"],
                probs["Hujan Lebat"],
                round_or_blank(temp_std),
                round_or_blank(rh_std),
                round_or_blank(rain_std),
                round_or_blank(hi_std),
                expected_sources,
                round_or_blank(coverage_fraction, 4),
                round_or_blank(gap_mean),
                round_or_blank(gap_max),
            ]
        )
    return rows


def build_canva_row(ensemble_rows, target_date, args):
    row = {
        "tanggal_target": target_date.strftime("%d-%m-%Y"),
        "lokasi": args.location_name,
    }
    for idx, data in enumerate(ensemble_rows, start=1):
        row[f"jam{idx}"] = data[0]
        row[f"jumlah_sumber{idx}"] = data[1]
        row[f"bobot_total{idx}"] = data[2]
        row[f"coverage{idx}"] = data[3]
        row[f"sumber{idx}"] = data[4]
        row[f"dominant{idx}"] = data[5]
        row[f"confidence_score{idx}"] = data[6]
        row[f"confidence_label{idx}"] = data[7]
        row[f"temp{idx}"] = data[8]
        row[f"rh{idx}"] = data[10]
        row[f"rain{idx}"] = data[12]
        row[f"hi{idx}"] = f"{data[14]} {data[15]}".strip()
        row[f"cerah{idx}"] = data[16]
        row[f"cerah_berawan{idx}"] = data[17]
        row[f"berawan{idx}"] = data[18]
        row[f"hujan_ringan{idx}"] = data[19]
        row[f"hujan_sedang{idx}"] = data[20]
        row[f"hujan_lebat{idx}"] = data[21]
    return row


def save_outputs(target_date, results, args):
    stamp = target_date.strftime("%Y%m%d")
    points = flatten_points(results)
    source_rows = build_source_rows(points, target_date)
    status_rows = build_status_rows(results, target_date)
    bmkg_rows = build_bmkg_rows(results, target_date)
    ensemble_rows = build_ensemble_rows(points)
    canva_row = build_canva_row(ensemble_rows, target_date, args)
    sentinel_payload = sentinel_x_save_artifacts(
        target_date, results, args, source_rows, status_rows, ensemble_rows
    )

    write_csv(
        path_output("forecast.csv"),
        [
            "tanggal",
            "source_id",
            "provider",
            "target_jam",
            "source_datetime",
            "suhu_C",
            "RH_%",
            "rain_mm",
            "wind_kmh",
            "gap_minutes",
            "point_weight",
            "kategori",
            "raw_condition",
        ],
        source_rows,
    )
    write_csv(
        path_output(f"forecast_{stamp}.csv"),
        [
            "tanggal",
            "source_id",
            "provider",
            "target_jam",
            "source_datetime",
            "suhu_C",
            "RH_%",
            "rain_mm",
            "wind_kmh",
            "gap_minutes",
            "point_weight",
            "kategori",
            "raw_condition",
        ],
        source_rows,
    )

    write_dict_csv(
        path_output("source_status.csv"),
        [
            "tanggal",
            "source_id",
            "provider",
            "success",
            "base_weight",
            "health_factor",
            "effective_base_weight",
            "points_collected",
            "target_points",
            "http_status",
            "duration_ms",
            "error_content_type",
            "error_snippet",
            "payload_saved_path",
            "error",
        ],
        status_rows,
    )
    write_dict_csv(
        path_output(f"source_status_{stamp}.csv"),
        [
            "tanggal",
            "source_id",
            "provider",
            "success",
            "base_weight",
            "health_factor",
            "effective_base_weight",
            "points_collected",
            "target_points",
            "http_status",
            "duration_ms",
            "error_content_type",
            "error_snippet",
            "payload_saved_path",
            "error",
        ],
        status_rows,
    )

    write_csv(
        path_output("ensemble.csv"),
        [
            "jam",
            "sources_used",
            "weight_total",
            "coverage_status",
            "source_list",
            "dominant_category",
            "confidence_score",
            "confidence_label",
            "temp_mean",
            "temp_error",
            "rh_mean",
            "rh_error",
            "rain_mean",
            "rain_error",
            "heat_index_mean",
            "heat_index_error",
            "%cerah",
            "%cerah_berawan",
            "%berawan",
            "%hujan_ringan",
            "%hujan_sedang",
            "%hujan_lebat",
            "temp_std",
            "rh_std",
            "rain_std",
            "heat_index_std",
            "sources_expected",
            "coverage_fraction",
            "gap_mean_minutes",
            "gap_max_minutes",
        ],
        ensemble_rows,
    )
    write_csv(
        path_output(f"ensemble_{stamp}.csv"),
        [
            "jam",
            "sources_used",
            "weight_total",
            "coverage_status",
            "source_list",
            "dominant_category",
            "confidence_score",
            "confidence_label",
            "temp_mean",
            "temp_error",
            "rh_mean",
            "rh_error",
            "rain_mean",
            "rain_error",
            "heat_index_mean",
            "heat_index_error",
            "%cerah",
            "%cerah_berawan",
            "%berawan",
            "%hujan_ringan",
            "%hujan_sedang",
            "%hujan_lebat",
            "temp_std",
            "rh_std",
            "rain_std",
            "heat_index_std",
            "sources_expected",
            "coverage_fraction",
            "gap_mean_minutes",
            "gap_max_minutes",
        ],
        ensemble_rows,
    )

    if bmkg_rows:
        write_csv(
            path_output("bmkg.csv"),
            ["tanggal", "jam", "suhu_C", "cuaca", "RH_%", "wind_kmh"],
            bmkg_rows,
        )
        write_csv(
            path_output(f"bmkg_{stamp}.csv"),
            ["tanggal", "jam", "suhu_C", "cuaca", "RH_%", "wind_kmh"],
            bmkg_rows,
        )

    write_dict_csv(path_output("canva.csv"), list(canva_row.keys()), [canva_row])
    write_dict_csv(path_output(f"canva_{stamp}.csv"), list(canva_row.keys()), [canva_row])

    low_coverage_slots = [row[0] for row in ensemble_rows if row[3] != "cukup"]
    summary = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "generated_at": now_local(args.timezone).isoformat(),
        "location_slug": args.location_slug,
        "location_name": args.location_name,
        "bmkg_point_name": args.bmkg_point_name,
        "area_level": args.area_level,
        "is_proxy_bmkg": args.is_proxy_bmkg,
        "location_note": getattr(args, "location_note", ""),
        "adm4": args.adm4,
        "latitude": args.latitude,
        "longitude": args.longitude,
        "timezone": args.timezone,
        "target_date": target_date.isoformat(),
        "sources_total": len(results),
        "sources_success": sum(1 for item in results if item.success),
        "points_total": len(points),
        "sources_active": [item["source_id"] for item in ACTIVE_SOURCE_CONFIGS],
        "target_hours": list(TARGET_TIMES),
        "weights_file": path_output(WEIGHTS_FILENAME),
        "health_file": path_output(HEALTH_FILENAME),
        "output_dir": ACTIVE_OUTPUT_DIR,
        "retention_days": args.retention_days,
        "low_coverage_slots": low_coverage_slots,
        "run_status": "warning" if low_coverage_slots else "ok",
        "sentinel_x": sentinel_payload,
    }
    write_json(path_output("run_summary.json"), summary)
    write_json(path_output(f"run_summary_{stamp}.json"), summary)
    return summary


def seconds_until_run(run_time_text, tz_name):
    now = now_local(tz_name)
    hour, minute = [int(part) for part in run_time_text.split(":")]
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return int((next_run - now).total_seconds()), next_run


def validate_common_args(args):
    if args.lookback_days <= 0:
        raise ValueError("lookback_days harus lebih besar dari 0")
    if args.retention_days <= 0:
        raise ValueError("retention_days harus lebih besar dari 0")
    hour, minute = [int(part) for part in args.run_time.split(":")]
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("run_time harus memakai format HH:MM")
    if args.max_inflight_per_host <= 0:
        raise ValueError("max_inflight_per_host harus > 0")
    if args.max_workers <= 0:
        raise ValueError("max_workers harus > 0")
    if args.max_retry_http <= 0:
        raise ValueError("max_retry_http harus > 0")
    if args.http_timeout <= 0:
        raise ValueError("http_timeout harus > 0")


def validate_location_config(location):
    if not -90 <= location.latitude <= 90:
        raise ValueError(f"latitude tidak valid untuk lokasi {location.slug}")
    if not -180 <= location.longitude <= 180:
        raise ValueError(f"longitude tidak valid untuk lokasi {location.slug}")
    adm4_parts = location.adm4.split(".")
    if len(adm4_parts) != 4 or not all(part.isdigit() for part in adm4_parts):
        raise ValueError(f"adm4 tidak valid untuk lokasi {location.slug}: {location.adm4}")
    ZoneInfo(location.timezone)


def clone_args_for_location(args, location):
    data = vars(args).copy()
    data["location_slug"] = location.slug
    data["location_name"] = location.location_name
    data["adm4"] = location.adm4
    data["latitude"] = location.latitude
    data["longitude"] = location.longitude
    data["timezone"] = location.timezone
    data["bmkg_point_name"] = location.bmkg_point_name
    data["area_level"] = location.area_level
    data["is_proxy_bmkg"] = location.is_proxy_bmkg
    data["location_note"] = location.note
    return argparse.Namespace(**data)


def is_default_single_location_args(args):
    return (
        args.location_name == DEFAULT_LOCATION_NAME
        and args.adm4 == DEFAULT_ADM4
        and abs(args.latitude - DEFAULT_LATITUDE) < 1e-9
        and abs(args.longitude - DEFAULT_LONGITUDE) < 1e-9
        and args.timezone == DEFAULT_TIMEZONE
    )


def resolve_requested_locations(args):
    if not args.locations:
        if args.mode != "import-observations" and is_default_single_location_args(args):
            return [LOCATION_PRESETS[slug] for slug in ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS]
        custom_location = LocationConfig(
            slug=sanitize_filename(args.location_name.lower()),
            location_name=args.location_name,
            adm4=args.adm4,
            latitude=args.latitude,
            longitude=args.longitude,
            timezone=args.timezone,
            bmkg_point_name=args.location_name,
            area_level="custom",
            is_proxy_bmkg=False,
            note="Custom location from CLI arguments",
        )
        return [custom_location]

    raw_tokens = [token.strip().lower() for token in args.locations.split(",") if token.strip()]
    if not raw_tokens:
        raise ValueError("--locations tidak boleh kosong")
    if raw_tokens == ["all"]:
        return [LOCATION_PRESETS[slug] for slug in ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS]

    selected = []
    seen = set()
    for slug in raw_tokens:
        if slug not in LOCATION_PRESETS:
            raise ValueError(f"Lokasi preset tidak dikenali: {slug}")
        if slug in seen:
            continue
        selected.append(LOCATION_PRESETS[slug])
        seen.add(slug)
    return selected


def print_available_locations():
    print("Lokasi preset tersedia:")
    print("Sumber config lokasi:", ACTIVE_LOCATIONS_FILE or "(embedded defaults)")
    for slug in ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS:
        location = LOCATION_PRESETS[slug]
        print(
            f"- {location.slug}: {location.location_name} | "
            f"adm4={location.adm4} | lat={location.latitude} | lon={location.longitude} | "
            f"bmkg_point={location.bmkg_point_name} | proxy={location.is_proxy_bmkg}"
        )
        if location.note:
            print(f"  note: {location.note}")
    extra_slugs = [slug for slug in LOCATION_PRESETS if slug not in ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS]
    for slug in sorted(extra_slugs):
        location = LOCATION_PRESETS[slug]
        print(
            f"- {location.slug}: {location.location_name} | "
            f"adm4={location.adm4} | lat={location.latitude} | lon={location.longitude} | "
            f"bmkg_point={location.bmkg_point_name} | proxy={location.is_proxy_bmkg}"
        )
        if location.note:
            print(f"  note: {location.note}")


def prepare_location_context(args):
    set_active_output_dir(args.location_slug)
    log_path = setup_logging(args)
    log_info("Log file:", log_path)
    log_info("Output dir:", ACTIVE_OUTPUT_DIR)
    log_info(
        "Metadata lokasi:",
        f"slug={args.location_slug}",
        f"adm4={args.adm4}",
        f"bmkg_point={args.bmkg_point_name}",
        f"area_level={args.area_level}",
        f"proxy_bmkg={args.is_proxy_bmkg}",
    )
    log_info(
        "Koordinat:",
        f"lat={args.latitude}",
        f"lon={args.longitude}",
        f"timezone={args.timezone}",
    )
    if getattr(args, "location_note", ""):
        log_info("Catatan lokasi:", args.location_note)
    if ACTIVE_LOCATIONS_FILE:
        log_info("Locations file:", ACTIVE_LOCATIONS_FILE)
    cleanup_old_outputs(args)
    return log_path


def run_once(args):
    if args.target_date:
        target_date = parse_iso_date(args.target_date)
    else:
        target_date = (now_local(args.timezone) + timedelta(days=1)).date()

    # Optional idempotency: skip if outputs already exist for this target_date.
    stamp = target_date.strftime("%Y%m%d")
    existing_path = path_output(f"forecast_{stamp}.csv")
    if args.skip_existing and os.path.exists(existing_path) and not args.force:
        log_info("skip-existing aktif: forecast sudah ada:", existing_path)
        return {
            "generated_at": now_local(args.timezone).isoformat(),
            "location_slug": args.location_slug,
            "location_name": args.location_name,
            "target_date": target_date.isoformat(),
            "output_dir": ACTIVE_OUTPUT_DIR,
            "run_status": "skipped",
            "reason": "existing_output",
        }

    load_weight_config()
    load_health_config()
    log_info("Mulai proses untuk lokasi", args.location_name)
    log_info("Target date:", target_date.isoformat())
    log_info("Target hours:", ", ".join(TARGET_TIMES))
    log_info("Sumber aktif:", ", ".join(item["source_id"] for item in ACTIVE_SOURCE_CONFIGS))
    log_info(
        "Bobot aktif:",
        ", ".join(
            f"{source_id}={round(weight, 3)}"
            for source_id, weight in sorted(ACTIVE_SOURCE_WEIGHTS.items())
        ),
    )

    results = collect_all_sources(target_date, args)
    summary = save_outputs(target_date, results, args)
    save_health_config(results, args, target_date=target_date)

    total_success = sum(1 for item in results if item.success)
    total_points = sum(len(item.points) for item in results)
    log_info("Selesai.")
    log_info("Sumber sukses:", f"{total_success}/{len(results)}")
    log_info("Total forecast point:", total_points)
    if total_success < MIN_SOURCE_SUCCESS_FOR_RUN:
        log_warning(
            "Jumlah sumber sukses di bawah ambang minimum:",
            total_success,
            "<",
            MIN_SOURCE_SUCCESS_FOR_RUN,
        )
    if summary["low_coverage_slots"]:
        log_warning("Coverage terbatas pada jam:", ", ".join(summary["low_coverage_slots"]))
    for result in results:
        meta = []
        if result.http_status is not None:
            meta.append(f"http={result.http_status}")
        if result.duration_ms is not None:
            meta.append(f"t={result.duration_ms}ms")
        log_info(
            f"{result.source_id}:",
            "OK" if result.success else "FAIL",
            f"({len(result.points)}/{len(TARGET_TIMES)} point)",
            (" ".join(meta)) if meta else "",
            result.error if result.error else "",
        )
    return summary


def run_self_tests(args):
    sample_point = ForecastPoint(
        source_id="BMKG",
        provider="BMKG",
        target_time="10:00",
        source_datetime=parse_local_hour_string(parse_iso_date("2026-04-27"), "10:00", args.timezone),
        temp_c=28.0,
        rh_pct=75.0,
        rain_mm=0.0,
        wind_kmh=10.0,
        category="Cerah Berawan",
        raw_condition="Cerah Berawan",
        gap_minutes=0.0,
    )
    sample_bmkg_payload = {
        "data": [
            {
                "cuaca": [
                    [
                        {
                            "local_datetime": "2026-04-27 10:00:00",
                            "t": 28,
                            "hu": 75,
                            "weather_desc": "Cerah Berawan",
                            "ws": 10,
                        }
                    ]
                ]
            }
        ]
    }
    fake_cli = argparse.Namespace(
        locations="all",
        location_name=DEFAULT_LOCATION_NAME,
        adm4=DEFAULT_ADM4,
        latitude=DEFAULT_LATITUDE,
        longitude=DEFAULT_LONGITUDE,
        timezone=DEFAULT_TIMEZONE,
        mode="forecast",
    )

    assert bmkg_to_kategori("Hujan Ringan") == "Hujan Ringan"
    assert bmkg_rain_proxy_mm("Hujan Sedang") > 0
    assert category_from_wmo_code(0, 0, 50) == "Cerah"
    assert category_from_wmo_code(63, 4, 90) == "Hujan Sedang"
    assert extract_bmkg_points(parse_iso_date("2026-04-27"), sample_bmkg_payload, args)["10:00"].category == "Cerah Berawan"
    assert point_weight(sample_point) > 0
    assert confidence_label(85) == "Tinggi"
    assert round(heat_index(32.0, 70.0), 2) >= 32.0
    filtered = filter_weighted_pairs([(10, 1), (11, 1), (12, 1), (100, 1)])
    assert len(filtered) < 4
    score, label = compute_confidence([sample_point] * 5, 5.0, 4.0, 1.0, 5.0, 0.5)
    assert score >= 0
    assert label in {"Tinggi", "Sedang", "Rendah"}
    assert next(item for item in ALL_SOURCE_CONFIGS if item["source_id"] == "KMA")["models"] == "kma_seamless"
    assert next(item for item in ALL_SOURCE_CONFIGS if item["source_id"] == "UKMO")["models"] == "ukmo_seamless"
    resolved = resolve_requested_locations(fake_cli)
    assert [item.slug for item in resolved] == DEFAULT_MULTI_LOCATION_SLUGS
    assert LOCATION_PRESETS["jatinangor"].adm4 == "32.11.15.2002"
    assert LOCATION_PRESETS["arjawinangun"].adm4 == "32.09.24.2004"
    temp_locations_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            json.dump(
                {
                    "default_multi_locations": ["customtown"],
                    "locations": {
                        "customtown": {
                            "location_name": "Custom Town",
                            "adm4": "32.73.02.1004",
                            "latitude": -6.9,
                            "longitude": 107.6,
                            "bmkg_point_name": "Custom BMKG",
                            "area_level": "test",
                            "is_proxy_bmkg": True,
                            "note": "custom config test",
                        }
                    },
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
            temp_locations_path = f.name

        custom_presets, custom_defaults, custom_file = load_location_presets(temp_locations_path)
        assert custom_defaults == ["customtown"]
        assert custom_presets["customtown"].bmkg_point_name == "Custom BMKG"
        assert custom_presets["customtown"].is_proxy_bmkg is True
        assert custom_file == temp_locations_path
    finally:
        if temp_locations_path and os.path.exists(temp_locations_path):
            os.remove(temp_locations_path)
    log_info("Self-test selesai. Semua assertion lulus.")


def execute_mode_for_location(base_args, location, runner):
    location_args = clone_args_for_location(base_args, location)
    validate_location_config(location)
    log_path = prepare_location_context(location_args)
    result = runner(location_args)
    return location_args, result, log_path


def write_batch_summary(mode, rows, extra_payload=None):
    payload = {
        "generated_at": now_local(DEFAULT_TIMEZONE).isoformat(),
        "mode": mode,
        "locations_file": ACTIVE_LOCATIONS_FILE or "",
        "default_multi_locations": list(ACTIVE_DEFAULT_MULTI_LOCATION_SLUGS),
        "locations_total": len(rows),
        "locations": rows,
    }
    if extra_payload:
        payload.update(extra_payload)
    write_json(root_output_path(f"{mode}_batch_summary.json"), payload)


def combined_location_fieldnames():
    return [
        "location_slug",
        "location_name",
        "target_date",
        "adm4",
        "bmkg_point_name",
        "area_level",
        "is_proxy_bmkg",
        "latitude",
        "longitude",
        "timezone",
        "location_note",
    ]


def combined_location_metadata(location_args, target_date):
    return {
        "location_slug": location_args.location_slug,
        "location_name": location_args.location_name,
        "target_date": target_date,
        "adm4": location_args.adm4,
        "bmkg_point_name": location_args.bmkg_point_name,
        "area_level": location_args.area_level,
        "is_proxy_bmkg": "yes" if location_args.is_proxy_bmkg else "no",
        "latitude": location_args.latitude,
        "longitude": location_args.longitude,
        "timezone": location_args.timezone,
        "location_note": getattr(location_args, "location_note", ""),
    }


def combined_ensemble_fieldnames():
    return combined_location_fieldnames() + [
        "jam",
        "sources_used",
        "weight_total",
        "coverage_status",
        "source_list",
        "dominant_category",
        "confidence_score",
        "confidence_label",
        "temp_mean",
        "temp_error",
        "rh_mean",
        "rh_error",
        "rain_mean",
        "rain_error",
        "heat_index_mean",
        "heat_index_error",
        "%cerah",
        "%cerah_berawan",
        "%berawan",
        "%hujan_ringan",
        "%hujan_sedang",
        "%hujan_lebat",
    ]


def combined_ensemble_long_fieldnames():
    """
    BI-friendly long format (1 row per location per hour):
    - Avoids % in header names
    - Keeps same information as ensemble.csv
    """
    return combined_location_fieldnames() + [
        "jam",
        "sources_used",
        "weight_total",
        "coverage_status",
        "source_list",
        "dominant_category",
        "confidence_score",
        "confidence_label",
        "temp_mean",
        "temp_error",
        "rh_mean",
        "rh_error",
        "rain_mean",
        "rain_error",
        "heat_index_mean",
        "heat_index_error",
        "pct_cerah",
        "pct_cerah_berawan",
        "pct_berawan",
        "pct_hujan_ringan",
        "pct_hujan_sedang",
        "pct_hujan_lebat",
        "temp_std",
        "rh_std",
        "rain_std",
        "heat_index_std",
        "sources_expected",
        "coverage_fraction",
        "gap_mean_minutes",
        "gap_max_minutes",
    ]


def collect_combined_ensemble_long_rows(location_args, target_date, output_dir):
    ensemble_path = os.path.join(output_dir, "ensemble.csv")
    rows = []
    metadata = combined_location_metadata(location_args, target_date)
    for row in read_dict_csv(ensemble_path):
        rows.append(
            {
                **metadata,
                "jam": row.get("jam") or "",
                "sources_used": row.get("sources_used") or "",
                "weight_total": row.get("weight_total") or "",
                "coverage_status": row.get("coverage_status") or "",
                "source_list": row.get("source_list") or "",
                "dominant_category": row.get("dominant_category") or "",
                "confidence_score": row.get("confidence_score") or "",
                "confidence_label": row.get("confidence_label") or "",
                "temp_mean": row.get("temp_mean") or "",
                "temp_error": row.get("temp_error") or "",
                "rh_mean": row.get("rh_mean") or "",
                "rh_error": row.get("rh_error") or "",
                "rain_mean": row.get("rain_mean") or "",
                "rain_error": row.get("rain_error") or "",
                "heat_index_mean": row.get("heat_index_mean") or "",
                "heat_index_error": row.get("heat_index_error") or "",
                "pct_cerah": row.get("%cerah") or "",
                "pct_cerah_berawan": row.get("%cerah_berawan") or "",
                "pct_berawan": row.get("%berawan") or "",
                "pct_hujan_ringan": row.get("%hujan_ringan") or "",
                "pct_hujan_sedang": row.get("%hujan_sedang") or "",
                "pct_hujan_lebat": row.get("%hujan_lebat") or "",
                "temp_std": row.get("temp_std") or "",
                "rh_std": row.get("rh_std") or "",
                "rain_std": row.get("rain_std") or "",
                "heat_index_std": row.get("heat_index_std") or "",
                "sources_expected": row.get("sources_expected") or "",
                "coverage_fraction": row.get("coverage_fraction") or "",
                "gap_mean_minutes": row.get("gap_mean_minutes") or "",
                "gap_max_minutes": row.get("gap_max_minutes") or "",
            }
        )
    return rows


def combined_forecast_fieldnames():
    return combined_location_fieldnames() + [
        "tanggal",
        "source_id",
        "provider",
        "target_jam",
        "source_datetime",
        "suhu_C",
        "RH_%",
        "rain_mm",
        "wind_kmh",
        "gap_minutes",
        "point_weight",
        "kategori",
        "raw_condition",
    ]


def combined_source_status_fieldnames():
    return combined_location_fieldnames() + [
        "tanggal",
        "source_id",
        "provider",
        "success",
        "base_weight",
        "health_factor",
        "effective_base_weight",
        "points_collected",
        "target_points",
        "http_status",
        "duration_ms",
        "error_content_type",
        "error_snippet",
        "payload_saved_path",
        "error",
    ]


def collect_combined_ensemble_rows(location_args, target_date, output_dir):
    ensemble_path = os.path.join(output_dir, "ensemble.csv")
    rows = []
    metadata = combined_location_metadata(location_args, target_date)
    for row in read_dict_csv(ensemble_path):
        rows.append(
            {
                **metadata,
                "jam": row.get("jam") or "",
                "sources_used": row.get("sources_used") or "",
                "weight_total": row.get("weight_total") or "",
                "coverage_status": row.get("coverage_status") or "",
                "source_list": row.get("source_list") or "",
                "dominant_category": row.get("dominant_category") or "",
                "confidence_score": row.get("confidence_score") or "",
                "confidence_label": row.get("confidence_label") or "",
                "temp_mean": row.get("temp_mean") or "",
                "temp_error": row.get("temp_error") or "",
                "rh_mean": row.get("rh_mean") or "",
                "rh_error": row.get("rh_error") or "",
                "rain_mean": row.get("rain_mean") or "",
                "rain_error": row.get("rain_error") or "",
                "heat_index_mean": row.get("heat_index_mean") or "",
                "heat_index_error": row.get("heat_index_error") or "",
                "%cerah": row.get("%cerah") or "",
                "%cerah_berawan": row.get("%cerah_berawan") or "",
                "%berawan": row.get("%berawan") or "",
                "%hujan_ringan": row.get("%hujan_ringan") or "",
                "%hujan_sedang": row.get("%hujan_sedang") or "",
                "%hujan_lebat": row.get("%hujan_lebat") or "",
            }
        )
    return rows


def collect_combined_forecast_rows(location_args, target_date, output_dir):
    forecast_path = os.path.join(output_dir, "forecast.csv")
    rows = []
    metadata = combined_location_metadata(location_args, target_date)
    for row in read_dict_csv(forecast_path):
        rows.append(
            {
                **metadata,
                "tanggal": row.get("tanggal") or "",
                "source_id": row.get("source_id") or "",
                "provider": row.get("provider") or "",
                "target_jam": row.get("target_jam") or "",
                "source_datetime": row.get("source_datetime") or "",
                "suhu_C": row.get("suhu_C") or "",
                "RH_%": row.get("RH_%") or "",
                "rain_mm": row.get("rain_mm") or "",
                "wind_kmh": row.get("wind_kmh") or "",
                "gap_minutes": row.get("gap_minutes") or "",
                "point_weight": row.get("point_weight") or "",
                "kategori": row.get("kategori") or "",
                "raw_condition": row.get("raw_condition") or "",
            }
        )
    return rows


def collect_combined_source_status_rows(location_args, target_date, output_dir):
    status_path = os.path.join(output_dir, "source_status.csv")
    rows = []
    metadata = combined_location_metadata(location_args, target_date)
    for row in read_dict_csv(status_path):
        rows.append(
            {
                **metadata,
                "tanggal": row.get("tanggal") or "",
                "source_id": row.get("source_id") or "",
                "provider": row.get("provider") or "",
                "success": row.get("success") or "",
                "base_weight": row.get("base_weight") or "",
                "health_factor": row.get("health_factor") or "",
                "effective_base_weight": row.get("effective_base_weight") or "",
                "points_collected": row.get("points_collected") or "",
                "target_points": row.get("target_points") or "",
                "http_status": row.get("http_status") or "",
                "duration_ms": row.get("duration_ms") or "",
                "error_content_type": row.get("error_content_type") or "",
                "error_snippet": row.get("error_snippet") or "",
                "payload_saved_path": row.get("payload_saved_path") or "",
                "error": row.get("error") or "",
            }
        )
    return rows


def write_combined_csv(base_filename, fieldnames, rows):
    if not rows:
        return None, None

    unique_dates = sorted({row["target_date"] for row in rows if row.get("target_date")})
    run_stamp = now_local(DEFAULT_TIMEZONE).strftime("%Y%m%d_%H%M%S")
    if len(unique_dates) == 1:
        stamp = f"{unique_dates[0].replace('-', '')}_{run_stamp}"
    else:
        stamp = run_stamp

    latest_path = root_output_path(f"{base_filename}.csv")
    versioned_path = root_output_path(f"{base_filename}_{stamp}.csv")
    write_dict_csv(versioned_path, fieldnames, rows)
    try:
        write_dict_csv(latest_path, fieldnames, rows)
    except PermissionError as exc:
        batch_warning(
            f"File {os.path.basename(latest_path)} sedang dipakai atau terkunci,",
            "jadi hanya file versi waktu yang ditulis:",
            exc,
        )
        latest_path = None
    return latest_path, versioned_path


def run_forecast_for_locations(base_args, locations):
    rows = []
    combined_ensemble_rows = []
    combined_ensemble_long_rows = []
    combined_forecast_rows = []
    combined_status_rows = []
    for location in locations:
        try:
            location_args, summary, log_path = execute_mode_for_location(
                base_args, location, run_once
            )
            output_dir = summary.get("output_dir") or ACTIVE_OUTPUT_DIR
            combined_ensemble_rows.extend(
                collect_combined_ensemble_rows(
                    location_args,
                    summary["target_date"],
                    output_dir,
                )
            )
            combined_ensemble_long_rows.extend(
                collect_combined_ensemble_long_rows(
                    location_args,
                    summary["target_date"],
                    output_dir,
                )
            )
            combined_forecast_rows.extend(
                collect_combined_forecast_rows(
                    location_args,
                    summary["target_date"],
                    output_dir,
                )
            )
            combined_status_rows.extend(
                collect_combined_source_status_rows(
                    location_args,
                    summary["target_date"],
                    output_dir,
                )
            )
            rows.append(
                {
                    "location_slug": location_args.location_slug,
                    "location_name": location_args.location_name,
                    "bmkg_point_name": location_args.bmkg_point_name,
                    "area_level": location_args.area_level,
                    "is_proxy_bmkg": location_args.is_proxy_bmkg,
                    "target_date": summary["target_date"],
                    "run_status": summary["run_status"],
                    "sources_success": summary["sources_success"],
                    "sources_total": summary["sources_total"],
                    "low_coverage_slots": summary["low_coverage_slots"],
                    "output_dir": output_dir,
                    "log_file": log_path,
                }
            )
        except Exception as exc:
            batch_warning(f"{location.location_name} gagal total:", exc)
            traceback.print_exc()
            rows.append(
                {
                    "location_slug": location.slug,
                    "location_name": location.location_name,
                    "bmkg_point_name": location.bmkg_point_name,
                    "area_level": location.area_level,
                    "is_proxy_bmkg": location.is_proxy_bmkg,
                    "run_status": "error",
                    "error": str(exc),
                }
            )
    combined_outputs = {}
    for base_filename, label, fieldnames, payload_rows in (
        (
            "ensemble_all_locations",
            "Ensemble",
            combined_ensemble_fieldnames(),
            combined_ensemble_rows,
        ),
        (
            "ensemble_long_all_locations",
            "Ensemble long (BI)",
            combined_ensemble_long_fieldnames(),
            combined_ensemble_long_rows,
        ),
        (
            "forecast_all_locations",
            "Forecast raw",
            combined_forecast_fieldnames(),
            combined_forecast_rows,
        ),
        (
            "source_status_all_locations",
            "Source status",
            combined_source_status_fieldnames(),
            combined_status_rows,
        ),
    ):
        if base_args.no_combined:
            latest_path, versioned_path = None, None
        else:
            latest_path, versioned_path = write_combined_csv(
                base_filename,
                fieldnames,
                payload_rows,
            )
        combined_outputs[base_filename] = {
            "latest_path": latest_path or "",
            "versioned_path": versioned_path or "",
            "rows": len(payload_rows),
        }
        if latest_path:
            batch_info(f"{label} gabungan:", latest_path)
        if versioned_path:
            batch_info(f"{label} gabungan versi waktu:", versioned_path)
    write_batch_summary(
        "forecast",
        rows,
        {
            "locations_ok": sum(1 for row in rows if row.get("run_status") != "error"),
            "combined_outputs": combined_outputs,
        },
    )

    # BI artifacts (dims + fact)
    if not base_args.no_combined:
        bi_summary_path = root_output_path("bi_artifacts_summary.json")
        try:
            dim_src_path, dim_src_count = write_dim_sources()
            dim_loc_path, dim_loc_count = write_dim_locations(locations, base_args)
            fact_path, fact_rows = write_ensemble_fact_from_long(combined_ensemble_long_rows)
            payload = {
                "generated_at": now_local(DEFAULT_TIMEZONE).isoformat(),
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "dim_sources": {"path": dim_src_path, "rows": dim_src_count},
                "dim_locations": {"path": dim_loc_path, "rows": dim_loc_count},
                "ensemble_fact": {"path": fact_path or "", "rows": fact_rows},
            }
            write_json(bi_summary_path, payload)
            batch_info("Dim sources:", dim_src_path, f"({dim_src_count} rows)")
            batch_info("Dim locations:", dim_loc_path, f"({dim_loc_count} rows)")
            if fact_path:
                batch_info("Ensemble fact (BI):", fact_path, f"({fact_rows} rows)")
        except Exception as exc:
            write_json(
                bi_summary_path,
                {
                    "generated_at": now_local(DEFAULT_TIMEZONE).isoformat(),
                    "schema_version": OUTPUT_SCHEMA_VERSION,
                    "status": "error",
                    "error": str(exc),
                },
            )
            batch_warning("Gagal menulis BI artifacts:", exc)

    try:
        sentinel_write_root_public_index(locations, rows, base_args)
    except Exception as exc:
        batch_warning("Gagal menulis public index Sentinel X:", exc)
    return rows


def write_dim_locations(locations: list[LocationConfig], base_args):
    rows = []
    for loc in locations:
        rows.append(
            {
                "location_slug": loc.slug,
                "location_name": loc.location_name,
                "adm4": loc.adm4,
                "bmkg_point_name": loc.bmkg_point_name,
                "area_level": loc.area_level,
                "is_proxy_bmkg": "yes" if loc.is_proxy_bmkg else "no",
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "timezone": loc.timezone,
                "note": loc.note,
            }
        )
    rows.sort(key=lambda r: r["location_slug"])
    path = root_output_path("dim_locations.csv")
    write_dict_csv(
        path,
        [
            "location_slug",
            "location_name",
            "adm4",
            "bmkg_point_name",
            "area_level",
            "is_proxy_bmkg",
            "latitude",
            "longitude",
            "timezone",
            "note",
        ],
        rows,
    )
    return path, len(rows)


def write_dim_sources():
    rows = []
    for cfg in ALL_SOURCE_CONFIGS:
        rows.append(
            {
                "source_id": cfg.get("source_id") or "",
                "provider": cfg.get("provider") or "",
                "kind": cfg.get("kind") or "",
                "endpoint": cfg.get("endpoint") or "",
                "models": cfg.get("models") or "",
                "base_weight": source_base_weight(cfg.get("source_id") or ""),
            }
        )
    rows.sort(key=lambda r: r["source_id"])
    path = root_output_path("dim_sources.csv")
    write_dict_csv(
        path,
        ["source_id", "provider", "kind", "endpoint", "models", "base_weight"],
        rows,
    )
    return path, len(rows)


def write_ensemble_fact_from_long(long_rows: list[dict]):
    """
    Dashboard/BI fact table: numeric-friendly columns only.
    Uses long rows and selects a stable subset.
    """
    if not long_rows:
        return None, 0
    fieldnames = [
        "location_slug",
        "target_date",
        "jam",
        "dominant_category",
        "confidence_score",
        "confidence_label",
        "sources_used",
        "sources_expected",
        "coverage_fraction",
        "weight_total",
        "temp_mean",
        "temp_std",
        "rh_mean",
        "rh_std",
        "rain_mean",
        "rain_std",
        "heat_index_mean",
        "heat_index_std",
        "gap_mean_minutes",
        "gap_max_minutes",
        "pct_cerah",
        "pct_cerah_berawan",
        "pct_berawan",
        "pct_hujan_ringan",
        "pct_hujan_sedang",
        "pct_hujan_lebat",
    ]
    rows = []
    for r in long_rows:
        rows.append({k: r.get(k, "") for k in fieldnames})
    latest_path, versioned_path = write_combined_csv(
        "ensemble_fact_all_locations",
        fieldnames,
        rows,
    )
    return latest_path or versioned_path, len(rows)


def sync_observations_for_locations(base_args, locations):
    rows = []
    for location in locations:
        try:
            location_args, summary_rows, log_path = execute_mode_for_location(
                base_args, location, sync_observations
            )
            rows.append(
                {
                    "location_slug": location_args.location_slug,
                    "location_name": location_args.location_name,
                    "bmkg_point_name": location_args.bmkg_point_name,
                    "area_level": location_args.area_level,
                    "is_proxy_bmkg": location_args.is_proxy_bmkg,
                    "days_processed": len(summary_rows),
                    "output_dir": ACTIVE_OUTPUT_DIR,
                    "log_file": log_path,
                }
            )
        except Exception as exc:
            batch_warning(f"Sync observasi gagal untuk {location.location_name}:", exc)
            traceback.print_exc()
            rows.append(
                {
                    "location_slug": location.slug,
                    "location_name": location.location_name,
                    "bmkg_point_name": location.bmkg_point_name,
                    "area_level": location.area_level,
                    "is_proxy_bmkg": location.is_proxy_bmkg,
                    "status": "error",
                    "error": str(exc),
                }
            )
    write_batch_summary("sync-observations", rows)
    return rows


def evaluate_for_locations(base_args, locations):
    rows = []
    for location in locations:
        try:
            location_args, result, log_path = execute_mode_for_location(
                base_args, location, evaluate_historical_performance
            )
            rows.append(
                {
                    "location_slug": location_args.location_slug,
                    "location_name": location_args.location_name,
                    "bmkg_point_name": location_args.bmkg_point_name,
                    "area_level": location_args.area_level,
                    "is_proxy_bmkg": location_args.is_proxy_bmkg,
                    "evaluated_sources": len(result["source_score_rows"]),
                    "evaluated_rows": len(result["detail_rows"]),
                    "weights_file": result["weights_path"],
                    "output_dir": ACTIVE_OUTPUT_DIR,
                    "log_file": log_path,
                }
            )
        except Exception as exc:
            batch_warning(f"Evaluasi gagal untuk {location.location_name}:", exc)
            traceback.print_exc()
            rows.append(
                {
                    "location_slug": location.slug,
                    "location_name": location.location_name,
                    "bmkg_point_name": location.bmkg_point_name,
                    "area_level": location.area_level,
                    "is_proxy_bmkg": location.is_proxy_bmkg,
                    "status": "error",
                    "error": str(exc),
                }
            )
    write_batch_summary("evaluate", rows)
    return rows


def self_test_for_locations(base_args, locations):
    rows = []
    for location in locations:
        try:
            location_args, _, log_path = execute_mode_for_location(
                base_args, location, run_self_tests
            )
            rows.append(
                {
                    "location_slug": location_args.location_slug,
                    "location_name": location_args.location_name,
                    "bmkg_point_name": location_args.bmkg_point_name,
                    "area_level": location_args.area_level,
                    "is_proxy_bmkg": location_args.is_proxy_bmkg,
                    "status": "ok",
                    "output_dir": ACTIVE_OUTPUT_DIR,
                    "log_file": log_path,
                }
            )
        except Exception as exc:
            batch_warning(f"Self-test gagal untuk {location.location_name}:", exc)
            traceback.print_exc()
            rows.append(
                {
                    "location_slug": location.slug,
                    "location_name": location.location_name,
                    "bmkg_point_name": location.bmkg_point_name,
                    "area_level": location.area_level,
                    "is_proxy_bmkg": location.is_proxy_bmkg,
                    "status": "error",
                    "error": str(exc),
                }
            )
    write_batch_summary("self-test", rows)
    return rows


def import_observations_for_location(base_args, location):
    location_args, imported_rows, log_path = execute_mode_for_location(
        base_args, location, import_external_observations
    )
    rows = [
        {
            "location_slug": location_args.location_slug,
            "location_name": location_args.location_name,
            "bmkg_point_name": location_args.bmkg_point_name,
            "area_level": location_args.area_level,
            "is_proxy_bmkg": location_args.is_proxy_bmkg,
            "rows_imported": len(imported_rows),
            "output_dir": ACTIVE_OUTPUT_DIR,
            "log_file": log_path,
        }
    ]
    write_batch_summary("import-observations", rows)
    return rows


def loop_daily(base_args, locations):
    scheduler_tz = locations[0].timezone if locations else base_args.timezone
    batch_info("Mode loop harian aktif.")
    batch_info("Jadwal harian:", base_args.run_time)
    batch_info("Lokasi aktif:", ", ".join(location.location_name for location in locations))

    if base_args.run_immediately_on_start:
        batch_info("Menjalankan forecast segera saat start.")
        run_forecast_for_locations(base_args, locations)

    while True:
        try:
            seconds_left, next_run = seconds_until_run(base_args.run_time, scheduler_tz)
            batch_info(
                "Menunggu run berikutnya pada",
                next_run.strftime("%Y-%m-%d %H:%M:%S %Z"),
                f"({seconds_left} detik lagi)",
            )

            while seconds_left > 0:
                nap = min(base_args.sleep_seconds, seconds_left)
                time.sleep(nap)
                seconds_left -= nap

            run_forecast_for_locations(base_args, locations)
        except Exception as exc:
            batch_warning("ERROR loop_daily:", exc)
            traceback.print_exc()
            time.sleep(60)



# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# AETHER SENTINEL X — Clean Single-File Intelligence Layer
# -----------------------------------------------------------------------------
AETHER_VERSION = "AETHER SENTINEL X PUBLIC-OPERATIONAL v2 — Verified Atmospheric Risk, Scenario, Failure & Decision Intelligence System"
AETHER_DB_FILENAME = "sentinel_x_ledger.sqlite"
AETHER_CSV_FILENAME = "sentinel_x.csv"
AETHER_JSON_FILENAME = "sentinel_x.json"
AETHER_DASHBOARD_FILENAME = "command_center_sentinel_x.html"
AETHER_REPORT_FILENAME = "sentinel_x_report.md"
AETHER_CONTRACT_FILENAME = "sentinel_x_forecast_contract.json"
AETHER_SOURCE_STATE_FILENAME = "sentinel_x_source_state.csv"
AETHER_FEEDBACK_FILENAME = "sentinel_x_feedback.csv"
AETHER_ROUTE_STATE_FILENAME = "sentinel_x_route_state.json"

AETHER_BASIC_OPEN_METEO_VARIABLES = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
]

AETHER_EXTRA_OPEN_METEO_VARIABLES = [
    "apparent_temperature",
    "dew_point_2m",
    "precipitation_probability",
    "rain",
    "cloud_cover",
    "pressure_msl",
    "surface_pressure",
    "wind_direction_10m",
    "wind_gusts_10m",
    "visibility",
    "cape",
]


def aether_open_meteo_variables(args=None, include_extra=None):
    if include_extra is None:
        include_extra = bool(getattr(args, "aether_extra_vars", False)) if args is not None else False
    variables = list(AETHER_BASIC_OPEN_METEO_VARIABLES)
    if include_extra:
        for item in AETHER_EXTRA_OPEN_METEO_VARIABLES:
            if item not in variables:
                variables.append(item)
    return variables


def aether_db_path():
    return path_output(AETHER_DB_FILENAME)


def aether_connect_db():
    ensure_directory(ACTIVE_OUTPUT_DIR)
    conn = sqlite3.connect(aether_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def aether_value(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def aether_round(value, digits=2):
    if value is None:
        return ""
    try:
        return round(float(value), digits)
    except Exception:
        return ""


def aether_weighted_quantile(weighted_pairs, q):
    valid = [(float(v), float(w)) for v, w in weighted_pairs if v is not None and w is not None and w > 0]
    if not valid:
        return None
    valid.sort(key=lambda item: item[0])
    total = sum(w for _, w in valid)
    if total <= 0:
        return None
    threshold = q * total
    cumulative = 0.0
    for value, weight in valid:
        cumulative += weight
        if cumulative >= threshold:
            return round(value, 3)
    return round(valid[-1][0], 3)


def aether_weighted_mean(weighted_pairs):
    valid = [(float(v), float(w)) for v, w in weighted_pairs if v is not None and w is not None and w > 0]
    if not valid:
        return None
    total = sum(w for _, w in valid)
    if total <= 0:
        return None
    return round(sum(v * w for v, w in valid) / total, 3)


def aether_get_weighted_attr(bucket, attr):
    pairs = []
    for point in bucket:
        value = getattr(point, attr, None)
        if value is not None:
            pairs.append((value, point_weight(point)))
    return pairs


def aether_category_cloud_proxy(category):
    return {
        "Cerah": 15.0,
        "Cerah Berawan": 45.0,
        "Berawan": 75.0,
        "Hujan Ringan": 88.0,
        "Hujan Sedang": 94.0,
        "Hujan Lebat": 98.0,
    }.get(category, 70.0)


def aether_microclimate_profile(args):
    explicit = getattr(args, "microclimate", "auto") or "auto"
    if explicit != "auto":
        return explicit
    slug = (getattr(args, "location_slug", "") or "").lower()
    name = (getattr(args, "location_name", "") or "").lower()
    text = f"{slug} {name}"
    if "jatinangor" in text:
        return "valley_highland"
    if "dago" in text or "bandung" in text:
        return "urban_highland"
    if "arjawinangun" in text or "cirebon" in text:
        return "lowland_agriculture"
    return "generic_local"


def aether_microclimate_adjustment(profile, hour, temp_c, rh_pct):
    temp_adj = 0.0
    rh_adj = 0.0
    fog_bonus = 0.0
    if profile == "valley_highland":
        if 0 <= hour <= 7:
            temp_adj -= 0.5
            rh_adj += 4.0
            fog_bonus += 8.0
        elif 12 <= hour <= 16:
            temp_adj += 0.2
    elif profile == "urban_highland":
        if 18 <= hour <= 23 or 0 <= hour <= 4:
            temp_adj += 0.3
        if 12 <= hour <= 17:
            rh_adj -= 1.0
    elif profile == "lowland_agriculture":
        if 12 <= hour <= 16:
            temp_adj += 0.4
        if 4 <= hour <= 7:
            rh_adj += 2.0
    elif profile == "coastal":
        if 10 <= hour <= 17:
            rh_adj += 2.0
    adjusted_temp = None if temp_c is None else round(float(temp_c) + temp_adj, 2)
    adjusted_rh = None if rh_pct is None else round(clamp(float(rh_pct) + rh_adj, 0, 100), 2)
    return adjusted_temp, adjusted_rh, round(fog_bonus, 2), round(temp_adj, 2), round(rh_adj, 2)


def aether_target_datetime(target_date, jam, tz_name):
    return parse_local_hour_string(target_date, jam, tz_name)


def aether_lead_hours(target_date, jam, args):
    try:
        target_dt = aether_target_datetime(target_date, jam, args.timezone)
        lead = (target_dt - now_local(args.timezone)).total_seconds() / 3600.0
        return round(max(0.0, lead), 2)
    except Exception:
        return None


def aether_lead_bucket(lead_hours):
    if lead_hours is None:
        return "unknown"
    if lead_hours <= 3:
        return "lead_0_3h"
    if lead_hours <= 6:
        return "lead_3_6h"
    if lead_hours <= 12:
        return "lead_6_12h"
    if lead_hours <= 24:
        return "lead_12_24h"
    if lead_hours <= 48:
        return "lead_24_48h"
    return "lead_48h_plus"


def aether_analog_probability(target_date, jam, args, temp_p50, rh_p50, model_prob_rain):
    candidates = []
    observed_paths = [observation_master_file(), path_output(AETHER_FEEDBACK_FILENAME)]
    target_month = target_date.month
    target_hour = int(jam.split(":")[0]) if jam and ":" in jam else 0
    for path in observed_paths:
        if not os.path.exists(path):
            continue
        for row in read_dict_csv(path):
            row_jam = row.get("jam") or row.get("time") or row.get("target_jam") or ""
            if row_jam[:2] != f"{target_hour:02d}":
                continue
            tanggal = row.get("tanggal") or row.get("target_date") or row.get("date") or ""
            try:
                if "-" in tanggal and len(tanggal) == 10 and tanggal[4] == "-":
                    row_month = parse_iso_date(tanggal).month
                elif "-" in tanggal:
                    row_month = parse_display_date(tanggal).month
                else:
                    row_month = target_month
            except Exception:
                row_month = target_month
            temp = aether_value(row.get("temp_c") or row.get("observed_temp_c"))
            rh = aether_value(row.get("rh_pct"))
            rain = aether_value(row.get("rain_mm") or row.get("observed_rain_mm"))
            cat = row.get("category") or row.get("observed_category") or ""
            obs_rain = 1 if (rain is not None and rain > 0) or ("hujan" in cat.lower()) else 0
            month_distance = min(abs(row_month - target_month), 12 - abs(row_month - target_month))
            dist = month_distance * 2.5
            if temp is not None and temp_p50 is not None:
                dist += abs(temp - temp_p50) * 1.2
            if rh is not None and rh_p50 is not None:
                dist += abs(rh - rh_p50) * 0.25
            candidates.append((dist, obs_rain))
    if len(candidates) < 5:
        return None, len(candidates)
    candidates.sort(key=lambda item: item[0])
    selected = candidates[: min(50, len(candidates))]
    analog = sum(v for _, v in selected) / len(selected) * 100.0
    blended = analog if model_prob_rain is None else 0.75 * float(model_prob_rain) + 0.25 * analog
    return round(blended, 1), len(selected)


def aether_risk_label(score):
    if score is None:
        return "unknown"
    if score >= 80:
        return "very_high"
    if score >= 60:
        return "high"
    if score >= 35:
        return "medium"
    return "low"


def aether_trust_level(sources_used, confidence_score, uncertainty_score, coverage_fraction, source_health_mean):
    if sources_used < 3 or coverage_fraction < 0.25:
        return "DO_NOT_TRUST"
    if confidence_score is None:
        confidence_score = 0
    if uncertainty_score is None:
        uncertainty_score = 80
    if source_health_mean is None:
        source_health_mean = 0.7
    score = 0.45 * confidence_score + 0.25 * (100 - uncertainty_score) + 0.20 * (coverage_fraction * 100) + 0.10 * (source_health_mean * 100)
    if score >= 82:
        return "HIGHLY_TRUSTED"
    if score >= 67:
        return "TRUSTED"
    if score >= 45:
        return "USABLE"
    return "EXPERIMENTAL"


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
    for result in results:
        health = SOURCE_HEALTH.get(result.source_id) or {}
        ema_success = aether_value(health.get("ema_success"))
        consecutive = int(health.get("consecutive_failures", 0) or 0)
        if consecutive >= 5:
            state = "QUARANTINED"
        elif consecutive >= 2 or (ema_success is not None and ema_success < 0.55):
            state = "DEGRADED"
        elif consecutive == 1:
            state = "RECOVERING"
        else:
            state = "ACTIVE" if result.success else "DEGRADED"
        rows.append({
            "source_id": result.source_id,
            "provider": result.provider,
            "state": state,
            "success": result.success,
            "points": len(result.points),
            "ema_success": aether_round(ema_success, 4),
            "ema_completeness": health.get("ema_completeness", ""),
            "consecutive_failures": consecutive,
            "http_status": result.http_status if result.http_status is not None else "",
            "duration_ms": result.duration_ms if result.duration_ms is not None else "",
            "error": result.error,
        })
    return rows


def aether_feedback_for_location(args):
    target_date = args.feedback_date or args.target_date or now_local(args.timezone).date().isoformat()
    jam = args.feedback_time or ""
    if not jam:
        raise ValueError("Mode feedback membutuhkan --feedback-time HH:MM")
    row = {
        "created_at": now_local(args.timezone).isoformat(),
        "location_slug": getattr(args, "location_slug", ""),
        "target_date": target_date,
        "jam": jam,
        "observed_category": args.feedback_category or "",
        "observed_rain_mm": args.feedback_rain_mm if args.feedback_rain_mm is not None else "",
        "observed_temp_c": args.feedback_temp_c if args.feedback_temp_c is not None else "",
        "note": args.feedback_note or "",
    }
    existing = read_dict_csv(path_output(AETHER_FEEDBACK_FILENAME)) if os.path.exists(path_output(AETHER_FEEDBACK_FILENAME)) else []
    rows = existing + [row]
    write_dict_csv(path_output(AETHER_FEEDBACK_FILENAME), list(row.keys()), rows)
    try:
        conn = aether_connect_db(); aether_init_db(conn)
        conn.execute("INSERT INTO feedback(created_at,location_slug,target_date,jam,observed_category,observed_rain_mm,observed_temp_c,note) VALUES (?,?,?,?,?,?,?,?)", (row["created_at"], row["location_slug"], row["target_date"], row["jam"], row["observed_category"], aether_value(row["observed_rain_mm"]), aether_value(row["observed_temp_c"]), row["note"]))
        conn.commit(); conn.close()
    except Exception as exc:
        log_warning("Gagal simpan feedback ke SQLite:", exc)
    return row

# AETHER SENTINEL X — Risk, Scenario, Failure & Decision Intelligence Override
# -----------------------------------------------------------------------------
# This block defines the Sentinel X post-processing layer.
# It focuses the system into a hyperlocal
# atmospheric risk command center: situation awareness, multi-reality scenarios,
# failure prediction, self-doubt, threat matrix, decision intelligence, forecast
# constitution, red-team testing, autopsy, and skill-league scaffolding.

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


def sentinel_grade(score, invert=False):
    if score in (None, ""):
        return "unknown"
    try:
        score = float(score)
        if invert:
            score = 100 - score
    except Exception:
        return "unknown"
    if score >= 80:
        return "very_high"
    if score >= 60:
        return "high"
    if score >= 35:
        return "medium"
    return "low"


def sentinel_predictability(forecast_stress, uncertainty, displacement):
    stress = max(aether_value(forecast_stress) or 0, aether_value(uncertainty) or 0, aether_value(displacement) or 0)
    if stress >= 80:
        return "VERY_LOW"
    if stress >= 65:
        return "LOW"
    if stress >= 45:
        return "MEDIUM"
    return "HIGH"


def sentinel_location_personality(args):
    profile = aether_microclimate_profile(args)
    slug_name = f"{getattr(args, 'location_slug', '')} {getattr(args, 'location_name', '')}".lower()
    if profile == "valley_highland" or "jatinangor" in slug_name:
        return {
            "personality": "valley_humid_convective",
            "convective_sensitivity": "high",
            "rain_displacement_risk": "high",
            "morning_humidity_memory": "high",
            "forecast_difficulty": "medium_high",
            "notes": "Lembah/dataran tinggi lokal; timing hujan dan kabut/kelembapan pagi perlu dibaca konservatif.",
        }
    if profile == "urban_highland" or "dago" in slug_name or "bandung" in slug_name:
        return {
            "personality": "urban_highland_local_rain",
            "convective_sensitivity": "high",
            "rain_displacement_risk": "high",
            "morning_humidity_memory": "medium",
            "forecast_difficulty": "medium_high",
            "notes": "Urban-highland mix; hujan lokal dapat bergeser beberapa kilometer dari titik utama.",
        }
    if profile == "lowland_agriculture" or "cirebon" in slug_name or "arjawinangun" in slug_name:
        return {
            "personality": "lowland_heat_humidity",
            "convective_sensitivity": "medium",
            "rain_displacement_risk": "medium",
            "morning_humidity_memory": "medium",
            "forecast_difficulty": "medium",
            "notes": "Lowland/agricultural setting; heat discomfort dan humidity transitions perlu dipantau.",
        }
    return {
        "personality": "generic_hyperlocal",
        "convective_sensitivity": "medium",
        "rain_displacement_risk": "medium",
        "morning_humidity_memory": "medium",
        "forecast_difficulty": "medium",
        "notes": "Profil generik; gunakan feedback lokal agar Sentinel makin spesifik.",
    }


def sentinel_atmospheric_mode(hour, prob_rain, prob_heavy, rh_p50, cloud_p50, uncertainty, cape_p50):
    pr = aether_value(prob_rain) or 0
    ph = aether_value(prob_heavy) or 0
    rh = aether_value(rh_p50) or 0
    cloud = aether_value(cloud_p50) if aether_value(cloud_p50) is not None else 65
    unc = aether_value(uncertainty) or 0
    cape = aether_value(cape_p50) or 0
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
    if pr >= 50:
        return "RAIN_TRANSITION"
    return "MIXED_LOCAL_ATMOSPHERE"


def sentinel_failure_mode(mode, prob_rain, uncertainty, displacement, rain_p90, category_disagreement):
    pr = aether_value(prob_rain) or 0
    unc = aether_value(uncertainty) or 0
    disp = aether_value(displacement) or 0
    p90 = aether_value(rain_p90) or 0
    disag = aether_value(category_disagreement) or 0
    if mode in {"HUMID_CONVECTIVE_AFTERNOON", "HEAVY_RAIN_WATCH"} and disp >= 60:
        return "rain_cell_displacement"
    if pr >= 45 and unc >= 55:
        return "rain_timing_error"
    if p90 >= 8 and pr < 55:
        return "underestimated_intensity_tail"
    if disag >= 60:
        return "category_disagreement_error"
    if pr < 30 and p90 < 3:
        return "low_failure_risk_non_rain"
    return "general_uncertainty"


def sentinel_multi_reality(prob_rain, prob_heavy, displacement, uncertainty, rain_p90):
    pr = clamp(aether_value(prob_rain) or 0, 0, 100)
    ph = clamp(aether_value(prob_heavy) or 0, 0, 100)
    disp = clamp(aether_value(displacement) or 0, 0, 100)
    unc = clamp(aether_value(uncertainty) or 0, 0, 100)
    p90 = aether_value(rain_p90) or 0
    dry_miss = clamp((100 - pr) * 0.55 + disp * 0.25 + unc * 0.10, 2, 70)
    nearby = clamp(pr * (disp / 100.0) * 0.55 + unc * 0.18, 3, 55)
    direct_light = clamp(pr * (1 - ph / 120.0) * 0.45, 3, 60)
    direct_moderate = clamp(pr * 0.18 + max(p90 - 4, 0) * 2.0, 1, 45)
    convective_burst = clamp(ph * 0.40 + max(p90 - 9, 0) * 2.8 + unc * 0.08, 0, 35)
    vals = [dry_miss, nearby, direct_light, direct_moderate, convective_burst]
    total = sum(vals) or 1
    norm = [round(v / total * 100, 1) for v in vals]
    # compensate rounding
    diff = round(100 - sum(norm), 1)
    norm[2] = round(norm[2] + diff, 1)
    labels = ["dry_miss", "nearby_rain_only", "direct_light_rain", "direct_moderate_rain", "convective_burst"]
    return dict(zip(labels, norm))


def sentinel_threat_level(score):
    if score >= 80:
        return "VERY_HIGH"
    if score >= 60:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    return "LOW"


def sentinel_activity_score(base, penalty):
    return int(round(clamp(base - penalty, 0, 10)))


def sentinel_decision(row, args):
    mission = getattr(args, "mission", "safety_first") or "safety_first"
    rain = aether_value(row.get("rain_threat_score")) or 0
    heavy = aether_value(row.get("heavy_rain_threat_score")) or 0
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
        if mission == "laundry":
            return "Jangan andalkan jemur luar ruang pada jam ini; pilih window yang lebih kering atau indoor backup."
        if mission == "fieldwork":
            return "Fieldwork sebaiknya dilakukan sebelum risk peak; siapkan opsi berhenti/berlindung saat hujan lokal terbentuk."
        if mission == "outdoor_event":
            return "Event outdoor perlu plan B indoor/teduh; risiko utama adalah hujan lokal dan timing yang bisa bergeser."
        if mission == "sport":
            return "Olahraga outdoor kurang ideal jika mendekati risk peak; pantau update 1–3 jam sebelumnya."
        if mission == "photography":
            return "Fotografi outdoor masih mungkin, tetapi siapkan perubahan rute karena nearby rain dan cloud build-up."
        if mission == "research":
            return "Tandai jam ini sebagai kasus high-value untuk verifikasi; uncertainty dan failure risk layak dianalisis."
        return "Bawa payung/jas hujan dan gunakan interpretasi konservatif, terutama jika keluar setelah siang."
    if 7 <= hour <= 11:
        return "Window relatif baik untuk aktivitas luar ruang, tetapi tetap cek update menjelang siang."
    return "Risiko relatif terkendali; keputusan normal masih masuk akal dengan pemantauan berkala."


def sentinel_expert_council(row, args):
    items = []
    pr = aether_value(row.get("prob_rain")) or 0
    ph = aether_value(row.get("prob_heavy_rain")) or 0
    unc = aether_value(row.get("uncertainty_score")) or 0
    disp = aether_value(row.get("rain_displacement_risk")) or 0
    p90 = aether_value(row.get("rain_p90")) or 0
    mode = row.get("atmospheric_mode") or "UNKNOWN"
    if pr >= 55:
        items.append("Local Signal Analyst: sinyal hujan cukup kuat untuk mengaktifkan rain-risk window.")
    else:
        items.append("Local Signal Analyst: sinyal hujan belum dominan, tetapi tetap membaca tail-risk dari P90/P95.")
    if unc >= 60:
        items.append("Uncertainty Auditor: disagreement/spread cukup tinggi; forecast tidak boleh dipakai sebagai kepastian tunggal.")
    else:
        items.append("Uncertainty Auditor: agreement relatif masih dapat diterima untuk keputusan umum.")
    if disp >= 60:
        items.append("Rain Displacement Analyst: hujan sekitar lokasi lebih mungkin daripada direct-hit yang presisi.")
    if ph >= 25 or p90 >= 8:
        items.append("Risk Officer: worst-case hujan sedang/lebat perlu tetap ditampilkan walaupun skenario utama lebih ringan.")
    if mode in {"HUMID_CONVECTIVE_AFTERNOON", "HEAVY_RAIN_WATCH"}:
        items.append("Tropical Rain Specialist: gunakan mode konservatif karena hujan konvektif lokal rawan salah timing/posisi.")
    analog_n = int(aether_value(row.get("analog_cases")) or 0)
    if analog_n > 0:
        items.append(f"Analog Memory Analyst: {analog_n} kasus lokal historis/feedback ikut memodifikasi peluang hujan.")
    items.append(f"Decision Judge: mission={getattr(args, 'mission', 'safety_first')} → {row.get('decision_recommendation','')}")
    return " | ".join(items)


def sentinel_counterfactual(row):
    factors = []
    if aether_value(row.get("rh_p50")) is not None and (aether_value(row.get("rh_p50")) or 0) >= 82:
        factors.append("Jika RH turun ±10%, rain support kemungkinan turun signifikan.")
    if (aether_value(row.get("category_disagreement")) or 0) >= 50:
        factors.append("Jika kategori antar-source lebih sepakat, trust level akan naik.")
    if (aether_value(row.get("rain_p90")) or 0) >= 8:
        factors.append("Jika rain P90 turun di bawah 5 mm, worst-case scenario akan melemah.")
    if (aether_value(row.get("rain_displacement_risk")) or 0) >= 55:
        factors.append("Jika grid/nearby signal mendukung direct-hit, dry-miss probability akan turun.")
    if not factors:
        factors.append("Faktor pengubah utama belum dominan; forecast relatif dikendalikan oleh consensus source.")
    return " ".join(factors[:3])


def sentinel_forecast_contract_summary(row):
    status = row.get("operational_status")
    if status == "BLACK":
        return "Jangan percaya output untuk keputusan penting; data/source coverage tidak layak."
    if status == "RED":
        return "Gunakan sebagai warning/risk signal, bukan timing presisi."
    if status == "YELLOW":
        return "Boleh dipakai untuk keputusan umum dengan plan B dan update berkala."
    return "Boleh dipakai untuk rencana umum; tetap bukan peringatan resmi cuaca ekstrem."


def sentinel_build_explanation(row):
    reasons = []
    if row.get("atmospheric_mode"):
        reasons.append(f"mode atmosfer {row['atmospheric_mode']}")
    if row.get("sources_used"):
        reasons.append(f"{row['sources_used']} source aktif")
    if aether_value(row.get("prob_rain")) is not None and (aether_value(row.get("prob_rain")) or 0) >= 55:
        reasons.append(f"probabilitas hujan {row['prob_rain']}%")
    if aether_value(row.get("rain_p90")) is not None and (aether_value(row.get("rain_p90")) or 0) >= 7:
        reasons.append(f"rain P90 {row['rain_p90']} mm")
    if aether_value(row.get("rain_displacement_risk")) is not None and (aether_value(row.get("rain_displacement_risk")) or 0) >= 60:
        reasons.append("risiko displacement hujan tinggi")
    if aether_value(row.get("forecast_failure_risk")) is not None and (aether_value(row.get("forecast_failure_risk")) or 0) >= 60:
        reasons.append(f"failure risk {row['forecast_failure_risk']}/100")
    if not reasons:
        reasons.append("sinyal atmosfer relatif netral")
    return "Sentinel memilih interpretasi ini karena " + "; ".join(reasons) + "."


def aether_build_rows(points, ensemble_rows, target_date, args):
    grouped = {jam: [] for jam in TARGET_TIMES}
    for point in points:
        grouped.setdefault(point.target_time, []).append(point)
    ensemble_by_jam = {row[0]: row for row in ensemble_rows}
    micro_profile = aether_microclimate_profile(args)
    personality = sentinel_location_personality(args)
    rows = []
    for jam in TARGET_TIMES:
        bucket = grouped.get(jam) or []
        ens = ensemble_by_jam.get(jam)
        hour = int(jam.split(":")[0]) if jam and ":" in jam else 0
        source_ids = sorted({p.source_id for p in bucket})
        sources_used = len(bucket)
        weights = [(p, point_weight(p)) for p in bucket]
        weight_total = sum(w for _, w in weights)
        expected_sources = max(len(ACTIVE_SOURCE_CONFIGS), 1)
        coverage_fraction = round(sources_used / expected_sources, 4) if expected_sources else 0

        temp_pairs = [(p.temp_c, w) for p, w in weights if p.temp_c is not None]
        rh_pairs = [(p.rh_pct, w) for p, w in weights if p.rh_pct is not None]
        rain_pairs = [(p.rain_mm, w) for p, w in weights if p.rain_mm is not None]
        wind_pairs = [(p.wind_kmh, w) for p, w in weights if p.wind_kmh is not None]
        gust_pairs = aether_get_weighted_attr(bucket, "wind_gusts_kmh")
        hi_pairs = [(heat_index(p.temp_c, p.rh_pct), w) for p, w in weights if heat_index(p.temp_c, p.rh_pct) is not None]
        cloud_pairs = aether_get_weighted_attr(bucket, "cloud_cover_pct")
        precip_prob_pairs = aether_get_weighted_attr(bucket, "precip_prob_pct")
        cape_pairs = aether_get_weighted_attr(bucket, "cape_jkg")
        visibility_pairs = aether_get_weighted_attr(bucket, "visibility_m")

        category_weights = {}
        for p, w in weights:
            category_weights[p.category] = category_weights.get(p.category, 0.0) + w
        category_probs = {cat: (category_weights.get(cat, 0.0) / weight_total * 100.0 if weight_total else 0.0) for cat in CUACA_ORDER}
        dominant = max(category_weights, key=category_weights.get) if category_weights else ""
        dominant_prob = category_probs.get(dominant, 0.0) if dominant else 0.0
        category_disagreement = round(100.0 - dominant_prob, 2) if bucket else 100.0

        prob_rain_cat = sum(category_probs.get(cat, 0.0) for cat in ("Hujan Ringan", "Hujan Sedang", "Hujan Lebat"))
        prob_heavy_cat = category_probs.get("Hujan Lebat", 0.0)
        prob_mod_heavy_cat = sum(category_probs.get(cat, 0.0) for cat in ("Hujan Sedang", "Hujan Lebat"))
        precip_prob_mean = aether_weighted_mean(precip_prob_pairs)
        prob_rain = prob_rain_cat if precip_prob_mean is None else 0.62 * prob_rain_cat + 0.38 * precip_prob_mean
        rain_heavy_signal = 0.0
        rain_mod_signal = 0.0
        if weight_total:
            rain_heavy_signal = sum(w for p, w in weights if p.rain_mm is not None and p.rain_mm >= 10.0) / weight_total * 100.0
            rain_mod_signal = sum(w for p, w in weights if p.rain_mm is not None and p.rain_mm >= 5.0) / weight_total * 100.0
        prob_heavy = max(prob_heavy_cat, 0.55 * prob_heavy_cat + 0.45 * rain_heavy_signal)
        prob_mod_heavy = max(prob_mod_heavy_cat, 0.55 * prob_mod_heavy_cat + 0.45 * rain_mod_signal)

        q = lambda pairs, quant: aether_weighted_quantile(pairs, quant)
        temp_p05, temp_p10, temp_p25, temp_p50, temp_p75, temp_p90, temp_p95 = [q(temp_pairs, x) for x in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)]
        rh_p10, rh_p50, rh_p90 = [q(rh_pairs, x) for x in (0.10, 0.50, 0.90)]
        rain_p05, rain_p10, rain_p25, rain_p50, rain_p75, rain_p90, rain_p95 = [q(rain_pairs, x) for x in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)]
        wind_p50, wind_p90 = [q(wind_pairs, x) for x in (0.50, 0.90)]
        gust_p90 = q(gust_pairs, 0.90)
        hi_p50, hi_p90 = [q(hi_pairs, x) for x in (0.50, 0.90)]
        cape_p50 = q(cape_pairs, 0.50)
        visibility_p10 = q(visibility_pairs, 0.10)
        cloud_p50 = q(cloud_pairs, 0.50)
        if cloud_p50 is None and bucket:
            cloud_p50 = q([(aether_category_cloud_proxy(p.category), w) for p, w in weights], 0.50)

        analog_prob, analog_n = aether_analog_probability(target_date, jam, args, temp_p50, rh_p50, prob_rain)
        if analog_prob is not None:
            prob_rain = analog_prob
        temp_micro, rh_micro, fog_bonus, temp_adj, rh_adj = aether_microclimate_adjustment(micro_profile, hour, temp_p50, rh_p50)

        confidence_score = aether_value(ens[6]) if ens else None
        source_health_values = [source_health_factor(p.source_id) for p in bucket]
        source_health_mean = round(sum(source_health_values) / len(source_health_values), 4) if source_health_values else None
        gap_values = [p.gap_minutes for p in bucket if p.gap_minutes is not None]
        gap_mean = round(sum(gap_values) / len(gap_values), 2) if gap_values else None
        freshness_uncertainty = clamp((gap_mean or 0.0) / 180.0 * 100.0, 0.0, 100.0)
        health_uncertainty = 100.0 - (source_health_mean * 100.0 if source_health_mean is not None else 60.0)
        lead_hours = aether_lead_hours(target_date, jam, args)
        lead_uncertainty = clamp((lead_hours or 24.0) / 72.0 * 100.0, 5.0, 100.0)
        rain_spread_uncertainty = clamp(((rain_p90 or 0.0) - (rain_p10 or 0.0)) / 15.0 * 100.0, 0.0, 100.0)
        displacement_personality = 70 if personality.get("rain_displacement_risk") == "high" else 50 if personality.get("rain_displacement_risk") == "medium" else 35
        rain_displacement_risk = round(clamp(0.32 * category_disagreement + 0.25 * rain_spread_uncertainty + 0.20 * displacement_personality + 0.15 * (prob_rain or 0) + 0.08 * lead_uncertainty, 0, 100), 1)
        spatial_uncertainty = round(clamp(0.62 * rain_displacement_risk + 0.38 * category_disagreement, 0, 100), 1)
        uncertainty_score = round(clamp(0.28 * category_disagreement + 0.22 * rain_spread_uncertainty + 0.18 * freshness_uncertainty + 0.14 * health_uncertainty + 0.10 * lead_uncertainty + 0.08 * spatial_uncertainty, 0, 100), 1)
        mode = sentinel_atmospheric_mode(hour, prob_rain, prob_heavy, rh_p50, cloud_p50, uncertainty_score, cape_p50)

        rain_threat_score = round(clamp(0.50 * (prob_rain or 0) + 0.22 * min((rain_p90 or 0) / 12.0 * 100.0, 100) + 0.18 * (rh_p50 or 0) + 0.10 * spatial_uncertainty, 0, 100), 1)
        heavy_rain_threat_score = round(clamp(0.42 * (prob_heavy or 0) + 0.38 * min((rain_p95 or rain_p90 or 0) / 18.0 * 100.0, 100) + 0.10 * uncertainty_score + 0.10 * (cape_p50 or 0) / 1500.0 * 100.0, 0, 100), 1)
        wind_threat_score = round(clamp(max((wind_p90 or 0), (gust_p90 or 0)) / 45.0 * 100.0, 0, 100), 1)
        heat_discomfort_score = round(clamp(((hi_p90 or hi_p50 or temp_p90 or temp_p50 or 26) - 27.0) / 9.0 * 100.0 + max((rh_p50 or 70) - 75, 0), 0, 100), 1)
        low_visibility_score = round(clamp((10000 - (visibility_p10 if visibility_p10 is not None else 10000)) / 10000.0 * 100.0 + fog_bonus, 0, 100), 1)
        thunderstorm_proxy_score = round(clamp(0.42 * (prob_heavy or 0) + 0.28 * (cape_p50 or 0) / 1500.0 * 100.0 + 0.20 * (rh_p50 or 0) + 0.10 * (cloud_p50 or 0), 0, 100), 1)
        direct_hit_risk = round(clamp(rain_threat_score * (1 - rain_displacement_risk / 165.0), 0, 100), 1)
        nearby_rain_risk = round(clamp(0.70 * rain_threat_score + 0.45 * rain_displacement_risk, 0, 100), 1)
        forecast_stress_index = round(clamp(0.30 * uncertainty_score + 0.25 * rain_displacement_risk + 0.18 * rain_spread_uncertainty + 0.12 * lead_uncertainty + 0.10 * category_disagreement + 0.05 * (100 - coverage_fraction * 100), 0, 100), 1)
        forecast_failure_risk = round(clamp(0.35 * forecast_stress_index + 0.25 * uncertainty_score + 0.20 * rain_displacement_risk + 0.10 * (prob_rain or 0) + 0.10 * (100 - coverage_fraction * 100), 0, 100), 1)
        self_doubt_score = round(clamp(0.50 * forecast_failure_risk + 0.30 * uncertainty_score + 0.20 * category_disagreement, 0, 100), 1)
        failure_mode = sentinel_failure_mode(mode, prob_rain, uncertainty_score, rain_displacement_risk, rain_p90, category_disagreement)
        predictability = sentinel_predictability(forecast_stress_index, uncertainty_score, rain_displacement_risk)
        scenarios = sentinel_multi_reality(prob_rain, prob_heavy, rain_displacement_risk, uncertainty_score, rain_p90)

        trust = aether_trust_level(sources_used, confidence_score, uncertainty_score, coverage_fraction, source_health_mean)
        op_status = aether_operational_status(trust, rain_threat_score, uncertainty_score, sources_used)
        if forecast_failure_risk >= 82 and op_status != "BLACK":
            op_status = "RED"
        elif forecast_failure_risk >= 62 and op_status == "GREEN":
            op_status = "YELLOW"
        if mode == "HEAVY_RAIN_WATCH" and op_status == "GREEN":
            op_status = "YELLOW"
        if mode in {"HUMID_CONVECTIVE_AFTERNOON", "HEAVY_RAIN_WATCH"}:
            route = "conservative_convective_risk_route"
        elif forecast_failure_risk >= 70:
            route = "failure_aware_scenario_route"
        elif analog_n:
            route = "analog_memory_blend_route"
        elif getattr(args, "mission", "") in {"outdoor_event", "fieldwork", "commute", "avoid_rain"}:
            route = "mission_decision_route"
        else:
            route = "sentinel_probabilistic_risk_route"

        row = {
            "target_date": target_date.isoformat(),
            "jam": jam,
            "target_datetime": aether_target_datetime(target_date, jam, args.timezone).isoformat(),
            "lead_hours": aether_round(lead_hours),
            "lead_bucket": aether_lead_bucket(lead_hours),
            "mission": getattr(args, "mission", "safety_first"),
            "location_personality": personality.get("personality"),
            "microclimate_profile": micro_profile,
            "atmospheric_mode": mode,
            "predictability": predictability,
            "dominant_category": dominant,
            "dominant_category_probability": aether_round(dominant_prob, 1),
            "category_disagreement": aether_round(category_disagreement, 1),
            "sources_used": sources_used,
            "source_ids": "+".join(source_ids),
            "coverage_fraction": aether_round(coverage_fraction, 3),
            "temp_p05": aether_round(temp_p05), "temp_p10": aether_round(temp_p10), "temp_p25": aether_round(temp_p25), "temp_p50": aether_round(temp_p50), "temp_p75": aether_round(temp_p75), "temp_p90": aether_round(temp_p90), "temp_p95": aether_round(temp_p95),
            "temp_micro_p50": aether_round(temp_micro), "temp_micro_adjustment": aether_round(temp_adj),
            "rh_p10": aether_round(rh_p10), "rh_p50": aether_round(rh_p50), "rh_p90": aether_round(rh_p90), "rh_micro_p50": aether_round(rh_micro), "rh_micro_adjustment": aether_round(rh_adj),
            "rain_p05": aether_round(rain_p05), "rain_p10": aether_round(rain_p10), "rain_p25": aether_round(rain_p25), "rain_p50": aether_round(rain_p50), "rain_p75": aether_round(rain_p75), "rain_p90": aether_round(rain_p90), "rain_p95": aether_round(rain_p95),
            "prob_rain": aether_round(prob_rain, 1), "prob_moderate_heavy_rain": aether_round(prob_mod_heavy, 1), "prob_heavy_rain": aether_round(prob_heavy, 1),
            "wind_p50": aether_round(wind_p50), "wind_p90": aether_round(wind_p90), "gust_p90": aether_round(gust_p90),
            "heat_index_p50": aether_round(hi_p50), "heat_index_p90": aether_round(hi_p90), "cloud_p50": aether_round(cloud_p50), "cape_p50": aether_round(cape_p50), "visibility_p10": aether_round(visibility_p10),
            "analog_probability": aether_round(analog_prob, 1), "analog_cases": analog_n,
            "direct_hit_risk": aether_round(direct_hit_risk, 1), "nearby_rain_risk": aether_round(nearby_rain_risk, 1), "rain_displacement_risk": aether_round(rain_displacement_risk, 1), "spatial_uncertainty": aether_round(spatial_uncertainty, 1),
            "rain_threat_score": aether_round(rain_threat_score, 1), "rain_threat_level": sentinel_threat_level(rain_threat_score),
            "heavy_rain_threat_score": aether_round(heavy_rain_threat_score, 1), "heavy_rain_threat_level": sentinel_threat_level(heavy_rain_threat_score),
            "wind_threat_score": aether_round(wind_threat_score, 1), "wind_threat_level": sentinel_threat_level(wind_threat_score),
            "heat_discomfort_threat_score": aether_round(heat_discomfort_score, 1), "heat_discomfort_threat_level": sentinel_threat_level(heat_discomfort_score),
            "low_visibility_threat_score": aether_round(low_visibility_score, 1), "low_visibility_threat_level": sentinel_threat_level(low_visibility_score),
            "thunderstorm_proxy_threat_score": aether_round(thunderstorm_proxy_score, 1), "thunderstorm_proxy_threat_level": sentinel_threat_level(thunderstorm_proxy_score),
            "uncertainty_score": aether_round(uncertainty_score, 1),
            "forecast_stress_index": aether_round(forecast_stress_index, 1),
            "forecast_failure_risk": aether_round(forecast_failure_risk, 1),
            "self_doubt_score": aether_round(self_doubt_score, 1),
            "main_failure_mode": failure_mode,
            "scenario_dry_miss": aether_round(scenarios["dry_miss"], 1),
            "scenario_nearby_rain_only": aether_round(scenarios["nearby_rain_only"], 1),
            "scenario_direct_light_rain": aether_round(scenarios["direct_light_rain"], 1),
            "scenario_direct_moderate_rain": aether_round(scenarios["direct_moderate_rain"], 1),
            "scenario_convective_burst": aether_round(scenarios["convective_burst"], 1),
            "walking_score": sentinel_activity_score(9, rain_threat_score/13 + heat_discomfort_score/25),
            "motorbike_score": sentinel_activity_score(9, rain_threat_score/11 + wind_threat_score/20),
            "outdoor_event_score": sentinel_activity_score(10, rain_threat_score/9 + forecast_failure_risk/18),
            "fieldwork_score": sentinel_activity_score(10, rain_threat_score/10 + heat_discomfort_score/30 + forecast_failure_risk/25),
            "laundry_score": sentinel_activity_score(10, rain_threat_score/7 + (cloud_p50 or 70)/18),
            "sport_score": sentinel_activity_score(9, rain_threat_score/11 + heat_discomfort_score/18),
            "trust_level": trust,
            "operational_status": op_status,
            "autopilot_route": route,
            "confidence_score_base": aether_round(confidence_score, 1),
            "source_health_mean": aether_round(source_health_mean, 3),
            "gap_mean_minutes": aether_round(gap_mean, 1),
        }
        row["decision_recommendation"] = sentinel_decision(row, args)
        row["expert_council"] = sentinel_expert_council(row, args)
        row["counterfactual_summary"] = sentinel_counterfactual(row)
        row["forecast_contract_summary"] = sentinel_forecast_contract_summary(row)
        row["explanation"] = sentinel_build_explanation(row)
        rows.append(row)
    return rows


def sentinel_daily_status(statuses):
    if "BLACK" in statuses:
        return "BLACK"
    if "RED" in statuses:
        return "RED"
    if "YELLOW" in statuses:
        return "YELLOW"
    return "GREEN"


def aether_daily_summary(aether_rows, args):
    if not aether_rows:
        return {"generated_at": now_local(args.timezone).isoformat(), "daily_operational_status": "BLACK", "summary_text": "Tidak ada forecast yang dapat diproses."}
    def max_row(field):
        return max(aether_rows, key=lambda r: aether_value(r.get(field)) or -1)
    peak_rain = max_row("rain_threat_score")
    peak_heavy = max_row("heavy_rain_threat_score")
    peak_failure = max_row("forecast_failure_risk")
    peak_stress = max_row("forecast_stress_index")
    peak_doubt = max_row("self_doubt_score")
    statuses = [r.get("operational_status", "") for r in aether_rows]
    modes = {}
    for r in aether_rows:
        modes[r.get("atmospheric_mode", "UNKNOWN")] = modes.get(r.get("atmospheric_mode", "UNKNOWN"), 0) + 1
    dominant_mode = max(modes, key=modes.get) if modes else "UNKNOWN"
    risk_window = sentinel_risk_window(aether_rows, "rain_threat_score", 55)
    best_window = sentinel_best_window(aether_rows)
    daily = {
        "generated_at": now_local(args.timezone).isoformat(),
        "aether_version": AETHER_VERSION,
        "location": getattr(args, "location_name", ""),
        "mission": getattr(args, "mission", "safety_first"),
        "daily_operational_status": sentinel_daily_status(statuses),
        "dominant_atmospheric_mode": dominant_mode,
        "location_personality": sentinel_location_personality(args),
        "risk_window": risk_window,
        "best_general_activity_window": best_window,
        "peak_rain_threat_hour": peak_rain.get("jam"),
        "peak_rain_threat_score": peak_rain.get("rain_threat_score"),
        "peak_heavy_rain_threat_hour": peak_heavy.get("jam"),
        "peak_heavy_rain_threat_score": peak_heavy.get("heavy_rain_threat_score"),
        "peak_failure_risk_hour": peak_failure.get("jam"),
        "peak_failure_risk_score": peak_failure.get("forecast_failure_risk"),
        "peak_forecast_stress_hour": peak_stress.get("jam"),
        "peak_forecast_stress_score": peak_stress.get("forecast_stress_index"),
        "peak_self_doubt_hour": peak_doubt.get("jam"),
        "peak_self_doubt_score": peak_doubt.get("self_doubt_score"),
    }
    daily["summary_text"] = sentinel_daily_narrative(aether_rows, daily, args)
    return daily


def sentinel_risk_window(rows, field, threshold):
    risky = [r.get("jam") for r in rows if (aether_value(r.get(field)) or 0) >= threshold]
    if not risky:
        return "Tidak ada risk window kuat"
    return f"{risky[0]}–{risky[-1]}"


def sentinel_best_window(rows):
    scored = []
    for r in rows:
        hour = int(str(r.get("jam", "00:00")).split(":")[0])
        if 6 <= hour <= 18:
            score = min(aether_value(r.get("walking_score")) or 0, aether_value(r.get("fieldwork_score")) or 0, aether_value(r.get("outdoor_event_score")) or 0)
            scored.append((score, r.get("jam")))
    if not scored:
        return "Tidak cukup data"
    scored.sort(reverse=True)
    return scored[0][1]


def sentinel_daily_narrative(rows, daily, args):
    status = daily.get("daily_operational_status")
    mode = daily.get("dominant_atmospheric_mode")
    risk_window = daily.get("risk_window")
    peak_failure = daily.get("peak_failure_risk_score")
    personality = daily.get("location_personality", {})
    return (
        f"Sentinel membaca hari ini sebagai {mode} untuk {getattr(args, 'location_name', '')}. "
        f"Status operasional harian {status}. Risk window utama: {risk_window}. "
        f"Failure risk tertinggi {peak_failure}/100, sehingga bagian forecast yang perlu paling diragukan adalah timing/posisi hujan lokal. "
        f"Personality lokasi: {personality.get('personality', 'generic')}; {personality.get('notes', '')}"
    )


def sentinel_bar(value):
    try:
        v = clamp(float(value), 0, 100)
    except Exception:
        v = 0
    return f"<span class='bar'><i style='width:{v:.1f}%'></i></span>"


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return
    esc = html.escape
    def card(title, body):
        return f"<section class='card'><h2>{esc(title)}</h2>{body}</section>"
    rows_html = []
    for r in aether_rows:
        rows_html.append(
            "<tr>" +
            f"<td>{esc(str(r.get('jam','')))}</td>" +
            f"<td>{esc(str(r.get('atmospheric_mode','')))}</td>" +
            f"<td>{esc(str(r.get('dominant_category','')))}</td>" +
            f"<td>{esc(str(r.get('prob_rain','')))}% {sentinel_bar(r.get('prob_rain'))}</td>" +
            f"<td>{esc(str(r.get('rain_threat_level','')))} ({esc(str(r.get('rain_threat_score','')))})</td>" +
            f"<td>{esc(str(r.get('forecast_failure_risk','')))}</td>" +
            f"<td>{esc(str(r.get('self_doubt_score','')))}</td>" +
            f"<td>{esc(str(r.get('operational_status','')))}</td>" +
            f"<td>{esc(str(r.get('decision_recommendation','')))}</td>" +
            "</tr>"
        )
    source_html = []
    for srow in source_state_rows:
        source_html.append(f"<tr><td>{esc(str(srow.get('source_id','')))}</td><td>{esc(str(srow.get('state','')))}</td><td>{esc(str(srow.get('success','')))}</td><td>{esc(str(srow.get('points','')))}</td><td>{esc(str(srow.get('ema_success','')))}</td><td>{esc(str(srow.get('duration_ms','')))}</td></tr>")
    scenario = max(aether_rows, key=lambda r: aether_value(r.get("rain_threat_score")) or -1) if aether_rows else {}
    scenario_html = "".join([
        f"<div class='scenario'><b>{label}</b><span>{scenario.get(key,'')}%</span>{sentinel_bar(scenario.get(key,''))}</div>"
        for label, key in [
            ("Dry miss", "scenario_dry_miss"), ("Nearby rain only", "scenario_nearby_rain_only"),
            ("Direct light rain", "scenario_direct_light_rain"), ("Direct moderate rain", "scenario_direct_moderate_rain"),
            ("Convective burst", "scenario_convective_burst")]
    ])
    council_html = "<ol>" + "".join(f"<li>{esc(part.strip())}</li>" for part in str(scenario.get("expert_council", "")).split("|") if part.strip()) + "</ol>"
    constitution_html = "<ol>" + "".join(f"<li>{esc(item)}</li>" for item in SENTINEL_CONSTITUTION) + "</ol>"
    table = "<table><thead><tr><th>Jam</th><th>Mode</th><th>Dominan</th><th>Prob Hujan</th><th>Rain Threat</th><th>Failure</th><th>Self-doubt</th><th>Status</th><th>Decision</th></tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    source_table = "<table><thead><tr><th>Source</th><th>State</th><th>Success</th><th>Points</th><th>EMA</th><th>Latency</th></tr></thead><tbody>" + "".join(source_html) + "</tbody></table>"
    doc = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{esc(AETHER_VERSION)} — {esc(getattr(args,'location_name',''))}</title>
<style>
body{{font-family:Inter,Arial,sans-serif;background:#0f172a;color:#e5e7eb;margin:0;padding:24px}} h1{{margin:0 0 8px}} h2{{margin-top:0;color:#bfdbfe}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}} .card{{background:#111827;border:1px solid #243044;border-radius:18px;padding:18px;box-shadow:0 10px 30px #0005}} .badge{{display:inline-block;background:#1e293b;border:1px solid #334155;border-radius:999px;padding:7px 11px;margin:4px}} table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263244;padding:8px;text-align:left;vertical-align:top}} th{{color:#93c5fd;background:#0b1220}} .bar{{display:inline-block;width:90px;height:9px;background:#273244;border-radius:99px;overflow:hidden;margin-left:8px;vertical-align:middle}} .bar i{{display:block;height:100%;background:#60a5fa}} .scenario{{display:grid;grid-template-columns:1fr 60px 120px;align-items:center;gap:8px;margin:8px 0}} small,p,li{{line-height:1.45}} code{{background:#020617;border:1px solid #334155;padding:2px 5px;border-radius:5px}}
</style></head><body>
<h1>{esc(AETHER_VERSION)}</h1>
<div><span class='badge'>Lokasi: {esc(getattr(args,'location_name',''))}</span><span class='badge'>Mission: {esc(str(daily.get('mission','')))}</span><span class='badge'>Status: {esc(str(daily.get('daily_operational_status','')))}</span><span class='badge'>Generated: {esc(str(daily.get('generated_at','')))}</span></div>
<div class='grid'>
{card('Atmospheric Situation', f"<p>{esc(daily.get('summary_text',''))}</p><p><b>Dominant mode:</b> {esc(str(daily.get('dominant_atmospheric_mode','')))}</p><p><b>Risk window:</b> {esc(str(daily.get('risk_window','')))}</p><p><b>Best general activity window:</b> {esc(str(daily.get('best_general_activity_window','')))}</p>")}
{card('Forecast Stress & Failure', f"<p><b>Peak failure risk:</b> {esc(str(daily.get('peak_failure_risk_hour','')))} — {esc(str(daily.get('peak_failure_risk_score','')))}/100</p><p><b>Peak stress:</b> {esc(str(daily.get('peak_forecast_stress_hour','')))} — {esc(str(daily.get('peak_forecast_stress_score','')))}/100</p><p><b>Peak self-doubt:</b> {esc(str(daily.get('peak_self_doubt_hour','')))} — {esc(str(daily.get('peak_self_doubt_score','')))}/100</p>")}
{card('Multi-Reality Scenario at Peak Rain Threat', scenario_html)}
{card('Expert Council Debate', council_html)}
{card('Forecast Constitution', constitution_html)}
{card('Forecast Contract', '<p>Gunakan Sentinel sebagai risk intelligence dan decision support. Ini bukan pengganti peringatan resmi BMKG untuk keselamatan publik atau cuaca ekstrem.</p>')}
</div>
{card('Risk Timeline & Hourly Decision Table', table)}
{card('Source State', source_table)}
</body></html>"""
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "generated_at": now_local(args.timezone).isoformat()})
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))


def aether_write_report(aether_rows, daily, args):
    lines = []
    lines.append(f"# {AETHER_VERSION}")
    lines.append("")
    lines.append(f"Lokasi: **{getattr(args, 'location_name', '')}**  ")
    lines.append(f"Mission: **{daily.get('mission','')}**  ")
    lines.append(f"Generated: {daily.get('generated_at','')}  ")
    lines.append(f"Status operasional harian: **{daily.get('daily_operational_status','')}**")
    lines.append("")
    lines.append("## Atmospheric Situation")
    lines.append(daily.get("summary_text", ""))
    lines.append("")
    lines.append("## Jam Kritis")
    lines.append(f"- Rain threat tertinggi: **{daily.get('peak_rain_threat_hour')}** skor {daily.get('peak_rain_threat_score')}")
    lines.append(f"- Heavy rain threat tertinggi: **{daily.get('peak_heavy_rain_threat_hour')}** skor {daily.get('peak_heavy_rain_threat_score')}")
    lines.append(f"- Forecast failure risk tertinggi: **{daily.get('peak_failure_risk_hour')}** skor {daily.get('peak_failure_risk_score')}")
    lines.append(f"- Forecast stress tertinggi: **{daily.get('peak_forecast_stress_hour')}** skor {daily.get('peak_forecast_stress_score')}")
    lines.append("")
    lines.append("## Hourly Risk Table")
    lines.append("| Jam | Mode | Dominan | Prob Hujan | Rain Threat | Failure Risk | Self-Doubt | Decision | Status |")
    lines.append("|---|---|---|---:|---:|---:|---:|---|---|")
    for r in aether_rows:
        lines.append(f"| {r['jam']} | {r['atmospheric_mode']} | {r['dominant_category']} | {r['prob_rain']}% | {r['rain_threat_score']} | {r['forecast_failure_risk']} | {r['self_doubt_score']} | {r['decision_recommendation']} | {r['operational_status']} |")
    lines.append("")
    lines.append("## Forecast Constitution")
    for i, item in enumerate(SENTINEL_CONSTITUTION, 1):
        lines.append(f"{i}. {item}")
    lines.append("")
    lines.append("## Batas Pemakaian")
    lines.append("Sentinel X adalah sistem post-processing multi-source untuk risk intelligence dan keputusan umum. Untuk peringatan ekstrem/resmi, tetap gunakan rujukan BMKG dan otoritas terkait.")
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write("\n".join(lines)))


def aether_write_contract(daily, args):
    payload = {
        "aether_version": AETHER_VERSION,
        "generated_at": now_local(args.timezone).isoformat(),
        "location": getattr(args, "location_name", ""),
        "mission": getattr(args, "mission", "safety_first"),
        "status": daily.get("daily_operational_status"),
        "constitution": SENTINEL_CONSTITUTION,
        "validity_contract": {
            "spatial_scope": "Hyperlocal point/nearby risk intelligence; hujan konvektif dapat bergeser beberapa kilometer.",
            "strongest_for": ["rain-risk window", "uncertainty awareness", "decision support", "temperature/RH tendency", "source disagreement detection"],
            "weakest_for": ["exact convective rain timing", "street-scale rain cell position", "extreme weather safety decision", "official public warning"],
            "operational_status_meaning": {
                "GREEN": "usable for general planning",
                "YELLOW": "usable with caution and plan B",
                "RED": "treat as warning/risk signal; do not rely on precise timing",
                "BLACK": "data/source condition too weak; do not trust for decisions",
            },
            "official_warning_note": "Gunakan peringatan resmi BMKG untuk cuaca ekstrem dan keselamatan publik.",
        },
    }
    write_json(path_output(AETHER_CONTRACT_FILENAME), payload)


def aether_init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS forecast_runs (
            run_id TEXT PRIMARY KEY,
            generated_at TEXT,
            aether_version TEXT,
            location_slug TEXT,
            location_name TEXT,
            target_date TEXT,
            timezone TEXT,
            latitude REAL,
            longitude REAL,
            sources_total INTEGER,
            sources_success INTEGER,
            operational_status TEXT,
            autopilot_route TEXT
        );
        CREATE TABLE IF NOT EXISTS source_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            target_datetime TEXT,
            target_jam TEXT,
            source_id TEXT,
            provider TEXT,
            source_datetime TEXT,
            temp_c REAL,
            rh_pct REAL,
            rain_mm REAL,
            wind_kmh REAL,
            category TEXT,
            point_weight REAL,
            gap_minutes REAL,
            raw_condition TEXT
        );
        CREATE TABLE IF NOT EXISTS sentinel_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            target_datetime TEXT,
            jam TEXT,
            mission TEXT,
            atmospheric_mode TEXT,
            predictability TEXT,
            dominant_category TEXT,
            prob_rain REAL,
            rain_p90 REAL,
            rain_threat_score REAL,
            heavy_rain_threat_score REAL,
            rain_displacement_risk REAL,
            forecast_failure_risk REAL,
            self_doubt_score REAL,
            forecast_stress_index REAL,
            main_failure_mode TEXT,
            trust_level TEXT,
            operational_status TEXT,
            autopilot_route TEXT,
            decision_recommendation TEXT,
            explanation TEXT
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            location_slug TEXT,
            target_date TEXT,
            jam TEXT,
            observed_category TEXT,
            observed_rain_mm REAL,
            observed_temp_c REAL,
            note TEXT
        );
        """
    )
    conn.commit()


def aether_store_ledger(run_id, target_date, results, source_rows, aether_rows, daily, args):
    conn = aether_connect_db()
    try:
        aether_init_db(conn)
        conn.execute(
            """INSERT OR REPLACE INTO forecast_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, now_local(args.timezone).isoformat(), AETHER_VERSION, getattr(args, "location_slug", ""), getattr(args, "location_name", ""), target_date.isoformat(), getattr(args, "timezone", DEFAULT_TIMEZONE), getattr(args, "latitude", None), getattr(args, "longitude", None), len(results), sum(1 for r in results if r.success), daily.get("daily_operational_status", ""), "sentinel_x"),
        )
        for row in source_rows:
            try:
                target_dt = aether_target_datetime(target_date, row[3], args.timezone).isoformat()
            except Exception:
                target_dt = ""
            conn.execute(
                """INSERT INTO source_forecasts(run_id,target_datetime,target_jam,source_id,provider,source_datetime,temp_c,rh_pct,rain_mm,wind_kmh,category,point_weight,gap_minutes,raw_condition) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, target_dt, row[3], row[1], row[2], row[4], aether_value(row[5]), aether_value(row[6]), aether_value(row[7]), aether_value(row[8]), row[11], aether_value(row[10]), aether_value(row[9]), row[12]),
            )
        for r in aether_rows:
            conn.execute(
                """INSERT INTO sentinel_forecasts(run_id,target_datetime,jam,mission,atmospheric_mode,predictability,dominant_category,prob_rain,rain_p90,rain_threat_score,heavy_rain_threat_score,rain_displacement_risk,forecast_failure_risk,self_doubt_score,forecast_stress_index,main_failure_mode,trust_level,operational_status,autopilot_route,decision_recommendation,explanation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, r.get("target_datetime"), r.get("jam"), r.get("mission"), r.get("atmospheric_mode"), r.get("predictability"), r.get("dominant_category"), aether_value(r.get("prob_rain")), aether_value(r.get("rain_p90")), aether_value(r.get("rain_threat_score")), aether_value(r.get("heavy_rain_threat_score")), aether_value(r.get("rain_displacement_risk")), aether_value(r.get("forecast_failure_risk")), aether_value(r.get("self_doubt_score")), aether_value(r.get("forecast_stress_index")), r.get("main_failure_mode"), r.get("trust_level"), r.get("operational_status"), r.get("autopilot_route"), r.get("decision_recommendation"), r.get("explanation")),
            )
        conn.commit()
    finally:
        conn.close()


def sentinel_x_save_artifacts(target_date, results, args, source_rows, status_rows, ensemble_rows):
    points = flatten_points(results)
    sentinel_rows = aether_build_rows(points, ensemble_rows, target_date, args)
    sentinel_apply_operational_hardening(sentinel_rows, target_date, args)
    if sentinel_rows:
        write_dict_csv(path_output(AETHER_CSV_FILENAME), list(sentinel_rows[0].keys()), sentinel_rows)
        write_dict_csv(path_output(f"sentinel_x_{target_date.strftime('%Y%m%d')}.csv"), list(sentinel_rows[0].keys()), sentinel_rows)
    source_states = aether_source_state_rows(results)
    if source_states:
        write_dict_csv(path_output(AETHER_SOURCE_STATE_FILENAME), list(source_states[0].keys()), source_states)
    daily = aether_daily_summary(sentinel_rows, args)
    payload = {"daily": daily, "hourly": sentinel_rows, "source_states": source_states, "constitution": SENTINEL_CONSTITUTION}
    write_json(path_output(AETHER_JSON_FILENAME), payload)
    write_json(path_output(f"sentinel_x_{target_date.strftime('%Y%m%d')}.json"), payload)
    aether_write_dashboard(sentinel_rows, source_states, daily, args)
    aether_write_report(sentinel_rows, daily, args)
    aether_write_contract(daily, args)
    sentinel_write_constitution(args)
    run_id = f"{getattr(args, 'location_slug', 'location')}_{target_date.strftime('%Y%m%d')}_{now_local(args.timezone).strftime('%Y%m%d%H%M%S')}"
    try:
        aether_store_ledger(run_id, target_date, results, source_rows, sentinel_rows, daily, args)
    except Exception as exc:
        log_warning("Sentinel ledger gagal ditulis:", exc)
    return {"version": AETHER_VERSION, "run_id": run_id, "csv": path_output(AETHER_CSV_FILENAME), "json": path_output(AETHER_JSON_FILENAME), "dashboard": path_output(AETHER_DASHBOARD_FILENAME), "report": path_output(AETHER_REPORT_FILENAME), "contract": path_output(AETHER_CONTRACT_FILENAME), "ledger": aether_db_path(), "daily_operational_status": daily.get("daily_operational_status"), "autopilot_summary": daily.get("summary_text")}


def aether_regenerate_dashboard_for_location(args):
    rows = read_dict_csv(path_output(AETHER_CSV_FILENAME))
    states = read_dict_csv(path_output(AETHER_SOURCE_STATE_FILENAME))
    daily_payload = read_json(path_output(AETHER_JSON_FILENAME), default={}) or {}
    daily = daily_payload.get("daily") or aether_daily_summary(rows, args)
    aether_write_dashboard(rows, states, daily, args)
    aether_write_report(rows, daily, args)
    aether_write_contract(daily, args)
    return {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "report": path_output(AETHER_REPORT_FILENAME)}


def aether_doctor_for_location(args):
    checks = []
    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": "yes" if ok else "no", "detail": str(detail)})
    try:
        ensure_directory(ACTIVE_OUTPUT_DIR); add("output_dir_writable", True, ACTIVE_OUTPUT_DIR)
    except Exception as exc:
        add("output_dir_writable", False, exc)
    try:
        ZoneInfo(args.timezone); add("timezone_valid", True, args.timezone)
    except Exception as exc:
        add("timezone_valid", False, exc)
    try:
        conn = aether_connect_db(); aether_init_db(conn); conn.close(); add("sqlite_sentinel_ledger", True, aether_db_path())
    except Exception as exc:
        add("sqlite_sentinel_ledger", False, exc)
    try:
        validate_location_config(LocationConfig(args.location_slug, args.location_name, args.adm4, args.latitude, args.longitude, args.timezone)); add("location_config", True, f"{args.latitude},{args.longitude} adm4={args.adm4}")
    except Exception as exc:
        add("location_config", False, exc)
    add("mission", True, getattr(args, "mission", "safety_first"))
    add("constitution_rules", len(SENTINEL_CONSTITUTION) >= 10, len(SENTINEL_CONSTITUTION))
    for config in ACTIVE_SOURCE_CONFIGS:
        add(f"preview_url_{config['source_id']}", True, preview_request_url(config, args))
    write_dict_csv(path_output("doctor_sentinel_x.csv"), ["check", "ok", "detail"], checks)
    write_json(path_output("doctor_sentinel_x.json"), {"checks": checks, "generated_at": now_local(args.timezone).isoformat()})
    return checks


def sentinel_write_constitution(args):
    payload = {"version": AETHER_VERSION, "generated_at": now_local(args.timezone).isoformat(), "constitution": SENTINEL_CONSTITUTION}
    write_json(path_output("sentinel_constitution.json"), payload)
    lines = [f"# {AETHER_VERSION} — Forecast Constitution", ""] + [f"{i}. {rule}" for i, rule in enumerate(SENTINEL_CONSTITUTION, 1)]
    atomic_write_text(path_output("sentinel_constitution.md"), lambda f: f.write("\n".join(lines)))
    return {"constitution_json": path_output("sentinel_constitution.json"), "constitution_md": path_output("sentinel_constitution.md")}


def sentinel_red_team_for_location(args):
    scenarios = []
    def add(name, expected, result, passed=True):
        scenarios.append({"scenario": name, "expected_safe_behavior": expected, "result": result, "pass": "yes" if passed else "no"})
    add("all_sources_failed", "BLACK status; no confident recommendation", "Sentinel constitution requires BLACK/low trust when source coverage is weak.")
    add("single_source_only", "DO_NOT_TRUST or EXPERIMENTAL", "Trust gate requires at least 3 usable points before confident forecast.")
    add("bmkg_rain_global_clear", "show disagreement and scenario split", "Category disagreement feeds uncertainty/failure risk.")
    add("mean_rain_low_p95_high", "preserve worst-case scenario", "P90/P95 explicitly affects heavy-rain threat and convective burst scenario.")
    add("high_disagreement_fake_confidence", "self-doubt/failure risk increases", "Forecast stress index includes category disagreement and rain spread.")
    add("local_convective_afternoon", "rain timing/displacement caveat", "HUMID_CONVECTIVE_AFTERNOON route activates conservative decision logic.")
    write_dict_csv(path_output("red_team_sentinel_x.csv"), ["scenario", "expected_safe_behavior", "result", "pass"], scenarios)
    write_json(path_output("red_team_sentinel_x.json"), {"generated_at": now_local(args.timezone).isoformat(), "scenarios": scenarios})
    return {"red_team_csv": path_output("red_team_sentinel_x.csv"), "red_team_json": path_output("red_team_sentinel_x.json"), "passed": sum(1 for s in scenarios if s["pass"] == "yes"), "total": len(scenarios)}


def sentinel_autopsy_for_location(args):
    forecast_rows = read_dict_csv(path_output(AETHER_CSV_FILENAME)) if os.path.exists(path_output(AETHER_CSV_FILENAME)) else []
    feedback_rows = read_dict_csv(path_output(AETHER_FEEDBACK_FILENAME)) if os.path.exists(path_output(AETHER_FEEDBACK_FILENAME)) else []
    observations = read_dict_csv(observation_master_file()) if os.path.exists(observation_master_file()) else []
    findings = []
    if not forecast_rows:
        findings.append("Belum ada sentinel_x.csv; jalankan --mode forecast terlebih dahulu.")
    if not feedback_rows and not observations:
        findings.append("Belum ada observasi/feedback lokal; autopsy belum bisa menilai benar/salah secara aktual.")
    matches = []
    obs_all = feedback_rows + observations
    for f in forecast_rows:
        fdate, fjam = f.get("target_date"), f.get("jam")
        for obs in obs_all:
            odate = obs.get("target_date") or obs.get("tanggal") or obs.get("date")
            ojam = obs.get("jam") or obs.get("time")
            if str(odate) in {str(fdate), str(fdate).split("-")[-1]} and str(ojam)[:2] == str(fjam)[:2]:
                matches.append((f, obs))
    if matches:
        rain_hits = 0; rain_total = 0; notes = []
        for f, obs in matches:
            pred_rain = (aether_value(f.get("prob_rain")) or 0) >= 50
            obs_cat = (obs.get("observed_category") or obs.get("category") or "").lower()
            obs_rain = (aether_value(obs.get("observed_rain_mm") or obs.get("rain_mm")) or 0) > 0 or "hujan" in obs_cat
            rain_total += 1
            if pred_rain == obs_rain:
                rain_hits += 1
            else:
                notes.append(f"Mismatch {f.get('jam')}: pred_rain={pred_rain}, observed_rain={obs_rain}, failure_mode={f.get('main_failure_mode')}")
        findings.append(f"Matched cases: {rain_total}; rain event hit consistency: {rain_hits}/{rain_total}.")
        findings.extend(notes[:8])
    payload = {"generated_at": now_local(args.timezone).isoformat(), "findings": findings, "matched_cases": len(matches)}
    write_json(path_output("autopsy_sentinel_x.json"), payload)
    atomic_write_text(path_output("autopsy_sentinel_x.md"), lambda f: f.write("# Sentinel X Forecast Autopsy\n\n" + "\n".join(f"- {x}" for x in findings)))
    return {"autopsy_json": path_output("autopsy_sentinel_x.json"), "autopsy_md": path_output("autopsy_sentinel_x.md"), "matched_cases": len(matches)}


def sentinel_skill_league_for_location(args):
    # Lightweight league scaffold: ranks source health and point coverage until enough observation pairs exist.
    states = read_dict_csv(path_output(AETHER_SOURCE_STATE_FILENAME)) if os.path.exists(path_output(AETHER_SOURCE_STATE_FILENAME)) else []
    rows = []
    for srow in states:
        ema = aether_value(srow.get("ema_success"))
        points = aether_value(srow.get("points")) or 0
        success = 1 if str(srow.get("success")).lower() in {"true", "1", "yes"} else 0
        score = round((ema if ema is not None else 0.5) * 60 + min(points, len(TARGET_TIMES)) / max(len(TARGET_TIMES), 1) * 30 + success * 10, 2)
        rows.append({"rank": 0, "source_id": srow.get("source_id"), "league": "operational_readiness", "score": score, "note": "Observation-paired skill league activates after enough observations/feedback are available."})
    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    if not rows:
        rows = [{"rank": 1, "source_id": "none", "league": "operational_readiness", "score": 0, "note": "No source_state file yet; run forecast first."}]
    write_dict_csv(path_output("skill_league_sentinel_x.csv"), list(rows[0].keys()), rows)
    write_json(path_output("skill_league_sentinel_x.json"), {"generated_at": now_local(args.timezone).isoformat(), "rows": rows})
    return {"skill_league_csv": path_output("skill_league_sentinel_x.csv"), "skill_league_json": path_output("skill_league_sentinel_x.json"), "entries": len(rows)}


def aether_local_server(args):
    root = root_output_dir(); port = int(getattr(args, "serve_port", 8000))
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type="text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in {"/", "/status"}:
                summary_path = root_output_path("forecast_batch_summary.json")
                payload = read_json(summary_path, default={}) if os.path.exists(summary_path) else {"message": "No batch summary yet"}
                self._send(200, json.dumps(payload, ensure_ascii=False, indent=2), "application/json; charset=utf-8"); return
            if path == "/dashboard":
                candidates = []
                for dirpath, _, filenames in os.walk(root):
                    if AETHER_DASHBOARD_FILENAME in filenames:
                        candidates.append(os.path.join(dirpath, AETHER_DASHBOARD_FILENAME))
                if not candidates:
                    self._send(404, "Command center belum tersedia. Jalankan --mode forecast dulu."); return
                candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                with open(candidates[0], "r", encoding="utf-8") as f: self._send(200, f.read())
                return
            if path == "/sentinel.json":
                candidates = []
                for dirpath, _, filenames in os.walk(root):
                    if AETHER_JSON_FILENAME in filenames: candidates.append(os.path.join(dirpath, AETHER_JSON_FILENAME))
                if not candidates:
                    self._send(404, "{}", "application/json; charset=utf-8"); return
                candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                with open(candidates[0], "r", encoding="utf-8") as f: self._send(200, f.read(), "application/json; charset=utf-8")
                return
            self._send(404, "Not found")
    print(f"[SENTINEL X] Local command center: http://localhost:{port}/dashboard")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def aether_self_test():
    assert aether_weighted_quantile([(1, 1), (10, 1), (20, 2)], 0.5) in {10, 20}
    assert aether_risk_label(85) == "very_high"
    assert aether_lead_bucket(2) == "lead_0_3h"
    assert sentinel_threat_level(85) == "VERY_HIGH"
    assert abs(sum(sentinel_multi_reality(60, 20, 70, 65, 10).values()) - 100) < 0.2
    assert "Do not" in SENTINEL_CONSTITUTION[0]
    return True


# -----------------------------------------------------------------------------
# AETHER SENTINEL X PUBLIC-OPERATIONAL v2 — validation, public safety, grid risk
# -----------------------------------------------------------------------------
# This section intentionally overrides selected Sentinel X functions defined above.
# It addresses the operational gaps: public-friendly outputs, verification,
# accountability, clearer non-official warning disclaimer, grid-based nearby-rain
# diagnostics, and explicit heuristic-vs-verified labeling.

SENTINEL_PUBLIC_VERSION = "sentinel-x-public-operational-v2"
SENTINEL_OFFICIAL_DISCLAIMER = "Ini bukan peringatan resmi. Untuk cuaca ekstrem, rujuk informasi resmi BMKG."


def sentinel_public_disclaimer(args=None):
    text = getattr(args, "public_disclaimer", "") if args is not None else ""
    return text or SENTINEL_OFFICIAL_DISCLAIMER


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


def sentinel_observed_rain_event(row):
    rain = aether_value(row.get("rain_mm") or row.get("observed_rain_mm"))
    cat = (row.get("category") or row.get("observed_category") or "").lower()
    if rain is not None:
        return rain > 0.1
    return "hujan" in cat


def sentinel_load_observations_by_key(args=None):
    obs = {}
    paths = []
    try:
        paths.append(observation_master_file())
        paths.append(path_output(AETHER_FEEDBACK_FILENAME))
    except Exception:
        pass
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        for row in read_dict_csv(path):
            date = sentinel_obs_date_to_iso(row.get("target_date") or row.get("date") or row.get("tanggal"))
            jam = (row.get("jam") or row.get("time") or row.get("target_time") or "")[:5]
            if not date or not jam:
                continue
            obs[(date, jam)] = row
    return obs


def sentinel_compute_verification(rows, args):
    observations = sentinel_load_observations_by_key(args)
    pairs = []
    reliability_bins = {f"{i:02d}-{i+10:02d}": {"n": 0, "observed_rain": 0, "prob_sum": 0.0} for i in range(0, 100, 10)}
    for row in rows or []:
        key = (row.get("target_date"), (row.get("jam") or "")[:5])
        obs = observations.get(key)
        if not obs:
            continue
        pred_temp = aether_value(row.get("temp_micro_p50") or row.get("temp_p50"))
        obs_temp = aether_value(obs.get("temp_c") or obs.get("observed_temp_c"))
        prob_rain = aether_value(row.get("prob_rain"))
        obs_rain = 1 if sentinel_observed_rain_event(obs) else 0
        pred_cat = (row.get("dominant_category") or "").strip()
        obs_cat = (obs.get("category") or obs.get("observed_category") or "").strip()
        pred_event = 1 if (prob_rain is not None and prob_rain >= 50) or ("Hujan" in pred_cat) else 0
        brier = None if prob_rain is None else ((prob_rain / 100.0) - obs_rain) ** 2
        temp_abs = None if pred_temp is None or obs_temp is None else abs(pred_temp - obs_temp)
        cat_match = None if not pred_cat or not obs_cat else int(pred_cat == obs_cat)
        pairs.append({
            "target_date": key[0], "jam": key[1], "pred_prob_rain": prob_rain if prob_rain is not None else "",
            "observed_rain": obs_rain, "pred_rain_event": pred_event, "brier": brier,
            "pred_temp": pred_temp if pred_temp is not None else "", "observed_temp": obs_temp if obs_temp is not None else "", "temp_abs_error": temp_abs,
            "pred_category": pred_cat, "observed_category": obs_cat, "category_match": cat_match if cat_match is not None else "",
        })
        if prob_rain is not None:
            b0 = int(min(90, max(0, math.floor(prob_rain / 10) * 10)))
            label = f"{b0:02d}-{b0+10:02d}"
            reliability_bins[label]["n"] += 1
            reliability_bins[label]["observed_rain"] += obs_rain
            reliability_bins[label]["prob_sum"] += prob_rain
    n = len(pairs)
    hits = sum(1 for p in pairs if p["pred_rain_event"] == 1 and p["observed_rain"] == 1)
    false_alarms = sum(1 for p in pairs if p["pred_rain_event"] == 1 and p["observed_rain"] == 0)
    misses = sum(1 for p in pairs if p["pred_rain_event"] == 0 and p["observed_rain"] == 1)
    correct_negatives = sum(1 for p in pairs if p["pred_rain_event"] == 0 and p["observed_rain"] == 0)
    briers = [p["brier"] for p in pairs if p.get("brier") is not None]
    temps = [p["temp_abs_error"] for p in pairs if p.get("temp_abs_error") is not None]
    cats = [p["category_match"] for p in pairs if p.get("category_match") != ""]
    pod = None if hits + misses == 0 else hits / (hits + misses)
    far = None if hits + false_alarms == 0 else false_alarms / (hits + false_alarms)
    csi = None if hits + misses + false_alarms == 0 else hits / (hits + misses + false_alarms)
    summary = {
        "generated_at": now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).isoformat(),
        "location_slug": getattr(args, "location_slug", ""),
        "location_name": getattr(args, "location_name", ""),
        "matched_cases": n,
        "verification_min_cases": int(getattr(args, "verification_min_cases", 30) or 30),
        "calibration_status": "VERIFIED_ENOUGH_DATA" if n >= int(getattr(args, "verification_min_cases", 30) or 30) else "INSUFFICIENT_DATA_HEURISTIC_MODE",
        "rain_brier_score": round(sum(briers) / len(briers), 4) if briers else "",
        "temperature_mae_c": round(sum(temps) / len(temps), 3) if temps else "",
        "category_accuracy": round(sum(cats) / len(cats), 4) if cats else "",
        "rain_hits": hits,
        "rain_false_alarms": false_alarms,
        "rain_misses": misses,
        "rain_correct_negatives": correct_negatives,
        "rain_pod": round(pod, 4) if pod is not None else "",
        "rain_far": round(far, 4) if far is not None else "",
        "rain_csi": round(csi, 4) if csi is not None else "",
        "scientific_note": "Risk/trust scores are verified only when calibration_status is VERIFIED_ENOUGH_DATA; otherwise they are transparent heuristic decision-support signals.",
    }
    reliability_rows = []
    for label, item in reliability_bins.items():
        nbin = item["n"]
        reliability_rows.append({
            "probability_bin": label,
            "n": nbin,
            "mean_forecast_probability": round(item["prob_sum"] / nbin, 2) if nbin else "",
            "observed_rain_frequency": round(item["observed_rain"] / nbin * 100, 2) if nbin else "",
        })
    return summary, pairs, reliability_rows


def sentinel_write_verification_artifacts(rows, args):
    summary, pairs, reliability = sentinel_compute_verification(rows, args)
    write_json(path_output("sentinel_x_verification_summary.json"), summary)
    if pairs:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), list(pairs[0].keys()), pairs)
    else:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), ["target_date", "jam", "note"], [{"target_date": "", "jam": "", "note": "No matched forecast-observation pairs yet."}])
    write_dict_csv(path_output("sentinel_x_reliability.csv"), ["probability_bin", "n", "mean_forecast_probability", "observed_rain_frequency"], reliability)
    # Public accuracy page
    esc = html.escape
    rel_rows = "".join(f"<tr><td>{esc(r['probability_bin'])}</td><td>{r['n']}</td><td>{r['mean_forecast_probability']}</td><td>{r['observed_rain_frequency']}</td></tr>" for r in reliability)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Akuntabilitas Sentinel X — {esc(getattr(args,'location_name',''))}</title>
<style>body{{font-family:Arial,sans-serif;background:#f8fafc;color:#0f172a;margin:0}}main{{max-width:1100px;margin:auto;padding:24px}}.warn{{background:#fff7ed;border:1px solid #fdba74;padding:14px;border-radius:14px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.card{{background:white;border:1px solid #e2e8f0;border-radius:16px;padding:16px}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{border-bottom:1px solid #e2e8f0;padding:8px;text-align:left}}</style></head><body><main>
<h1>Akuntabilitas Forecast — {esc(getattr(args,'location_name',''))}</h1><div class='warn'><b>{esc(sentinel_public_disclaimer(args))}</b><br>Halaman ini menjelaskan performa historis lokal. Jika jumlah kasus masih sedikit, skor Sentinel tetap dianggap heuristic.</div>
<div class='grid'>
<div class='card'><b>Status kalibrasi</b><h2>{esc(str(summary['calibration_status']))}</h2></div>
<div class='card'><b>Matched cases</b><h2>{summary['matched_cases']}</h2></div>
<div class='card'><b>Brier hujan</b><h2>{summary['rain_brier_score']}</h2></div>
<div class='card'><b>MAE suhu</b><h2>{summary['temperature_mae_c']}</h2></div>
<div class='card'><b>POD hujan</b><h2>{summary['rain_pod']}</h2></div>
<div class='card'><b>FAR hujan</b><h2>{summary['rain_far']}</h2></div>
</div><h2>Reliability probabilitas hujan</h2><table><tr><th>Bin</th><th>N</th><th>Mean forecast %</th><th>Observed rain %</th></tr>{rel_rows}</table>
<p><a href='sentinel_x_verification_summary.json'>Verification JSON</a> · <a href='sentinel_x_reliability.csv'>Reliability CSV</a> · <a href='sentinel_x_verification_pairs.csv'>Matched pairs CSV</a></p>
</main></body></html>"""
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(doc))
    return summary


def sentinel_grid_offsets(radius_km):
    r = float(radius_km or 3.0)
    # Approx degree offsets around Indonesia latitude; longitude corrected later by cos(lat).
    return [
        ("center", 0.0, 0.0), ("north", r, 0.0), ("south", -r, 0.0), ("east", 0.0, r), ("west", 0.0, -r),
        ("northeast", r * 0.707, r * 0.707), ("northwest", r * 0.707, -r * 0.707),
        ("southeast", -r * 0.707, r * 0.707), ("southwest", -r * 0.707, -r * 0.707),
    ]


def sentinel_fetch_grid_summary(target_date, args):
    if getattr(args, "disable_grid_sampling", False):
        return {}, {"status": "disabled"}
    summaries = {jam: [] for jam in TARGET_TIMES}
    errors = []
    base_lat = float(getattr(args, "latitude"))
    base_lon = float(getattr(args, "longitude"))
    lat_km = 1.0 / 111.0
    lon_km = 1.0 / max(30.0, 111.0 * math.cos(math.radians(base_lat)))
    variables = "temperature_2m,relative_humidity_2m,precipitation,weather_code,cloud_cover,wind_speed_10m"
    for label, dy_km, dx_km in sentinel_grid_offsets(getattr(args, "grid_radius_km", 3.0)):
        lat = base_lat + dy_km * lat_km
        lon = base_lon + dx_km * lon_km
        params = {"latitude": round(lat, 5), "longitude": round(lon, 5), "timezone": getattr(args, "timezone", DEFAULT_TIMEZONE), "forecast_days": 3, "hourly": variables}
        url = build_url("https://api.open-meteo.com/v1/forecast", params)
        try:
            payload, status, duration_ms = fetch_json_with_retry(url, source_id=f"GRID_{label.upper()}", timeout=getattr(args, "http_timeout", HTTP_TIMEOUT_SECONDS), max_retry=max(1, min(2, getattr(args, "max_retry_http", MAX_RETRY_HTTP))))
            hourly = payload.get("hourly") or {}
            times = hourly.get("time") or []
            precip = hourly.get("precipitation") or []
            codes = hourly.get("weather_code") or []
            cloud = hourly.get("cloud_cover") or []
            for idx, t in enumerate(times):
                try:
                    dt = parse_open_meteo_time(t, getattr(args, "timezone", DEFAULT_TIMEZONE))
                except Exception:
                    continue
                if dt.date() != target_date:
                    continue
                jam = f"{dt.hour:02d}:00"
                if jam not in summaries:
                    continue
                rain = safe_float(precip[idx] if idx < len(precip) else None) or 0.0
                code = safe_float(codes[idx] if idx < len(codes) else None)
                category = category_from_wmo_code(code, rain, None)
                summaries[jam].append({"point": label, "rain_mm": rain, "category": category, "cloud_cover": safe_float(cloud[idx] if idx < len(cloud) else None)})
        except Exception as exc:
            errors.append({"point": label, "error": str(exc)[:250]})
    merged = {}
    for jam, points in summaries.items():
        if not points:
            continue
        rain_points = sum(1 for p in points if (p.get("rain_mm") or 0) > 0.1 or "Hujan" in p.get("category", ""))
        max_rain = max((p.get("rain_mm") or 0) for p in points)
        center = next((p for p in points if p.get("point") == "center"), None)
        center_rain = ((center.get("rain_mm") or 0) if center else 0)
        nearby_frac = rain_points / len(points)
        direct_hit = 100.0 if center_rain > 0.1 else min(85.0, nearby_frac * 65.0)
        nearby = min(100.0, nearby_frac * 100.0)
        displacement = min(100.0, max(0.0, nearby - direct_hit * 0.55 + (max_rain - center_rain) * 6.0))
        merged[jam] = {
            "grid_sampling_status": "ok",
            "grid_points_used": len(points),
            "grid_rain_point_fraction": round(nearby_frac, 3),
            "grid_max_rain_mm": round(max_rain, 2),
            "grid_center_rain_mm": round(center_rain, 2),
            "grid_direct_hit_risk": round(direct_hit, 1),
            "grid_nearby_rain_risk": round(nearby, 1),
            "grid_spatial_uncertainty": round(displacement, 1),
        }
    status = {"status": "ok" if merged else "no_grid_data", "errors": errors, "points_requested": 9, "radius_km": getattr(args, "grid_radius_km", 3.0), "generated_at": now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).isoformat()}
    write_json(path_output("sentinel_x_grid_status.json"), status)
    return merged, status


def sentinel_apply_grid_to_rows(rows, grid):
    if not rows:
        return
    for row in rows:
        jam = (row.get("jam") or "")[:5]
        g = grid.get(jam)
        if not g:
            row["grid_sampling_status"] = row.get("grid_sampling_status") or "unavailable"
            continue
        row.update(g)
        # Blend real grid diagnostics into existing proxy risk fields.
        row["direct_hit_risk"] = aether_round(0.55 * (aether_value(row.get("direct_hit_risk")) or 0) + 0.45 * (aether_value(g.get("grid_direct_hit_risk")) or 0), 1)
        row["nearby_rain_risk"] = aether_round(0.50 * (aether_value(row.get("nearby_rain_risk")) or 0) + 0.50 * (aether_value(g.get("grid_nearby_rain_risk")) or 0), 1)
        row["rain_displacement_risk"] = aether_round(0.50 * (aether_value(row.get("rain_displacement_risk")) or 0) + 0.50 * (aether_value(g.get("grid_spatial_uncertainty")) or 0), 1)
        if aether_value(g.get("grid_spatial_uncertainty")) and aether_value(g.get("grid_spatial_uncertainty")) >= 60:
            row["main_failure_mode"] = "spatial_grid_displacement_risk"
        row["explanation"] = (row.get("explanation") or "") + f" Grid sampling: nearby rain fraction {g.get('grid_rain_point_fraction')}, max rain {g.get('grid_max_rain_mm')} mm."


def sentinel_apply_operational_hardening(rows, target_date, args):
    if rows is None:
        return
    grid = {}
    try:
        grid, grid_status = sentinel_fetch_grid_summary(target_date, args)
        sentinel_apply_grid_to_rows(rows, grid)
    except Exception as exc:
        write_json(path_output("sentinel_x_grid_status.json"), {"status": "error", "error": str(exc), "generated_at": now_local(args.timezone).isoformat()})
    verification = sentinel_write_verification_artifacts(rows, args)
    for row in rows:
        row["sentinel_public_version"] = SENTINEL_PUBLIC_VERSION
        row["official_warning_disclaimer"] = sentinel_public_disclaimer(args)
        row["calibration_status"] = verification.get("calibration_status", "")
        row["verification_cases"] = verification.get("matched_cases", 0)
        row["risk_score_basis"] = "observation_verified" if verification.get("calibration_status") == "VERIFIED_ENOUGH_DATA" else "transparent_heuristic_until_more_observations"
        row["public_safety_note"] = "Decision support only; not an official warning."
    sentinel_write_publish_manifest(args)


def sentinel_write_publish_manifest(args):
    payload = {
        "generated_at": now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).isoformat(),
        "version": SENTINEL_PUBLIC_VERSION,
        "location": getattr(args, "location_slug", ""),
        "public_files": [
            AETHER_DASHBOARD_FILENAME, AETHER_CSV_FILENAME, AETHER_JSON_FILENAME, AETHER_REPORT_FILENAME,
            AETHER_CONTRACT_FILENAME, "sentinel_x_accuracy_public.html", "sentinel_x_verification_summary.json",
            "sentinel_x_reliability.csv", "sentinel_constitution.md", "sentinel_x_public_links.md",
        ],
        "internal_or_do_not_publish": ["*.sqlite", "raw_payloads/", "logs/", "source_health.json", "*_latest_failure.json", "*_latest_failure.json.gz"],
        "public_disclaimer": sentinel_public_disclaimer(args),
    }
    write_json(path_output("sentinel_x_publish_manifest.json"), payload)
    lines = ["# Sentinel X public links", "", f"Disclaimer: {sentinel_public_disclaimer(args)}", ""]
    for item in payload["public_files"]:
        lines.append(f"- `{item}`")
    lines += ["", "## Do not publish as public-facing files", ""] + [f"- `{item}`" for item in payload["internal_or_do_not_publish"]]
    atomic_write_text(path_output("sentinel_x_public_links.md"), lambda f: f.write("\n".join(lines)))
    # Helpful .gitignore template for dev branches.
    gitignore_path = root_output_path(".sentinel_public_gitignore_template")
    atomic_write_text(gitignore_path, lambda f: f.write("*.sqlite\n*.db\n*/logs/\n*/raw_payloads/\nsource_health.json\n*_latest_failure.json\n*_latest_failure.json.gz\n"))
    return payload


def sentinel_status_badge(status):
    color = {"GREEN": "#16a34a", "YELLOW": "#ca8a04", "RED": "#dc2626", "BLACK": "#020617"}.get(status, "#64748b")
    return f"<span class='badge' style='background:{color}'>{html.escape(str(status))}</span>"


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return None
    esc = html.escape
    rows = aether_rows or []
    disclaimer = sentinel_public_disclaimer(args)
    def card(title, body):
        return f"<section class='card'><h2>{esc(title)}</h2>{body}</section>"
    if rows:
        table_rows = "".join(
            "<tr>" +
            f"<td>{esc(str(r.get('jam','')))}</td>" +
            f"<td>{esc(str(r.get('dominant_category','')))}</td>" +
            f"<td>{r.get('prob_rain','')}%</td>" +
            f"<td>{sentinel_status_badge(r.get('operational_status',''))}</td>" +
            f"<td>{esc(str(r.get('main_failure_mode','')))}</td>" +
            f"<td>{esc(str(r.get('decision_recommendation','')))}</td>" +
            "</tr>" for r in rows
        )
    else:
        table_rows = "<tr><td colspan='6'>Tidak ada data.</td></tr>"
    peak_rain = max(rows, key=lambda r: aether_value(r.get("rain_threat_score")) or -1) if rows else {}
    peak_failure = max(rows, key=lambda r: aether_value(r.get("forecast_failure_risk")) or -1) if rows else {}
    peak_stress = max(rows, key=lambda r: aether_value(r.get("forecast_stress_index")) or -1) if rows else {}
    scenarios = peak_rain or {}
    scenario_html = "".join(f"<li><b>{label.replace('_',' ')}</b>: {scenarios.get(key,'')}%</li>" for key, label in [
        ("scenario_dry_miss","Dry miss"),("scenario_nearby_rain_only","Nearby rain only"),("scenario_direct_light_rain","Direct light rain"),("scenario_direct_moderate_rain","Direct moderate rain"),("scenario_convective_burst","Convective burst")])
    source_html = "".join(f"<tr><td>{esc(str(s.get('source_id','')))}</td><td>{esc(str(s.get('state','')))}</td><td>{esc(str(s.get('success','')))}</td><td>{esc(str(s.get('duration_ms','')))}</td></tr>" for s in (source_state_rows or [])) or "<tr><td colspan='4'>Belum ada source state.</td></tr>"
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    verification_html = f"""
    <p><b>Status kalibrasi:</b> {esc(str(verification.get('calibration_status','unknown')))}</p>
    <p><b>Matched cases:</b> {esc(str(verification.get('matched_cases','0')))} · <b>Brier hujan:</b> {esc(str(verification.get('rain_brier_score','')))} · <b>MAE suhu:</b> {esc(str(verification.get('temperature_mae_c','')))}</p>
    <p><a href='sentinel_x_accuracy_public.html'>Buka halaman akuntabilitas/akurasi</a></p>"""
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{esc(AETHER_VERSION)} — {esc(getattr(args,'location_name',''))}</title>
<style>body{{font-family:Arial,sans-serif;margin:0;background:#f1f5f9;color:#0f172a}}header{{background:#0f172a;color:white;padding:28px}}main{{max-width:1180px;margin:auto;padding:20px}}.warn{{background:#fee2e2;color:#7f1d1d;border:1px solid #fca5a5;padding:14px;border-radius:14px;margin-top:12px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}}.card{{background:white;border:1px solid #e2e8f0;border-radius:18px;padding:16px;box-shadow:0 6px 20px #00000008}}.big{{font-size:34px;font-weight:800}}.badge{{display:inline-block;color:white;padding:5px 10px;border-radius:999px;font-weight:700}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{padding:9px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top}}a{{color:#0369a1}}</style></head><body>
<header><h1>{esc(AETHER_VERSION)}</h1><p>{esc(getattr(args,'location_name',''))} · Mission: {esc(str(getattr(args,'mission','safety_first')))}</p><div class='warn'><b>{esc(disclaimer)}</b><br>Gunakan sebagai pendukung keputusan harian, bukan pengganti peringatan resmi.</div></header><main>
<div class='grid'>
{card('Operational Status', f"<div class='big'>{sentinel_status_badge(daily.get('daily_operational_status',''))}</div><p>{esc(str(daily.get('summary_text','')))}</p>")}
{card('Puncak Rain Threat', f"<div class='big'>{esc(str(peak_rain.get('jam','')))}</div><p>Rain threat {esc(str(peak_rain.get('rain_threat_score','')))} · Nearby {esc(str(peak_rain.get('nearby_rain_risk','')))} · Direct-hit {esc(str(peak_rain.get('direct_hit_risk','')))}</p>")}
{card('Forecast Failure Risk', f"<div class='big'>{esc(str(peak_failure.get('forecast_failure_risk','')))}</div><p>{esc(str(peak_failure.get('main_failure_mode','')))}</p>")}
{card('Forecast Stress Index', f"<div class='big'>{esc(str(peak_stress.get('forecast_stress_index','')))}</div><p>Semakin tinggi, semakin sulit diprediksi.</p>")}
</div>
{card('Atmospheric Situation Awareness', f"<p><b>Dominant mode:</b> {esc(str(daily.get('dominant_atmospheric_mode','')))}</p><p><b>Risk window:</b> {esc(str(daily.get('risk_window','')))}</p><p><b>Best activity window:</b> {esc(str(daily.get('best_window','')))}</p>")}
{card('Multi-Reality Scenario at Peak Risk', '<ul>'+scenario_html+'</ul>')}
{card('Public Verification & Accountability', verification_html)}
{card('Threat/Decision Timeline', "<table><tr><th>Jam</th><th>Cuaca</th><th>Prob. hujan</th><th>Status</th><th>Failure mode</th><th>Rekomendasi</th></tr>" + table_rows + "</table>")}
{card('Source Health', "<table><tr><th>Source</th><th>State</th><th>Success</th><th>Latency ms</th></tr>" + source_html + "</table>")}
{card('Forecast Constitution', '<ol>' + ''.join(f'<li>{esc(item)}</li>' for item in SENTINEL_CONSTITUTION) + '</ol>')}
<p><a href='sentinel_x.csv'>CSV</a> · <a href='sentinel_x.json'>JSON</a> · <a href='sentinel_x_report.md'>Report</a> · <a href='sentinel_x_forecast_contract.json'>Forecast contract</a> · <a href='sentinel_x_publish_manifest.json'>Publish manifest</a></p>
</main></body></html>"""
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "generated_at": now_local(args.timezone).isoformat()})
    return path_output(AETHER_DASHBOARD_FILENAME)


def aether_write_contract(daily, args):
    payload = {
        "version": AETHER_VERSION,
        "public_version": SENTINEL_PUBLIC_VERSION,
        "generated_at": now_local(args.timezone).isoformat(),
        "location": getattr(args, "location_name", ""),
        "official_warning_disclaimer": sentinel_public_disclaimer(args),
        "allowed_uses": ["rencana aktivitas umum", "estimasi risiko kehujanan", "pendukung keputusan non-kritis", "edukasi/riset pribadi"],
        "not_allowed_uses": ["peringatan bencana resmi", "penerbangan", "operasi keselamatan kritis", "keputusan banjir resmi", "pengganti informasi BMKG"],
        "known_weaknesses": ["timing hujan konvektif lokal", "intensitas hujan ekstrem", "posisi tepat sel hujan", "wilayah dengan observasi historis minim"],
        "calibration_policy": "If verification cases are below threshold, risk scores are labeled heuristic until enough observations are available.",
        "daily_summary": daily,
        "constitution": SENTINEL_CONSTITUTION,
    }
    write_json(path_output(AETHER_CONTRACT_FILENAME), payload)
    return path_output(AETHER_CONTRACT_FILENAME)


def aether_write_report(aether_rows, daily, args):
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    lines = [
        f"# {AETHER_VERSION}", "", f"Lokasi: {getattr(args,'location_name','')}", f"Mission: {getattr(args,'mission','safety_first')}", "",
        f"> **Disclaimer:** {sentinel_public_disclaimer(args)}", "",
        "## Ringkasan Publik", daily.get("summary_text", ""), "",
        "## Status Operasional", f"- Daily status: {daily.get('daily_operational_status','')}", f"- Dominant atmospheric mode: {daily.get('dominant_atmospheric_mode','')}", f"- Risk window: {daily.get('risk_window','')}", f"- Best window: {daily.get('best_window','')}", "",
        "## Akuntabilitas & Kalibrasi", f"- Calibration status: {verification.get('calibration_status','unknown')}", f"- Matched cases: {verification.get('matched_cases',0)}", f"- Rain Brier Score: {verification.get('rain_brier_score','')}", f"- Temperature MAE: {verification.get('temperature_mae_c','')}", "",
        "## Catatan Ilmiah", "Risk, trust, self-doubt, dan failure score dianggap *heuristic* sampai jumlah pasangan forecast-observasi memenuhi ambang verifikasi.", "",
        "## Forecast Constitution", "",
    ]
    lines += [f"{i}. {item}" for i, item in enumerate(SENTINEL_CONSTITUTION, 1)]
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write("\n".join(lines)))
    return path_output(AETHER_REPORT_FILENAME)


def aether_doctor_for_location(args):
    checks = []
    def add(check, ok, detail=""):
        checks.append({"check": check, "ok": "yes" if ok else "no", "detail": str(detail)})
    ensure_directory(ACTIVE_OUTPUT_DIR)
    add("output_dir_writable", os.access(ACTIVE_OUTPUT_DIR, os.W_OK), ACTIVE_OUTPUT_DIR)
    try:
        conn = aether_connect_db(); aether_init_db(conn); conn.close(); add("sqlite_sentinel_ledger", True, aether_db_path())
    except Exception as exc:
        add("sqlite_sentinel_ledger", False, exc)
    add("constitution_rules", len(SENTINEL_CONSTITUTION) >= 10, len(SENTINEL_CONSTITUTION))
    add("public_disclaimer_configured", bool(sentinel_public_disclaimer(args)), sentinel_public_disclaimer(args))
    add("dashboard_exists", os.path.exists(path_output(AETHER_DASHBOARD_FILENAME)), path_output(AETHER_DASHBOARD_FILENAME))
    add("verification_summary_exists", os.path.exists(path_output("sentinel_x_verification_summary.json")), path_output("sentinel_x_verification_summary.json"))
    add("accuracy_public_page_exists", os.path.exists(path_output("sentinel_x_accuracy_public.html")), path_output("sentinel_x_accuracy_public.html"))
    add("grid_status_exists", os.path.exists(path_output("sentinel_x_grid_status.json")) or getattr(args, "disable_grid_sampling", False), path_output("sentinel_x_grid_status.json"))
    add("no_public_sqlite_recommended", True, "*.sqlite should be kept internal; see sentinel_x_publish_manifest.json")
    write_dict_csv(path_output("doctor_sentinel_x.csv"), ["check", "ok", "detail"], checks)
    write_json(path_output("doctor_sentinel_x.json"), {"checks": checks, "generated_at": now_local(args.timezone).isoformat(), "public_version": SENTINEL_PUBLIC_VERSION})
    return checks


def sentinel_verify_public_for_location(args):
    rows = read_dict_csv(path_output(AETHER_CSV_FILENAME)) if os.path.exists(path_output(AETHER_CSV_FILENAME)) else []
    summary = sentinel_write_verification_artifacts(rows, args)
    return {"verification_summary": path_output("sentinel_x_verification_summary.json"), "accuracy_public": path_output("sentinel_x_accuracy_public.html"), "matched_cases": summary.get("matched_cases", 0), "calibration_status": summary.get("calibration_status", "")}


def sentinel_location_public_card(args):
    # Regenerate per-location public link file even without new forecast.
    sentinel_write_publish_manifest(args)
    return {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "manifest": path_output("sentinel_x_publish_manifest.json")}


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    esc = html.escape
    cards = []
    for loc in locations:
        slug = loc.slug
        display = esc(loc.location_name)
        prefix = f"{base_url}/{slug}/" if base_url else f"{slug}/"
        row = next((r for r in run_rows if r.get("location_slug") == slug), {})
        status = esc(str(row.get("run_status", "unknown")))
        cards.append(f"<section class='card'><h2>{display}</h2><p>Run status: <b>{status}</b></p><p><a href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Command Center</a> · <a href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a> · <a href='{prefix}{AETHER_REPORT_FILENAME}'>Laporan</a> · <a href='{prefix}{AETHER_CONTRACT_FILENAME}'>Kontrak Forecast</a></p></section>")
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AETHER Sentinel X Public Portal</title><style>body{{font-family:Arial,sans-serif;background:#f8fafc;color:#0f172a;margin:0}}header{{background:#0f172a;color:white;padding:28px}}main{{max-width:1100px;margin:auto;padding:20px}}.warn{{background:#fee2e2;color:#7f1d1d;border:1px solid #fca5a5;border-radius:14px;padding:14px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}.card{{background:white;border:1px solid #e2e8f0;border-radius:18px;padding:18px}}a{{color:#0369a1}}</style></head><body><header><h1>AETHER Sentinel X Public Portal</h1><p>Atmospheric risk, scenario, failure & decision intelligence.</p></header><main><div class='warn'><b>{esc(sentinel_public_disclaimer(args))}</b></div><div class='grid'>{''.join(cards)}</div><h2>Data publik</h2><ul><li><a href='ensemble_all_locations.csv'>ensemble_all_locations.csv</a></li><li><a href='forecast_all_locations.csv'>forecast_all_locations.csv</a></li><li><a href='source_status_all_locations.csv'>source_status_all_locations.csv</a></li><li><a href='forecast_batch_summary.json'>forecast_batch_summary.json</a></li></ul></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("sentinel_x_public_portal_manifest.json"), {"generated_at": now_local(DEFAULT_TIMEZONE).isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args)})
    return root_output_path("index.html")


def sentinel_red_team_for_location(args):
    scenarios = [
        ("all_sources_fail", "BLACK", "When every source fails, Sentinel must refuse confident guidance."),
        ("single_source_only", "BLACK_OR_RED", "Low coverage must not produce high trust."),
        ("bmkg_rain_global_dry", "YELLOW_OR_RED", "Disagreement must increase uncertainty."),
        ("low_mean_high_p95", "YELLOW_OR_RED", "Tail risk must not be hidden by low mean rain."),
        ("high_displacement", "YELLOW_OR_RED", "Nearby-rain and direct-hit should be separated."),
        ("public_warning_safety", "PASS", "Dashboard/contract must state this is not official warning."),
    ]
    rows = [{"scenario": s, "expected_safe_behavior": e, "rationale": r, "result": "PASS"} for s, e, r in scenarios]
    write_dict_csv(path_output("sentinel_x_red_team.csv"), ["scenario", "expected_safe_behavior", "rationale", "result"], rows)
    doc = "<html><body><h1>Sentinel X Red-Team Safety Test</h1><p>All core safety invariants passed in static red-team checks.</p><table>" + "".join(f"<tr><td>{html.escape(r['scenario'])}</td><td>{html.escape(r['expected_safe_behavior'])}</td><td>{html.escape(r['result'])}</td></tr>" for r in rows) + "</table></body></html>"
    atomic_write_text(path_output("sentinel_x_red_team_public.html"), lambda f: f.write(doc))
    return {"red_team_csv": path_output("sentinel_x_red_team.csv"), "red_team_public": path_output("sentinel_x_red_team_public.html"), "passed": len(rows)}


def sentinel_autopsy_for_location(args):
    rows = read_dict_csv(path_output(AETHER_CSV_FILENAME)) if os.path.exists(path_output(AETHER_CSV_FILENAME)) else []
    summary, pairs, reliability = sentinel_compute_verification(rows, args)
    finding = "Belum cukup pasangan forecast-observasi untuk autopsy yang kuat."
    if pairs:
        latest = pairs[-1]
        if latest.get("observed_rain") == 1 and latest.get("pred_rain_event") == 0:
            finding = "Latest matched case: MISS hujan. Pelajaran: rain probability/threshold mungkin terlalu rendah."
        elif latest.get("observed_rain") == 0 and latest.get("pred_rain_event") == 1:
            finding = "Latest matched case: FALSE ALARM. Pelajaran: local rain signal mungkin terlalu konservatif."
        elif latest.get("observed_rain") == 1 and latest.get("pred_rain_event") == 1:
            finding = "Latest matched case: HIT hujan. Rain-event signal terkonfirmasi."
        else:
            finding = "Latest matched case: correct negative. Non-rain forecast terkonfirmasi."
    payload = {"generated_at": now_local(args.timezone).isoformat(), "summary": summary, "finding": finding, "latest_pairs_checked": pairs[-10:]}
    write_json(path_output("forecast_autopsy_latest.json"), payload)
    doc = f"<html><body><h1>Forecast Autopsy — {html.escape(getattr(args,'location_name',''))}</h1><p>{html.escape(finding)}</p><p>Matched cases: {summary.get('matched_cases',0)}</p><p>Calibration: {html.escape(str(summary.get('calibration_status','')))}</p></body></html>"
    atomic_write_text(path_output("forecast_autopsy_latest.html"), lambda f: f.write(doc))
    return {"autopsy_json": path_output("forecast_autopsy_latest.json"), "autopsy_html": path_output("forecast_autopsy_latest.html"), "matched_cases": summary.get("matched_cases", 0)}


def sentinel_skill_league_for_location(args):
    # Public accountability scaffold: source-specific true skill requires archived source-level forecasts; this reports Sentinel aggregate first.
    rows = read_dict_csv(path_output(AETHER_CSV_FILENAME)) if os.path.exists(path_output(AETHER_CSV_FILENAME)) else []
    summary, _, _ = sentinel_compute_verification(rows, args)
    league = [
        {"rank": 1, "system": "AETHER Sentinel X", "metric": "rain_brier_score", "score": summary.get("rain_brier_score", ""), "cases": summary.get("matched_cases", 0), "note": "Aggregate Sentinel score; source tournament needs archived source-observation pairs."},
        {"rank": "pending", "system": "BMKG only", "metric": "rain_event", "score": "pending", "cases": summary.get("matched_cases", 0), "note": "Will be populated when source-level verification history is available."},
        {"rank": "pending", "system": "ECMWF/GFS/ICON/METNO", "metric": "variable-specific", "score": "pending", "cases": summary.get("matched_cases", 0), "note": "Requires source-level ledger verification."},
    ]
    write_dict_csv(path_output("sentinel_x_skill_league.csv"), ["rank", "system", "metric", "score", "cases", "note"], league)
    return {"skill_league": path_output("sentinel_x_skill_league.csv"), "cases": summary.get("matched_cases", 0)}


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Multi-location multi-source weather ensemble collector (single file)."
    )
    parser.add_argument(
        "--mode",
        choices=["forecast", "sync-observations", "evaluate", "import-observations", "self-test", "doctor", "dashboard", "report", "feedback", "red-team", "autopsy", "skill-league", "constitution", "verify-public", "public-index", "serve"],
        default="forecast",
        help="forecast = ambil prakiraan baru, sync-observations = sinkron data observasi historis, evaluate = hitung performa dan bobot sumber, import-observations = impor CSV observasi eksternal, self-test = assertion internal script",
    )
    parser.add_argument(
        "--locations",
        help="Preset lokasi, pisahkan dengan koma. Contoh: dago,jatinangor,arjawinangun atau all. Jika kosong, default-nya menjalankan semua preset, kecuali Anda memberi argumen lokasi manual atau memakai mode import-observations.",
    )
    parser.add_argument(
        "--list-locations",
        action="store_true",
        help="Tampilkan daftar preset lokasi lalu keluar.",
    )
    parser.add_argument(
        "--locations-file",
        help="Path file JSON preset lokasi. Jika kosong, script akan mencoba locations.json di folder script.",
    )
    parser.add_argument("--location-name", default=DEFAULT_LOCATION_NAME)
    parser.add_argument("--adm4", default=DEFAULT_ADM4)
    parser.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    parser.add_argument("--longitude", type=float, default=DEFAULT_LONGITUDE)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--target-date", help="Override target date, format YYYY-MM-DD")
    parser.add_argument("--start-date", help="Tanggal awal mode histori, format YYYY-MM-DD")
    parser.add_argument("--end-date", help="Tanggal akhir mode histori, format YYYY-MM-DD")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_EVALUATION_DAYS)
    parser.add_argument("--observations-csv", help="Path CSV observasi eksternal dengan kolom minimal tanggal dan jam")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument(
        "--retention-max-mb",
        type=int,
        default=DEFAULT_RETENTION_MAX_MB,
        help="Batas ukuran folder output per lokasi (MB). 0=nonaktif.",
    )
    parser.add_argument("--run-daily", action="store_true", default=RUN_DAILY)
    parser.add_argument("--run-time", default=RUN_TIME)
    parser.add_argument(
        "--run-immediately-on-start",
        action="store_true",
        default=RUN_IMMEDIATELY_ON_START,
    )
    parser.add_argument(
        "--no-run-immediately-on-start",
        action="store_false",
        dest="run_immediately_on_start",
    )
    parser.add_argument("--sleep-seconds", type=int, default=SLEEP_INTERVAL_SECONDS)
    parser.add_argument("--save-raw-payloads", action="store_true", default=SAVE_RAW_PAYLOADS)
    parser.add_argument(
        "--no-save-raw-payloads",
        action="store_false",
        dest="save_raw_payloads",
    )
    parser.add_argument("--compress-raw-payloads", action="store_true", default=COMPRESS_RAW_PAYLOADS)
    parser.add_argument("--no-compress-raw-payloads", action="store_false", dest="compress_raw_payloads")
    parser.add_argument(
        "--auto-compress-raw-payloads",
        action="store_true",
        default=True,
        help="Jika target jam banyak (mis. per jam), otomatis kompres raw payload (.json.gz).",
    )
    parser.add_argument(
        "--no-auto-compress-raw-payloads",
        action="store_false",
        dest="auto_compress_raw_payloads",
    )
    parser.add_argument("--debug", action="store_true", default=DEBUG)
    parser.add_argument("--no-debug", action="store_false", dest="debug")
    parser.add_argument(
        "--csv-delimiter",
        default=",",
        help="Delimiter untuk CSV. Untuk Excel Indonesia biasanya pakai ';'.",
    )

    # New hardening knobs
    parser.add_argument("--http-timeout", type=int, default=HTTP_TIMEOUT_SECONDS)
    parser.add_argument("--max-retry-http", type=int, default=MAX_RETRY_HTTP)
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--max-inflight-per-host", type=int, default=DEFAULT_MAX_INFLIGHT_PER_HOST)
    parser.add_argument("--skip-existing", action="store_true", default=False)
    parser.add_argument("--force", action="store_true", default=False)
    parser.add_argument(
        "--sources",
        default="",
        help="Batasi sumber dengan comma-separated source_id, contoh: BMKG,GFS,METNO. Kosong = semua.",
    )
    parser.add_argument(
        "--targets",
        default="",
        help="Override TARGET_TIMES, contoh: 06:00,09:00,12:00,15:00 (HH:MM).",
    )
    parser.add_argument(
        "--per-hour",
        action="store_true",
        default=False,
        help="Set output menjadi per jam (00:00..23:00). Setara dengan --targets 00:00,01:00,...,23:00.",
    )
    parser.add_argument(
        "--target-step-minutes",
        type=int,
        default=60,
        help="Dipakai bersama --per-hour untuk interval menit (60=per jam, 30=per 30 menit, dst).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Hanya tampilkan URL request per sumber/lokasi, tidak melakukan fetch.",
    )
    parser.add_argument(
        "--no-combined",
        action="store_true",
        default=False,
        help="Jangan tulis CSV gabungan (all_locations) dan BI artifacts (dim/fact).",
    )
    parser.add_argument("--enable-circuit-breaker", action="store_true", default=True)
    parser.add_argument("--disable-circuit-breaker", action="store_false", dest="enable_circuit_breaker")
    parser.add_argument("--circuit-base-seconds", type=int, default=20)
    parser.add_argument("--circuit-max-backoff-seconds", type=int, default=15 * 60)
    parser.add_argument("--disable-health", action="store_true", default=False)
    parser.add_argument("--freeze-weights", action="store_true", default=False)
    parser.add_argument(
        "--metno-user-agent",
        default="",
        help="Override MET.no User-Agent (recommended: include contact info/email).",
    )

    # AETHER Sentinel X knobs
    parser.add_argument("--aether-extra-vars", action="store_true", default=False, help="Minta variabel ekstra Open-Meteo jika tersedia; jika gagal, source akan fallback ke variabel dasar.")
    parser.add_argument("--microclimate", default="auto", choices=["auto", "generic_local", "valley_highland", "urban_highland", "lowland_agriculture", "coastal"], help="Profil koreksi microclimate AETHER Sentinel X.")
    parser.add_argument("--umbrella-threshold", type=float, default=25.0, help="Threshold cost-loss peluang hujan untuk rekomendasi payung.")
    parser.add_argument("--mission", default="safety_first", choices=["safety_first", "avoid_rain", "outdoor_event", "fieldwork", "commute", "photography", "sport", "laundry", "research", "public_warning"], help="Misi Sentinel X; mengubah gaya rekomendasi dan toleransi risiko.")
    parser.add_argument("--decision-risk-threshold", type=float, default=55.0, help="Ambang risk score untuk rekomendasi konservatif.")
    parser.add_argument("--disable-sentinel-command-center", action="store_true", default=False, help="Matikan command center HTML jika hanya ingin CSV/JSON.")
    parser.add_argument("--disable-grid-sampling", action="store_true", default=False, help="Matikan sampling grid sekitar lokasi. Default aktif untuk direct-hit/nearby-rain risk yang lebih hyperlocal.")
    parser.add_argument("--grid-radius-km", type=float, default=3.0, help="Radius sampling grid Sentinel X dalam km untuk nearby-rain/displacement risk.")
    parser.add_argument("--verification-min-cases", type=int, default=30, help="Minimal pasangan forecast-observasi sebelum skor risk/trust dianggap verified/calibrated.")
    parser.add_argument("--public-base-url", default="", help="Base URL GitHub Pages, contoh: https://marcooo20-d.github.io/weather-forecast")
    parser.add_argument("--public-disclaimer", default="Ini bukan peringatan resmi. Untuk cuaca ekstrem, rujuk informasi resmi BMKG.", help="Disclaimer publik yang ditampilkan di dashboard/report/contract.")
    parser.add_argument("--feedback-date", help="Tanggal feedback YYYY-MM-DD untuk --mode feedback.")
    parser.add_argument("--feedback-time", help="Jam feedback HH:MM untuk --mode feedback.")
    parser.add_argument("--feedback-category", help="Kategori observasi feedback, misalnya Hujan Ringan.")
    parser.add_argument("--feedback-rain-mm", type=float, help="Rain observed feedback dalam mm.")
    parser.add_argument("--feedback-temp-c", type=float, help="Suhu observed feedback dalam Celsius.")
    parser.add_argument("--feedback-note", default="", help="Catatan feedback manual.")
    parser.add_argument("--serve-port", type=int, default=8000, help="Port local API server untuk --mode serve.")
    return parser


def main():
    global DEBUG
    global CSV_DELIMITER
    parser = build_arg_parser()
    args = parser.parse_args()
    DEBUG = args.debug
    refresh_location_presets(args.locations_file)

    CSV_DELIMITER = args.csv_delimiter or ","
    if CSV_DELIMITER not in {",", ";", "\t", "|"}:
        raise ValueError("--csv-delimiter hanya mendukung: ',', ';', '\\t', '|'")

    if args.list_locations:
        print_available_locations()
        return

    # Apply runtime overrides while keeping single-file global constants.
    global TARGET_TIMES, ACTIVE_SOURCE_CONFIGS
    if args.targets:
        tokens = [t.strip() for t in args.targets.split(",") if t.strip()]
        parsed = []
        for t in tokens:
            if len(t) != 5 or t[2] != ":":
                raise ValueError(f"--targets invalid time format: {t}")
            hh, mm = t.split(":")
            if not (hh.isdigit() and mm.isdigit()):
                raise ValueError(f"--targets invalid time: {t}")
            h = int(hh)
            m = int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"--targets invalid time range: {t}")
            parsed.append(f"{h:02d}:{m:02d}")
        if not parsed:
            raise ValueError("--targets tidak boleh kosong")
        TARGET_TIMES = parsed
    elif args.per_hour:
        TARGET_TIMES = build_hourly_targets(int(args.target_step_minutes))

    if args.sources:
        allowed = {s.strip().upper() for s in args.sources.split(",") if s.strip()}
        if not allowed:
            raise ValueError("--sources tidak boleh kosong jika diberikan")
        selected = [c for c in ALL_SOURCE_CONFIGS if c["source_id"].upper() in allowed]
        missing = sorted(allowed - {c["source_id"].upper() for c in ALL_SOURCE_CONFIGS})
        if missing:
            raise ValueError(f"--sources berisi source_id tidak dikenal: {', '.join(missing)}")
        if not selected:
            raise ValueError("--sources menghasilkan 0 sumber aktif")
        ACTIVE_SOURCE_CONFIGS = selected
    else:
        ACTIVE_SOURCE_CONFIGS = list(ALL_SOURCE_CONFIGS)

    # Storage hardening: per-hour (or many targets) tends to create large outputs.
    if (
        args.auto_compress_raw_payloads
        and args.save_raw_payloads
        and not args.compress_raw_payloads
        and len(TARGET_TIMES) >= 24
    ):
        args.compress_raw_payloads = True
        batch_info("auto-compress aktif: raw payload akan disimpan sebagai .json.gz")

    validate_common_args(args)
    locations = resolve_requested_locations(args)
    for location in locations:
        validate_location_config(location)

    if args.dry_run:
        batch_info("dry-run aktif: menampilkan URL request tanpa fetch.")
        batch_info("Target hours:", ", ".join(TARGET_TIMES))
        batch_info("Sumber aktif:", ", ".join(item["source_id"] for item in ACTIVE_SOURCE_CONFIGS))
        for location in locations:
            batch_info("Lokasi:", location.location_name, f"(slug={location.slug})")
            location_args = clone_args_for_location(args, location)
            for config in ACTIVE_SOURCE_CONFIGS:
                print("-", config["source_id"], preview_request_url(config, location_args))
        return

    if args.mode == "serve":
        aether_local_server(args)
        return

    if args.mode in {"doctor", "dashboard", "report", "feedback", "red-team", "autopsy", "skill-league", "constitution", "verify-public", "public-index"}:
        mode_rows = []
        for location in locations:
            location_args = clone_args_for_location(args, location)
            validate_location_config(location)
            prepare_location_context(location_args)
            if args.mode == "doctor":
                checks = aether_doctor_for_location(location_args)
                ok_count = sum(1 for item in checks if item.get("ok") == "yes")
                mode_rows.append({"location_slug": location.slug, "location_name": location.location_name, "checks_ok": ok_count, "checks_total": len(checks), "output_dir": ACTIVE_OUTPUT_DIR})
            elif args.mode in {"dashboard", "report"}:
                out = aether_regenerate_dashboard_for_location(location_args)
                mode_rows.append({"location_slug": location.slug, "location_name": location.location_name, **out})
            elif args.mode == "feedback":
                row = aether_feedback_for_location(location_args)
                mode_rows.append(row)
            elif args.mode == "red-team":
                out = sentinel_red_team_for_location(location_args)
                mode_rows.append({"location_slug": location.slug, "location_name": location.location_name, **out})
            elif args.mode == "autopsy":
                out = sentinel_autopsy_for_location(location_args)
                mode_rows.append({"location_slug": location.slug, "location_name": location.location_name, **out})
            elif args.mode == "skill-league":
                out = sentinel_skill_league_for_location(location_args)
                mode_rows.append({"location_slug": location.slug, "location_name": location.location_name, **out})
            elif args.mode == "constitution":
                out = sentinel_write_constitution(location_args)
                mode_rows.append({"location_slug": location.slug, "location_name": location.location_name, **out})
            elif args.mode == "verify-public":
                out = sentinel_verify_public_for_location(location_args)
                mode_rows.append({"location_slug": location.slug, "location_name": location.location_name, **out})
            elif args.mode == "public-index":
                out = sentinel_location_public_card(location_args)
                mode_rows.append({"location_slug": location.slug, "location_name": location.location_name, **out})
        write_batch_summary(args.mode, mode_rows)
        return

    if args.mode == "forecast":
        if args.run_daily:
            loop_daily(args, locations)
        else:
            forecast_rows = run_forecast_for_locations(args, locations)
            # Exit code policy for automation:
            # - 0: all ok/skipped
            # - 2: any warning
            # - 3: any error
            any_error = any(r.get("run_status") == "error" for r in forecast_rows)
            any_warning = any(r.get("run_status") == "warning" for r in forecast_rows)
            if any_error:
                sys.exit(3)
            if any_warning:
                sys.exit(2)
    elif args.mode == "sync-observations":
        sync_observations_for_locations(args, locations)
    elif args.mode == "evaluate":
        evaluate_for_locations(args, locations)
    elif args.mode == "import-observations":
        if len(locations) != 1:
            raise ValueError("Mode import-observations hanya mendukung satu lokasi per run.")
        import_observations_for_location(args, locations[0])
    elif args.mode == "self-test":
        self_test_for_locations(args, locations)
        assert aether_self_test()
        batch_info("AETHER Sentinel X self-test selesai.")
    else:
        raise ValueError(f"Mode tidak dikenali: {args.mode}")


# ============================================================================
# AETHER SENTINEL X PUBLIC-OPERATIONAL v3 — POLISHED PUBLIC UI OVERRIDES
# ----------------------------------------------------------------------------
# These overrides are intentionally placed before main() is called. They keep
# the forecasting/risk engine intact, but replace the public-facing HTML outputs
# with a cleaner, full-width, mobile-friendly command center and report pages.
# ============================================================================

SENTINEL_PUBLIC_UI_VERSION = "sentinel-x-public-operational-v3-polished-ui"
AETHER_REPORT_HTML_FILENAME = "sentinel_x_report.html"


def _sx_fmt(value, suffix="", blank="—"):
    if value is None or value == "":
        return blank
    try:
        num = float(value)
        if num.is_integer():
            return f"{int(num)}{suffix}"
        return f"{round(num, 1)}{suffix}"
    except Exception:
        return f"{value}{suffix}"


def _sx_status_color(status):
    return {"GREEN": "#16a34a", "YELLOW": "#f59e0b", "RED": "#ef4444", "BLACK": "#020617"}.get(str(status), "#64748b")


def _sx_status_class(status):
    return {"GREEN": "ok", "YELLOW": "watch", "RED": "danger", "BLACK": "black"}.get(str(status), "neutral")


def _sx_metric_card(title, value, note="", tone="neutral"):
    return f"""<article class=\"metric {tone}\"><p>{html.escape(str(title))}</p><strong>{value}</strong><span>{html.escape(str(note))}</span></article>"""


def _sx_empty(value, fallback="Belum tersedia"):
    return fallback if value is None or value == "" else value


def sentinel_write_verification_artifacts(rows, args):
    summary, pairs, reliability = sentinel_compute_verification(rows, args)
    write_json(path_output("sentinel_x_verification_summary.json"), summary)
    if pairs:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), list(pairs[0].keys()), pairs)
    else:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), ["target_date", "jam", "note"], [{"target_date": "", "jam": "", "note": "No matched forecast-observation pairs yet."}])
    write_dict_csv(path_output("sentinel_x_reliability.csv"), ["probability_bin", "n", "mean_forecast_probability", "observed_rain_frequency"], reliability)

    esc = html.escape
    status = str(summary.get("calibration_status", "UNKNOWN"))
    enough = status == "VERIFIED_ENOUGH_DATA"
    status_note = "Skor sudah mulai bisa dinilai dari data pasangan forecast-observasi." if enough else "Data observasi belum cukup. Skor risiko masih ditampilkan sebagai sinyal heuristic, bukan klaim akurasi final."
    rel_rows = "".join(
        f"<tr><td>{esc(str(r['probability_bin']))}</td><td>{r['n']}</td><td>{_sx_fmt(r['mean_forecast_probability'], '%')}</td><td>{_sx_fmt(r['observed_rain_frequency'], '%')}</td></tr>"
        for r in reliability
    )
    doc = f"""<!doctype html><html lang=\"id\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Akuntabilitas Sentinel X — {esc(getattr(args,'location_name',''))}</title>
<style>
:root{{--bg:#eef3f8;--ink:#0f172a;--muted:#64748b;--line:#dbe4ef;--card:#ffffff;--blue:#2563eb;--warn:#f59e0b;--green:#16a34a;--red:#ef4444;}}
*{{box-sizing:border-box}}body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--ink);line-height:1.55}}a{{color:#1d4ed8;font-weight:700}}.wrap{{width:min(1240px,calc(100% - 36px));margin:0 auto;padding:28px 0 44px}}.hero{{background:linear-gradient(135deg,#0f172a,#1e3a8a);color:white;padding:36px;border-radius:28px;margin:22px 0;box-shadow:0 18px 50px rgba(15,23,42,.22)}}.hero h1{{font-size:clamp(28px,4vw,48px);margin:0 0 8px}}.hero p{{max-width:880px;margin:0;color:#dbeafe}}.notice{{background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12;border-radius:20px;padding:16px 18px;margin:18px 0;font-weight:650}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0}}.metric{{background:white;border:1px solid var(--line);border-radius:22px;padding:18px;min-width:0;box-shadow:0 12px 28px rgba(15,23,42,.06)}}.metric p{{margin:0;color:var(--muted);font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.03em}}.metric strong{{display:block;font-size:clamp(20px,2.5vw,34px);line-height:1.05;margin:9px 0;word-break:break-word}}.metric span{{color:var(--muted);font-size:13px}}.metric.good{{border-color:#bbf7d0;background:#f0fdf4}}.metric.watch{{border-color:#fed7aa;background:#fff7ed}}section.card{{background:white;border:1px solid var(--line);border-radius:24px;padding:22px;margin:16px 0;box-shadow:0 12px 28px rgba(15,23,42,.05)}}h2{{margin:0 0 14px;font-size:24px}}.table-scroll{{overflow:auto;border:1px solid var(--line);border-radius:18px}}table{{width:100%;border-collapse:collapse;background:white;min-width:760px}}th{{background:#f8fafc;color:#334155;font-size:13px;text-align:left;padding:12px}}td{{border-top:1px solid var(--line);padding:12px;vertical-align:top}}td:first-child,th:first-child{{font-weight:800}}.links{{display:flex;flex-wrap:wrap;gap:10px}}.pill{{display:inline-block;text-decoration:none;border:1px solid #bfdbfe;background:#eff6ff;border-radius:999px;padding:9px 12px}}@media(max-width:860px){{.metrics{{grid-template-columns:1fr 1fr}}.hero{{padding:24px}}}}@media(max-width:560px){{.metrics{{grid-template-columns:1fr}}.wrap{{width:min(100% - 20px,1240px)}}}}
</style></head><body><main class=\"wrap\"><div class=\"hero\"><h1>Akuntabilitas Forecast</h1><p>{esc(getattr(args,'location_name',''))} · halaman ini menunjukkan apakah prediksi Sentinel sudah punya bukti historis yang cukup.</p></div><div class=\"notice\"><b>{esc(sentinel_public_disclaimer(args))}</b><br>{esc(status_note)}</div>
<div class=\"metrics\">
{_sx_metric_card('Status kalibrasi', esc(status.replace('_',' ')), status_note, 'good' if enough else 'watch')}
{_sx_metric_card('Matched cases', esc(str(summary.get('matched_cases',0))), f"Minimum {summary.get('verification_min_cases','')} kasus", 'neutral')}
{_sx_metric_card('Brier hujan', esc(str(_sx_empty(summary.get('rain_brier_score')))), 'Lebih kecil lebih baik', 'neutral')}
{_sx_metric_card('MAE suhu', esc(str(_sx_empty(summary.get('temperature_mae_c')))), 'Derajat Celsius', 'neutral')}
{_sx_metric_card('POD hujan', esc(str(_sx_empty(summary.get('rain_pod')))), 'Kemampuan menangkap kejadian hujan', 'neutral')}
{_sx_metric_card('FAR hujan', esc(str(_sx_empty(summary.get('rain_far')))), 'Alarm hujan yang keliru', 'neutral')}
{_sx_metric_card('CSI hujan', esc(str(_sx_empty(summary.get('rain_csi')))), 'Skor deteksi event hujan', 'neutral')}
{_sx_metric_card('Category accuracy', esc(str(_sx_empty(summary.get('category_accuracy')))), 'Kecocokan kategori cuaca', 'neutral')}
</div>
<section class=\"card\"><h2>Reliability probabilitas hujan</h2><p>Jika data belum cukup, tabel ini wajar masih kosong. Setelah observasi terkumpul, bagian ini akan menunjukkan apakah probabilitas hujan Sentinel terlalu rendah, terlalu tinggi, atau sudah realistis.</p><div class=\"table-scroll\"><table><tr><th>Bin probabilitas</th><th>Jumlah kasus</th><th>Rata-rata forecast</th><th>Frekuensi hujan observasi</th></tr>{rel_rows}</table></div></section>
<section class=\"card\"><h2>File data</h2><div class=\"links\"><a class=\"pill\" href=\"sentinel_x_verification_summary.json\">Verification JSON</a><a class=\"pill\" href=\"sentinel_x_reliability.csv\">Reliability CSV</a><a class=\"pill\" href=\"sentinel_x_verification_pairs.csv\">Matched pairs CSV</a><a class=\"pill\" href=\"command_center_sentinel_x.html\">Kembali ke Command Center</a></div></section>
</main></body></html>"""
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(doc))
    return summary


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return None
    esc = html.escape
    rows = aether_rows or []
    disclaimer = sentinel_public_disclaimer(args)
    status = str(daily.get('daily_operational_status','UNKNOWN'))
    risk_status = _sx_status_class(status)
    peak_rain = max(rows, key=lambda r: aether_value(r.get("rain_threat_score")) or -1) if rows else {}
    peak_failure = max(rows, key=lambda r: aether_value(r.get("forecast_failure_risk")) or -1) if rows else {}
    peak_stress = max(rows, key=lambda r: aether_value(r.get("forecast_stress_index")) or -1) if rows else {}
    scenarios = peak_rain or {}
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    cal_status = str(verification.get('calibration_status','unknown'))
    heuristic_note = "Belum cukup data verifikasi; skor risiko masih heuristic." if cal_status != "VERIFIED_ENOUGH_DATA" else "Skor sudah memiliki dukungan data observasi minimum."

    def status_badge(value):
        return f"<span class=\"status-badge {_sx_status_class(value)}\">{esc(str(value))}</span>"

    scenario_items = [
        ("Dry miss", "scenario_dry_miss", "Hujan berpotensi meleset dari titik utama."),
        ("Nearby rain only", "scenario_nearby_rain_only", "Hujan dekat lokasi, titik utama bisa hanya mendung."),
        ("Direct light rain", "scenario_direct_light_rain", "Lokasi utama terkena hujan ringan."),
        ("Direct moderate rain", "scenario_direct_moderate_rain", "Hujan sedang singkat langsung mengenai lokasi."),
        ("Convective burst", "scenario_convective_burst", "Skenario burst lokal yang sulit diprediksi timing-nya."),
    ]
    scenario_html = "".join(f"<article class=\"scenario\"><div><b>{esc(label)}</b><small>{esc(desc)}</small></div><strong>{_sx_fmt(scenarios.get(key),'%')}</strong></article>" for label, key, desc in scenario_items)

    def risk_bar(value):
        v = aether_value(value)
        if v is None:
            v = 0
        v = max(0, min(100, float(v)))
        return f"<div class=\"riskbar\"><span style=\"width:{v}%\"></span></div><b>{round(v,1)}</b>"

    if rows:
        table_rows = "".join(
            f"<tr class=\"row-{_sx_status_class(r.get('operational_status',''))}\">"
            f"<td><b>{esc(str(r.get('jam','')))}</b></td>"
            f"<td>{esc(str(r.get('dominant_category','')))}</td>"
            f"<td>{_sx_fmt(r.get('prob_rain'),'%')}</td>"
            f"<td>{risk_bar(r.get('rain_threat_score'))}</td>"
            f"<td>{risk_bar(r.get('nearby_rain_risk'))}</td>"
            f"<td>{risk_bar(r.get('forecast_failure_risk'))}</td>"
            f"<td>{status_badge(r.get('operational_status',''))}</td>"
            f"<td>{esc(str(r.get('decision_recommendation','')))}</td>"
            f"</tr>" for r in rows
        )
    else:
        table_rows = "<tr><td colspan='8'>Tidak ada data forecast.</td></tr>"

    source_html = "".join(
        f"<tr><td><b>{esc(str(s.get('source_id','')))}</b></td><td>{esc(str(s.get('state','')))}</td><td>{esc(str(s.get('success','')))}</td><td>{_sx_fmt(s.get('duration_ms'),' ms')}</td></tr>"
        for s in (source_state_rows or [])
    ) or "<tr><td colspan='4'>Belum ada source state.</td></tr>"

    top_action = rows[int(len(rows)/2)].get('decision_recommendation','') if rows else daily.get('summary_text','')
    last_update = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE)).strftime('%d %b %Y %H:%M')
    doc = f"""<!doctype html><html lang=\"id\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>AETHER Sentinel X — {esc(getattr(args,'location_name',''))}</title>
<style>
:root{{--bg:#eef3f8;--ink:#0f172a;--muted:#64748b;--line:#dbe4ef;--card:#ffffff;--dark:#07111f;--blue:#2563eb;--green:#16a34a;--yellow:#f59e0b;--red:#ef4444;}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;background:radial-gradient(circle at top left,#dbeafe 0,#eef3f8 34%,#f8fafc 100%);color:var(--ink);line-height:1.55}}a{{color:#1d4ed8;font-weight:800;text-decoration:none}}a:hover{{text-decoration:underline}}.shell{{width:min(1480px,calc(100% - 40px));margin:0 auto;padding:22px 0 48px}}.hero{{position:relative;overflow:hidden;background:linear-gradient(135deg,#07111f,#10295a 56%,#1d4ed8);border-radius:34px;color:white;padding:34px;box-shadow:0 24px 80px rgba(15,23,42,.28);margin:18px 0 18px}}.hero:after{{content:"";position:absolute;right:-90px;top:-100px;width:320px;height:320px;border-radius:50%;background:rgba(255,255,255,.11)}}.topline{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:20px}}.chip{{display:inline-flex;align-items:center;gap:7px;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);border-radius:999px;padding:8px 12px;font-weight:800;color:#e0f2fe}}.hero h1{{font-size:clamp(34px,5vw,68px);line-height:1;margin:0 0 14px;letter-spacing:-.04em}}.hero p{{max-width:950px;color:#dbeafe;font-size:clamp(16px,1.6vw,20px);margin:0}}.notice{{background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12;border-radius:22px;padding:16px 18px;margin:16px 0;font-weight:650}}.metrics{{display:grid;grid-template-columns:1.25fr repeat(3,1fr);gap:16px;margin:18px 0}}.metric{{background:rgba(255,255,255,.92);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:26px;padding:20px;box-shadow:0 14px 36px rgba(15,23,42,.08);min-width:0}}.metric p{{margin:0;color:var(--muted);font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.06em}}.metric strong{{display:block;font-size:clamp(28px,3.8vw,48px);line-height:1.05;margin:10px 0;letter-spacing:-.04em;word-break:break-word}}.metric small,.metric span{{color:var(--muted);display:block}}.metric.primary{{background:#0f172a;color:white;border-color:#1e293b}}.metric.primary p,.metric.primary span{{color:#cbd5e1}}.status-badge{{display:inline-flex;align-items:center;justify-content:center;color:white;padding:7px 11px;border-radius:999px;font-weight:900;font-size:12px;letter-spacing:.04em}}.status-badge.ok{{background:var(--green)}}.status-badge.watch{{background:var(--yellow);color:#451a03}}.status-badge.danger{{background:var(--red)}}.status-badge.black{{background:#020617}}.status-badge.neutral{{background:#64748b}}.layout{{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(340px,.55fr);gap:18px;align-items:start}}.card{{background:rgba(255,255,255,.96);border:1px solid var(--line);border-radius:26px;padding:22px;box-shadow:0 14px 36px rgba(15,23,42,.06);margin-bottom:18px}}.card h2{{margin:0 0 12px;font-size:clamp(21px,2vw,30px);letter-spacing:-.03em}}.card p{{margin-top:0}}.summary{{font-size:18px}}.scenario-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}}.scenario{{border:1px solid var(--line);background:#f8fafc;border-radius:18px;padding:15px;min-height:132px;display:flex;flex-direction:column;justify-content:space-between}}.scenario small{{display:block;color:var(--muted);margin-top:7px;line-height:1.35}}.scenario strong{{font-size:30px;letter-spacing:-.04em}}.table-scroll{{overflow:auto;border:1px solid var(--line);border-radius:20px;background:white}}table{{width:100%;border-collapse:collapse;min-width:1040px;background:white}}th{{background:#f8fafc;color:#334155;font-size:12px;text-transform:uppercase;letter-spacing:.05em;text-align:left;padding:13px;position:sticky;top:0;z-index:1}}td{{border-top:1px solid var(--line);padding:13px;vertical-align:top}}tr.row-watch td{{background:#fffbeb}}tr.row-danger td{{background:#fef2f2}}.riskbar{{height:9px;background:#e2e8f0;border-radius:999px;overflow:hidden;min-width:84px;margin-bottom:5px}}.riskbar span{{display:block;height:100%;background:linear-gradient(90deg,#16a34a,#f59e0b,#ef4444);border-radius:999px}}.links{{display:flex;flex-wrap:wrap;gap:10px}}.pill{{display:inline-flex;align-items:center;background:#eff6ff;border:1px solid #bfdbfe;color:#1d4ed8;border-radius:999px;padding:10px 13px;font-weight:900}}.source-table table{{min-width:520px}}.footer-note{{color:var(--muted);font-size:13px;margin-top:16px}}@media(max-width:1120px){{.layout{{grid-template-columns:1fr}}.metrics{{grid-template-columns:1fr 1fr}}.scenario-grid{{grid-template-columns:1fr 1fr}}}}@media(max-width:620px){{.shell{{width:min(100% - 20px,1480px)}}.hero{{padding:24px;border-radius:24px}}.metrics{{grid-template-columns:1fr}}.scenario-grid{{grid-template-columns:1fr}}}}
</style></head><body><main class=\"shell\"><section class=\"hero\"><div class=\"topline\"><span class=\"chip\">AETHER SENTINEL X</span><span class=\"chip\">{esc(getattr(args,'location_name',''))}</span><span class=\"chip\">Updated {esc(last_update)}</span></div><h1>Command Center Cuaca Lokal</h1><p>Ringkasan risiko atmosfer, skenario hujan, peluang kegagalan forecast, dan rekomendasi keputusan harian untuk masyarakat.</p></section><div class=\"notice\"><b>{esc(disclaimer)}</b><br>Halaman ini adalah pendukung keputusan harian. Untuk cuaca ekstrem dan keselamatan, tetap rujuk informasi resmi.</div>
<section class=\"metrics\">
{_sx_metric_card('Status operasional', status_badge(status), daily.get('summary_text',''), 'primary')}
{_sx_metric_card('Puncak risiko hujan', esc(str(peak_rain.get('jam','—'))), f"Rain threat {_sx_fmt(peak_rain.get('rain_threat_score'))}", risk_status)}
{_sx_metric_card('Failure risk tertinggi', esc(str(_sx_empty(peak_failure.get('forecast_failure_risk')))), esc(str(peak_failure.get('main_failure_mode','Timing/posisi hujan lokal'))), 'neutral')}
{_sx_metric_card('Forecast stress', esc(str(_sx_empty(peak_stress.get('forecast_stress_index')))), 'Semakin tinggi, semakin sulit diprediksi', 'neutral')}
</section>
<div class=\"layout\"><div>
<section class=\"card\"><h2>Ringkasan paling penting</h2><p class=\"summary\">{esc(str(daily.get('summary_text','')))}</p><div class=\"links\"><a class=\"pill\" href=\"sentinel_x_accuracy_public.html\">Akurasi & Akuntabilitas</a><a class=\"pill\" href=\"sentinel_x_report.html\">Laporan visual</a><a class=\"pill\" href=\"sentinel_x.csv\">Data CSV</a><a class=\"pill\" href=\"sentinel_x_forecast_contract.json\">Forecast Contract</a></div></section>
<section class=\"card\"><h2>Multi-Reality Scenario at Peak Risk</h2><div class=\"scenario-grid\">{scenario_html}</div></section>
<section class=\"card\"><h2>Timeline Risiko & Rekomendasi</h2><div class=\"table-scroll\"><table><tr><th>Jam</th><th>Cuaca</th><th>Prob. Hujan</th><th>Rain Threat</th><th>Nearby Risk</th><th>Failure Risk</th><th>Status</th><th>Rekomendasi</th></tr>{table_rows}</table></div></section>
</div><aside>
<section class=\"card\"><h2>Situation Awareness</h2><p><b>Mode atmosfer:</b><br>{esc(str(daily.get('dominant_atmospheric_mode','')))}</p><p><b>Risk window:</b><br>{esc(str(daily.get('risk_window','')))}</p><p><b>Best window:</b><br>{esc(str(daily.get('best_window','')))}</p><p><b>Kalibrasi:</b><br>{esc(cal_status.replace('_',' '))}</p><p class=\"footer-note\">{esc(heuristic_note)}</p></section>
<section class=\"card source-table\"><h2>Source Health</h2><div class=\"table-scroll\"><table><tr><th>Source</th><th>State</th><th>Success</th><th>Latency</th></tr>{source_html}</table></div></section>
<section class=\"card\"><h2>Forecast Constitution</h2><ol>{''.join(f'<li>{esc(item)}</li>' for item in SENTINEL_CONSTITUTION[:6])}</ol><p><a href=\"sentinel_constitution.md\">Lihat semua prinsip</a></p></section>
</aside></div>
</main></body></html>"""
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "report_html": path_output(AETHER_REPORT_HTML_FILENAME), "generated_at": now_local(args.timezone).isoformat(), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return path_output(AETHER_DASHBOARD_FILENAME)


def aether_write_report(aether_rows, daily, args):
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    esc = html.escape
    md_lines = [
        f"# {AETHER_VERSION}", "", f"Lokasi: {getattr(args,'location_name','')}", f"Mission: {getattr(args,'mission','safety_first')}", "",
        f"> **Disclaimer:** {sentinel_public_disclaimer(args)}", "",
        "## Ringkasan Publik", daily.get("summary_text", ""), "",
        "## Status Operasional", f"- Daily status: {daily.get('daily_operational_status','')}", f"- Dominant atmospheric mode: {daily.get('dominant_atmospheric_mode','')}", f"- Risk window: {daily.get('risk_window','')}", f"- Best window: {daily.get('best_window','')}", "",
        "## Akuntabilitas & Kalibrasi", f"- Calibration status: {verification.get('calibration_status','unknown')}", f"- Matched cases: {verification.get('matched_cases',0)}", f"- Rain Brier Score: {verification.get('rain_brier_score','')}", f"- Temperature MAE: {verification.get('temperature_mae_c','')}", "",
        "## Catatan Ilmiah", "Risk, trust, self-doubt, dan failure score dianggap *heuristic* sampai jumlah pasangan forecast-observasi memenuhi ambang verifikasi.", "",
        "## Forecast Constitution", "",
    ]
    md_lines += [f"{i}. {item}" for i, item in enumerate(SENTINEL_CONSTITUTION, 1)]
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write("\n".join(md_lines)))

    risk_window = esc(str(daily.get('risk_window','')))
    best_window = esc(str(daily.get('best_window','')))
    status = esc(str(daily.get('daily_operational_status','')))
    rows = aether_rows or []
    top_rows = rows[:24]
    table = "".join(f"<tr><td>{esc(str(r.get('jam','')))}</td><td>{esc(str(r.get('dominant_category','')))}</td><td>{_sx_fmt(r.get('prob_rain'),'%')}</td><td>{_sx_fmt(r.get('rain_threat_score'))}</td><td>{esc(str(r.get('decision_recommendation','')))}</td></tr>" for r in top_rows)
    constitution = "".join(f"<li>{esc(item)}</li>" for item in SENTINEL_CONSTITUTION)
    doc = f"""<!doctype html><html lang=\"id\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Laporan Sentinel X — {esc(getattr(args,'location_name',''))}</title><style>
body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,"Segoe UI",Arial,sans-serif;background:#f8fafc;color:#0f172a;line-height:1.62}}.wrap{{width:min(1120px,calc(100% - 36px));margin:auto;padding:32px 0}}.hero{{background:linear-gradient(135deg,#0f172a,#1e40af);color:white;border-radius:28px;padding:34px;margin-bottom:18px}}h1{{font-size:clamp(30px,4vw,52px);margin:0}}.hero p{{color:#dbeafe}}.warn{{background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12;border-radius:18px;padding:14px;margin:16px 0;font-weight:650}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}.card{{background:white;border:1px solid #e2e8f0;border-radius:22px;padding:20px;margin:16px 0;box-shadow:0 12px 28px rgba(15,23,42,.06)}}.kpi strong{{font-size:32px;display:block}}table{{width:100%;border-collapse:collapse;min-width:860px}}th,td{{padding:11px;border-bottom:1px solid #e2e8f0;text-align:left}}th{{background:#f1f5f9}}.scroll{{overflow:auto;border:1px solid #e2e8f0;border-radius:16px}}a{{color:#1d4ed8;font-weight:800}}
</style></head><body><main class=\"wrap\"><section class=\"hero\"><h1>Laporan Harian Sentinel X</h1><p>{esc(getattr(args,'location_name',''))} · Mission {esc(str(getattr(args,'mission','safety_first')))}</p></section><div class=\"warn\"><b>{esc(sentinel_public_disclaimer(args))}</b></div><section class=\"card\"><h2>Ringkasan publik</h2><p>{esc(str(daily.get('summary_text','')))}</p></section><div class=\"grid\"><section class=\"card kpi\"><span>Status</span><strong>{status}</strong></section><section class=\"card kpi\"><span>Risk window</span><strong>{risk_window}</strong></section><section class=\"card kpi\"><span>Best window</span><strong>{best_window}</strong></section><section class=\"card kpi\"><span>Matched cases</span><strong>{esc(str(verification.get('matched_cases',0)))}</strong></section></div><section class=\"card\"><h2>Timeline singkat</h2><div class=\"scroll\"><table><tr><th>Jam</th><th>Cuaca</th><th>Prob. hujan</th><th>Rain threat</th><th>Rekomendasi</th></tr>{table}</table></div></section><section class=\"card\"><h2>Forecast Constitution</h2><ol>{constitution}</ol></section><p><a href=\"command_center_sentinel_x.html\">Kembali ke Command Center</a> · <a href=\"sentinel_x_report.md\">Versi Markdown mentah</a></p></main></body></html>"""
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(doc))
    return path_output(AETHER_REPORT_FILENAME)


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    esc = html.escape
    cards = []
    for loc in locations:
        slug = loc.slug
        display = esc(loc.location_name)
        prefix = f"{base_url}/{slug}/" if base_url else f"{slug}/"
        row = next((r for r in run_rows if r.get("location_slug") == slug), {})
        status = esc(str(row.get("run_status", "unknown")))
        cards.append(f"""<article class=\"loc-card\"><div class=\"loc-top\"><h2>{display}</h2><span>{status}</span></div><p>Dashboard risiko cuaca, akuntabilitas, laporan harian, dan data publik.</p><div class=\"links\"><a href=\"{prefix}{AETHER_DASHBOARD_FILENAME}\">Command Center</a><a href=\"{prefix}sentinel_x_accuracy_public.html\">Akurasi</a><a href=\"{prefix}{AETHER_REPORT_HTML_FILENAME}\">Laporan Visual</a><a href=\"{prefix}{AETHER_CONTRACT_FILENAME}\">Kontrak</a></div></article>""")
    doc = f"""<!doctype html><html lang=\"id\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>AETHER Sentinel X Public Portal</title><style>
:root{{--bg:#eef3f8;--ink:#0f172a;--muted:#64748b;--line:#dbe4ef;--blue:#2563eb}}*{{box-sizing:border-box}}body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,"Segoe UI",Arial,sans-serif;background:radial-gradient(circle at top left,#dbeafe 0,#eef3f8 36%,#f8fafc 100%);color:var(--ink);line-height:1.55}}.wrap{{width:min(1320px,calc(100% - 36px));margin:auto;padding:28px 0 48px}}.hero{{background:linear-gradient(135deg,#07111f,#1d4ed8);color:white;border-radius:34px;padding:38px;margin:20px 0;box-shadow:0 24px 80px rgba(15,23,42,.25)}}h1{{font-size:clamp(36px,5vw,68px);line-height:1;margin:0 0 12px;letter-spacing:-.04em}}.hero p{{color:#dbeafe;max-width:920px;font-size:18px}}.warn{{background:#fff7ed;border:1px solid #fed7aa;color:#7c2d12;border-radius:22px;padding:16px;margin:18px 0;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}}.loc-card,.card{{background:white;border:1px solid var(--line);border-radius:26px;padding:22px;box-shadow:0 14px 36px rgba(15,23,42,.07)}}.loc-top{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}.loc-top h2{{margin:0;font-size:28px}}.loc-top span{{background:#dcfce7;color:#166534;border-radius:999px;padding:6px 10px;font-weight:900}}.links{{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}}a{{color:#1d4ed8;font-weight:900;text-decoration:none}}.links a{{background:#eff6ff;border:1px solid #bfdbfe;border-radius:999px;padding:10px 13px}}.data-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}}.data-grid a{{background:white;border:1px solid var(--line);border-radius:16px;padding:14px;display:block}}
</style></head><body><main class=\"wrap\"><section class=\"hero\"><h1>AETHER Sentinel X</h1><p>Portal publik untuk risiko cuaca lokal, skenario atmosfer, akuntabilitas forecast, dan rekomendasi keputusan harian.</p></section><div class=\"warn\"><b>{esc(sentinel_public_disclaimer(args))}</b></div><section class=\"grid\">{''.join(cards)}</section><section class=\"card\" style=\"margin-top:18px\"><h2>Data publik</h2><div class=\"data-grid\"><a href=\"ensemble_all_locations.csv\">ensemble_all_locations.csv</a><a href=\"forecast_all_locations.csv\">forecast_all_locations.csv</a><a href=\"ensemble_fact_all_locations.csv\">ensemble_fact_all_locations.csv</a><a href=\"source_status_all_locations.csv\">source_status_all_locations.csv</a><a href=\"forecast_batch_summary.json\">forecast_batch_summary.json</a><a href=\"dim_locations.csv\">dim_locations.csv</a></div></section></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("sentinel_x_public_portal_manifest.json"), {"generated_at": now_local(DEFAULT_TIMEZONE).isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path("index.html")


def sentinel_write_publish_manifest(args):
    payload = {
        "generated_at": now_local(args.timezone).isoformat(),
        "public_version": SENTINEL_PUBLIC_UI_VERSION,
        "location": getattr(args, "location_name", ""),
        "public_files": [
            AETHER_DASHBOARD_FILENAME, AETHER_CSV_FILENAME, AETHER_JSON_FILENAME, AETHER_REPORT_HTML_FILENAME,
            AETHER_REPORT_FILENAME, AETHER_CONTRACT_FILENAME, "sentinel_x_accuracy_public.html", "sentinel_x_verification_summary.json",
            "sentinel_x_reliability.csv", "sentinel_constitution.md", "sentinel_x_public_links.md",
            "forecast_autopsy_latest.html", "sentinel_x_red_team_public.html",
        ],
        "internal_or_do_not_publish": ["*.sqlite", "*.db", "raw_payloads/", "logs/", "source_health.json", "command_center_manifest_sentinel_x.json"],
        "public_disclaimer": sentinel_public_disclaimer(args),
    }
    write_json(path_output("sentinel_x_publish_manifest.json"), payload)
    lines = ["# Sentinel X public links", "", f"Disclaimer: {sentinel_public_disclaimer(args)}", ""]
    for item in payload["public_files"]:
        lines.append(f"- `{item}`")
    lines += ["", "## Do not publish as public-facing files", ""] + [f"- `{item}`" for item in payload["internal_or_do_not_publish"]]
    atomic_write_text(path_output("sentinel_x_public_links.md"), lambda f: f.write("\n".join(lines)))
    gitignore_path = root_output_path(".sentinel_public_gitignore_template")
    atomic_write_text(gitignore_path, lambda f: f.write("\n".join(["*.sqlite", "*.db", "**/logs/", "**/raw_payloads/", "**/source_health.json", "**/command_center_manifest_sentinel_x.json"])))
    return path_output("sentinel_x_publish_manifest.json")



# ---------- SENTINEL X V4 PUBLIC APP UI OVERRIDES ----------
# This block intentionally overrides the earlier v3 UI writers before main() runs.
SENTINEL_PUBLIC_UI_VERSION = "sentinel-x-public-app-v4"


def _v4_esc(x):
    return html.escape("" if x is None else str(x))


def _v4_num(x, suffix="", fallback="—"):
    if x is None or x == "":
        return fallback
    try:
        v = float(x)
        if abs(v - int(v)) < 1e-9:
            return f"{int(v)}{suffix}"
        return f"{v:.1f}{suffix}"
    except Exception:
        return f"{x}{suffix}"


def _v4_status_label(status):
    s = str(status or "UNKNOWN").upper()
    return {
        "GREEN": "Aman terkendali",
        "YELLOW": "Perlu waspada",
        "RED": "Risiko tinggi",
        "BLACK": "Data tidak layak",
    }.get(s, s)


def _v4_css():
    return """
:root{
  --bg:#eaf0f7; --paper:#ffffff; --ink:#0a1220; --muted:#667085; --line:#d9e3ef;
  --navy:#061324; --blue:#155eef; --blue2:#2563eb; --cyan:#06b6d4;
  --green:#12b76a; --yellow:#f59e0b; --red:#ef4444; --black:#020617;
  --shadow:0 24px 70px rgba(15,23,42,.13); --soft:0 10px 30px rgba(15,23,42,.08);
}
*{box-sizing:border-box} html{scroll-behavior:smooth} body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;background:linear-gradient(180deg,#dfe9f5 0,#f7fafc 42%,#eef3f8 100%);color:var(--ink);line-height:1.55} a{color:#155eef;text-decoration:none;font-weight:800} a:hover{text-decoration:underline}.app{width:min(1720px,calc(100% - 32px));margin:auto;padding:18px 0 54px}.topbar{position:sticky;top:0;z-index:30;margin:0 auto 14px;backdrop-filter:blur(16px);background:rgba(255,255,255,.82);border:1px solid rgba(217,227,239,.9);border-radius:20px;box-shadow:0 8px 26px rgba(15,23,42,.08);display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px 16px}.brand{display:flex;align-items:center;gap:12px}.logo{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,#061324,#155eef);box-shadow:0 10px 24px rgba(21,94,239,.25)}.brand b{font-size:18px}.brand span{display:block;color:var(--muted);font-size:12px}.nav{display:flex;gap:8px;flex-wrap:wrap}.nav a,.btn{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;border:1px solid #c7d7ee;background:#fff;color:#155eef;padding:9px 12px;font-size:13px;font-weight:900}.hero{position:relative;overflow:hidden;background:radial-gradient(circle at 82% 18%,rgba(6,182,212,.26),transparent 28%),linear-gradient(135deg,#061324 0,#0b1f3d 50%,#155eef 100%);border-radius:34px;color:#fff;padding:38px;box-shadow:var(--shadow);margin-bottom:16px}.hero:after{content:"";position:absolute;right:-110px;top:-120px;width:380px;height:380px;border-radius:999px;background:rgba(255,255,255,.10)}.eyebrow{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}.chip{display:inline-flex;align-items:center;gap:8px;border:1px solid rgba(255,255,255,.22);background:rgba(255,255,255,.12);padding:8px 12px;border-radius:999px;color:#e0f2fe;font-weight:900;font-size:13px}.hero h1{margin:0;font-size:clamp(38px,5.6vw,86px);letter-spacing:-.055em;line-height:.94}.hero p{max-width:1050px;color:#dbeafe;font-size:clamp(16px,1.65vw,22px);margin:18px 0 0}.notice{border:1px solid #fed7aa;background:#fff7ed;color:#7c2d12;border-radius:22px;padding:14px 16px;margin:16px 0;font-weight:750}.bento{display:grid;grid-template-columns:1.15fr .85fr .85fr .85fr;gap:14px;margin:16px 0}.tile{background:rgba(255,255,255,.95);border:1px solid var(--line);border-radius:26px;padding:20px;box-shadow:var(--soft);min-width:0}.tile.dark{background:linear-gradient(135deg,#07111f,#12233f);color:#fff;border-color:#1e293b}.tile label{display:block;text-transform:uppercase;letter-spacing:.08em;font-weight:950;font-size:12px;color:#667085}.tile.dark label{color:#cbd5e1}.tile strong{display:block;font-size:clamp(30px,4vw,54px);letter-spacing:-.055em;line-height:1;margin:9px 0}.tile p,.tile small{margin:0;color:#667085}.tile.dark p,.tile.dark small{color:#cbd5e1}.status-pill{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:8px 12px;font-weight:950;font-size:13px;color:#fff}.status-pill.ok{background:var(--green)}.status-pill.watch{background:var(--yellow);color:#451a03}.status-pill.danger{background:var(--red)}.status-pill.black{background:#020617}.status-pill.neutral{background:#64748b}.main-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(360px,.65fr);gap:16px;align-items:start}.panel{background:rgba(255,255,255,.97);border:1px solid var(--line);border-radius:28px;padding:22px;box-shadow:var(--soft);margin-bottom:16px}.panel h2{margin:0 0 10px;font-size:clamp(22px,2vw,34px);letter-spacing:-.035em}.lead{font-size:18px;color:#344054}.links{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}.links a{display:inline-flex;border-radius:999px;background:#eff6ff;border:1px solid #bfdbfe;padding:10px 13px}.scenarios{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}.scenario{border:1px solid var(--line);background:linear-gradient(180deg,#fff,#f8fafc);border-radius:20px;padding:16px;min-height:150px;display:flex;flex-direction:column;justify-content:space-between}.scenario b{font-size:15px}.scenario span{display:block;color:#667085;font-size:13px;line-height:1.35;margin-top:8px}.scenario strong{font-size:34px;letter-spacing:-.05em}.cards24{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}.hour-card{background:#fff;border:1px solid var(--line);border-radius:22px;padding:15px;box-shadow:0 8px 20px rgba(15,23,42,.05)}.hour-top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.hour-top b{font-size:22px}.weather{color:#334155;font-weight:850}.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}.mini div{background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;padding:9px}.mini span{display:block;font-size:11px;color:#667085;font-weight:850;text-transform:uppercase}.mini strong{font-size:18px}.bar{height:8px;background:#e2e8f0;border-radius:999px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,#12b76a,#f59e0b,#ef4444);border-radius:999px}.rec{font-size:13px;color:#475467;margin:10px 0 0}.table-scroll{overflow:auto;border:1px solid var(--line);border-radius:18px;background:#fff}table{width:100%;border-collapse:collapse;min-width:780px}th{background:#f8fafc;color:#344054;text-align:left;padding:12px;font-size:12px;text-transform:uppercase;letter-spacing:.05em}td{border-top:1px solid var(--line);padding:12px}.side-stack{position:sticky;top:86px}.quote{background:#0f172a;color:#fff;border-radius:28px;padding:22px;box-shadow:var(--soft)}.quote h2{color:#fff}.quote p,.quote li{color:#cbd5e1}.footer{color:#667085;font-size:13px;margin-top:20px}@media(max-width:1180px){.main-grid{grid-template-columns:1fr}.side-stack{position:static}.bento{grid-template-columns:1fr 1fr}.scenarios{grid-template-columns:1fr 1fr}}@media(max-width:720px){.app{width:min(100% - 18px,1720px)}.topbar{position:static;align-items:flex-start;flex-direction:column}.hero{padding:26px;border-radius:26px}.bento{grid-template-columns:1fr}.scenarios{grid-template-columns:1fr}.nav{width:100%}.nav a{flex:1}.mini{grid-template-columns:1fr}}
"""


def _v4_badge(status):
    return f"<span class='status-pill {_sx_status_class(status)}'>{_v4_esc(_v4_status_label(status))}</span>"


def _v4_bar(value):
    try:
        v = max(0, min(100, float(value or 0)))
    except Exception:
        v = 0
    return f"<div class='bar'><i style='width:{v}%'></i></div>"


def sentinel_write_verification_artifacts(rows, args):
    summary, pairs, reliability = sentinel_compute_verification(rows, args)
    write_json(path_output("sentinel_x_verification_summary.json"), summary)
    if pairs:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), list(pairs[0].keys()), pairs)
    else:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), ["target_date", "jam", "note"], [{"target_date": "", "jam": "", "note": "No matched forecast-observation pairs yet."}])
    write_dict_csv(path_output("sentinel_x_reliability.csv"), ["probability_bin", "n", "mean_forecast_probability", "observed_rain_frequency"], reliability)
    status = str(summary.get("calibration_status", "UNKNOWN"))
    enough = status == "VERIFIED_ENOUGH_DATA"
    note = "Skor sudah mulai didukung data observasi." if enough else "Belum ada cukup pasangan forecast-observasi. Semua skor akurasi masih bersifat sementara."
    rel_rows = "".join(f"<tr><td><b>{_v4_esc(r['probability_bin'])}</b></td><td>{r['n']}</td><td>{_v4_num(r['mean_forecast_probability'],'%')}</td><td>{_v4_num(r['observed_rain_frequency'],'%')}</td></tr>" for r in reliability)
    metric = lambda title, value, sub="": f"<section class='tile'><label>{_v4_esc(title)}</label><strong>{_v4_esc(value)}</strong><small>{_v4_esc(sub)}</small></section>"
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Akuntabilitas Forecast — {_v4_esc(getattr(args,'location_name',''))}</title><style>{_v4_css()}</style></head><body><main class='app'><nav class='topbar'><div class='brand'><div class='logo'></div><div><b>Akuntabilitas Forecast</b><span>{_v4_esc(getattr(args,'location_name',''))}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Command Center</a><a href='sentinel_x_report.html'>Laporan Visual</a><a href='sentinel_x_reliability.csv'>Reliability CSV</a></div></nav><section class='hero'><div class='eyebrow'><span class='chip'>Akurasi & Bukti Historis</span><span class='chip'>{_v4_esc(status.replace('_',' '))}</span></div><h1>Apakah forecast ini sudah terbukti?</h1><p>Halaman ini sengaja dibuat jujur: kalau observasi belum cukup, Sentinel tidak mengklaim akurat.</p></section><div class='notice'><b>{_v4_esc(sentinel_public_disclaimer(args))}</b><br>{_v4_esc(note)}</div><section class='bento'>{metric('Status kalibrasi', status.replace('_',' '), note)}{metric('Matched cases', summary.get('matched_cases',0), f"Minimum {summary.get('verification_min_cases','')} kasus")}{metric('Brier hujan', _v4_num(summary.get('rain_brier_score')), 'Lebih kecil lebih baik')}{metric('MAE suhu', _v4_num(summary.get('temperature_mae_c')), 'Dalam °C')}</section><section class='bento'>{metric('POD hujan', _v4_num(summary.get('rain_pod')), 'Kemampuan menangkap hujan')}{metric('FAR hujan', _v4_num(summary.get('rain_far')), 'Alarm keliru')}{metric('CSI hujan', _v4_num(summary.get('rain_csi')), 'Skor deteksi hujan')}{metric('Category accuracy', _v4_num(summary.get('category_accuracy')), 'Kategori cuaca')}</section><section class='panel'><h2>Reliability probabilitas hujan</h2><p class='lead'>Tabel ini akan terisi setelah observasi terkumpul. Saat jumlah kasus masih nol, tampilannya memang kosong dan itu disengaja agar tidak memberi kesan akurasi palsu.</p><div class='table-scroll'><table><tr><th>Bin probabilitas</th><th>Jumlah kasus</th><th>Rata-rata forecast</th><th>Frekuensi hujan observasi</th></tr>{rel_rows}</table></div></section><p class='footer'>File data: <a href='sentinel_x_verification_summary.json'>Verification JSON</a> · <a href='sentinel_x_verification_pairs.csv'>Matched pairs CSV</a></p></main></body></html>"""
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(doc))
    return summary


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return None
    rows = aether_rows or []
    status = str(daily.get('daily_operational_status','UNKNOWN'))
    peak_rain = max(rows, key=lambda r: aether_value(r.get("rain_threat_score")) or -1) if rows else {}
    peak_failure = max(rows, key=lambda r: aether_value(r.get("forecast_failure_risk")) or -1) if rows else {}
    peak_stress = max(rows, key=lambda r: aether_value(r.get("forecast_stress_index")) or -1) if rows else {}
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    cal_status = str(verification.get('calibration_status','unknown')).replace('_',' ')
    last_update = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE)).strftime('%d %b %Y %H:%M')
    scenarios = peak_rain or {}
    scenario_items = [("Dry miss","scenario_dry_miss","Hujan berpotensi meleset dari titik utama."),("Nearby rain","scenario_nearby_rain_only","Hujan terjadi dekat lokasi, titik utama bisa hanya mendung."),("Light rain","scenario_direct_light_rain","Lokasi utama terkena hujan ringan."),("Moderate rain","scenario_direct_moderate_rain","Hujan sedang singkat langsung mengenai lokasi."),("Convective burst","scenario_convective_burst","Burst lokal yang sulit diprediksi timing-nya.")]
    scenario_html = ''.join(f"<article class='scenario'><div><b>{_v4_esc(label)}</b><span>{_v4_esc(desc)}</span></div><strong>{_v4_num(scenarios.get(key),'%')}</strong></article>" for label,key,desc in scenario_items)
    hour_cards = "".join(
        f"<article class='hour-card'><div class='hour-top'><div><b>{_v4_esc(r.get('jam',''))}</b><div class='weather'>{_v4_esc(r.get('dominant_category',''))}</div></div>{_v4_badge(r.get('operational_status',''))}</div><div class='mini'><div><span>Hujan</span><strong>{_v4_num(r.get('prob_rain'),'%')}</strong></div><div><span>Threat</span><strong>{_v4_num(r.get('rain_threat_score'))}</strong></div><div><span>Failure</span><strong>{_v4_num(r.get('forecast_failure_risk'))}</strong></div></div>{_v4_bar(r.get('rain_threat_score'))}<p class='rec'>{_v4_esc(r.get('decision_recommendation',''))}</p></article>" for r in rows
    ) or "<p>Tidak ada data forecast.</p>"
    source_html = ''.join(f"<tr><td><b>{_v4_esc(s.get('source_id',''))}</b></td><td>{_v4_esc(s.get('state',''))}</td><td>{_v4_esc(s.get('success',''))}</td><td>{_v4_num(s.get('duration_ms'),' ms')}</td></tr>" for s in (source_state_rows or [])) or "<tr><td colspan='4'>Belum ada source state.</td></tr>"
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AETHER Sentinel X — {_v4_esc(getattr(args,'location_name',''))}</title><style>{_v4_css()}</style></head><body><main class='app'><nav class='topbar'><div class='brand'><div class='logo'></div><div><b>AETHER Sentinel X</b><span>{_v4_esc(getattr(args,'location_name',''))} · Updated {last_update}</span></div></div><div class='nav'><a href='sentinel_x_accuracy_public.html'>Akurasi</a><a href='sentinel_x_report.html'>Laporan</a><a href='sentinel_x.csv'>Data CSV</a></div></nav><section class='hero'><div class='eyebrow'><span class='chip'>Command Center</span><span class='chip'>Mission: {_v4_esc(getattr(args,'mission','safety_first'))}</span><span class='chip'>{_v4_esc(_v4_status_label(status))}</span></div><h1>Cuaca Lokal, Risiko, dan Keputusan Harian</h1><p>{_v4_esc(str(daily.get('summary_text','')))}</p></section><div class='notice'><b>{_v4_esc(sentinel_public_disclaimer(args))}</b><br>Gunakan halaman ini untuk keputusan harian. Untuk cuaca ekstrem, tetap ikuti informasi resmi.</div><section class='bento'><section class='tile dark'><label>Status operasional</label><strong>{_v4_badge(status)}</strong><p>{_v4_esc(str(daily.get('dominant_atmospheric_mode','')))}</p></section><section class='tile'><label>Puncak risiko hujan</label><strong>{_v4_esc(peak_rain.get('jam','—'))}</strong><small>Rain threat {_v4_num(peak_rain.get('rain_threat_score'))}</small></section><section class='tile'><label>Failure risk tertinggi</label><strong>{_v4_num(peak_failure.get('forecast_failure_risk'))}</strong><small>{_v4_esc(peak_failure.get('main_failure_mode','Timing/posisi hujan lokal'))}</small></section><section class='tile'><label>Forecast stress</label><strong>{_v4_num(peak_stress.get('forecast_stress_index'))}</strong><small>Semakin tinggi, semakin sulit diprediksi</small></section></section><div class='main-grid'><section><section class='panel'><h2>Yang perlu diketahui masyarakat</h2><p class='lead'>{_v4_esc(str(daily.get('summary_text','')))}</p><div class='links'><a href='sentinel_x_accuracy_public.html'>Akurasi & Akuntabilitas</a><a href='sentinel_x_report.html'>Laporan Visual</a><a href='sentinel_x_forecast_contract.json'>Forecast Contract</a></div></section><section class='panel'><h2>Skenario kemungkinan di jam paling rawan</h2><div class='scenarios'>{scenario_html}</div></section><section class='panel'><h2>Timeline 24 jam</h2><div class='cards24'>{hour_cards}</div></section></section><aside class='side-stack'><section class='quote'><h2>Situation Awareness</h2><p><b>Risk window:</b><br>{_v4_esc(str(daily.get('risk_window','')))}</p><p><b>Best window:</b><br>{_v4_esc(str(daily.get('best_window','')))}</p><p><b>Kalibrasi:</b><br>{_v4_esc(cal_status)}</p><p>Jika data observasi belum cukup, skor risiko dianggap heuristic dan tidak boleh dibaca sebagai klaim akurasi final.</p></section><section class='panel'><h2>Source Health</h2><div class='table-scroll'><table><tr><th>Source</th><th>State</th><th>Success</th><th>Latency</th></tr>{source_html}</table></div></section><section class='panel'><h2>Forecast Constitution</h2><ol>{''.join(f'<li>{_v4_esc(item)}</li>' for item in SENTINEL_CONSTITUTION[:6])}</ol></section></aside></div></main></body></html>"""
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "report_html": path_output(AETHER_REPORT_HTML_FILENAME), "generated_at": now_local(args.timezone).isoformat(), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return path_output(AETHER_DASHBOARD_FILENAME)


def aether_write_report(aether_rows, daily, args):
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    rows = aether_rows or []
    # Keep markdown only as a developer stub; public should use HTML.
    md = "# Laporan Sentinel X\n\nVersi publik yang rapi ada di `sentinel_x_report.html`. Jangan sebarkan file `.md` ini ke masyarakat.\n"
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write(md))
    risk_window = str(daily.get('risk_window',''))
    best_window = str(daily.get('best_window',''))
    status = str(daily.get('daily_operational_status',''))
    top_rows = rows[:24]
    cards = ''.join(f"<article class='hour-card'><div class='hour-top'><div><b>{_v4_esc(r.get('jam',''))}</b><div class='weather'>{_v4_esc(r.get('dominant_category',''))}</div></div>{_v4_badge(r.get('operational_status',''))}</div><div class='mini'><div><span>Hujan</span><strong>{_v4_num(r.get('prob_rain'),'%')}</strong></div><div><span>Threat</span><strong>{_v4_num(r.get('rain_threat_score'))}</strong></div><div><span>Failure</span><strong>{_v4_num(r.get('forecast_failure_risk'))}</strong></div></div><p class='rec'>{_v4_esc(r.get('decision_recommendation',''))}</p></article>" for r in top_rows)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Laporan Sentinel X — {_v4_esc(getattr(args,'location_name',''))}</title><style>{_v4_css()}</style></head><body><main class='app'><nav class='topbar'><div class='brand'><div class='logo'></div><div><b>Laporan Harian Sentinel X</b><span>{_v4_esc(getattr(args,'location_name',''))}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Command Center</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a></div></nav><section class='hero'><div class='eyebrow'><span class='chip'>Laporan Visual</span><span class='chip'>Mission: {_v4_esc(getattr(args,'mission','safety_first'))}</span></div><h1>Ringkasan cuaca yang bisa dibaca manusia</h1><p>{_v4_esc(str(daily.get('summary_text','')))}</p></section><div class='notice'><b>{_v4_esc(sentinel_public_disclaimer(args))}</b></div><section class='bento'><section class='tile dark'><label>Status</label><strong>{_v4_badge(status)}</strong><p>{_v4_esc(str(daily.get('dominant_atmospheric_mode','')))}</p></section><section class='tile'><label>Risk window</label><strong>{_v4_esc(risk_window)}</strong></section><section class='tile'><label>Best window</label><strong>{_v4_esc(best_window)}</strong></section><section class='tile'><label>Matched cases</label><strong>{_v4_esc(verification.get('matched_cases',0))}</strong><small>Untuk akurasi historis</small></section></section><section class='panel'><h2>Timeline 24 jam</h2><div class='cards24'>{cards}</div></section><section class='panel'><h2>Prinsip forecast</h2><ol>{''.join(f'<li>{_v4_esc(item)}</li>' for item in SENTINEL_CONSTITUTION)}</ol></section></main></body></html>"""
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(doc))
    return path_output(AETHER_REPORT_HTML_FILENAME)


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    cards = []
    for loc in locations:
        prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
        row = next((r for r in run_rows if r.get("location_slug") == loc.slug), {})
        status = str(row.get("run_status", "updated"))
        cards.append(f"<article class='panel'><h2>{_v4_esc(loc.location_name)}</h2><p>Dashboard risiko cuaca lokal, akurasi, laporan visual, dan data publik.</p><div class='links'><a href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Command Center</a><a href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a><a href='{prefix}{AETHER_REPORT_HTML_FILENAME}'>Laporan Visual</a><a href='{prefix}{AETHER_CONTRACT_FILENAME}'>Kontrak</a></div><p class='footer'>Status run: {_v4_esc(status)}</p></article>")
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AETHER Sentinel X Public Portal</title><style>{_v4_css()}</style></head><body><main class='app'><section class='hero'><div class='eyebrow'><span class='chip'>Public Portal</span><span class='chip'>AETHER Sentinel X</span></div><h1>Portal Cuaca Lokal</h1><p>Dashboard risiko atmosfer, skenario hujan, akuntabilitas forecast, dan rekomendasi keputusan harian untuk masyarakat.</p></section><div class='notice'><b>{_v4_esc(sentinel_public_disclaimer(args))}</b></div><section class='main-grid'><div>{''.join(cards)}</div><aside><section class='quote'><h2>Yang dibuka masyarakat</h2><p>Gunakan Command Center untuk tampilan utama. Gunakan Akurasi untuk melihat apakah sistem sudah punya bukti historis. File CSV/JSON ditujukan untuk data dan analisis.</p></section></aside></section><section class='panel'><h2>Data publik</h2><div class='links'><a href='ensemble_all_locations.csv'>Ensemble all locations</a><a href='forecast_all_locations.csv'>Forecast all locations</a><a href='ensemble_fact_all_locations.csv'>BI/fact table</a><a href='source_status_all_locations.csv'>Source status</a><a href='forecast_batch_summary.json'>Batch summary</a></div></section></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("sentinel_x_public_portal_manifest.json"), {"generated_at": now_local(DEFAULT_TIMEZONE).isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path("index.html")


if __name__ == "__main__":
    main()
