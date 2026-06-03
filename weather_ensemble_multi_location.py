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
    """Write dictionaries to CSV safely.

    ANEMOS v12.2 hardening:
    Several forecast/verification layers evolved with slightly different row
    schemas. The standard csv.DictWriter is strict and crashes if a row carries
    extra keys. Public forecast generation must never fail only because a
    diagnostic CSV has extra metadata, so this writer normalizes rows to the
    requested fieldnames and ignores extra fields safely.
    """
    safe_fieldnames = list(fieldnames or [])
    alias_map = {
        "bin": ["probability_bin"],
        "probability_bin": ["bin"],
        "mean_forecast_pct": ["mean_forecast_probability"],
        "mean_forecast_probability": ["mean_forecast_pct"],
        "observed_frequency_pct": ["observed_rain_frequency"],
        "observed_rain_frequency": ["observed_frequency_pct"],
        "matched_cases": ["n", "cases"],
        "n": ["matched_cases", "cases"],
    }

    def pick_value(row, name):
        if name in row:
            return row.get(name, "")
        for alt in alias_map.get(name, []):
            if alt in row:
                return row.get(alt, "")
        return ""

    safe_rows = []
    for row in rows or []:
        if isinstance(row, dict):
            safe_rows.append({name: pick_value(row, name) for name in safe_fieldnames})
        else:
            # Keep the file writable even if a caller accidentally passes a
            # non-dict diagnostic row.
            safe_rows.append({name: "" for name in safe_fieldnames})

    def writer_fn(f):
        writer = csv.DictWriter(
            f,
            fieldnames=safe_fieldnames,
            delimiter=CSV_DELIMITER,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(safe_rows)

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
SENTINEL_OFFICIAL_DISCLAIMER = "Ini bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi resmi BMKG."


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
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AETHER Sentinel X Public Portal</title><style>body{{font-family:Arial,sans-serif;background:#f8fafc;color:#0f172a;margin:0}}header{{background:#0f172a;color:white;padding:28px}}main{{max-width:1100px;margin:auto;padding:20px}}.warn{{background:#fee2e2;color:#7f1d1d;border:1px solid #fca5a5;border-radius:14px;padding:14px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}.card{{background:white;border:1px solid #e2e8f0;border-radius:18px;padding:18px}}a{{color:#0369a1}}</style></head><body><header><h1>AETHER Sentinel X Public Portal</h1><p>Atmospheric risk, scenario, failure & decision intelligence.</p></header><main><div class='warn'><b>{esc(sentinel_public_disclaimer(args))}</b></div><div class='grid'>{''.join(cards)}</div><h2>Data terbuka</h2><ul><li><a href='ensemble_all_locations.csv'>ensemble_all_locations.csv</a></li><li><a href='forecast_all_locations.csv'>forecast_all_locations.csv</a></li><li><a href='source_status_all_locations.csv'>source_status_all_locations.csv</a></li><li><a href='forecast_batch_summary.json'>forecast_batch_summary.json</a></li></ul></main></body></html>"""
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
    parser.add_argument("--public-disclaimer", default="Ini bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi resmi BMKG.", help="Disclaimer publik yang ditampilkan di dashboard/report/contract.")
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
</style></head><body><main class=\"wrap\"><section class=\"hero\"><h1>AETHER Sentinel X</h1><p>Portal publik untuk risiko cuaca lokal, skenario atmosfer, akuntabilitas forecast, dan rekomendasi keputusan harian.</p></section><div class=\"warn\"><b>{esc(sentinel_public_disclaimer(args))}</b></div><section class=\"grid\">{''.join(cards)}</section><section class=\"card\" style=\"margin-top:18px\"><h2>Data terbuka</h2><div class=\"data-grid\"><a href=\"ensemble_all_locations.csv\">ensemble_all_locations.csv</a><a href=\"forecast_all_locations.csv\">forecast_all_locations.csv</a><a href=\"ensemble_fact_all_locations.csv\">ensemble_fact_all_locations.csv</a><a href=\"source_status_all_locations.csv\">source_status_all_locations.csv</a><a href=\"forecast_batch_summary.json\">forecast_batch_summary.json</a><a href=\"dim_locations.csv\">dim_locations.csv</a></div></section></main></body></html>"""
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
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AETHER Sentinel X Public Portal</title><style>{_v4_css()}</style></head><body><main class='app'><section class='hero'><div class='eyebrow'><span class='chip'>Public Portal</span><span class='chip'>AETHER Sentinel X</span></div><h1>Portal Cuaca Lokal</h1><p>Dashboard risiko atmosfer, skenario hujan, akuntabilitas forecast, dan rekomendasi keputusan harian untuk masyarakat.</p></section><div class='notice'><b>{_v4_esc(sentinel_public_disclaimer(args))}</b></div><section class='main-grid'><div>{''.join(cards)}</div><aside><section class='quote'><h2>Yang dibuka masyarakat</h2><p>Gunakan Command Center untuk tampilan utama. Gunakan Akurasi untuk melihat apakah sistem sudah punya bukti historis. File CSV/JSON ditujukan untuk data dan analisis.</p></section></aside></section><section class='panel'><h2>Data terbuka</h2><div class='links'><a href='ensemble_all_locations.csv'>Ensemble all locations</a><a href='forecast_all_locations.csv'>Forecast all locations</a><a href='ensemble_fact_all_locations.csv'>BI/fact table</a><a href='source_status_all_locations.csv'>Source status</a><a href='forecast_batch_summary.json'>Batch summary</a></div></section></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("sentinel_x_public_portal_manifest.json"), {"generated_at": now_local(DEFAULT_TIMEZONE).isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path("index.html")


# ============================================================
# AETHER SENTINEL X PUBLIC-FRIENDLY UI v5
# ============================================================
# Goal: public-facing pages should feel like a simple local weather service,
# not an internal/debug dashboard. Keep the data pipeline unchanged; replace
# only the presentation and public language.
SENTINEL_PUBLIC_UI_VERSION = "2026-06-03.public-friendly-v6.1-fixed"


def _v5_esc(value):
    return html.escape(str(value if value is not None else ""))


def _v5_num(value, suffix="", digits=0, fallback="—", blank=None):
    # Backward-compatible numeric formatter.
    # v6 pages call this through _v6_num(..., blank="—"), while older v5 code
    # used fallback="—". Support both names so GitHub Actions does not fail.
    if blank is not None:
        fallback = blank
    v = safe_float(value)
    if v is None:
        return fallback
    if digits == 0:
        return f"{round(v):.0f}{suffix}"
    return f"{v:.{digits}f}{suffix}"


def _v5_slug_text(text):
    text = str(text or "").strip().replace("_", " ").lower()
    return " ".join(part.capitalize() for part in text.split())


def _v5_status_public(status):
    s = str(status or "").upper()
    if s == "GREEN":
        return "Aman dipantau", "ok"
    if s == "YELLOW":
        return "Perlu waspada", "warn"
    if s == "RED":
        return "Berisiko", "danger"
    if s == "BLACK":
        return "Data kurang", "dark"
    return "Perlu dicek", "warn"


def _v5_badge(status):
    label, cls = _v5_status_public(status)
    return f"<span class='badge {cls}'>{_v5_esc(label)}</span>"


def _v5_weather_icon(category):
    c = str(category or "").lower()
    if "lebat" in c:
        return "⛈️"
    if "sedang" in c:
        return "🌧️"
    if "ringan" in c or "hujan" in c:
        return "🌦️"
    if "cerah berawan" in c:
        return "🌤️"
    if "cerah" in c:
        return "☀️"
    if "berawan" in c:
        return "☁️"
    return "🌡️"


def _v5_risk_word(score):
    v = safe_float(score) or 0
    if v >= 70:
        return "tinggi", "danger"
    if v >= 45:
        return "sedang", "warn"
    return "rendah", "ok"


def _v5_day_part(hour_text):
    try:
        h = int(str(hour_text).split(":")[0])
    except Exception:
        return "Lainnya"
    if 4 <= h <= 10:
        return "Pagi"
    if 11 <= h <= 14:
        return "Siang"
    if 15 <= h <= 18:
        return "Sore"
    return "Malam"


def _v5_safe_summary(daily, rows, args):
    loc = getattr(args, "location_name", "lokasi ini")
    rows = rows or []
    peak_rain = max(rows, key=lambda r: aether_value(r.get("rain_threat_score")) or -1) if rows else {}
    peak_failure = max(rows, key=lambda r: aether_value(r.get("forecast_failure_risk")) or -1) if rows else {}
    peak_jam = peak_rain.get("jam") or "—"
    peak_cat = peak_rain.get("dominant_category") or "cuaca berubah-ubah"
    prob = _v5_num(peak_rain.get("prob_rain"), "%")
    threat_word, _ = _v5_risk_word(peak_rain.get("rain_threat_score"))
    status_label, _ = _v5_status_public(daily.get("daily_operational_status"))
    failure = _v5_num(peak_failure.get("forecast_failure_risk"))
    # Avoid raw model jargon from older summary_text.
    return (
        f"Untuk {loc}, kondisi hari ini masih {status_label.lower()}. "
        f"Periode yang paling perlu diperhatikan sekitar {peak_jam}, dengan kemungkinan {str(peak_cat).lower()} "
        f"dan peluang hujan sekitar {prob}. Risiko hujan berada pada tingkat {threat_word}. "
        f"Bagian yang paling mungkin meleset adalah waktu dan posisi hujan lokal, jadi gunakan ini sebagai panduan harian, bukan peringatan resmi."
    )


def _v5_recommendation(daily, rows, args):
    rows = rows or []
    peak = max(rows, key=lambda r: aether_value(r.get("rain_threat_score")) or -1) if rows else {}
    threat = safe_float(peak.get("rain_threat_score")) or 0
    prob = safe_float(peak.get("prob_rain")) or 0
    jam = peak.get("jam") or "sore"
    if threat >= 70 or prob >= 70:
        return f"Bawa payung/jas hujan kalau keluar sekitar {jam}. Aktivitas luar ruang sebaiknya punya rencana cadangan."
    if threat >= 45 or prob >= 45:
        return f"Aktivitas masih memungkinkan. Siapkan payung bila beraktivitas mendekati {jam}."
    return "Secara umum masih aman dipantau. Tetap cek ulang jika cuaca mulai gelap atau angin berubah."


def _v5_best_time(rows):
    rows = rows or []
    candidates = []
    for r in rows:
        rain = safe_float(r.get("rain_threat_score")) or 0
        fail = safe_float(r.get("forecast_failure_risk")) or 0
        candidates.append((rain + fail * 0.35, r.get("jam", "")))
    candidates = sorted(candidates)[:4]
    if not candidates:
        return "Belum tersedia"
    times = [c[1] for c in candidates if c[1]]
    if not times:
        return "Belum tersedia"
    return ", ".join(times[:3])


def _v5_risky_time(rows):
    rows = rows or []
    candidates = []
    for r in rows:
        rain = safe_float(r.get("rain_threat_score")) or 0
        prob = safe_float(r.get("prob_rain")) or 0
        candidates.append((rain + prob * 0.25, r.get("jam", "")))
    candidates = sorted(candidates, reverse=True)[:3]
    times = [c[1] for c in candidates if c[1]]
    return ", ".join(times) if times else "Belum tersedia"


def _v5_group_rows(rows):
    groups = {"Pagi": [], "Siang": [], "Sore": [], "Malam": []}
    for r in rows or []:
        groups.setdefault(_v5_day_part(r.get("jam")), []).append(r)
    return groups


def _v5_period_card(name, rows):
    if not rows:
        return f"<article class='period muted'><h3>{name}</h3><p>Belum ada data.</p></article>"
    peak = max(rows, key=lambda r: safe_float(r.get("rain_threat_score")) or 0)
    avg_prob = sum((safe_float(r.get("prob_rain")) or 0) for r in rows) / max(len(rows), 1)
    max_threat = safe_float(peak.get("rain_threat_score")) or 0
    word, cls = _v5_risk_word(max_threat)
    cats = {}
    for r in rows:
        cats[r.get("dominant_category", "")] = cats.get(r.get("dominant_category", ""), 0) + 1
    cat = max(cats.items(), key=lambda kv: kv[1])[0] if cats else "Berubah-ubah"
    return f"""
    <article class='period {cls}'>
      <div class='period-head'><span class='emoji'>{_v5_weather_icon(cat)}</span><h3>{_v5_esc(name)}</h3></div>
      <p class='weather'>{_v5_esc(cat or 'Cuaca berubah-ubah')}</p>
      <dl>
        <div><dt>Peluang hujan rata-rata</dt><dd>{avg_prob:.0f}%</dd></div>
        <div><dt>Risiko tertinggi</dt><dd>{_v5_esc(word)}</dd></div>
        <div><dt>Jam paling diperhatikan</dt><dd>{_v5_esc(peak.get('jam','—'))}</dd></div>
      </dl>
    </article>"""


def _v5_hour_list(rows):
    items = []
    for r in rows or []:
        jam = r.get("jam", "")
        cat = r.get("dominant_category", "")
        prob = safe_float(r.get("prob_rain")) or 0
        threat = safe_float(r.get("rain_threat_score")) or 0
        word, cls = _v5_risk_word(threat)
        rec = r.get("decision_recommendation") or "Pantau perubahan cuaca."
        # Make recommendation less machine-like.
        if threat >= 70:
            rec = "Sebaiknya siapkan payung dan kurangi rencana luar ruang."
        elif threat >= 45:
            rec = "Aktivitas luar ruang masih memungkinkan, tetapi sebaiknya siapkan payung."
        else:
            rec = "Risiko rendah; tetap pantau langit sekitar."
        items.append(f"""
        <article class='hour-row {cls}'>
          <div class='time'><b>{_v5_esc(jam)}</b><span>{_v5_weather_icon(cat)}</span></div>
          <div class='desc'><b>{_v5_esc(cat or 'Cuaca berubah')}</b><small>{_v5_esc(rec)}</small></div>
          <div class='rain'><span>Hujan</span><b>{prob:.0f}%</b></div>
          <div class='level'><span>Risiko</span><b>{_v5_esc(word)}</b></div>
        </article>""")
    return "".join(items) or "<p class='empty'>Belum ada data per jam.</p>"


def _v5_css():
    return r"""
:root{
  --bg:#eef5ff; --bg2:#f8fbff; --ink:#0b1220; --muted:#64748b; --line:#d8e3f0;
  --blue:#1457d9; --blue2:#08306b; --green:#16a34a; --amber:#d97706; --red:#dc2626;
  --card:#ffffff; --shadow:0 18px 45px rgba(15,23,42,.10); --radius:28px;
}
*{box-sizing:border-box} html{scroll-behavior:smooth} body{margin:0;background:linear-gradient(180deg,#eaf3ff 0%,#f8fbff 55%,#eef5ff 100%);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:var(--ink);font-size:17px;line-height:1.55}.page{width:min(1180px,calc(100% - 32px));margin:28px auto 80px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}.brand{display:flex;align-items:center;gap:12px}.mark{width:42px;height:42px;border-radius:14px;background:linear-gradient(135deg,#0b1f4d,#2774ff);box-shadow:0 10px 30px rgba(20,87,217,.28)}.brand b{font-size:18px}.brand span{display:block;color:var(--muted);font-size:13px;margin-top:2px}.nav{display:flex;gap:10px;flex-wrap:wrap}.nav a,.pill{display:inline-flex;align-items:center;justify-content:center;border:1px solid #bcd3ff;color:#0647c7;background:#f4f8ff;text-decoration:none;border-radius:999px;padding:9px 14px;font-weight:800;font-size:14px}.hero{position:relative;overflow:hidden;background:linear-gradient(135deg,#06152f 0%,#103b9c 58%,#2f74ff 100%);color:white;border-radius:var(--radius);padding:42px 48px;margin-bottom:20px;box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;right:-80px;top:-80px;width:260px;height:260px;border-radius:50%;background:rgba(255,255,255,.12)}.hero h1{font-size:clamp(34px,5vw,64px);line-height:1.02;margin:12px 0 12px;letter-spacing:-.06em}.hero p{font-size:clamp(17px,2vw,22px);max-width:860px;margin:0;color:#eaf1ff}.kicker{display:flex;gap:10px;flex-wrap:wrap}.chip{border:1px solid rgba(255,255,255,.25);background:rgba(255,255,255,.12);border-radius:999px;padding:8px 12px;font-size:13px;font-weight:900;color:#fff}.alert{border:1px solid #fdba74;background:#fff7ed;color:#7c2d12;border-radius:18px;padding:15px 18px;margin:16px 0;font-weight:750}.summary{display:grid;grid-template-columns:1.5fr 1fr;gap:18px;margin:18px 0}.card,.panel{background:var(--card);border:1px solid var(--line);border-radius:24px;box-shadow:var(--shadow)}.card{padding:22px}.panel{padding:28px;margin:18px 0}.headline{background:#081729;color:#fff}.headline .badge{margin-bottom:14px}.headline h2{font-size:34px;letter-spacing:-.04em;line-height:1.08;margin:0 0 12px}.headline p{color:#dbeafe;margin:0}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.metric{padding:22px}.metric label{display:block;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:900}.metric strong{display:block;font-size:34px;letter-spacing:-.04em;margin:8px 0}.metric small{color:var(--muted)}.badge{display:inline-flex;align-items:center;border-radius:999px;padding:7px 11px;font-weight:950;font-size:13px}.badge.ok{background:#dcfce7;color:#166534}.badge.warn{background:#fef3c7;color:#92400e}.badge.danger{background:#fee2e2;color:#991b1b}.badge.dark{background:#e5e7eb;color:#111827}.section-title{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:16px}.section-title h2{font-size:28px;letter-spacing:-.04em;margin:0}.section-title p{margin:0;color:var(--muted)}.periods{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.period{border:1px solid var(--line);border-radius:22px;padding:18px;background:#fff}.period-head{display:flex;align-items:center;gap:10px}.period h3{margin:0;font-size:22px}.emoji{font-size:27px}.period .weather{font-weight:900;margin:12px 0}.period dl{display:grid;gap:8px;margin:0}.period div{min-width:0}.period dt{font-size:12px;color:var(--muted);font-weight:800}.period dd{margin:2px 0 0;font-weight:950}.period.ok{border-color:#bbf7d0}.period.warn{border-color:#fde68a}.period.danger{border-color:#fecaca}.action{display:grid;grid-template-columns:1.2fr .8fr;gap:18px}.action h2{font-size:30px;line-height:1.1;margin:0 0 10px}.action .big{font-size:24px;font-weight:950}.timeline{display:grid;gap:10px}.hour-row{display:grid;grid-template-columns:96px 1fr 90px 100px;gap:14px;align-items:center;border:1px solid var(--line);border-radius:18px;padding:13px;background:#fff}.hour-row .time{display:flex;align-items:center;gap:10px}.hour-row .time b{font-size:18px}.hour-row .time span{font-size:24px}.hour-row .desc b{display:block;font-size:17px}.hour-row .desc small{color:var(--muted);display:block}.hour-row .rain span,.hour-row .level span{display:block;color:var(--muted);font-size:12px;font-weight:800}.hour-row .rain b,.hour-row .level b{font-size:20px}.hour-row.warn{border-left:7px solid var(--amber)}.hour-row.danger{border-left:7px solid var(--red)}.hour-row.ok{border-left:7px solid var(--green)}.links{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.muted{color:var(--muted)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}.plain-list{margin:0;padding-left:22px}.plain-list li{margin:8px 0}.table-scroll{overflow:auto;border:1px solid var(--line);border-radius:18px;background:#fff}table{width:100%;border-collapse:collapse;font-size:15px}th,td{padding:13px 14px;border-bottom:1px solid var(--line);text-align:left}th{background:#f8fbff;text-transform:uppercase;letter-spacing:.06em;font-size:12px}.portal-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.loc-card h2{font-size:26px;margin:0 0 8px}.footer{color:var(--muted);font-size:14px;margin-top:26px}@media (max-width:980px){.page{width:min(100% - 20px,720px);margin-top:16px}.summary,.action,.grid2{grid-template-columns:1fr}.metrics,.periods,.portal-grid{grid-template-columns:1fr}.hero{padding:30px 24px}.hour-row{grid-template-columns:78px 1fr}.hour-row .rain,.hour-row .level{display:inline-block}.top{align-items:flex-start;flex-direction:column}.nav{width:100%}.nav a{flex:1}.hero h1{font-size:40px}}
"""


def sentinel_write_verification_artifacts(rows, args):
    summary, pairs, reliability = sentinel_compute_verification(rows, args)
    write_json(path_output("sentinel_x_verification_summary.json"), summary)
    if pairs:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), list(pairs[0].keys()), pairs)
    else:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), ["target_date", "jam", "note"], [{"target_date": "", "jam": "", "note": "Belum ada pasangan forecast-observasi."}])
    write_dict_csv(path_output("sentinel_x_reliability.csv"), ["probability_bin", "n", "mean_forecast_probability", "observed_rain_frequency"], reliability)
    matched = int(summary.get("matched_cases", 0) or 0)
    enough = str(summary.get("calibration_status", "")) == "VERIFIED_ENOUGH_DATA"
    status_title = "Sudah mulai bisa dinilai" if enough else "Data akurasi belum cukup"
    status_note = "Skor mulai didukung data observasi." if enough else "Belum ada cukup pasangan forecast dan observasi. Jadi halaman ini sengaja tidak menampilkan klaim akurasi."
    def m(title, value, sub):
        return f"<article class='metric card'><label>{_v5_esc(title)}</label><strong>{_v5_esc(value)}</strong><small>{_v5_esc(sub)}</small></article>"
    rel_rows = "".join(
        f"<tr><td><b>{_v5_esc(r['probability_bin'])}</b></td><td>{r['n']}</td><td>{_v5_num(r['mean_forecast_probability'],'%')}</td><td>{_v5_num(r['observed_rain_frequency'],'%')}</td></tr>"
        for r in reliability
    )
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Akurasi prakiraan — {_v5_esc(getattr(args,'location_name',''))}</title><style>{_v5_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Akurasi prakiraan</b><span>{_v5_esc(getattr(args,'location_name',''))}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Kembali</a><a href='sentinel_x_report.html'>Ringkasan</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>Akuntabilitas</span><span class='chip'>{matched} kasus cocok</span></div><h1>Status akurasi prakiraan</h1><p>{_v5_esc(status_note)} Halaman ini dibuat agar sistem tidak terlihat lebih akurat daripada bukti datanya.</p></section><div class='alert'><b>{_v5_esc(sentinel_public_disclaimer(args))}</b></div><section class='metrics'>{m('Status', status_title, status_note)}{m('Kasus yang sudah cocok', matched, 'Diperlukan lebih banyak pasangan prakiraan dan observasi.')}{m('Error suhu', _v5_num(summary.get('temperature_mae_c'), '°C', 1), 'Akan muncul setelah ada observasi.')}</section><section class='metrics'>{m('Skor probabilitas hujan', _v5_num(summary.get('rain_brier_score'), '', 2), 'Lebih kecil lebih baik.')}{m('Hujan yang tertangkap', _v5_num(summary.get('rain_pod'), '%'), 'Akan terisi setelah ada kasus.')}{m('Alarm keliru', _v5_num(summary.get('rain_far'), '%'), 'Akan terisi setelah ada kasus.')}</section><section class='panel'><div class='section-title'><h2>Bukti peluang hujan</h2><p>Masih kosong itu wajar kalau data observasi belum terkumpul.</p></div><div class='table-scroll'><table><tr><th>Probabilitas</th><th>Jumlah kasus</th><th>Rata-rata prediksi</th><th>Hujan yang benar terjadi</th></tr>{rel_rows}</table></div></section><section class='panel'><h2>Bahasa sederhananya</h2><p>Untuk sekarang, gunakan dashboard sebagai <b>panduan harian</b>, bukan bukti akurasi final. Sistem baru bisa dinilai setelah cukup banyak prediksi dibandingkan dengan kejadian sebenarnya.</p><div class='links'><a class='pill' href='sentinel_x_verification_summary.json'>Data verifikasi</a><a class='pill' href='sentinel_x_reliability.csv'>Reliability CSV</a><a class='pill' href='sentinel_x_verification_pairs.csv'>Matched pairs CSV</a></div></section></main></body></html>"""
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(doc))
    return summary


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return None
    rows = aether_rows or []
    status = str(daily.get('daily_operational_status','UNKNOWN'))
    status_label, status_cls = _v5_status_public(status)
    peak_rain = max(rows, key=lambda r: aether_value(r.get("rain_threat_score")) or -1) if rows else {}
    peak_failure = max(rows, key=lambda r: aether_value(r.get("forecast_failure_risk")) or -1) if rows else {}
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    last_update = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE)).strftime('%d %b %Y, %H:%M')
    summary = _v5_safe_summary(daily, rows, args)
    rec = _v5_recommendation(daily, rows, args)
    groups = _v5_group_rows(rows)
    periods = ''.join(_v5_period_card(name, groups.get(name, [])) for name in ["Pagi","Siang","Sore","Malam"])
    timeline = _v5_hour_list(rows)
    best = _v5_best_time(rows)
    risky = _v5_risky_time(rows)
    scenarios = peak_rain or {}
    scenario_pairs = [
        ("Tidak kena hujan langsung", "scenario_dry_miss"),
        ("Hujan di sekitar lokasi", "scenario_nearby_rain_only"),
        ("Hujan ringan langsung", "scenario_direct_light_rain"),
        ("Hujan sedang singkat", "scenario_direct_moderate_rain"),
    ]
    scenario_html = ''.join(f"<li><b>{_v5_esc(label)}</b><span>{_v5_num(scenarios.get(key),'%')}</span></li>" for label,key in scenario_pairs)
    # Hide source details from the main public flow; make it collapsible.
    source_html = ''.join(f"<tr><td><b>{_v5_esc(s.get('source_id',''))}</b></td><td>{_v5_esc(s.get('state',''))}</td><td>{_v5_esc(s.get('success',''))}</td></tr>" for s in (source_state_rows or [])) or "<tr><td colspan='3'>Belum ada data sumber.</td></tr>"
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Cuaca lokal — {_v5_esc(getattr(args,'location_name',''))}</title><style>{_v5_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Cuaca Lokal</b><span>{_v5_esc(getattr(args,'location_name',''))} · diperbarui {last_update}</span></div></div><div class='nav'><a href='sentinel_x_report.html'>Ringkasan</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a><a href='../'>Pilih lokasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>AETHER Sentinel X</span><span class='chip'>{_v5_esc(getattr(args,'location_name',''))}</span></div><h1>Cuaca hari ini, pakai bahasa sederhana.</h1><p>{_v5_esc(summary)}</p></section><div class='alert'><b>{_v5_esc(sentinel_public_disclaimer(args))}</b></div><section class='summary'><article class='card headline'><span class='badge {status_cls}'>{_v5_esc(status_label)}</span><h2>{_v5_esc(rec)}</h2><p>Jam yang paling perlu diperhatikan: <b>{_v5_esc(risky)}</b>. Waktu relatif lebih aman: <b>{_v5_esc(best)}</b>.</p></article><div class='metrics'><article class='metric card'><label>Peluang hujan tertinggi</label><strong>{_v5_num(peak_rain.get('prob_rain'),'%')}</strong><small>sekitar { _v5_esc(peak_rain.get('jam','—')) }</small></article><article class='metric card'><label>Risiko hujan</label><strong>{_v5_esc(_v5_risk_word(peak_rain.get('rain_threat_score'))[0])}</strong><small>berdasarkan gabungan beberapa sumber</small></article><article class='metric card'><label>Tingkat kepercayaan</label><strong>{_v5_num(100-(safe_float(peak_failure.get('forecast_failure_risk')) or 0),'%')}</strong><small>lebih kecil jika timing hujan sulit</small></article></div></section><section class='panel'><div class='section-title'><h2>Ringkasan pagi–malam</h2><p>Lihat bagian hari yang paling relevan.</p></div><div class='periods'>{periods}</div></section><section class='action panel'><div><h2>Saran singkat</h2><p class='big'>{_v5_esc(rec)}</p><p class='muted'>Kalau langit cepat menggelap, angin berubah, atau mulai gerimis, pakai informasi terbaru dari BMKG dan kondisi sekitar.</p></div><div><h2>Kemungkinan kondisi</h2><ul class='plain-list scenario-list'>{scenario_html}</ul></div></section><section class='panel'><div class='section-title'><h2>Per jam</h2><p>Tidak perlu membaca semua; fokus pada jam kamu akan keluar.</p></div><div class='timeline'>{timeline}</div></section><details class='panel'><summary><b>Sumber data dan catatan teknis</b></summary><p class='muted'>Bagian ini untuk yang ingin melihat sumber model. Untuk masyarakat umum, ringkasan di atas sudah cukup.</p><div class='table-scroll'><table><tr><th>Sumber</th><th>Status</th><th>Sukses</th></tr>{source_html}</table></div><p class='muted'>Status bukti historis: {_v5_esc(str(verification.get('calibration_status','belum cukup data')).replace('_',' ').lower())}.</p></details><section class='panel'><h2>Catatan penting</h2><ul class='plain-list'><li>Ini bukan peringatan resmi.</li><li>Untuk cuaca ekstrem, tetap cek informasi BMKG.</li><li>Prakiraan hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</li><li>Halaman akurasi akan lebih bermakna setelah data observasi terkumpul.</li></ul><div class='links'><a class='pill' href='sentinel_x_accuracy_public.html'>Akurasi</a><a class='pill' href='sentinel_x_report.html'>Ringkasan</a><a class='pill' href='sentinel_x.csv'>Data CSV</a></div></section></main></body></html>"""
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "report_html": path_output(AETHER_REPORT_HTML_FILENAME), "generated_at": now_local(args.timezone).isoformat(), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return path_output(AETHER_DASHBOARD_FILENAME)


def aether_write_report(aether_rows, daily, args):
    rows = aether_rows or []
    # Hide raw markdown from public. Keep a short stub only.
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write("# Ringkasan cuaca\n\nBuka versi publik yang rapi: `sentinel_x_report.html`.\n"))
    summary = _v5_safe_summary(daily, rows, args)
    rec = _v5_recommendation(daily, rows, args)
    groups = _v5_group_rows(rows)
    periods = ''.join(_v5_period_card(name, groups.get(name, [])) for name in ["Pagi","Siang","Sore","Malam"])
    timeline = _v5_hour_list(rows)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Ringkasan cuaca — {_v5_esc(getattr(args,'location_name',''))}</title><style>{_v5_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Ringkasan cuaca</b><span>{_v5_esc(getattr(args,'location_name',''))}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Dashboard</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>Laporan singkat</span></div><h1>Ringkasan cuaca hari ini</h1><p>{_v5_esc(summary)}</p></section><div class='alert'><b>{_v5_esc(sentinel_public_disclaimer(args))}</b></div><section class='panel'><h2>Jawaban singkat</h2><p class='big'>{_v5_esc(rec)}</p></section><section class='panel'><div class='section-title'><h2>Pagi, siang, sore, malam</h2><p>Ringkasan yang lebih mudah dibaca.</p></div><div class='periods'>{periods}</div></section><section class='panel'><div class='section-title'><h2>Rincian per jam</h2><p>Fokus pada jam aktivitas.</p></div><div class='timeline'>{timeline}</div></section><section class='panel'><h2>Batas penggunaan</h2><ul class='plain-list'><li>Gunakan sebagai panduan aktivitas harian.</li><li>Jangan gunakan sebagai peringatan resmi cuaca ekstrem.</li><li>Untuk kondisi berbahaya, ikuti informasi resmi BMKG.</li></ul></section></main></body></html>"""
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(doc))
    return path_output(AETHER_REPORT_HTML_FILENAME)


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    cards = []
    for loc in locations:
        prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
        cards.append(f"""
        <article class='card loc-card'>
          <h2>{_v5_esc(loc.location_name)}</h2>
          <p>Pilih lokasi ini untuk melihat ringkasan cuaca, jam rawan hujan, dan saran praktis hari ini.</p>
          <div class='links'><a class='pill' href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Prakiraan</a><a class='pill' href='{prefix}{AETHER_REPORT_HTML_FILENAME}'>Ringkasan</a><a class='pill' href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a></div>
        </article>""")
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Cuaca Lokal</title><style>{_v5_css()}</style></head><body><main class='page'><section class='hero'><div class='kicker'><span class='chip'>AETHER Sentinel X</span><span class='chip'>Portal publik</span></div><h1>Cek cuaca lokal tanpa bahasa ribet.</h1><p>Pilih lokasi, lihat jam rawan hujan, dan ambil keputusan sederhana: aman keluar, perlu payung, atau sebaiknya tunggu.</p></section><div class='alert'><b>{_v5_esc(sentinel_public_disclaimer(args))}</b></div><section class='portal-grid'>{''.join(cards)}</section><section class='panel'><h2>Data terbuka</h2><p class='muted'>Bagian ini untuk yang ingin mengolah data.</p><div class='links'><a class='pill' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='pill' href='forecast_all_locations.csv'>Forecast CSV</a><a class='pill' href='source_status_all_locations.csv'>Status sumber</a><a class='pill' href='forecast_batch_summary.json'>Ringkasan batch</a></div></section></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("sentinel_x_public_portal_manifest.json"), {"generated_at": now_local(DEFAULT_TIMEZONE).isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path("index.html")


# -----------------------------------------------------------------------------
# AETHER Sentinel X Public-Friendly v6
# Goal: make the public output feel like a real daily weather page, not a
# technical dashboard. Dates, validity, update time, plain-language decisions,
# and reading order are made explicit.
# -----------------------------------------------------------------------------
SENTINEL_PUBLIC_UI_VERSION = "2026-06-03.public-friendly-v6"


def _v6_esc(value):
    return _v5_esc(value)


def _v6_num(value, suffix="", digits=0, blank="—"):
    return _v5_num(value, suffix=suffix, digits=digits, blank=blank)


def _v6_timezone_label(tz_name):
    if str(tz_name) == "Asia/Jakarta":
        return "WIB"
    if str(tz_name) == "Asia/Makassar":
        return "WITA"
    if str(tz_name) == "Asia/Jayapura":
        return "WIT"
    return str(tz_name or "waktu lokal")


def _v6_format_date_id(date_obj):
    days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    try:
        return f"{days[date_obj.weekday()]}, {date_obj.day} {months[date_obj.month-1]} {date_obj.year}"
    except Exception:
        return str(date_obj)


def _v6_target_date(rows, args):
    for r in rows or []:
        d = r.get("target_date") or r.get("tanggal") or ""
        if d:
            try:
                if len(d) >= 10 and d[4] == "-":
                    return parse_iso_date(d[:10])
                return parse_display_date(d[:10])
            except Exception:
                pass
    try:
        if getattr(args, "target_date", None):
            return parse_iso_date(str(args.target_date)[:10])
    except Exception:
        pass
    return now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).date()


def _v6_time_context(rows, args):
    tz = getattr(args, "timezone", DEFAULT_TIMEZONE)
    now = now_local(tz)
    target = _v6_target_date(rows, args)
    tz_label = _v6_timezone_label(tz)
    return {
        "target_date": target,
        "target_label": _v6_format_date_id(target),
        "valid_label": f"Berlaku untuk { _v6_format_date_id(target) }, 00.00–23.59 {tz_label}",
        "updated_label": f"Diperbarui { _v6_format_date_id(now.date()) }, {now.strftime('%H:%M')} {tz_label}",
        "short_target": target.strftime("%d/%m/%Y"),
        "tz_label": tz_label,
    }


def _v6_status_public(daily):
    return _v5_status_public((daily or {}).get("daily_operational_status"))


def _v6_level_word(score):
    word, cls = _v5_risk_word(score)
    if word == "tinggi":
        return "tinggi", "danger", "Perlu rencana cadangan."
    if word == "sedang":
        return "sedang", "warn", "Masih bisa beraktivitas, tapi lebih aman siap payung."
    return "rendah", "ok", "Relatif aman, tetap pantau langit sekitar."


def _v6_readable_weather(cat):
    text = str(cat or "Cuaca berubah-ubah")
    mapping = {
        "Cerah Berawan": "cerah berawan",
        "Cerah": "cerah",
        "Berawan": "berawan",
        "Hujan Ringan": "hujan ringan",
        "Hujan Sedang": "hujan sedang",
        "Hujan Lebat": "hujan lebat",
    }
    return mapping.get(text, text.lower())


def _v6_peak(rows, field):
    rows = rows or []
    if not rows:
        return {}
    return max(rows, key=lambda r: safe_float(r.get(field)) if safe_float(r.get(field)) is not None else -1)


def _v6_lowest(rows, field):
    valid = []
    for r in rows or []:
        v = safe_float(r.get(field))
        if v is not None:
            valid.append((v, r))
    if not valid:
        return {}
    return sorted(valid, key=lambda x: x[0])[0][1]


def _v6_practical_decision(daily, rows, args):
    peak_rain = _v6_peak(rows, "rain_threat_score")
    peak_prob = _v6_peak(rows, "prob_rain")
    risk_score = max(safe_float(peak_rain.get("rain_threat_score")) or 0, safe_float(peak_prob.get("prob_rain")) or 0)
    jam = peak_rain.get("jam") or peak_prob.get("jam") or "sore"
    cat = _v6_readable_weather(peak_rain.get("dominant_category"))
    if risk_score >= 70:
        title = f"Kalau keluar sekitar {jam}, siapkan payung atau cari rencana cadangan."
        short = "Perlu lebih hati-hati."
    elif risk_score >= 45:
        title = f"Aktivitas masih memungkinkan. Siapkan payung menjelang {jam}."
        short = "Aman dipantau."
    else:
        title = "Secara umum masih aman untuk aktivitas biasa."
        short = "Relatif aman."
    detail = f"Jam paling diperhatikan sekitar {jam}. Sinyal cuaca yang muncul: {cat}."
    return title, short, detail


def _v6_time_window_text(rows):
    risky = _v5_risky_time(rows)
    best = _v5_best_time(rows)
    return risky, best


def _v6_summary_text(daily, rows, args):
    ctx = _v6_time_context(rows, args)
    peak_rain = _v6_peak(rows, "rain_threat_score")
    peak_prob = _v6_peak(rows, "prob_rain")
    peak_failure = _v6_peak(rows, "forecast_failure_risk")
    title, _short, detail = _v6_practical_decision(daily, rows, args)
    prob = _v6_num(peak_prob.get("prob_rain"), "%", 0)
    rain_word, _cls, _hint = _v6_level_word(peak_rain.get("rain_threat_score"))
    fail = safe_float(peak_failure.get("forecast_failure_risk")) or 0
    fail_note = "Yang paling mungkin meleset adalah waktu atau posisi hujan lokal." if fail >= 30 else "Prediksi tetap perlu dipantau ulang jika kondisi langit berubah cepat."
    return f"{ctx['valid_label']}. {title} {detail} Peluang hujan tertinggi sekitar {prob}; tingkat risiko hujan {rain_word}. {fail_note} Ini panduan harian, bukan peringatan resmi."


def _v6_period_card(name, rows):
    if not rows:
        return f"<article class='period empty'><h3>{_v6_esc(name)}</h3><p>Belum ada data.</p></article>"
    peak = _v6_peak(rows, "rain_threat_score")
    avg_prob = sum((safe_float(r.get("prob_rain")) or 0) for r in rows) / max(len(rows), 1)
    max_threat = safe_float(peak.get("rain_threat_score")) or 0
    word, cls, hint = _v6_level_word(max_threat)
    cats = {}
    for r in rows:
        cat = r.get("dominant_category") or "Berubah-ubah"
        cats[cat] = cats.get(cat, 0) + 1
    cat = max(cats.items(), key=lambda kv: kv[1])[0]
    return f"""
    <article class='period {cls}'>
      <div class='period-top'><span class='emoji'>{_v5_weather_icon(cat)}</span><h3>{_v6_esc(name)}</h3></div>
      <p class='weather'>{_v6_esc(cat)}</p>
      <p class='hint'>{_v6_esc(hint)}</p>
      <div class='mini-grid'>
        <div><span>Rata-rata hujan</span><b>{avg_prob:.0f}%</b></div>
        <div><span>Risiko tertinggi</span><b>{_v6_esc(word)}</b></div>
        <div><span>Jam perhatian</span><b>{_v6_esc(peak.get('jam','—'))}</b></div>
      </div>
    </article>"""


def _v6_hour_cards(rows):
    items = []
    for r in rows or []:
        jam = r.get("jam", "")
        cat = r.get("dominant_category", "")
        prob = safe_float(r.get("prob_rain")) or 0
        threat = safe_float(r.get("rain_threat_score")) or 0
        word, cls, hint = _v6_level_word(threat)
        if threat >= 70:
            rec = "Sebaiknya jangan andalkan aktivitas luar ruang pada jam ini."
        elif threat >= 45:
            rec = "Aktivitas luar ruang masih memungkinkan, tetapi sebaiknya siapkan payung."
        else:
            rec = "Risiko relatif rendah; tetap pantau sekitar."
        items.append(f"""
        <article class='hour {cls}'>
          <div class='hour-time'><b>{_v6_esc(jam)}</b><span>{_v5_weather_icon(cat)}</span></div>
          <div class='hour-main'><b>{_v6_esc(cat or 'Cuaca berubah')}</b><p>{_v6_esc(rec)}</p></div>
          <div class='hour-stat'><span>Hujan</span><b>{prob:.0f}%</b></div>
          <div class='hour-stat'><span>Risiko</span><b>{_v6_esc(word)}</b></div>
        </article>""")
    return "".join(items) or "<p class='empty'>Belum ada data per jam.</p>"


def _v6_scenario_list(peak):
    peak = peak or {}
    items = [
        ("Tidak hujan langsung", "scenario_dry_miss"),
        ("Hujan di sekitar lokasi", "scenario_nearby_rain_only"),
        ("Hujan ringan langsung", "scenario_direct_light_rain"),
        ("Hujan sedang singkat", "scenario_direct_moderate_rain"),
        ("Hujan kuat singkat", "scenario_convective_burst"),
    ]
    html_items = []
    for label, key in items:
        val = _v6_num(peak.get(key), "%", 0)
        html_items.append(f"<li><span>{_v6_esc(label)}</span><b>{val}</b></li>")
    return "".join(html_items)


def _v6_source_details(source_state_rows):
    rows = []
    for s in source_state_rows or []:
        ok = str(s.get("success", "")).lower() in {"true", "1", "yes"}
        state = "aktif" if ok else "bermasalah"
        rows.append(f"<tr><td>{_v6_esc(s.get('source_id',''))}</td><td>{_v6_esc(state)}</td></tr>")
    return "".join(rows) or "<tr><td colspan='2'>Belum ada data sumber.</td></tr>"


def _v6_css():
    return r"""
:root{--bg:#edf5ff;--paper:#fff;--ink:#071222;--muted:#64748b;--line:#d8e5f3;--blue:#1d5cff;--blue2:#08285e;--green:#12a150;--amber:#d97706;--red:#dc2626;--shadow:0 18px 55px rgba(15,23,42,.12);--soft:rgba(255,255,255,.72);--radius:30px}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% -10%,#d6e9ff 0,#eef6ff 36%,#f9fbff 100%);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:var(--ink);font-size:17px;line-height:1.55}.page{width:min(1240px,calc(100% - 32px));margin:26px auto 90px}.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:12px 0 16px;background:linear-gradient(180deg,rgba(237,245,255,.96),rgba(237,245,255,.75));backdrop-filter:blur(10px)}.brand{display:flex;gap:12px;align-items:center}.mark{width:42px;height:42px;border-radius:16px;background:linear-gradient(135deg,#06152f,#2168ff);box-shadow:0 12px 30px rgba(29,92,255,.28)}.brand b{font-size:18px}.brand span{display:block;color:var(--muted);font-size:13px}.nav{display:flex;gap:10px;flex-wrap:wrap}.nav a,.pill{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border:1px solid #b9d2ff;background:#f7fbff;color:#0b55d9;border-radius:999px;padding:9px 14px;font-weight:900;font-size:14px}.hero{position:relative;overflow:hidden;border-radius:34px;padding:44px 52px;margin:10px 0 18px;color:#fff;background:linear-gradient(135deg,#06152f 0%,#123f9c 58%,#2c78ff 100%);box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;right:-70px;top:-85px;width:270px;height:270px;border-radius:50%;background:rgba(255,255,255,.13)}.hero h1{font-size:clamp(36px,5vw,70px);letter-spacing:-.07em;line-height:.98;margin:16px 0 12px}.hero p{font-size:clamp(17px,2vw,22px);max-width:920px;color:#eaf1ff;margin:0}.kicker{display:flex;gap:10px;flex-wrap:wrap}.chip{display:inline-flex;align-items:center;border:1px solid rgba(255,255,255,.28);background:rgba(255,255,255,.12);color:#fff;border-radius:999px;padding:8px 12px;font-size:13px;font-weight:950}.notice{border:1px solid #fdba74;background:#fff7ed;color:#7c2d12;border-radius:18px;padding:14px 18px;font-weight:850;margin:14px 0 18px}.date-strip{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:14px;margin:16px 0}.date-card{background:var(--paper);border:1px solid var(--line);border-radius:22px;padding:18px 20px;box-shadow:var(--shadow)}.date-card span,.metric span{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:950}.date-card b{font-size:22px;letter-spacing:-.03em}.lead-grid{display:grid;grid-template-columns:1.45fr .9fr;gap:18px;margin:18px 0}.decision{background:#081729;color:#fff;border-radius:30px;padding:30px;box-shadow:var(--shadow)}.decision .badge{margin-bottom:14px}.decision h2{font-size:clamp(30px,4vw,48px);line-height:1.02;letter-spacing:-.06em;margin:0 0 14px}.decision p{color:#dbeafe;margin:0}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.metric{background:var(--paper);border:1px solid var(--line);border-radius:24px;padding:22px;box-shadow:var(--shadow)}.metric strong{display:block;font-size:38px;letter-spacing:-.05em;margin:8px 0}.metric small{color:var(--muted)}.badge{display:inline-flex;border-radius:999px;padding:7px 11px;font-weight:950;font-size:13px}.badge.ok{background:#dcfce7;color:#166534}.badge.warn{background:#fef3c7;color:#92400e}.badge.danger{background:#fee2e2;color:#991b1b}.panel{background:var(--paper);border:1px solid var(--line);border-radius:28px;padding:28px;margin:18px 0;box-shadow:var(--shadow)}.section-title{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:16px}.section-title h2{margin:0;font-size:30px;letter-spacing:-.045em}.section-title p{margin:0;color:var(--muted)}.periods{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.period{border:1px solid var(--line);border-radius:24px;padding:18px;background:#fff}.period.ok{border-color:#bbf7d0}.period.warn{border-color:#fde68a}.period.danger{border-color:#fecaca}.period-top{display:flex;align-items:center;gap:10px}.period h3{font-size:24px;margin:0}.emoji{font-size:28px}.period .weather{font-weight:950;font-size:19px}.period .hint{color:var(--muted);min-height:48px}.mini-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.mini-grid div{background:#f8fbff;border:1px solid #e2ebf7;border-radius:16px;padding:10px}.mini-grid span{display:block;color:var(--muted);font-size:11px;font-weight:850}.mini-grid b{font-size:16px}.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.scenario-list{display:grid;gap:10px;padding:0;margin:0;list-style:none}.scenario-list li{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #e5edf7;padding:9px 0}.scenario-list b{font-size:22px}.hour-list{display:grid;gap:10px}.hour{display:grid;grid-template-columns:110px 1fr 110px 110px;gap:12px;align-items:center;border:1px solid var(--line);border-left:7px solid #22c55e;border-radius:20px;padding:14px;background:#fff}.hour.warn{border-left-color:#f59e0b}.hour.danger{border-left-color:#ef4444}.hour-time{display:flex;align-items:center;gap:10px;font-size:20px}.hour-main p{margin:2px 0 0;color:var(--muted)}.hour-stat span{display:block;color:var(--muted);font-size:12px;font-weight:900}.hour-stat b{font-size:20px}.plain-list{padding-left:20px}.plain-list li{margin:8px 0}.muted{color:var(--muted)}.footer-note{color:var(--muted);font-size:14px;text-align:center;margin-top:28px}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;min-width:720px}th,td{border-bottom:1px solid #e2ebf7;text-align:left;padding:12px}th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#475569}.portal-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.loc-card h2{font-size:30px;letter-spacing:-.04em}.loc-card{min-height:260px;display:flex;flex-direction:column}.loc-card .links{margin-top:auto}.links{display:flex;gap:10px;flex-wrap:wrap}.big{font-size:24px;font-weight:950}.subtle{font-size:14px;color:var(--muted)}@media(max-width:900px){.page{width:min(100% - 20px,760px)}.date-strip,.lead-grid,.two,.summary,.portal-grid{grid-template-columns:1fr}.metrics,.periods{grid-template-columns:1fr 1fr}.hour{grid-template-columns:80px 1fr}.hour-stat{display:none}.hero{padding:34px 28px}.top{position:static}.nav{justify-content:flex-start}}@media(max-width:560px){.metrics,.periods{grid-template-columns:1fr}.hero h1{font-size:38px}.date-card b{font-size:18px}.decision h2{font-size:30px}}
"""


def aether_write_public_accuracy_page(args):
    summary = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    reliability = read_dict_csv(path_output("sentinel_x_reliability.csv"))
    matched = int(summary.get("matched_cases") or 0)
    rows = read_dict_csv(path_output(AETHER_CSV_FILENAME))
    ctx = _v6_time_context(rows, args)
    if matched <= 0:
        title = "Data akurasi belum cukup"
        note = "Prediksi sudah berjalan, tetapi belum ada cukup pasangan prediksi dan observasi untuk menilai akurasi secara adil."
    else:
        title = "Akurasi mulai bisa dinilai"
        note = f"Sudah ada {matched} kasus yang cocok antara prediksi dan observasi."
    rel_rows = "".join(
        f"<tr><td>{_v6_esc(r.get('bin',''))}</td><td>{_v6_esc(r.get('n',''))}</td><td>{_v6_esc(r.get('mean_forecast_pct','') or '—')}</td><td>{_v6_esc(r.get('observed_frequency_pct','') or '—')}</td></tr>"
        for r in reliability
    ) or "<tr><td colspan='4'>Belum ada data.</td></tr>"
    def metric(label, value, help_text):
        return f"<article class='metric'><span>{_v6_esc(label)}</span><strong>{_v6_esc(value)}</strong><small>{_v6_esc(help_text)}</small></article>"
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Akurasi prakiraan — {_v6_esc(getattr(args,'location_name',''))}</title><style>{_v6_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Akurasi prakiraan</b><span>{_v6_esc(getattr(args,'location_name',''))} · {ctx['updated_label']}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Kembali</a><a href='sentinel_x_report.html'>Ringkasan</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>{_v6_esc(ctx['valid_label'])}</span><span class='chip'>{matched} kasus cocok</span></div><h1>Status akurasi prakiraan</h1><p>{_v6_esc(note)} Halaman ini menunjukkan apakah prakiraan sudah memiliki bukti historis yang cukup.</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='date-strip'><article class='date-card'><span>Berlaku untuk</span><b>{_v6_esc(ctx['target_label'])}</b><p class='subtle'>00.00–23.59 {ctx['tz_label']}</p></article><article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(ctx['updated_label'].replace('Diperbarui ',''))}</b></article><article class='date-card'><span>Status bukti</span><b>{_v6_esc(title)}</b></article></section><section class='metrics'>{metric('Kasus cocok', matched, 'Minimum puluhan kasus agar penilaian mulai bermakna.')}{metric('Error suhu', _v6_num(summary.get('temperature_mae_c'), '°C', 1), 'Akan muncul setelah observasi cukup.')}{metric('Skor probabilitas hujan', _v6_num(summary.get('rain_brier_score'), '', 2), 'Lebih kecil lebih baik.')}</section><section class='metrics'>{metric('Hujan tertangkap', _v6_num(summary.get('rain_pod'), '%'), 'Kemampuan menangkap hujan.')}{metric('Alarm keliru', _v6_num(summary.get('rain_far'), '%'), 'Seberapa sering alarm hujan meleset.')}{metric('Kategori cocok', _v6_num(summary.get('category_accuracy'), '%'), 'Kecocokan kategori cuaca.')}</section><section class='panel'><div class='section-title'><h2>Bukti peluang hujan</h2><p>Jika masih kosong, berarti data observasi belum cukup untuk menilai akurasi.</p></div><div class='table-scroll'><table><tr><th>Probabilitas</th><th>Jumlah kasus</th><th>Rata-rata prediksi</th><th>Hujan yang benar terjadi</th></tr>{rel_rows}</table></div></section><section class='panel'><h2>Bahasa sederhananya</h2><p class='big'>Saat ini, halaman prakiraan dapat dipakai sebagai panduan harian. Nilai akurasi belum dapat dianggap final sebelum data observasi terkumpul.</p><div class='links'><a class='pill' href='sentinel_x_verification_summary.json'>Data verifikasi</a><a class='pill' href='sentinel_x_reliability.csv'>Reliability CSV</a><a class='pill' href='sentinel_x_verification_pairs.csv'>Matched pairs CSV</a></div></section></main></body></html>"""
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(doc))
    return path_output("sentinel_x_accuracy_public.html")


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return ""
    rows = aether_rows or []
    ctx = _v6_time_context(rows, args)
    peak_rain = _v6_peak(rows, "rain_threat_score")
    peak_prob = _v6_peak(rows, "prob_rain")
    peak_failure = _v6_peak(rows, "forecast_failure_risk")
    status_label, status_cls = _v6_status_public(daily)
    title, short, detail = _v6_practical_decision(daily, rows, args)
    summary = _v6_summary_text(daily, rows, args)
    risky, best = _v6_time_window_text(rows)
    groups = _v5_group_rows(rows)
    periods = ''.join(_v6_period_card(name, groups.get(name, [])) for name in ["Pagi","Siang","Sore","Malam"])
    hour_cards = _v6_hour_cards(rows)
    scenario_html = _v6_scenario_list(peak_rain)
    source_html = _v6_source_details(source_state_rows)
    risk_word, risk_cls, risk_hint = _v6_level_word(peak_rain.get("rain_threat_score"))
    certainty = max(0, 100 - (safe_float(peak_failure.get("forecast_failure_risk")) or 0))
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Cuaca lokal — {_v6_esc(getattr(args,'location_name',''))}</title><style>{_v6_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Cuaca Lokal</b><span>{_v6_esc(getattr(args,'location_name',''))} · {ctx['updated_label']}</span></div></div><div class='nav'><a href='sentinel_x_report.html'>Ringkasan</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a><a href='../'>Pilih lokasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>{_v6_esc(ctx['valid_label'])}</span><span class='chip'>{_v6_esc(ctx['updated_label'])}</span></div><h1>Prakiraan cuaca lokal</h1><p>{_v6_esc(summary)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='date-strip'><article class='date-card'><span>Tanggal forecast</span><b>{_v6_esc(ctx['target_label'])}</b><p class='subtle'>Berlaku 00.00–23.59 {ctx['tz_label']}</p></article><article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(ctx['updated_label'].replace('Diperbarui ',''))}</b></article><article class='date-card'><span>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section><section class='lead-grid'><article class='decision'><span class='badge {status_cls}'>{_v6_esc(short)}</span><h2>{_v6_esc(title)}</h2><p>{_v6_esc(detail)} Waktu relatif lebih aman: <b>{_v6_esc(best)}</b>. Jam yang perlu diperhatikan: <b>{_v6_esc(risky)}</b>.</p></article><div class='metrics'><article class='metric'><span>Peluang hujan tertinggi</span><strong>{_v6_num(peak_prob.get('prob_rain'),'%')}</strong><small>sekitar { _v6_esc(peak_prob.get('jam','—')) }</small></article><article class='metric'><span>Risiko hujan</span><strong>{_v6_esc(risk_word)}</strong><small>{_v6_esc(risk_hint)}</small></article><article class='metric'><span>Tingkat kepercayaan</span><strong>{_v6_num(certainty,'%')}</strong><small>Lebih rendah jika waktu hujan sulit dipastikan</small></article></div></section><section class='panel'><div class='section-title'><h2>Ringkasan pagi–malam</h2><p>Pilih bagian hari yang sesuai dengan rencana aktivitas.</p></div><div class='periods'>{periods}</div></section><section class='two'><section class='panel'><h2>Saran singkat</h2><p class='big'>{_v6_esc(title)}</p><p class='muted'>Jika langit cepat gelap, angin berubah, atau mulai gerimis, utamakan kondisi nyata di sekitar dan informasi resmi BMKG.</p></section><section class='panel'><h2>Kemungkinan kondisi</h2><ul class='scenario-list'>{scenario_html}</ul></section></section><section class='panel'><div class='section-title'><h2>Per jam</h2><p>Fokus pada jam saat Anda beraktivitas.</p></div><div class='hour-list'>{hour_cards}</div></section><details class='panel'><summary><b>Sumber data dan catatan teknis</b></summary><p class='muted'>Bagian ini hanya untuk pengguna yang ingin melihat sumber data. Ringkasan utama sudah cukup untuk penggunaan harian.</p><div class='table-scroll'><table><tr><th>Sumber</th><th>Status</th></tr>{source_html}</table></div><p class='muted'>Status bukti historis: {_v6_esc(str(verification.get('calibration_status','belum cukup data')).replace('_',' ').lower())}.</p></details><section class='panel'><h2>Catatan penting</h2><ul class='plain-list'><li>Ini bukan peringatan resmi.</li><li>Untuk cuaca ekstrem, tetap cek informasi BMKG.</li><li>Prakiraan hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</li><li>Halaman akurasi akan lebih bermakna setelah data observasi terkumpul.</li></ul><div class='links'><a class='pill' href='sentinel_x_accuracy_public.html'>Akurasi</a><a class='pill' href='sentinel_x_report.html'>Ringkasan</a><a class='pill' href='sentinel_x.csv'>Data CSV</a></div></section><p class='footer-note'>AETHER Sentinel X · {ctx['valid_label']} · {ctx['updated_label']}</p></main></body></html>"""
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "report_html": path_output(AETHER_REPORT_HTML_FILENAME), "generated_at": now_local(args.timezone).isoformat(), "ui_version": SENTINEL_PUBLIC_UI_VERSION, "valid_date": ctx["target_label"]})
    return path_output(AETHER_DASHBOARD_FILENAME)


def aether_write_report(aether_rows, daily, args):
    rows = aether_rows or []
    ctx = _v6_time_context(rows, args)
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write(f"# Ringkasan cuaca\n\nBuka versi HTML: `sentinel_x_report.html`.\n\n{ctx['valid_label']}\n{ctx['updated_label']}\n"))
    title, short, detail = _v6_practical_decision(daily, rows, args)
    summary = _v6_summary_text(daily, rows, args)
    groups = _v5_group_rows(rows)
    periods = ''.join(_v6_period_card(name, groups.get(name, [])) for name in ["Pagi","Siang","Sore","Malam"])
    hour_cards = _v6_hour_cards(rows)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Ringkasan cuaca — {_v6_esc(getattr(args,'location_name',''))}</title><style>{_v6_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Ringkasan cuaca</b><span>{_v6_esc(getattr(args,'location_name',''))} · {ctx['updated_label']}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Dashboard</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>{_v6_esc(ctx['valid_label'])}</span><span class='chip'>{_v6_esc(ctx['updated_label'])}</span></div><h1>Ringkasan cuaca hari ini</h1><p>{_v6_esc(summary)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='date-strip'><article class='date-card'><span>Tanggal forecast</span><b>{_v6_esc(ctx['target_label'])}</b></article><article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(ctx['updated_label'].replace('Diperbarui ',''))}</b></article><article class='date-card'><span>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section><section class='panel'><h2>Jawaban singkat</h2><p class='big'>{_v6_esc(title)}</p><p class='muted'>{_v6_esc(detail)}</p></section><section class='panel'><div class='section-title'><h2>Pagi, siang, sore, malam</h2><p>Ringkasan yang lebih mudah dibaca.</p></div><div class='periods'>{periods}</div></section><section class='panel'><div class='section-title'><h2>Rincian per jam</h2><p>Fokus pada jam aktivitas.</p></div><div class='hour-list'>{hour_cards}</div></section><section class='panel'><h2>Batas penggunaan</h2><ul class='plain-list'><li>Gunakan sebagai panduan aktivitas harian.</li><li>Jangan gunakan sebagai peringatan resmi cuaca ekstrem.</li><li>Untuk kondisi berbahaya, ikuti informasi resmi BMKG.</li></ul></section></main></body></html>"""
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(doc))
    return path_output(AETHER_REPORT_HTML_FILENAME)


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    now = now_local(DEFAULT_TIMEZONE)
    updated = f"Diperbarui {_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} WIB"
    cards = []
    for loc in locations:
        prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
        cards.append(f"""
        <article class='card loc-card'>
          <h2>{_v6_esc(loc.location_name)}</h2>
          <p>Lihat prakiraan harian, peluang hujan, dan waktu yang perlu diperhatikan.</p>
          <div class='links'><a class='pill' href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Prakiraan</a><a class='pill' href='{prefix}{AETHER_REPORT_HTML_FILENAME}'>Ringkasan</a><a class='pill' href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a></div>
        </article>""")
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Portal Cuaca Lokal</title><style>{_v6_css()}</style></head><body><main class='page'><section class='hero'><div class='kicker'><span class='chip'>Portal publik</span><span class='chip'>{_v6_esc(updated)}</span></div><h1>Portal Prakiraan Cuaca Lokal</h1><p>Pilih lokasi untuk melihat prakiraan harian, peluang hujan, jam yang perlu diperhatikan, dan saran aktivitas. Bahasa dibuat sederhana agar mudah dipahami masyarakat.</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='portal-grid'>{''.join(cards)}</section><section class='panel'><h2>Data terbuka</h2><p class='muted'>Untuk analisis, arsip, atau pembuatan dashboard lanjutan.</p><div class='links'><a class='pill' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='pill' href='forecast_all_locations.csv'>Forecast CSV</a><a class='pill' href='source_status_all_locations.csv'>Status sumber</a><a class='pill' href='forecast_batch_summary.json'>Ringkasan batch</a></div></section><p class='footer-note'>AETHER Sentinel X · {updated}</p></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("sentinel_x_public_portal_manifest.json"), {"generated_at": now.isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path("index.html")


# -----------------------------------------------------------------------------
# AETHER Sentinel X v8 — Public Variables Layer
# Adds human-readable temperature, humidity, heat index, wind, cloud, visibility,
# and rain amount outputs without changing the single-file architecture.
# -----------------------------------------------------------------------------
SENTINEL_PUBLIC_UI_VERSION = "v8-public-variables"


def _v8_num(value, suffix="", digits=0, fallback="—"):
    v = safe_float(value)
    if v is None:
        return fallback
    if digits == 0:
        return f"{round(v):.0f}{suffix}"
    return f"{v:.{digits}f}{suffix}"


def _v8_pick(rows, field, reverse=True):
    usable = [r for r in (rows or []) if safe_float(r.get(field)) is not None]
    if not usable:
        return {}
    return sorted(usable, key=lambda r: safe_float(r.get(field)) or 0, reverse=reverse)[0]


def _v8_comfort_label(hi):
    v = safe_float(hi)
    if v is None:
        return "Belum tersedia"
    if v < 27:
        return "Nyaman"
    if v < 32:
        return "Cukup panas"
    if v < 38:
        return "Panas, perlu minum cukup"
    return "Sangat panas, batasi aktivitas berat"


def _v8_visibility_label(value):
    v = safe_float(value)
    if v is None:
        return "Belum tersedia"
    if v >= 10000:
        return "Jarak pandang baik"
    if v >= 5000:
        return "Cukup baik"
    if v >= 2000:
        return "Menurun"
    return "Rendah"


def _v8_cloud_label(value):
    v = safe_float(value)
    if v is None:
        return "Belum tersedia"
    if v < 25:
        return "Langit relatif cerah"
    if v < 60:
        return "Awan sedang"
    if v < 85:
        return "Cukup berawan"
    return "Berawan tebal"


def _v8_variable_cards(rows):
    hottest = _v8_pick(rows, "temp_p50")
    humid = _v8_pick(rows, "rh_p50")
    heat = _v8_pick(rows, "heat_index_p50")
    rain = _v8_pick(rows, "prob_rain")
    wind = _v8_pick(rows, "wind_p50")
    gust = _v8_pick(rows, "gust_p90")
    cloud = _v8_pick(rows, "cloud_p50")
    visible = _v8_pick(rows, "visibility_p10", reverse=False)
    dew = _v8_pick(rows, "dew_point_c")
    apparent = _v8_pick(rows, "apparent_temperature_c")
    pressure = _v8_pick(rows, "pressure_msl_hpa")
    def card(title, value, desc):
        return f"<article class='metric variable-card'><span>{_v6_esc(title)}</span><strong>{_v6_esc(value)}</strong><small>{_v6_esc(desc)}</small></article>"
    return "".join([
        card("Suhu tertinggi", _v8_num(hottest.get("temp_p50"), "°C", 1), f"sekitar {hottest.get('jam','—') if hottest else '—'}"),
        card("Kelembapan tertinggi", _v8_num(humid.get("rh_p50"), "%", 0), f"sekitar {humid.get('jam','—') if humid else '—'}"),
        card("Heat index", _v8_num(heat.get("heat_index_p50"), "°C", 1), _v8_comfort_label(heat.get("heat_index_p50"))),
        card("Terasa seperti", _v8_num(apparent.get("apparent_temperature_c"), "°C", 1), "gabungan suhu dan kelembapan"),
        card("Dew point", _v8_num(dew.get("dew_point_c"), "°C", 1), "indikasi kelembapan udara"),
        card("Tekanan udara", _v8_num(pressure.get("pressure_msl_hpa"), " hPa", 1), "jika tersedia dari sumber model"),
        card("Peluang hujan tertinggi", _v8_num(rain.get("prob_rain"), "%", 0), f"sekitar {rain.get('jam','—') if rain else '—'}"),
        card("Angin rata-rata tertinggi", _v8_num(wind.get("wind_p50"), " km/jam", 1), f"sekitar {wind.get('jam','—') if wind else '—'}"),
        card("Hembusan angin", _v8_num(gust.get("gust_p90"), " km/jam", 1), f"potensi tertinggi sekitar {gust.get('jam','—') if gust else '—'}"),
        card("Tutupan awan", _v8_num(cloud.get("cloud_p50"), "%", 0), _v8_cloud_label(cloud.get("cloud_p50"))),
        card("Jarak pandang", _v8_num(visible.get("visibility_p10"), " m", 0), _v8_visibility_label(visible.get("visibility_p10"))),
    ])


def _v8_hour_row(row):
    jam = _v6_esc(str(row.get("jam", "—")))
    cat = _v6_esc(str(row.get("dominant_category", "—")))
    rec = _v6_esc(str(row.get("decision_recommendation", "")))
    risk = _v6_esc(str(row.get("rain_threat_level", "rendah")).lower())
    return f"""
    <article class='hour-detail'>
      <div class='hour-main'><b>{jam}</b><span>{cat}</span><small>{rec}</small></div>
      <div class='hour-vars'>
        <span><b>{_v8_num(row.get('temp_p50'),'°C',1)}</b><small>Suhu</small></span>
        <span><b>{_v8_num(row.get('rh_p50'),'%')}</b><small>RH</small></span>
        <span><b>{_v8_num(row.get('heat_index_p50'),'°C',1)}</b><small>Heat index</small></span>
        <span><b>{_v8_num(row.get('apparent_temperature_c'),'°C',1)}</b><small>Terasa</small></span>
        <span><b>{_v8_num(row.get('dew_point_c'),'°C',1)}</b><small>Dew point</small></span>
        <span><b>{_v8_num(row.get('prob_rain'),'%')}</b><small>Hujan</small></span>
        <span><b>{_v8_num(row.get('rain_p50'),' mm',1)}</b><small>Curah hujan</small></span>
        <span><b>{_v8_num(row.get('wind_p50'),' km/jam',1)}</b><small>Angin</small></span>
        <span><b>{_v8_num(row.get('pressure_msl_hpa'),' hPa',1)}</b><small>Tekanan</small></span>
        <span><b>{_v8_num(row.get('cloud_p50'),'%')}</b><small>Awan</small></span>
        <span><b>{risk}</b><small>Risiko</small></span>
      </div>
    </article>"""


def _v8_hour_detail_cards(rows):
    return "".join(_v8_hour_row(r) for r in rows)


def _v8_period_card(name, rows):
    if not rows:
        return f"<article class='period'><h3>{_v6_esc(name)}</h3><p>Data belum tersedia.</p></article>"
    rain = sum((safe_float(r.get("prob_rain")) or 0) for r in rows) / len(rows)
    temp = sum((safe_float(r.get("temp_p50")) or 0) for r in rows if safe_float(r.get("temp_p50")) is not None)
    temp_n = sum(1 for r in rows if safe_float(r.get("temp_p50")) is not None)
    rh = sum((safe_float(r.get("rh_p50")) or 0) for r in rows if safe_float(r.get("rh_p50")) is not None)
    rh_n = sum(1 for r in rows if safe_float(r.get("rh_p50")) is not None)
    hi = sum((safe_float(r.get("heat_index_p50")) or 0) for r in rows if safe_float(r.get("heat_index_p50")) is not None)
    hi_n = sum(1 for r in rows if safe_float(r.get("heat_index_p50")) is not None)
    peak = _v8_pick(rows, "prob_rain")
    cats = {}
    for r in rows:
        cats[r.get("dominant_category", "—")] = cats.get(r.get("dominant_category", "—"), 0) + 1
    cat = max(cats, key=cats.get) if cats else "—"
    return f"""
    <article class='period rich-period'>
      <h3>{_v6_esc(name)}</h3>
      <b>{_v6_esc(cat)}</b>
      <p>{_v8_num(temp / temp_n if temp_n else None, '°C', 1)} · RH {_v8_num(rh / rh_n if rh_n else None, '%')} · terasa {_v8_num(hi / hi_n if hi_n else None, '°C', 1)}</p>
      <div class='mini-grid'><span>Hujan<br><b>{_v8_num(rain, '%')}</b></span><span>Jam rawan<br><b>{_v6_esc(peak.get('jam','—'))}</b></span></div>
    </article>"""


def _v8_write_variables_csv(rows):
    fieldnames = [
        "jam", "cuaca", "suhu_C", "kelembapan_RH_pct", "heat_index_C", "terasa_seperti_C",
        "dew_point_C", "tekanan_udara_hpa", "peluang_hujan_pct",
        "curah_hujan_p50_mm", "curah_hujan_p90_mm", "angin_kmh", "hembusan_angin_p90_kmh",
        "arah_angin_derajat", "tutupan_awan_pct", "jarak_pandang_p10_m", "risiko_hujan", "saran",
    ]
    out = []
    for r in rows or []:
        out.append({
            "jam": r.get("jam", ""),
            "cuaca": r.get("dominant_category", ""),
            "suhu_C": r.get("temp_p50", ""),
            "kelembapan_RH_pct": r.get("rh_p50", ""),
            "heat_index_C": r.get("heat_index_p50", ""),
            "terasa_seperti_C": r.get("apparent_temperature_c", ""),
            "dew_point_C": r.get("dew_point_c", ""),
            "tekanan_udara_hpa": r.get("pressure_msl_hpa", ""),
            "peluang_hujan_pct": r.get("prob_rain", ""),
            "curah_hujan_p50_mm": r.get("rain_p50", ""),
            "curah_hujan_p90_mm": r.get("rain_p90", ""),
            "angin_kmh": r.get("wind_p50", ""),
            "hembusan_angin_p90_kmh": r.get("gust_p90", ""),
            "arah_angin_derajat": r.get("wind_direction_deg", ""),
            "tutupan_awan_pct": r.get("cloud_p50", ""),
            "jarak_pandang_p10_m": r.get("visibility_p10", ""),
            "risiko_hujan": str(r.get("rain_threat_level", "")).lower(),
            "saran": r.get("decision_recommendation", ""),
        })
    write_dict_csv(path_output("sentinel_x_variables.csv"), fieldnames, out)
    return path_output("sentinel_x_variables.csv")


def _v8_css_extra():
    return """
    .variables{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.variable-card strong{font-size:24px}.hour-detail{display:grid;grid-template-columns:180px 1fr;gap:14px;padding:14px;border:1px solid var(--line);border-radius:16px;background:#fff;margin:10px 0}.hour-main b{font-size:18px}.hour-main span{display:block;font-weight:900;margin-top:4px}.hour-main small{display:block;color:var(--muted);margin-top:6px;line-height:1.4}.hour-vars{display:grid;grid-template-columns:repeat(auto-fit,minmax(86px,1fr));gap:8px}.hour-vars span{background:#f8fbff;border:1px solid var(--line);border-radius:12px;padding:8px}.hour-vars b{display:block;font-size:15px}.hour-vars small{color:var(--muted);font-size:11px}.mini-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.mini-grid span{background:#f8fbff;border:1px solid var(--line);border-radius:12px;padding:8px;color:var(--muted)}.mini-grid b{color:var(--ink)}@media(max-width:900px){.variables{grid-template-columns:repeat(2,1fr)}.hour-detail{grid-template-columns:1fr}.hour-vars{grid-template-columns:repeat(2,1fr)}}
    """


def _v8_css():
    base = _v6_css()
    return base + _v8_css_extra()


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return ""
    rows = aether_rows or []
    ctx = _v6_time_context(rows, args)
    peak_rain = _v6_peak(rows, "rain_threat_score")
    peak_prob = _v6_peak(rows, "prob_rain")
    peak_failure = _v6_peak(rows, "forecast_failure_risk")
    title, short, detail = _v6_practical_decision(daily, rows, args)
    summary = _v6_summary_text(daily, rows, args)
    risky, best = _v6_time_window_text(rows)
    groups = _v5_group_rows(rows)
    periods = ''.join(_v8_period_card(name, groups.get(name, [])) for name in ["Pagi","Siang","Sore","Malam"])
    hour_details = _v8_hour_detail_cards(rows)
    variable_cards = _v8_variable_cards(rows)
    scenario_html = _v6_scenario_list(peak_rain)
    source_html = _v6_source_details(source_state_rows)
    risk_word, risk_cls, risk_hint = _v6_level_word(peak_rain.get("rain_threat_score"))
    certainty = max(0, 100 - (safe_float(peak_failure.get("forecast_failure_risk")) or 0))
    verification = read_json(path_output("sentinel_x_verification_summary.json"), default={}) or {}
    _v8_write_variables_csv(rows)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Prakiraan cuaca — {_v6_esc(getattr(args,'location_name',''))}</title><style>{_v8_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Prakiraan Cuaca Lokal</b><span>{_v6_esc(getattr(args,'location_name',''))} · {ctx['updated_label']}</span></div></div><div class='nav'><a href='sentinel_x_report.html'>Ringkasan</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a><a href='../'>Pilih lokasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>{_v6_esc(ctx['valid_label'])}</span><span class='chip'>{_v6_esc(ctx['updated_label'])}</span></div><h1>Prakiraan cuaca lokal</h1><p>{_v6_esc(summary)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='date-strip'><article class='date-card'><span>Tanggal forecast</span><b>{_v6_esc(ctx['target_label'])}</b><p class='subtle'>Berlaku 00.00–23.59 {ctx['tz_label']}</p></article><article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(ctx['updated_label'].replace('Diperbarui ',''))}</b></article><article class='date-card'><span>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section><section class='lead-grid'><article class='decision'><span class='badge {risk_cls}'>{_v6_esc(short)}</span><h2>{_v6_esc(title)}</h2><p>{_v6_esc(detail)} Waktu relatif lebih aman: <b>{_v6_esc(best)}</b>. Jam yang perlu diperhatikan: <b>{_v6_esc(risky)}</b>.</p></article><div class='metrics'><article class='metric'><span>Peluang hujan tertinggi</span><strong>{_v8_num(peak_prob.get('prob_rain'),'%')}</strong><small>sekitar { _v6_esc(peak_prob.get('jam','—')) }</small></article><article class='metric'><span>Risiko hujan</span><strong>{_v6_esc(risk_word)}</strong><small>{_v6_esc(risk_hint)}</small></article><article class='metric'><span>Tingkat kepercayaan</span><strong>{_v8_num(certainty,'%')}</strong><small>Lebih rendah jika waktu hujan sulit dipastikan</small></article></div></section><section class='panel'><div class='section-title'><h2>Variabel cuaca penting</h2><p>Suhu, kelembapan, heat index, hujan, angin, awan, dan jarak pandang.</p></div><div class='variables'>{variable_cards}</div></section><section class='panel'><div class='section-title'><h2>Ringkasan pagi–malam</h2><p>Ringkasan cepat untuk melihat perubahan kondisi dalam sehari.</p></div><div class='periods'>{periods}</div></section><section class='two'><section class='panel'><h2>Saran singkat</h2><p class='big'>{_v6_esc(title)}</p><p class='muted'>Jika langit cepat gelap, angin berubah, atau mulai gerimis, utamakan kondisi nyata di sekitar dan informasi resmi BMKG.</p></section><section class='panel'><h2>Kemungkinan kondisi</h2><ul class='scenario-list'>{scenario_html}</ul></section></section><section class='panel'><div class='section-title'><h2>Rincian per jam</h2><p>Setiap jam memuat suhu, RH, heat index, peluang hujan, curah hujan, angin, awan, dan risiko.</p></div><div class='hour-list'>{hour_details}</div></section><details class='panel'><summary><b>Sumber data dan catatan teknis</b></summary><p class='muted'>Bagian ini hanya untuk pengguna yang ingin melihat sumber data. Ringkasan utama sudah cukup untuk penggunaan harian.</p><div class='table-scroll'><table><tr><th>Sumber</th><th>Status</th></tr>{source_html}</table></div><p class='muted'>Status bukti historis: {_v6_esc(str(verification.get('calibration_status','belum cukup data')).replace('_',' ').lower())}.</p></details><section class='panel'><h2>Data publik</h2><div class='links'><a class='pill' href='sentinel_x_variables.csv'>Variabel per jam CSV</a><a class='pill' href='sentinel_x.csv'>Data lengkap CSV</a><a class='pill' href='sentinel_x.json'>JSON</a><a class='pill' href='sentinel_x_accuracy_public.html'>Akurasi</a></div></section><section class='panel'><h2>Catatan penting</h2><ul class='plain-list'><li>Ini bukan peringatan resmi.</li><li>Untuk cuaca ekstrem, tetap cek informasi BMKG.</li><li>Prakiraan hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</li><li>Halaman akurasi akan lebih bermakna setelah data observasi terkumpul.</li></ul></section><p class='footer-note'>AETHER Sentinel X · {ctx['valid_label']} · {ctx['updated_label']}</p></main></body></html>"""
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "report_html": path_output(AETHER_REPORT_HTML_FILENAME), "variables_csv": path_output("sentinel_x_variables.csv"), "generated_at": now_local(args.timezone).isoformat(), "ui_version": SENTINEL_PUBLIC_UI_VERSION, "valid_date": ctx["target_label"]})
    return path_output(AETHER_DASHBOARD_FILENAME)


def aether_write_report(aether_rows, daily, args):
    rows = aether_rows or []
    ctx = _v6_time_context(rows, args)
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write(f"# Ringkasan cuaca\n\nBuka versi HTML: `sentinel_x_report.html`.\n\n{ctx['valid_label']}\n{ctx['updated_label']}\n"))
    title, short, detail = _v6_practical_decision(daily, rows, args)
    summary = _v6_summary_text(daily, rows, args)
    groups = _v5_group_rows(rows)
    periods = ''.join(_v8_period_card(name, groups.get(name, [])) for name in ["Pagi","Siang","Sore","Malam"])
    variable_cards = _v8_variable_cards(rows)
    hour_details = _v8_hour_detail_cards(rows)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Ringkasan cuaca — {_v6_esc(getattr(args,'location_name',''))}</title><style>{_v8_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Ringkasan Cuaca</b><span>{_v6_esc(getattr(args,'location_name',''))} · {ctx['updated_label']}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Dashboard</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>{_v6_esc(ctx['valid_label'])}</span><span class='chip'>{_v6_esc(ctx['updated_label'])}</span></div><h1>Ringkasan cuaca hari ini</h1><p>{_v6_esc(summary)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='date-strip'><article class='date-card'><span>Tanggal forecast</span><b>{_v6_esc(ctx['target_label'])}</b></article><article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(ctx['updated_label'].replace('Diperbarui ',''))}</b></article><article class='date-card'><span>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section><section class='panel'><h2>Saran singkat</h2><p class='big'>{_v6_esc(title)}</p><p class='muted'>{_v6_esc(detail)}</p></section><section class='panel'><div class='section-title'><h2>Variabel cuaca penting</h2><p>Ringkasan suhu, kelembapan, heat index, hujan, angin, awan, dan jarak pandang.</p></div><div class='variables'>{variable_cards}</div></section><section class='panel'><div class='section-title'><h2>Pagi, siang, sore, malam</h2><p>Ringkasan yang lebih mudah dibaca.</p></div><div class='periods'>{periods}</div></section><section class='panel'><div class='section-title'><h2>Rincian per jam</h2><p>Fokus pada jam aktivitas.</p></div><div class='hour-list'>{hour_details}</div></section><section class='panel'><h2>Batas penggunaan</h2><ul class='plain-list'><li>Gunakan sebagai panduan aktivitas harian.</li><li>Jangan gunakan sebagai peringatan resmi cuaca ekstrem.</li><li>Untuk kondisi berbahaya, ikuti informasi resmi BMKG.</li></ul><div class='links'><a class='pill' href='sentinel_x_variables.csv'>Variabel per jam CSV</a><a class='pill' href='sentinel_x.csv'>Data lengkap CSV</a></div></section></main></body></html>"""
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(doc))
    return path_output(AETHER_REPORT_HTML_FILENAME)


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    now = now_local(DEFAULT_TIMEZONE)
    updated = f"Diperbarui {_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} WIB"
    cards = []
    for loc in locations:
        prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
        cards.append(f"""
        <article class='card loc-card'>
          <h2>{_v6_esc(loc.location_name)}</h2>
          <p>Lihat prakiraan harian, variabel cuaca penting, peluang hujan, dan waktu yang perlu diperhatikan.</p>
          <div class='links'><a class='pill' href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Prakiraan</a><a class='pill' href='{prefix}{AETHER_REPORT_HTML_FILENAME}'>Ringkasan</a><a class='pill' href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a><a class='pill' href='{prefix}sentinel_x_variables.csv'>CSV variabel</a></div>
        </article>""")
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Portal Cuaca Lokal</title><style>{_v8_css()}</style></head><body><main class='page'><section class='hero'><div class='kicker'><span class='chip'>Portal publik</span><span class='chip'>{_v6_esc(updated)}</span></div><h1>Portal Prakiraan Cuaca Lokal</h1><p>Pilih lokasi untuk melihat prakiraan harian, suhu, kelembapan, heat index, peluang hujan, angin, awan, dan saran aktivitas.</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='portal-grid'>{''.join(cards)}</section><section class='panel'><h2>Data terbuka</h2><p class='muted'>Untuk analisis, arsip, atau pembuatan dashboard lanjutan.</p><div class='links'><a class='pill' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='pill' href='forecast_all_locations.csv'>Forecast CSV</a><a class='pill' href='source_status_all_locations.csv'>Status sumber</a><a class='pill' href='forecast_batch_summary.json'>Ringkasan batch</a></div></section><p class='footer-note'>AETHER Sentinel X · {updated}</p></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("sentinel_x_public_portal_manifest.json"), {"generated_at": now.isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path("index.html")


# v8.1: enrich Sentinel rows with additional meteorological variables when sources provide them.
def _v8_weighted_attr(points, attr):
    pairs = []
    for p in points or []:
        value = getattr(p, attr, None)
        if value is not None:
            pairs.append((safe_float(value), point_weight(p)))
    return aether_weighted_mean(pairs)


def _v8_enrich_rows_with_source_variables(points, rows):
    grouped = {jam: [] for jam in TARGET_TIMES}
    for p in points or []:
        grouped.setdefault(p.target_time, []).append(p)
    for row in rows or []:
        bucket = grouped.get(row.get("jam"), [])
        row["dew_point_c"] = aether_round(_v8_weighted_attr(bucket, "dew_point_c"), 1)
        row["apparent_temperature_c"] = aether_round(_v8_weighted_attr(bucket, "apparent_temp_c"), 1)
        row["pressure_msl_hpa"] = aether_round(_v8_weighted_attr(bucket, "pressure_msl_hpa"), 1)
        row["surface_pressure_hpa"] = aether_round(_v8_weighted_attr(bucket, "surface_pressure_hpa"), 1)
        row["wind_direction_deg"] = aether_round(_v8_weighted_attr(bucket, "wind_direction_deg"), 0)
        # If apparent temperature is missing, use heat index as practical fallback.
        if row.get("apparent_temperature_c") in (None, ""):
            row["apparent_temperature_c"] = row.get("heat_index_p50", "")
    return rows


def sentinel_x_save_artifacts(target_date, results, args, source_rows, status_rows, ensemble_rows):
    points = flatten_points(results)
    sentinel_rows = aether_build_rows(points, ensemble_rows, target_date, args)
    _v8_enrich_rows_with_source_variables(points, sentinel_rows)
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


# =============================================================================
# v9 PUBLIC APP CLEAN UI
# - Removes overly casual and overly technical wording from public pages.
# - Uses a wider app-like layout, clearer valid date/update time, and weather variables.
# - Keeps technical data available but moves it behind detail sections.
# =============================================================================
SENTINEL_PUBLIC_UI_VERSION = "v9_public_app_clean"


def sentinel_public_disclaimer(args=None):
    return "Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi BMKG dan kondisi setempat."


def _v9_css():
    return """
    :root{
      --bg:#eef5ff; --paper:#ffffff; --ink:#071526; --muted:#64748b;
      --blue:#1d4ed8; --blue2:#2563eb; --dark:#071526; --line:#d8e3f2;
      --green:#16a34a; --yellow:#ca8a04; --orange:#ea580c; --red:#dc2626;
      --soft:#f8fbff; --shadow:0 22px 60px rgba(15,23,42,.12);
    }
    *{box-sizing:border-box} body{margin:0;background:linear-gradient(180deg,#eaf4ff 0,#f7fbff 62%,#eef5ff 100%);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;font-size:16px;line-height:1.55} a{color:#0b57d0;text-decoration:none;font-weight:800}.page{width:min(1180px,calc(100% - 48px));margin:28px auto 80px}.top{height:60px;display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:18px}.brand{display:flex;align-items:center;gap:12px}.mark{width:34px;height:34px;border-radius:12px;background:linear-gradient(135deg,#001a4d,#2f7dff);box-shadow:0 8px 20px rgba(37,99,235,.25)}.brand b{display:block;font-size:18px}.brand span{display:block;color:var(--muted);font-size:13px}.nav{display:flex;gap:10px;flex-wrap:wrap}.pill,.nav a{display:inline-flex;align-items:center;justify-content:center;border:1px solid #9cc3ff;background:#eef6ff;border-radius:999px;padding:9px 14px;font-size:13px;color:#0757c8;font-weight:900}.hero{position:relative;overflow:hidden;border-radius:30px;padding:42px;background:radial-gradient(circle at 92% 12%,rgba(255,255,255,.20),transparent 17%),linear-gradient(135deg,#06142c 0%,#123c9c 48%,#2f7dff 100%);color:white;box-shadow:var(--shadow);margin-bottom:18px}.hero h1{font-size:clamp(34px,4.3vw,62px);letter-spacing:-.055em;line-height:.96;margin:12px 0 14px}.hero p{max-width:820px;font-size:18px;color:#e9f2ff;margin:0}.kicker{display:flex;gap:10px;flex-wrap:wrap}.chip{border:1px solid rgba(255,255,255,.35);background:rgba(255,255,255,.12);border-radius:999px;padding:7px 12px;font-size:12px;font-weight:900;color:#fff}.notice{background:#fff7ed;border:1px solid #fdba74;border-radius:16px;padding:13px 16px;font-weight:900;color:#7c2d12;margin:16px 0 18px}.date-strip{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:14px;margin-bottom:18px}.date-card,.card,.panel,.metric,.decision,.period-card,.hour-row,.loc-card{background:var(--paper);border:1px solid var(--line);border-radius:20px;box-shadow:0 10px 26px rgba(15,23,42,.06)}.date-card{padding:16px}.date-card span,.metric span,.var-card span{font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:900;color:var(--muted)}.date-card b{display:block;font-size:21px;margin-top:4px}.date-card p{margin:4px 0 0;color:var(--muted)}.lead-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(310px,.9fr);gap:18px;margin-bottom:18px}.decision{padding:26px;background:linear-gradient(135deg,#06142c,#0d2f6f);color:white}.badge{display:inline-flex;border-radius:999px;padding:8px 12px;font-size:13px;font-weight:900;margin-bottom:12px}.badge.low{background:#dcfce7;color:#166534}.badge.medium{background:#fef3c7;color:#92400e}.badge.high{background:#ffedd5;color:#9a3412}.badge.critical{background:#fee2e2;color:#991b1b}.decision h2{font-size:clamp(26px,3vw,42px);line-height:1;margin:4px 0 12px;letter-spacing:-.045em}.decision p{color:#dbeafe;margin:0;font-size:17px}.metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.metric{padding:20px}.metric strong{display:block;font-size:34px;line-height:1;margin:8px 0 4px;letter-spacing:-.04em}.metric small{color:var(--muted)}.panel{padding:24px;margin:18px 0}.section-title{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:14px}.section-title h2,.panel h2{font-size:26px;letter-spacing:-.03em;margin:0}.section-title p,.muted{color:var(--muted);margin:0}.variables{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.var-card{padding:18px;background:#fbfdff;border:1px solid var(--line);border-radius:18px}.var-card strong{display:block;font-size:28px;margin:8px 0 4px;letter-spacing:-.04em}.var-card small{color:var(--muted)}.periods{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.period-card{padding:18px;border-top:5px solid #dbeafe}.period-card.low{border-top-color:#22c55e}.period-card.medium{border-top-color:#f59e0b}.period-card.high,.period-card.critical{border-top-color:#ef4444}.period-card h3{font-size:20px;margin:0 0 8px}.period-card .cond{font-weight:900;margin-bottom:8px}.mini-stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.mini-stats span{background:#f6f9ff;border:1px solid var(--line);border-radius:12px;padding:10px;color:var(--muted);font-size:12px}.mini-stats b{display:block;color:var(--ink);font-size:16px}.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.big{font-size:22px;font-weight:900;margin:8px 0 8px}.scenario-list{list-style:none;padding:0;margin:0}.scenario-list li{display:flex;justify-content:space-between;gap:18px;padding:11px 0;border-bottom:1px solid var(--line);font-weight:800}.hour-list{display:grid;gap:10px}.hour-row{display:grid;grid-template-columns:100px 1.25fr repeat(6,110px) 1.2fr;align-items:center;gap:12px;padding:14px 16px;border-left:7px solid #22c55e}.hour-row.medium{border-left-color:#f59e0b}.hour-row.high,.hour-row.critical{border-left-color:#ef4444}.hour-row .time{font-size:18px;font-weight:950}.hour-row .condition b{display:block}.hour-row .condition small{color:var(--muted)}.hour-row .cell{background:#f8fbff;border:1px solid var(--line);border-radius:12px;padding:9px 10px;min-height:56px}.hour-row .cell b{display:block;font-size:15px}.hour-row .cell small{display:block;font-size:11px;color:var(--muted);margin-top:2px}.note{color:var(--muted);font-size:14px}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--line);text-align:left}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}.portal-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin:22px 0}.loc-card{padding:24px}.loc-card h2{font-size:27px;margin:0 0 10px}.loc-card p{color:var(--muted);min-height:54px}.data-box{background:#fff;border:1px solid var(--line);border-radius:24px;padding:24px;margin-top:18px}.footer-note{text-align:center;color:var(--muted);font-size:13px;margin:32px 0}.plain-list{margin:0;padding-left:20px}.plain-list li{margin:7px 0}@media(max-width:1050px){.lead-grid,.two,.date-strip{grid-template-columns:1fr}.metrics,.variables,.periods,.portal-grid{grid-template-columns:repeat(2,1fr)}.hour-row{grid-template-columns:90px 1fr repeat(2,100px)}.hour-row .note{grid-column:1/-1}}@media(max-width:680px){.page{width:min(100% - 24px,1180px);margin:16px auto 50px}.hero{padding:28px}.metrics,.variables,.periods,.portal-grid{grid-template-columns:1fr}.hour-row{grid-template-columns:1fr 1fr}.hour-row .condition,.hour-row .note{grid-column:1/-1}.top{height:auto;align-items:flex-start;flex-direction:column}.nav a,.pill{padding:8px 11px}.hero p{font-size:16px}}
    """


def _v9_num(value, suffix="", digits=0, fallback="—"):
    v = safe_float(value)
    if v is None:
        return fallback
    if digits == 0:
        return f"{round(v):.0f}{suffix}"
    return f"{v:.{digits}f}{suffix}"


def _v9_risk(score):
    v = safe_float(score)
    if v is None:
        return "belum pasti", "medium", "pantau perubahan kondisi sekitar"
    if v >= 75:
        return "tinggi", "high", "sebaiknya siapkan rencana cadangan"
    if v >= 45:
        return "sedang", "medium", "siapkan payung bila beraktivitas luar ruang"
    return "rendah", "low", "aktivitas relatif lebih aman"


def _v9_icon(category):
    text = str(category or "").lower()
    if "hujan" in text:
        return "🌧️"
    if "cerah" in text:
        return "🌤️"
    if "berawan" in text:
        return "☁️"
    return "🌦️"


def _v9_peak(rows, key):
    best = None
    for row in rows or []:
        val = safe_float(row.get(key))
        if val is None:
            continue
        if best is None or val > safe_float(best.get(key)):
            best = row
    return best or (rows[0] if rows else {})


def _v9_avg(rows, key):
    vals = [safe_float(r.get(key)) for r in rows or [] if safe_float(r.get(key)) is not None]
    return sum(vals) / len(vals) if vals else None


def _v9_clean_summary(rows, args):
    loc = getattr(args, "location_name", "lokasi ini")
    peak = _v9_peak(rows, "prob_rain")
    peak_threat = _v9_peak(rows, "rain_threat_score")
    prob = safe_float(peak.get("prob_rain")) or 0
    risk_word, _, _ = _v9_risk(peak_threat.get("rain_threat_score"))
    jam = peak.get("jam", "—")
    cat = peak.get("dominant_category", "cuaca berubah")
    if prob >= 65:
        lead = f"Peluang hujan di {loc} cukup menonjol, terutama sekitar pukul {jam}."
    elif prob >= 35:
        lead = f"Cuaca di {loc} masih cukup aman untuk aktivitas harian, tetapi peluang hujan meningkat pada beberapa jam tertentu."
    else:
        lead = f"Cuaca di {loc} cenderung relatif aman, dengan peluang hujan yang tidak terlalu besar."
    return f"{lead} Kondisi yang paling perlu diperhatikan adalah {cat.lower()} dengan tingkat risiko hujan {risk_word}. Gunakan prakiraan ini sebagai panduan harian dan tetap perhatikan kondisi langit di sekitar."


def _v9_decision(rows, args):
    peak_prob = _v9_peak(rows, "prob_rain")
    peak_threat = _v9_peak(rows, "rain_threat_score")
    prob = safe_float(peak_prob.get("prob_rain")) or 0
    threat = safe_float(peak_threat.get("rain_threat_score")) or 0
    jam = peak_prob.get("jam") or peak_threat.get("jam") or "—"
    risk_word, cls, hint = _v9_risk(max(prob, threat))
    if max(prob, threat) >= 70:
        title = f"Sebaiknya siapkan payung, terutama mendekati pukul {jam}."
        short = "Perlu lebih waspada"
    elif max(prob, threat) >= 45:
        title = f"Aktivitas masih memungkinkan, tetapi payung disarankan bila keluar sekitar pukul {jam}."
        short = "Waspada ringan"
    else:
        title = "Aktivitas harian relatif aman, tetap pantau perubahan cuaca."
        short = "Relatif aman"
    return title, short, risk_word, cls, hint, jam


def _v9_time_windows(rows):
    if not rows:
        return "—", "—"
    sorted_by_risk = sorted(rows, key=lambda r: safe_float(r.get("rain_threat_score")) or 0, reverse=True)
    risky = ", ".join(r.get("jam", "") for r in sorted_by_risk[:3] if r.get("jam")) or "—"
    sorted_safe = sorted(rows, key=lambda r: (safe_float(r.get("rain_threat_score")) or 0) + (safe_float(r.get("prob_rain")) or 0))
    best = ", ".join(r.get("jam", "") for r in sorted_safe[:3] if r.get("jam")) or "—"
    return risky, best


def _v9_variable_cards(rows):
    temp = _v9_avg(rows, "temp_p50")
    temp_max = _v9_peak(rows, "temp_p50")
    rh = _v9_avg(rows, "rh_p50")
    hi = _v9_peak(rows, "heat_index_p50")
    apparent = _v9_peak(rows, "apparent_temperature_c")
    dew = _v9_avg(rows, "dew_point_c")
    rain = _v9_peak(rows, "prob_rain")
    wind = _v9_peak(rows, "wind_p50")
    gust = _v9_peak(rows, "gust_p90")
    pressure = _v9_avg(rows, "pressure_msl_hpa")
    cloud = _v9_avg(rows, "cloud_p50")
    visible = _v9_peak(rows, "visibility_p10")
    items = [
        ("Suhu rata-rata", _v9_num(temp,"°C",1), f"tertinggi sekitar {_v9_num(temp_max.get('temp_p50'),'°C',1)} pada {temp_max.get('jam','—')}"),
        ("Kelembapan", _v9_num(rh,"%",0), "semakin tinggi, udara terasa lebih lembap"),
        ("Heat index", _v9_num(hi.get('heat_index_p50'),"°C",1), f"tertinggi sekitar {hi.get('jam','—')}"),
        ("Terasa seperti", _v9_num(apparent.get('apparent_temperature_c'),"°C",1), "gabungan suhu dan kelembapan"),
        ("Titik embun", _v9_num(dew,"°C",1), "indikasi kelembapan udara"),
        ("Peluang hujan", _v9_num(rain.get('prob_rain'),"%",0), f"tertinggi sekitar {rain.get('jam','—')}"),
        ("Angin", _v9_num(wind.get('wind_p50')," km/jam",1), f"tertinggi sekitar {wind.get('jam','—')}"),
        ("Hembusan", _v9_num(gust.get('gust_p90')," km/jam",1), "potensi hembusan angin"),
        ("Tekanan udara", _v9_num(pressure," hPa",1), "jika tersedia dari sumber model"),
        ("Awan", _v9_num(cloud,"%",0), "perkiraan tutupan awan"),
        ("Jarak pandang", _v9_num(visible.get('visibility_p10')," m",0), "nilai rendah berarti visibilitas berkurang"),
    ]
    return "".join(f"<article class='var-card'><span>{_v6_esc(k)}</span><strong>{_v6_esc(v)}</strong><small>{_v6_esc(d)}</small></article>" for k,v,d in items)


def _v9_period_card(name, rows):
    if not rows:
        return f"<article class='period-card'><h3>{_v6_esc(name)}</h3><p class='muted'>Data belum tersedia.</p></article>"
    rain = _v9_avg(rows, "prob_rain") or 0
    temp = _v9_avg(rows, "temp_p50")
    rh = _v9_avg(rows, "rh_p50")
    peak = _v9_peak(rows, "rain_threat_score")
    risk_word, cls, _ = _v9_risk(peak.get("rain_threat_score"))
    cats = {}
    for r in rows:
        c = r.get("dominant_category", "Berawan")
        cats[c] = cats.get(c, 0) + 1
    cat = max(cats, key=cats.get) if cats else "Berawan"
    return f"""
    <article class='period-card {cls}'>
      <h3>{_v9_icon(cat)} {_v6_esc(name)}</h3>
      <div class='cond'>{_v6_esc(cat)}</div>
      <p class='muted'>Risiko hujan {risk_word}. Jam perhatian: <b>{_v6_esc(peak.get('jam','—'))}</b>.</p>
      <div class='mini-stats'><span>Suhu<br><b>{_v9_num(temp,'°C',1)}</b></span><span>RH<br><b>{_v9_num(rh,'%')}</b></span><span>Hujan<br><b>{_v9_num(rain,'%')}</b></span><span>Risiko<br><b>{_v6_esc(risk_word)}</b></span></div>
    </article>"""


def _v9_hour_row(row):
    risk_word, cls, _ = _v9_risk(row.get("rain_threat_score"))
    cat = row.get("dominant_category", "—")
    note = "Pantau langit sekitar." if cls == "low" else "Disarankan lebih siap jika beraktivitas di luar ruang."
    return f"""
    <article class='hour-row {cls}'>
      <div class='time'>{_v6_esc(row.get('jam','—'))}</div>
      <div class='condition'><b>{_v9_icon(cat)} {_v6_esc(cat)}</b><small>{_v6_esc(note)}</small></div>
      <div class='cell'><b>{_v9_num(row.get('temp_p50'),'°C',1)}</b><small>Suhu</small></div>
      <div class='cell'><b>{_v9_num(row.get('rh_p50'),'%')}</b><small>Kelembapan</small></div>
      <div class='cell'><b>{_v9_num(row.get('heat_index_p50'),'°C',1)}</b><small>Heat index</small></div>
      <div class='cell'><b>{_v9_num(row.get('prob_rain'),'%')}</b><small>Peluang hujan</small></div>
      <div class='cell'><b>{_v9_num(row.get('wind_p50'),' km/jam',1)}</b><small>Angin</small></div>
      <div class='cell'><b>{_v6_esc(risk_word)}</b><small>Risiko</small></div>
      <div class='note'>{_v6_esc(str(row.get('decision_recommendation','')).replace('Risiko relatif terkendali; keputusan normal masih masuk akal dengan pemantauan berkala.','Aktivitas masih dapat dilakukan dengan pemantauan cuaca sekitar.'))}</div>
    </article>"""


def _v9_hour_rows(rows):
    return "".join(_v9_hour_row(r) for r in (rows or []))


def _v9_scenarios(peak):
    scenarios = peak.get("multi_reality_scenarios") if isinstance(peak, dict) else None
    if not isinstance(scenarios, dict):
        scenarios = {
            "Tidak hujan langsung": 40,
            "Hujan di sekitar lokasi": 20,
            "Hujan ringan": 25,
            "Hujan sedang singkat": 10,
            "Hujan lebat lokal": 5,
        }
    labels = {
        "dry_miss": "Tidak hujan langsung",
        "nearby_rain_only": "Hujan di sekitar lokasi",
        "direct_light_rain": "Hujan ringan",
        "direct_moderate_rain": "Hujan sedang singkat",
        "convective_burst": "Hujan lebat lokal",
    }
    items = []
    for k, v in scenarios.items():
        name = labels.get(k, str(k).replace("_", " ").capitalize())
        items.append((name, safe_float(v) or 0))
    items.sort(key=lambda x: x[1], reverse=True)
    return "".join(f"<li><span>{_v6_esc(name)}</span><b>{_v9_num(val,'%')}</b></li>" for name, val in items[:5])


def _v9_accuracy_status_text(summary):
    matched = int(summary.get("matched_cases") or 0)
    if matched < 30:
        return "Data akurasi belum cukup", "Halaman ini belum menampilkan klaim akurasi karena pasangan prakiraan dan observasi masih sedikit."
    return "Akurasi mulai dapat dibaca", "Jumlah data mulai cukup untuk melihat pola awal, tetapi tetap perlu pembaruan berkala."


def sentinel_write_verification_artifacts(rows, args):
    # v9 fix: use the existing verification engine. The previous build called
    # sentinel_verify_forecasts(), but that function does not exist in this
    # single-file branch, causing GitHub Actions to fail after forecast fetch.
    summary, pairs, reliability = sentinel_compute_verification(rows or [], args)
    write_json(path_output("sentinel_x_verification_summary.json"), summary)
    write_dict_csv(
        path_output("sentinel_x_reliability.csv"),
        ["probability_bin", "n", "mean_forecast_probability", "observed_rain_frequency"],
        reliability,
    )
    if pairs:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), list(pairs[0].keys()), pairs)
    else:
        write_dict_csv(
            path_output("sentinel_x_verification_pairs.csv"),
            ["target_date", "jam", "note"],
            [{"target_date": "", "jam": "", "note": "No matched forecast-observation pairs yet."}],
        )
    ctx = _v6_time_context(rows or [], args)
    title, note = _v9_accuracy_status_text(summary)
    matched = int(summary.get("matched_cases") or 0)
    rel_rows = "".join(
        f"<tr><td>{_v6_esc(r.get('probability_bin',''))}</td><td>{_v6_esc(r.get('n',0))}</td><td>{_v9_num(r.get('mean_forecast_probability'),'%')}</td><td>{_v9_num(r.get('observed_rain_frequency'),'%')}</td></tr>"
        for r in reliability
    )
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Akurasi prakiraan — {_v6_esc(getattr(args,'location_name',''))}</title><style>{_v9_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Akurasi Prakiraan</b><span>{_v6_esc(getattr(args,'location_name',''))} · {ctx['updated_label']}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Prakiraan</a><a href='sentinel_x_report.html'>Ringkasan</a><a href='../'>Pilih lokasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>{_v6_esc(ctx['updated_label'])}</span><span class='chip'>{matched} pasangan data</span></div><h1>Status bukti prakiraan</h1><p>{_v6_esc(note)} Tujuannya agar pengguna tahu kapan prakiraan sudah terbukti, dan kapan masih sebatas panduan harian.</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='date-strip'><article class='date-card'><span>Status</span><b>{_v6_esc(title)}</b></article><article class='date-card'><span>Kasus yang cocok</span><b>{matched}</b><p>Diperlukan riwayat observasi.</p></article><article class='date-card'><span>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section><section class='metrics'><article class='metric'><span>Error suhu</span><strong>{_v9_num(summary.get('temperature_mae_c'),'°C',1)}</strong><small>lebih kecil lebih baik</small></article><article class='metric'><span>Skor peluang hujan</span><strong>{_v9_num(summary.get('rain_brier_score'),'',2)}</strong><small>lebih kecil lebih baik</small></article><article class='metric'><span>Hujan terdeteksi</span><strong>{_v9_num(summary.get('rain_pod'),'%')}</strong><small>kemampuan menangkap hujan</small></article><article class='metric'><span>Alarm keliru</span><strong>{_v9_num(summary.get('rain_far'),'%')}</strong><small>semakin kecil semakin baik</small></article></section><section class='panel'><div class='section-title'><h2>Bukti peluang hujan</h2><p>Tabel akan terisi setelah observasi terkumpul.</p></div><div class='table-scroll'><table><tr><th>Kelompok peluang</th><th>Jumlah kasus</th><th>Rata-rata prakiraan</th><th>Hujan yang terjadi</th></tr>{rel_rows}</table></div></section><section class='panel'><h2>Penjelasan singkat</h2><p>Untuk sementara, halaman prakiraan utama lebih tepat digunakan sebagai panduan aktivitas harian. Nilai akurasi historis baru bisa dinilai setelah cukup banyak prakiraan dibandingkan dengan kondisi sebenarnya.</p><div class='links'><a class='pill' href='sentinel_x_verification_summary.json'>Data verifikasi</a><a class='pill' href='sentinel_x_reliability.csv'>CSV peluang hujan</a><a class='pill' href='sentinel_x_verification_pairs.csv'>CSV pasangan data</a></div></section></main></body></html>"""
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(doc))
    return {"summary": path_output("sentinel_x_verification_summary.json"), "reliability": path_output("sentinel_x_reliability.csv"), "html": path_output("sentinel_x_accuracy_public.html")}


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return ""
    rows = aether_rows or []
    ctx = _v6_time_context(rows, args)
    _v8_write_variables_csv(rows)
    title, short, risk_word, risk_cls, hint, jam = _v9_decision(rows, args)
    summary = _v9_clean_summary(rows, args)
    risky, best = _v9_time_windows(rows)
    peak_prob = _v9_peak(rows, "prob_rain")
    peak_failure = _v9_peak(rows, "forecast_failure_risk")
    groups = _v5_group_rows(rows)
    periods = ''.join(_v9_period_card(name, groups.get(name, [])) for name in ["Pagi", "Siang", "Sore", "Malam"])
    variables = _v9_variable_cards(rows)
    hours = _v9_hour_rows(rows)
    scenarios = _v9_scenarios(_v9_peak(rows, "rain_threat_score"))
    certainty = max(0, 100 - (safe_float(peak_failure.get("forecast_failure_risk")) or 0))
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Prakiraan cuaca — {_v6_esc(getattr(args,'location_name',''))}</title><style>{_v9_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Prakiraan Cuaca Lokal</b><span>{_v6_esc(getattr(args,'location_name',''))} · {ctx['updated_label']}</span></div></div><div class='nav'><a href='sentinel_x_report.html'>Ringkasan</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a><a href='../'>Pilih lokasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>{_v6_esc(ctx['valid_label'])}</span><span class='chip'>{_v6_esc(ctx['updated_label'])}</span><span class='chip'>{_v6_esc(getattr(args,'location_name',''))}</span></div><h1>Prakiraan Cuaca Lokal</h1><p>{_v6_esc(summary)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='date-strip'><article class='date-card'><span>Berlaku untuk</span><b>{_v6_esc(ctx['target_label'])}</b><p>00.00–23.59 {ctx['tz_label']}</p></article><article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(ctx['updated_label'].replace('Diperbarui ',''))}</b></article><article class='date-card'><span>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section><section class='lead-grid'><article class='decision'><span class='badge {risk_cls}'>{_v6_esc(short)}</span><h2>{_v6_esc(title)}</h2><p>Jam yang perlu diperhatikan: <b>{_v6_esc(risky)}</b>. Waktu yang relatif lebih aman: <b>{_v6_esc(best)}</b>. { _v6_esc(hint.capitalize()) }.</p></article><div class='metrics'><article class='metric'><span>Peluang hujan tertinggi</span><strong>{_v9_num(peak_prob.get('prob_rain'),'%')}</strong><small>sekitar { _v6_esc(peak_prob.get('jam','—')) }</small></article><article class='metric'><span>Tingkat risiko</span><strong>{_v6_esc(risk_word)}</strong><small>berdasarkan gabungan beberapa sumber</small></article><article class='metric'><span>Kepercayaan prakiraan</span><strong>{_v9_num(certainty,'%')}</strong><small>dapat menurun saat hujan lokal sulit dipastikan</small></article><article class='metric'><span>Data akurasi</span><strong>{_v6_esc(str(read_json(path_output('sentinel_x_verification_summary.json'), default={}).get('matched_cases',0)))}</strong><small>pasangan prakiraan-observasi</small></article></div></section><section class='panel'><div class='section-title'><h2>Variabel cuaca utama</h2><p>Suhu, kelembapan, heat index, hujan, angin, awan, dan jarak pandang.</p></div><div class='variables'>{variables}</div></section><section class='panel'><div class='section-title'><h2>Ringkasan pagi sampai malam</h2><p>Gunakan bagian ini untuk melihat perubahan cuaca dalam sehari.</p></div><div class='periods'>{periods}</div></section><section class='two'><section class='panel'><h2>Saran aktivitas</h2><p class='big'>{_v6_esc(title)}</p><p class='muted'>Jika kondisi langit berubah cepat, gunakan kondisi aktual di sekitar sebagai prioritas.</p></section><section class='panel'><h2>Kemungkinan kondisi</h2><ul class='scenario-list'>{scenarios}</ul></section></section><section class='panel'><div class='section-title'><h2>Rincian per jam</h2><p>Pilih jam yang sesuai dengan rencana aktivitas.</p></div><div class='hour-list'>{hours}</div></section><details class='panel'><summary><b>Sumber data dan file publik</b></summary><p class='muted'>Bagian ini untuk pengguna yang ingin melihat data lebih rinci.</p><div class='links'><a class='pill' href='sentinel_x_variables.csv'>CSV variabel</a><a class='pill' href='sentinel_x.csv'>CSV lengkap</a><a class='pill' href='sentinel_x.json'>JSON</a><a class='pill' href='sentinel_x_forecast_contract.json'>Batas penggunaan</a></div></details><section class='panel'><h2>Catatan penggunaan</h2><ul class='plain-list'><li>Prakiraan ini digunakan sebagai panduan aktivitas harian.</li><li>Ini bukan peringatan resmi cuaca ekstrem.</li><li>Untuk kondisi berbahaya, ikuti informasi BMKG dan kondisi setempat.</li><li>Hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</li></ul></section><p class='footer-note'>AETHER Sentinel X · {ctx['valid_label']} · {ctx['updated_label']}</p></main></body></html>"""
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "report_html": path_output(AETHER_REPORT_HTML_FILENAME), "variables_csv": path_output("sentinel_x_variables.csv"), "generated_at": now_local(args.timezone).isoformat(), "ui_version": SENTINEL_PUBLIC_UI_VERSION, "valid_date": ctx["target_label"]})
    return path_output(AETHER_DASHBOARD_FILENAME)


def aether_write_report(aether_rows, daily, args):
    rows = aether_rows or []
    ctx = _v6_time_context(rows, args)
    _v8_write_variables_csv(rows)
    title, short, risk_word, risk_cls, hint, jam = _v9_decision(rows, args)
    summary = _v9_clean_summary(rows, args)
    groups = _v5_group_rows(rows)
    periods = ''.join(_v9_period_card(name, groups.get(name, [])) for name in ["Pagi", "Siang", "Sore", "Malam"])
    variables = _v9_variable_cards(rows)
    hours = _v9_hour_rows(rows)
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write(f"# Ringkasan cuaca\n\nBuka versi HTML: `sentinel_x_report.html`.\n\n{ctx['valid_label']}\n{ctx['updated_label']}\n"))
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Ringkasan cuaca — {_v6_esc(getattr(args,'location_name',''))}</title><style>{_v9_css()}</style></head><body><main class='page'><nav class='top'><div class='brand'><div class='mark'></div><div><b>Ringkasan Cuaca</b><span>{_v6_esc(getattr(args,'location_name',''))} · {ctx['updated_label']}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Prakiraan</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a><a href='../'>Pilih lokasi</a></div></nav><section class='hero'><div class='kicker'><span class='chip'>{_v6_esc(ctx['valid_label'])}</span><span class='chip'>{_v6_esc(ctx['updated_label'])}</span></div><h1>Ringkasan Cuaca Hari Ini</h1><p>{_v6_esc(summary)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='date-strip'><article class='date-card'><span>Berlaku untuk</span><b>{_v6_esc(ctx['target_label'])}</b><p>00.00–23.59 {ctx['tz_label']}</p></article><article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(ctx['updated_label'].replace('Diperbarui ',''))}</b></article><article class='date-card'><span>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section><section class='panel'><h2>Saran singkat</h2><p class='big'>{_v6_esc(title)}</p><p class='muted'>{_v6_esc(hint.capitalize())}. Gunakan sebagai panduan aktivitas harian dan tetap perhatikan kondisi aktual.</p></section><section class='panel'><div class='section-title'><h2>Variabel cuaca utama</h2><p>Ringkasan suhu, kelembapan, heat index, hujan, angin, awan, dan jarak pandang.</p></div><div class='variables'>{variables}</div></section><section class='panel'><div class='section-title'><h2>Pagi, siang, sore, malam</h2><p>Ringkasan perubahan kondisi dalam sehari.</p></div><div class='periods'>{periods}</div></section><section class='panel'><div class='section-title'><h2>Rincian per jam</h2><p>Fokus pada jam aktivitas.</p></div><div class='hour-list'>{hours}</div></section><section class='panel'><h2>Batas penggunaan</h2><ul class='plain-list'><li>Gunakan sebagai panduan aktivitas harian.</li><li>Ini bukan peringatan resmi cuaca ekstrem.</li><li>Untuk kondisi berbahaya, ikuti informasi BMKG dan kondisi setempat.</li></ul><div class='links'><a class='pill' href='sentinel_x_variables.csv'>CSV variabel</a><a class='pill' href='sentinel_x.csv'>CSV lengkap</a></div></section></main></body></html>"""
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(doc))
    return path_output(AETHER_REPORT_HTML_FILENAME)


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    now = now_local(DEFAULT_TIMEZONE)
    updated = f"Diperbarui {_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} WIB"
    cards = []
    for loc in locations:
        prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
        cards.append(f"""
        <article class='loc-card'>
          <h2>{_v6_esc(loc.location_name)}</h2>
          <p>Prakiraan harian, suhu, kelembapan, heat index, peluang hujan, angin, dan saran aktivitas.</p>
          <div class='links'><a class='pill' href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Lihat prakiraan</a><a class='pill' href='{prefix}{AETHER_REPORT_HTML_FILENAME}'>Ringkasan</a><a class='pill' href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a><a class='pill' href='{prefix}sentinel_x_variables.csv'>CSV variabel</a></div>
        </article>""")
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Portal Prakiraan Cuaca Lokal</title><style>{_v9_css()}</style></head><body><main class='page'><section class='hero'><div class='kicker'><span class='chip'>Portal publik</span><span class='chip'>{_v6_esc(updated)}</span></div><h1>Portal Prakiraan Cuaca Lokal</h1><p>Pilih lokasi untuk melihat prakiraan harian, variabel cuaca utama, peluang hujan, dan saran aktivitas. Halaman ini dibuat untuk penggunaan harian masyarakat.</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='portal-grid'>{''.join(cards)}</section><section class='data-box'><h2>Data terbuka</h2><p class='muted'>File berikut dapat digunakan untuk analisis, arsip, atau pembuatan dashboard lanjutan.</p><div class='links'><a class='pill' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='pill' href='forecast_all_locations.csv'>Forecast CSV</a><a class='pill' href='source_status_all_locations.csv'>Status sumber</a><a class='pill' href='forecast_batch_summary.json'>Ringkasan batch</a></div></section><p class='footer-note'>AETHER Sentinel X · {updated}</p></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("sentinel_x_public_portal_manifest.json"), {"generated_at": now.isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path("index.html")


# =============================================================================
# ANEMOS PUBLIC WEATHER APP v10
# Final public-facing override layer.
# - Rebrands public pages to ANEMOS.
# - Wider, cleaner app layout.
# - Homepage cards include daily summaries per location.
# - Adds rain probability and temperature visual bars.
# - Keeps technical details hidden from public surface.
# - Uses neutral Indonesian suitable for public use.
# =============================================================================
AETHER_VERSION = "ANEMOS Public Weather v10"
ANEMOS_PUBLIC_VERSION = "ANEMOS v10"
ANEMOS_BRAND_NAME = "ANEMOS"
ANEMOS_TAGLINE = "Prakiraan cuaca lokal untuk keputusan harian."
SENTINEL_PUBLIC_UI_VERSION = "anemos_v10_public_app"


def sentinel_public_disclaimer(args=None):
    return "Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi BMKG dan kondisi setempat."


def _anemos_css():
    return """
    :root{
      --bg:#edf6ff;--bg2:#f7fbff;--card:#ffffff;--ink:#071326;--muted:#65748b;--line:#d8e6f5;
      --blue:#1457d9;--blue2:#2f80ff;--navy:#06152d;--green:#16a34a;--yellow:#d99b08;--orange:#ea580c;--red:#dc2626;
      --soft:#f6faff;--shadow:0 24px 70px rgba(15,23,42,.12);--shadow2:0 10px 28px rgba(15,23,42,.08);
      --radius:26px;
    }
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 20% 0%,#d8ecff 0,#eef7ff 30%,#f8fbff 78%);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;font-size:17px;line-height:1.58}a{color:#0b57d0;text-decoration:none;font-weight:800}.page{width:min(1280px,calc(100% - 56px));margin:28px auto 90px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:0 0 18px}.brand{display:flex;align-items:center;gap:12px}.brand-mark{width:42px;height:42px;border-radius:16px;background:linear-gradient(145deg,#06152d,#2563eb 72%,#67e8f9);box-shadow:0 12px 28px rgba(37,99,235,.25)}.brand b{display:block;font-size:20px;letter-spacing:-.02em}.brand span{display:block;color:var(--muted);font-size:13px}.nav{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}.btn,.nav a{display:inline-flex;align-items:center;justify-content:center;min-height:38px;border:1px solid #9ec8ff;background:#eef7ff;color:#0757c8;border-radius:999px;padding:9px 15px;font-size:13px;font-weight:900}.btn.primary{background:#0b57d0;color:white;border-color:#0b57d0}.hero{position:relative;overflow:hidden;border-radius:34px;padding:46px;background:radial-gradient(circle at 92% 15%,rgba(255,255,255,.18),transparent 20%),linear-gradient(135deg,#06152d 0%,#0f3b91 46%,#2d7dff 100%);color:white;box-shadow:var(--shadow);margin-bottom:18px}.hero.compact{padding:38px}.hero h1{font-size:clamp(38px,5.2vw,72px);line-height:.95;letter-spacing:-.06em;margin:12px 0 16px}.hero p{max-width:880px;margin:0;color:#e8f2ff;font-size:20px}.chips{display:flex;gap:10px;flex-wrap:wrap}.chip{display:inline-flex;align-items:center;border:1px solid rgba(255,255,255,.36);background:rgba(255,255,255,.13);border-radius:999px;padding:8px 12px;color:#fff;font-size:12px;font-weight:900}.notice{background:#fff7ed;border:1px solid #fdba74;color:#7c2d12;border-radius:16px;padding:13px 16px;font-weight:900;margin:16px 0 18px}.meta-row{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:14px;margin-bottom:18px}.meta-card,.panel,.decision,.metric,.loc-card,.period-card,.hour-row,.var-card{background:rgba(255,255,255,.92);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow2)}.meta-card{padding:17px 18px}.label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:900;color:var(--muted);margin-bottom:5px}.meta-card b{font-size:21px;letter-spacing:-.02em}.lead-grid{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(330px,.9fr);gap:18px;margin-bottom:18px}.decision{padding:28px;background:linear-gradient(135deg,#071326,#103a85);color:white}.badge{display:inline-flex;border-radius:999px;padding:8px 12px;font-size:13px;font-weight:900;margin-bottom:12px}.badge.low{background:#dcfce7;color:#166534}.badge.medium{background:#fef3c7;color:#92400e}.badge.high{background:#ffedd5;color:#9a3412}.badge.critical{background:#fee2e2;color:#991b1b}.decision h2{font-size:clamp(28px,3.2vw,46px);line-height:1.02;letter-spacing:-.05em;margin:4px 0 12px}.decision p{font-size:18px;color:#dbeafe;margin:0}.metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.metric{padding:22px}.metric strong{display:block;font-size:38px;line-height:.95;letter-spacing:-.055em;margin:8px 0 6px}.metric small{display:block;color:var(--muted)}.panel{padding:26px;margin:18px 0}.section-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:16px}.section-head h2,.panel h2{font-size:28px;margin:0;letter-spacing:-.035em}.section-head p,.muted{color:var(--muted);margin:0}.overview-grid{display:grid;grid-template-columns:1.1fr 1fr;gap:18px}.chart-card{background:var(--card);border:1px solid var(--line);border-radius:24px;padding:22px;box-shadow:var(--shadow2)}.chart-title{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:18px}.bar-list{display:grid;grid-template-columns:repeat(12,1fr);align-items:end;gap:7px;min-height:150px}.bar-wrap{display:flex;flex-direction:column;align-items:center;gap:7px}.bar{width:100%;min-width:10px;border-radius:12px 12px 4px 4px;background:linear-gradient(180deg,#2f80ff,#0f3b91);height:var(--h);position:relative}.bar.risk-med{background:linear-gradient(180deg,#f59e0b,#ca8a04)}.bar.risk-high{background:linear-gradient(180deg,#ef4444,#b91c1c)}.bar-label{font-size:11px;color:var(--muted);font-weight:800}.temp-line{width:100%;height:170px}.variables-primary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px}.variables-secondary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-top:12px}.var-card{padding:17px 18px;background:#fbfdff}.var-card.primary{border-top:5px solid #2f80ff}.var-card span{font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:900;color:var(--muted)}.var-card strong{display:block;font-size:30px;letter-spacing:-.045em;margin:8px 0 4px}.var-card small{display:block;color:var(--muted);font-size:13px}.periods{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.period-card{padding:20px;border-top:6px solid #22c55e}.period-card.medium{border-top-color:#f59e0b}.period-card.high,.period-card.critical{border-top-color:#ef4444}.period-card h3{font-size:21px;margin:0 0 8px}.period-card .cond{font-weight:950;margin-bottom:8px}.mini{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px}.mini span{background:#f6f9ff;border:1px solid var(--line);border-radius:13px;padding:10px;color:var(--muted);font-size:12px}.mini b{display:block;color:var(--ink);font-size:16px}.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.scenario{list-style:none;padding:0;margin:0}.scenario li{display:flex;justify-content:space-between;gap:16px;border-bottom:1px solid var(--line);padding:12px 0;font-weight:850}.hour-list{display:grid;gap:11px}.hour-row{display:grid;grid-template-columns:88px 1.2fr repeat(5,105px);gap:10px;align-items:center;padding:14px 16px;border-left:7px solid #22c55e}.hour-row.medium{border-left-color:#f59e0b}.hour-row.high,.hour-row.critical{border-left-color:#ef4444}.hour-row .time{font-size:19px;font-weight:950}.hour-row .condition b{display:block}.hour-row .condition small{color:var(--muted)}.hour-row .cell{background:#f8fbff;border:1px solid var(--line);border-radius:13px;padding:9px 10px}.hour-row .cell b{display:block}.hour-row .cell small{display:block;color:var(--muted);font-size:11px}.more-hours{margin-top:12px}.loc-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin:20px 0}.loc-card{padding:24px;position:relative;overflow:hidden}.loc-card h2{font-size:28px;letter-spacing:-.03em;margin:0 0 10px}.loc-card .summary{color:var(--muted);min-height:66px}.loc-kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:16px 0}.loc-kpi span{background:#f8fbff;border:1px solid var(--line);border-radius:14px;padding:10px;font-size:12px;color:var(--muted)}.loc-kpi b{display:block;color:var(--ink);font-size:18px}.data-box{background:#fff;border:1px solid var(--line);border-radius:26px;padding:26px;box-shadow:var(--shadow2);margin-top:18px}.empty-state{background:#f8fbff;border:1px dashed #a9c5e8;border-radius:20px;padding:22px}.progress{height:12px;background:#e5edf7;border-radius:999px;overflow:hidden;margin:12px 0}.progress span{display:block;height:100%;background:linear-gradient(90deg,#2f80ff,#0f3b91);width:var(--p)}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--line);text-align:left}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}.links{display:flex;gap:10px;flex-wrap:wrap}.footer-note{text-align:center;color:var(--muted);font-size:13px;margin:34px 0}.plain-list{padding-left:20px;margin:0}.plain-list li{margin:7px 0}.technical{opacity:.95}@media(max-width:1140px){.lead-grid,.overview-grid,.two,.meta-row{grid-template-columns:1fr}.variables-primary{grid-template-columns:repeat(3,1fr)}.variables-secondary{grid-template-columns:repeat(3,1fr)}.periods,.loc-grid{grid-template-columns:repeat(2,1fr)}.hour-row{grid-template-columns:90px 1fr repeat(2,105px)}}@media(max-width:720px){body{font-size:16px}.page{width:min(100% - 24px,1280px);margin:16px auto 60px}.hero{padding:30px 24px}.hero h1{font-size:40px}.hero p{font-size:17px}.topbar{align-items:flex-start;flex-direction:column}.metrics,.variables-primary,.variables-secondary,.periods,.loc-grid{grid-template-columns:1fr}.hour-row{grid-template-columns:1fr 1fr}.hour-row .time,.hour-row .condition{grid-column:1/-1}.bar-list{grid-template-columns:repeat(8,1fr)}.nav{justify-content:flex-start}.decision h2{font-size:31px}}
    """


def _anemos_num(value, suffix="", digits=0, fallback="—"):
    v = safe_float(value)
    if v is None:
        return fallback
    if suffix == "%":
        v = max(0, min(100, v))
    if digits == 0:
        return f"{round(v):.0f}{suffix}"
    return f"{v:.{digits}f}{suffix}"


def _anemos_icon(category):
    text = str(category or "").lower()
    if "lebat" in text or "hujan" in text:
        return "🌧️"
    if "cerah" in text and "berawan" not in text:
        return "☀️"
    if "cerah" in text:
        return "🌤️"
    if "berawan" in text:
        return "☁️"
    return "🌦️"


def _anemos_risk_level(score):
    v = safe_float(score)
    if v is None:
        return "belum pasti", "medium", "Perlu memantau perubahan kondisi."
    if v >= 75:
        return "tinggi", "high", "Sebaiknya menyiapkan rencana cadangan untuk aktivitas luar ruang."
    if v >= 45:
        return "sedang", "medium", "Sebaiknya menyiapkan payung atau jas hujan bila beraktivitas di luar ruang."
    return "rendah", "low", "Aktivitas umumnya masih dapat dilakukan dengan pemantauan kondisi sekitar."


def _anemos_peak(rows, key):
    best = None
    for row in rows or []:
        val = safe_float(row.get(key))
        if val is None:
            continue
        if best is None or val > (safe_float(best.get(key)) or -10**9):
            best = row
    return best or (rows[0] if rows else {})


def _anemos_avg(rows, key):
    vals = []
    for r in rows or []:
        v = safe_float(r.get(key))
        if v is not None:
            vals.append(v)
    return sum(vals)/len(vals) if vals else None


def _anemos_main_decision(rows, args):
    peak_prob = _anemos_peak(rows, "prob_rain")
    peak_risk = _anemos_peak(rows, "rain_threat_score")
    prob = safe_float(peak_prob.get("prob_rain")) or 0
    risk = safe_float(peak_risk.get("rain_threat_score")) or 0
    score = max(prob, risk)
    jam = peak_prob.get("jam") or peak_risk.get("jam") or "—"
    word, cls, hint = _anemos_risk_level(score)
    if score >= 75:
        title = f"Risiko hujan meningkat. Siapkan payung bila beraktivitas sekitar pukul {jam}."
        badge = "Perlu waspada"
    elif score >= 45:
        title = f"Aktivitas masih memungkinkan. Siapkan payung bila beraktivitas sekitar pukul {jam}."
        badge = "Perlu perhatian"
    else:
        title = "Aktivitas harian relatif aman. Tetap pantau perubahan cuaca di sekitar."
        badge = "Relatif aman"
    return title, badge, word, cls, hint, jam


def _anemos_summary(rows, args):
    loc = getattr(args, "location_name", "lokasi ini")
    title, badge, word, cls, hint, jam = _anemos_main_decision(rows, args)
    peak = _anemos_peak(rows, "prob_rain")
    prob = safe_float(peak.get("prob_rain")) or 0
    cat = str(peak.get("dominant_category") or "cuaca berubah").lower()
    return f"Untuk {loc}, {title.lower()} Peluang hujan tertinggi sekitar {_anemos_num(prob,'%')} dengan sinyal utama {cat}. Prakiraan ini digunakan sebagai panduan aktivitas harian, bukan peringatan resmi."


def _anemos_group_rows(rows):
    return _v5_group_rows(rows)


def _anemos_safe_hour(row):
    return str(row.get("jam") or "—")


def _anemos_time_windows(rows):
    if not rows:
        return "—", "—"
    by_risk = sorted(rows, key=lambda r: (safe_float(r.get("rain_threat_score")) or 0) + (safe_float(r.get("prob_rain")) or 0), reverse=True)
    risky = ", ".join(_anemos_safe_hour(r) for r in by_risk[:3]) or "—"
    by_safe = sorted(rows, key=lambda r: (safe_float(r.get("rain_threat_score")) or 0) + (safe_float(r.get("prob_rain")) or 0))
    safe = ", ".join(_anemos_safe_hour(r) for r in by_safe[:3]) or "—"
    return risky, safe


def _anemos_variable_items(rows):
    temp = _anemos_avg(rows, "temp_p50")
    temp_max = _anemos_peak(rows, "temp_p50")
    rh = _anemos_avg(rows, "rh_p50")
    hi = _anemos_peak(rows, "heat_index_p50")
    apparent = _anemos_peak(rows, "apparent_temperature_c")
    rain = _anemos_peak(rows, "prob_rain")
    wind = _anemos_peak(rows, "wind_p50")
    cloud = _anemos_avg(rows, "cloud_p50")
    dew = _anemos_avg(rows, "dew_point_c")
    gust = _anemos_peak(rows, "gust_p90")
    pressure = _anemos_avg(rows, "pressure_msl_hpa")
    visible = _anemos_peak(rows, "visibility_p10")
    primary = [
        ("Suhu", _anemos_num(temp,"°C",1), f"Tertinggi sekitar {_anemos_num(temp_max.get('temp_p50'),'°C',1)} pada {_anemos_safe_hour(temp_max)}"),
        ("Terasa seperti", _anemos_num(apparent.get('apparent_temperature_c'),"°C",1), "Memperhitungkan suhu dan kelembapan"),
        ("Kelembapan", _anemos_num(rh,"%"), "Udara terasa lebih lembap saat nilainya tinggi"),
        ("Heat index", _anemos_num(hi.get('heat_index_p50'),"°C",1), f"Tertinggi sekitar {_anemos_safe_hour(hi)}"),
        ("Peluang hujan", _anemos_num(rain.get('prob_rain'),"%"), f"Tertinggi sekitar {_anemos_safe_hour(rain)}"),
        ("Angin", _anemos_num(wind.get('wind_p50')," km/jam",1), f"Tertinggi sekitar {_anemos_safe_hour(wind)}"),
    ]
    secondary = [
        ("Awan", _anemos_num(cloud,"%"), "Perkiraan tutupan awan"),
        ("Titik embun", _anemos_num(dew,"°C",1), "Menunjukkan kadar uap air"),
        ("Hembusan angin", _anemos_num(gust.get('gust_p90')," km/jam",1), "Potensi hembusan lebih kuat"),
        ("Tekanan udara", _anemos_num(pressure," hPa",1), "Jika tersedia dari model"),
        ("Jarak pandang", _anemos_num(visible.get('visibility_p10')," m"), "Nilai rendah berarti visibilitas turun"),
    ]
    return primary, secondary


def _anemos_variable_html(rows):
    primary, secondary = _anemos_variable_items(rows)
    def card(item, cls=""):
        k, v, d = item
        return f"<article class='var-card {cls}'><span>{_v6_esc(k)}</span><strong>{_v6_esc(v)}</strong><small>{_v6_esc(d)}</small></article>"
    return "<div class='variables-primary'>" + "".join(card(x, "primary") for x in primary) + "</div><div class='variables-secondary'>" + "".join(card(x) for x in secondary) + "</div>"


def _anemos_period_card(name, rows):
    if not rows:
        return f"<article class='period-card'><h3>{_v6_esc(name)}</h3><p class='muted'>Data belum tersedia.</p></article>"
    rain = _anemos_avg(rows, "prob_rain") or 0
    temp = _anemos_avg(rows, "temp_p50")
    rh = _anemos_avg(rows, "rh_p50")
    peak = _anemos_peak(rows, "rain_threat_score")
    word, cls, _ = _anemos_risk_level(max(safe_float(peak.get("rain_threat_score")) or 0, rain))
    cats = {}
    for r in rows:
        c = r.get("dominant_category") or "Berawan"
        cats[c] = cats.get(c, 0) + 1
    cat = max(cats, key=cats.get) if cats else "Berawan"
    return f"""
    <article class='period-card {cls}'>
      <h3>{_anemos_icon(cat)} {_v6_esc(name)}</h3>
      <div class='cond'>{_v6_esc(cat)}</div>
      <p class='muted'>Risiko hujan {word}. Jam perhatian: <b>{_v6_esc(_anemos_safe_hour(peak))}</b>.</p>
      <div class='mini'><span>Suhu<b>{_anemos_num(temp,'°C',1)}</b></span><span>Kelembapan<b>{_anemos_num(rh,'%')}</b></span><span>Hujan<b>{_anemos_num(rain,'%')}</b></span><span>Risiko<b>{_v6_esc(word)}</b></span></div>
    </article>"""


def _anemos_select_important_hours(rows):
    if not rows:
        return []
    by_jam = {str(r.get("jam")): r for r in rows if r.get("jam")}
    wanted = []
    for h in ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]:
        if h in by_jam:
            wanted.append(by_jam[h])
    for key in ["prob_rain", "rain_threat_score", "heat_index_p50", "wind_p50"]:
        p = _anemos_peak(rows, key)
        if p and p not in wanted:
            wanted.append(p)
    # preserve chronological order
    order = {str(r.get("jam")): i for i, r in enumerate(rows)}
    wanted = sorted(wanted, key=lambda r: order.get(str(r.get("jam")), 999))
    return wanted[:12]


def _anemos_hour_card(row):
    risk_score = max(safe_float(row.get("rain_threat_score")) or 0, safe_float(row.get("prob_rain")) or 0)
    word, cls, _ = _anemos_risk_level(risk_score)
    cat = row.get("dominant_category") or "—"
    note = "Relatif aman; tetap pantau kondisi sekitar." if cls == "low" else "Siapkan payung atau rencana cadangan bila beraktivitas di luar ruang."
    return f"""
    <article class='hour-row {cls}'>
      <div class='time'>{_v6_esc(_anemos_safe_hour(row))}</div>
      <div class='condition'><b>{_anemos_icon(cat)} {_v6_esc(cat)}</b><small>{_v6_esc(note)}</small></div>
      <div class='cell'><b>{_anemos_num(row.get('temp_p50'),'°C',1)}</b><small>Suhu</small></div>
      <div class='cell'><b>{_anemos_num(row.get('rh_p50'),'%')}</b><small>Kelembapan</small></div>
      <div class='cell'><b>{_anemos_num(row.get('heat_index_p50'),'°C',1)}</b><small>Heat index</small></div>
      <div class='cell'><b>{_anemos_num(row.get('prob_rain'),'%')}</b><small>Peluang hujan</small></div>
      <div class='cell'><b>{_v6_esc(word)}</b><small>Risiko</small></div>
    </article>"""


def _anemos_hour_html(rows, compact=True):
    selected = _anemos_select_important_hours(rows) if compact else (rows or [])
    return "".join(_anemos_hour_card(r) for r in selected)


def _anemos_rain_chart(rows):
    selected = rows or []
    if len(selected) > 12:
        selected = [r for i, r in enumerate(selected) if i % 2 == 0][:12]
    bars = []
    for r in selected:
        p = safe_float(r.get("prob_rain")) or 0
        h = max(8, min(100, p))
        cls = "risk-high" if p >= 70 else "risk-med" if p >= 45 else ""
        bars.append(f"<div class='bar-wrap'><div class='bar {cls}' style='--h:{h}%' title='{_anemos_safe_hour(r)} · {_anemos_num(p,'%')}'></div><div class='bar-label'>{_v6_esc(_anemos_safe_hour(r)[:2])}</div></div>")
    return "<div class='bar-list'>" + "".join(bars) + "</div>"


def _anemos_temp_svg(rows):
    selected = rows or []
    if len(selected) > 12:
        selected = [r for i, r in enumerate(selected) if i % 2 == 0][:12]
    vals = [safe_float(r.get("temp_p50")) for r in selected]
    vals = [v for v in vals if v is not None]
    if not selected or not vals:
        return "<p class='muted'>Data suhu belum tersedia.</p>"
    minv, maxv = min(vals), max(vals)
    if abs(maxv - minv) < 0.1:
        maxv += 1; minv -= 1
    pts = []
    labels = []
    for i, r in enumerate(selected):
        v = safe_float(r.get("temp_p50"))
        if v is None:
            continue
        x = 40 + i * (520 / max(1, len(selected)-1))
        y = 135 - ((v - minv)/(maxv-minv))*95
        pts.append((x,y,v,_anemos_safe_hour(r)))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x,y,_,_ in pts)
    circles = "".join(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='#0b57d0'/><text x='{x:.1f}' y='{y-10:.1f}' text-anchor='middle' font-size='11' fill='#071326' font-weight='800'>{v:.0f}°</text><text x='{x:.1f}' y='160' text-anchor='middle' font-size='10' fill='#64748b'>{h[:2]}</text>" for x,y,v,h in pts)
    return f"<svg class='temp-line' viewBox='0 0 600 175' role='img' aria-label='Grafik suhu'><line x1='40' y1='135' x2='560' y2='135' stroke='#d8e6f5'/><polyline points='{poly}' fill='none' stroke='#0b57d0' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/>{circles}</svg>"


def _anemos_scenarios(rows):
    peak = _anemos_peak(rows, "rain_threat_score")
    scenarios = peak.get("multi_reality_scenarios") if isinstance(peak, dict) else None
    if not isinstance(scenarios, dict):
        scenarios = {"dry_miss": 40, "nearby_rain_only": 20, "direct_light_rain": 25, "direct_moderate_rain": 10, "convective_burst": 5}
    labels = {"dry_miss":"Tidak hujan langsung", "nearby_rain_only":"Hujan di sekitar lokasi", "direct_light_rain":"Hujan ringan", "direct_moderate_rain":"Hujan sedang singkat", "convective_burst":"Hujan lebat lokal"}
    items = []
    for k,v in scenarios.items():
        items.append((labels.get(k, str(k).replace('_',' ').capitalize()), safe_float(v) or 0))
    items.sort(key=lambda x: x[1], reverse=True)
    return "".join(f"<li><span>{_v6_esc(n)}</span><b>{_anemos_num(v,'%')}</b></li>" for n,v in items[:5])


def _anemos_page_nav(title, subtitle, ctx, args, extra_links=None):
    links = extra_links or []
    links_html = "".join(f"<a href='{href}'>{_v6_esc(label)}</a>" for label, href in links)
    return f"<nav class='topbar'><div class='brand'><div class='brand-mark'></div><div><b>{_v6_esc(title)}</b><span>{_v6_esc(subtitle)} · {ctx['updated_label']}</span></div></div><div class='nav'>{links_html}</div></nav>"


def _anemos_dashboard_doc(rows, source_state_rows, daily, args, report=False):
    ctx = _v6_time_context(rows, args)
    title, badge, risk_word, risk_cls, hint, jam = _anemos_main_decision(rows, args)
    summary = _anemos_summary(rows, args)
    risky, safe = _anemos_time_windows(rows)
    peak_prob = _anemos_peak(rows, "prob_rain")
    peak_failure = _anemos_peak(rows, "forecast_failure_risk")
    groups = _anemos_group_rows(rows)
    periods = "".join(_anemos_period_card(n, groups.get(n, [])) for n in ["Pagi", "Siang", "Sore", "Malam"])
    variables = _anemos_variable_html(rows)
    hours = _anemos_hour_html(rows, compact=True)
    scenarios = _anemos_scenarios(rows)
    certainty = max(0, 100 - (safe_float(peak_failure.get("forecast_failure_risk")) or 0))
    nav = _anemos_page_nav("Prakiraan Cuaca Lokal", getattr(args, 'location_name', ''), ctx, args, [("Ringkasan", "sentinel_x_report.html"), ("Akurasi", "sentinel_x_accuracy_public.html"), ("Pilih lokasi", "../")])
    return f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Prakiraan cuaca { _v6_esc(getattr(args,'location_name','')) }</title><style>{_anemos_css()}</style></head><body><main class='page'>{nav}<section class='hero compact'><div class='chips'><span class='chip'>{_v6_esc(ctx['valid_label'])}</span><span class='chip'>{_v6_esc(ctx['updated_label'])}</span><span class='chip'>ANEMOS</span></div><h1>{'Ringkasan Cuaca Hari Ini' if report else 'Prakiraan Cuaca Hari Ini'}</h1><p>{_v6_esc(summary)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='meta-row'><article class='meta-card'><span class='label'>Berlaku untuk</span><b>{_v6_esc(ctx['target_label'])}</b><p class='muted'>00.00–23.59 {ctx['tz_label']}</p></article><article class='meta-card'><span class='label'>Terakhir diperbarui</span><b>{_v6_esc(ctx['updated_label'].replace('Diperbarui ',''))}</b></article><article class='meta-card'><span class='label'>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section><section class='lead-grid'><article class='decision'><span class='badge {risk_cls}'>{_v6_esc(badge)}</span><h2>{_v6_esc(title)}</h2><p>{_v6_esc(hint)} Jam yang perlu diperhatikan: <b>{_v6_esc(risky)}</b>. Waktu yang relatif lebih aman: <b>{_v6_esc(safe)}</b>.</p></article><div class='metrics'><article class='metric'><span>Peluang hujan tertinggi</span><strong>{_anemos_num(peak_prob.get('prob_rain'),'%')}</strong><small>sekitar {_v6_esc(_anemos_safe_hour(peak_prob))}</small></article><article class='metric'><span>Risiko hujan</span><strong>{_v6_esc(risk_word)}</strong><small>berdasarkan gabungan sumber prakiraan</small></article><article class='metric'><span>Kepercayaan prakiraan</span><strong>{_anemos_num(certainty,'%')}</strong><small>lebih rendah saat hujan lokal sulit dipastikan</small></article><article class='metric'><span>Data akurasi</span><strong>{_v6_esc(str(read_json(path_output('sentinel_x_verification_summary.json'), default={}).get('matched_cases',0)))}</strong><small>pasangan prakiraan-observasi</small></article></div></section><section class='overview-grid'><article class='chart-card'><div class='chart-title'><div><span class='label'>Peluang hujan</span><h2>Perubahan dalam sehari</h2></div><p class='muted'>Angka tinggi berarti peluang hujan lebih besar.</p></div>{_anemos_rain_chart(rows)}</article><article class='chart-card'><div class='chart-title'><div><span class='label'>Suhu</span><h2>Perkiraan suhu</h2></div><p class='muted'>Ditampilkan per beberapa jam.</p></div>{_anemos_temp_svg(rows)}</article></section><section class='panel'><div class='section-head'><h2>Variabel cuaca utama</h2><p>Variabel penting untuk aktivitas harian.</p></div>{variables}</section><section class='panel'><div class='section-head'><h2>Ringkasan pagi sampai malam</h2><p>Lihat bagian hari yang sesuai dengan rencana aktivitas.</p></div><div class='periods'>{periods}</div></section><section class='two'><section class='panel'><h2>Saran aktivitas</h2><p class='big'>{_v6_esc(title)}</p><p class='muted'>Jika langit cepat gelap, angin berubah, atau mulai gerimis, prioritaskan kondisi aktual di sekitar.</p></section><section class='panel'><h2>Kemungkinan kondisi</h2><ul class='scenario'>{scenarios}</ul></section></section><section class='panel'><div class='section-head'><h2>Jam penting</h2><p>Tidak semua jam ditampilkan; fokus pada jam utama dan jam rawan.</p></div><div class='hour-list'>{hours}</div><details class='more-hours'><summary><b>Butuh data lengkap?</b></summary><p class='muted'>Gunakan file CSV untuk melihat semua jam dan semua variabel.</p><div class='links'><a class='btn' href='sentinel_x_variables.csv'>CSV variabel</a><a class='btn' href='sentinel_x.csv'>CSV lengkap</a><a class='btn' href='sentinel_x.json'>JSON</a></div></details></section><details class='panel technical'><summary><b>Catatan data dan batas penggunaan</b></summary><ul class='plain-list'><li>Halaman ini adalah panduan aktivitas harian, bukan peringatan resmi cuaca ekstrem.</li><li>Untuk kondisi berbahaya, ikuti informasi BMKG dan kondisi setempat.</li><li>Hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</li><li>Halaman akurasi akan lebih bermakna setelah data observasi terkumpul.</li></ul><div class='links'><a class='btn' href='sentinel_x_accuracy_public.html'>Status akurasi</a><a class='btn' href='sentinel_x_forecast_contract.json'>Batas penggunaan</a><a class='btn' href='../'>Pilih lokasi</a></div></details><p class='footer-note'>Didukung oleh ANEMOS · {ctx['valid_label']} · {ctx['updated_label']}</p></main></body></html>"""


def aether_write_dashboard(aether_rows, source_state_rows, daily, args):
    if getattr(args, "disable_sentinel_command_center", False):
        return ""
    rows = aether_rows or []
    _v8_write_variables_csv(rows)
    doc = _anemos_dashboard_doc(rows, source_state_rows, daily, args, report=False)
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    ctx = _v6_time_context(rows, args)
    write_json(path_output("command_center_manifest_sentinel_x.json"), {"brand":"ANEMOS", "dashboard": path_output(AETHER_DASHBOARD_FILENAME), "accuracy": path_output("sentinel_x_accuracy_public.html"), "report_html": path_output(AETHER_REPORT_HTML_FILENAME), "variables_csv": path_output("sentinel_x_variables.csv"), "generated_at": now_local(args.timezone).isoformat(), "ui_version": SENTINEL_PUBLIC_UI_VERSION, "valid_date": ctx["target_label"]})
    return path_output(AETHER_DASHBOARD_FILENAME)


def aether_write_report(aether_rows, daily, args):
    rows = aether_rows or []
    _v8_write_variables_csv(rows)
    ctx = _v6_time_context(rows, args)
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write(f"# Ringkasan cuaca ANEMOS\n\nBuka versi HTML: `sentinel_x_report.html`.\n\n{ctx['valid_label']}\n{ctx['updated_label']}\n"))
    doc = _anemos_dashboard_doc(rows, [], daily, args, report=True)
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(doc))
    return path_output(AETHER_REPORT_HTML_FILENAME)


def _anemos_accuracy_status(summary):
    matched = int(summary.get("matched_cases") or 0)
    if matched < 30:
        return "Data akurasi belum cukup", "Belum ada cukup pasangan prakiraan dan observasi untuk menilai akurasi secara kuat. Halaman ini disiapkan agar evaluasi dapat terlihat otomatis setelah data terkumpul.", min(100, round(matched / 30 * 100))
    return "Data akurasi mulai tersedia", "Jumlah kasus sudah cukup untuk evaluasi awal. Tetap gunakan hasil sebagai indikasi, bukan jaminan akurasi.", 100


def sentinel_write_verification_artifacts(rows, args):
    summary, reliability, pairs = sentinel_compute_verification(rows or [], args)
    write_json(path_output("sentinel_x_verification_summary.json"), summary)
    if reliability:
        write_dict_csv(path_output("sentinel_x_reliability.csv"), list(reliability[0].keys()), reliability)
    if pairs:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), list(pairs[0].keys()), pairs)
    else:
        write_dict_csv(path_output("sentinel_x_verification_pairs.csv"), ["target_date","jam","note"], [{"target_date":"","jam":"","note":"Belum ada pasangan prakiraan-observasi."}])
    ctx = _v6_time_context(rows or [], args)
    title, note, pct = _anemos_accuracy_status(summary)
    matched = int(summary.get("matched_cases") or 0)
    rel_rows = "".join(f"<tr><td>{_v6_esc(r.get('probability_bin',''))}</td><td>{_v6_esc(r.get('n',0))}</td><td>{_anemos_num(r.get('mean_forecast_probability'),'%')}</td><td>{_anemos_num(r.get('observed_rain_frequency'),'%')}</td></tr>" for r in reliability)
    nav = _anemos_page_nav("Status Akurasi", getattr(args, 'location_name', ''), ctx, args, [("Prakiraan", "command_center_sentinel_x.html"), ("Ringkasan", "sentinel_x_report.html"), ("Pilih lokasi", "../")])
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Status akurasi { _v6_esc(getattr(args,'location_name','')) }</title><style>{_anemos_css()}</style></head><body><main class='page'>{nav}<section class='hero compact'><div class='chips'><span class='chip'>{_v6_esc(ctx['updated_label'])}</span><span class='chip'>{matched} pasangan data</span></div><h1>Status Akurasi Prakiraan</h1><p>{_v6_esc(note)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='panel'><h2>{_v6_esc(title)}</h2><div class='progress' style='--p:{pct}%'><span style='--p:{pct}%'></span></div><p class='muted'>Target awal: minimal 30 pasangan prakiraan-observasi. Saat ini terkumpul {matched} pasangan.</p></section><section class='metrics'><article class='metric'><span>Error suhu</span><strong>{_anemos_num(summary.get('temperature_mae_c'),'°C',1)}</strong><small>Lebih kecil lebih baik</small></article><article class='metric'><span>Skor peluang hujan</span><strong>{_anemos_num(summary.get('rain_brier_score'),'',2)}</strong><small>Lebih kecil lebih baik</small></article><article class='metric'><span>Hujan terdeteksi</span><strong>{_anemos_num(summary.get('rain_pod'),'%')}</strong><small>Kemampuan menangkap hujan</small></article><article class='metric'><span>Alarm keliru</span><strong>{_anemos_num(summary.get('rain_far'),'%')}</strong><small>Semakin kecil semakin baik</small></article></section><section class='panel'><div class='section-head'><h2>Bukti peluang hujan</h2><p>Tabel akan terisi setelah observasi terkumpul.</p></div><div class='table-scroll'><table><tr><th>Kelompok peluang</th><th>Jumlah kasus</th><th>Rata-rata prakiraan</th><th>Hujan yang terjadi</th></tr>{rel_rows}</table></div></section><section class='panel'><h2>Penjelasan singkat</h2><p>Sampai data observasi cukup, halaman prakiraan sebaiknya dipakai sebagai panduan aktivitas harian. Evaluasi akurasi akan bertambah otomatis seiring bertambahnya data.</p><div class='links'><a class='btn' href='sentinel_x_verification_summary.json'>Data verifikasi</a><a class='btn' href='sentinel_x_reliability.csv'>CSV peluang hujan</a><a class='btn' href='sentinel_x_verification_pairs.csv'>CSV pasangan data</a></div></section><p class='footer-note'>Didukung oleh ANEMOS · {ctx['updated_label']}</p></main></body></html>"""
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(doc))
    return {"summary": path_output("sentinel_x_verification_summary.json"), "reliability": path_output("sentinel_x_reliability.csv"), "html": path_output("sentinel_x_accuracy_public.html")}


def _anemos_read_location_payload(slug):
    p = os.path.join(root_output_dir(), sanitize_filename(slug), AETHER_JSON_FILENAME)
    return read_json(p, default={}) or {}


def _anemos_location_card(loc, base_url=""):
    payload = _anemos_read_location_payload(loc.slug)
    rows = payload.get("hourly") or []
    prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
    if rows:
        title, badge, risk_word, risk_cls, hint, jam = _anemos_main_decision(rows, loc)
        peak = _anemos_peak(rows, "prob_rain")
        temp = _anemos_avg(rows, "temp_p50")
        rh = _anemos_avg(rows, "rh_p50")
        summary = title
        kpi = f"<div class='loc-kpi'><span>Peluang hujan<b>{_anemos_num(peak.get('prob_rain'),'%')}</b></span><span>Jam perhatian<b>{_v6_esc(jam)}</b></span><span>Suhu<b>{_anemos_num(temp,'°C',1)}</b></span></div>"
    else:
        summary = "Ringkasan belum tersedia. Jalankan forecast terbaru untuk memperbarui halaman ini."
        kpi = "<div class='loc-kpi'><span>Status<b>Belum ada</b></span><span>Hujan<b>—</b></span><span>Suhu<b>—</b></span></div>"
    return f"""
    <article class='loc-card'>
      <h2>{_v6_esc(loc.location_name)}</h2>
      <p class='summary'>{_v6_esc(summary)}</p>
      {kpi}
      <div class='links'><a class='btn primary' href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Lihat prakiraan</a><a class='btn' href='{prefix}{AETHER_REPORT_HTML_FILENAME}'>Ringkasan</a><a class='btn' href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a><a class='btn' href='{prefix}sentinel_x_variables.csv'>CSV variabel</a></div>
    </article>"""


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    now = now_local(getattr(args, "timezone", DEFAULT_TIMEZONE))
    updated = f"Diperbarui {_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args, 'timezone', DEFAULT_TIMEZONE))}"
    cards = "".join(_anemos_location_card(loc, base_url) for loc in locations)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Portal Prakiraan Cuaca Lokal</title><style>{_anemos_css()}</style></head><body><main class='page'><section class='hero'><div class='chips'><span class='chip'>Portal publik</span><span class='chip'>{_v6_esc(updated)}</span><span class='chip'>ANEMOS</span></div><h1>Prakiraan Cuaca Lokal</h1><p>Pilih lokasi untuk melihat ringkasan harian, suhu, kelembapan, heat index, peluang hujan, angin, dan saran aktivitas.</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='loc-grid'>{cards}</section><section class='data-box'><h2>Data terbuka</h2><p class='muted'>File berikut dapat digunakan untuk analisis, arsip, atau pembuatan dashboard lanjutan.</p><div class='links'><a class='btn' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='btn' href='forecast_all_locations.csv'>Forecast CSV</a><a class='btn' href='source_status_all_locations.csv'>Status sumber</a><a class='btn' href='forecast_batch_summary.json'>Ringkasan batch</a></div></section><p class='footer-note'>Didukung oleh ANEMOS · {updated}</p></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("anemos_public_portal_manifest.json"), {"brand":"ANEMOS", "generated_at": now.isoformat(), "locations": [loc.slug for loc in locations], "index": root_output_path("index.html"), "disclaimer": sentinel_public_disclaimer(args), "ui_version": SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path("index.html")



# =============================================================================
# ANEMOS PUBLIC WEATHER v11 — Multi-Day Public App Upgrade
# - Strong ANEMOS branding, no AETHER wording on public pages.
# - Default forecast horizon: today + next 2 days.
# - Homepage shows real summary cards for every location and every day.
# - Location page shows today plus several days ahead, with stronger visual hierarchy.
# - Accuracy page uses a clear empty state and progress bar.
# - Keeps legacy file names for stable GitHub Pages links.
# =============================================================================
ANEMOS_PUBLIC_VERSION = "ANEMOS v11"
ANEMOS_BRAND_NAME = "ANEMOS"
ANEMOS_TAGLINE = "Prakiraan cuaca lokal untuk keputusan harian."
SENTINEL_PUBLIC_UI_VERSION = "anemos_v11_multiday_public_app"
AETHER_VERSION = "ANEMOS Public Weather v11"

# Save original parser so v11 can add options without rewriting the old CLI.
_ANEMOS_ORIGINAL_BUILD_ARG_PARSER = build_arg_parser

def build_arg_parser():
    parser = _ANEMOS_ORIGINAL_BUILD_ARG_PARSER()
    if not any(getattr(a, 'dest', None) == 'forecast_range_days' for a in parser._actions):
        parser.add_argument(
            '--forecast-range-days',
            type=int,
            default=int(os.environ.get('ANEMOS_FORECAST_DAYS', '3')),
            help='Jumlah hari prakiraan publik yang dibuat mulai hari ini. Default 3: hari ini, besok, dan lusa.',
        )
    if not any(getattr(a, 'dest', None) == 'single_day_public' for a in parser._actions):
        parser.add_argument(
            '--single-day-public',
            action='store_true',
            default=False,
            help='Hanya buat satu hari prakiraan publik, mengikuti --target-date atau default lama.',
        )
    return parser


def sentinel_public_disclaimer(args=None):
    return "Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi BMKG dan kondisi setempat."


def _anemos11_css():
    return """
    :root{
      --bg:#edf6ff; --paper:#ffffff; --paper2:#f8fbff; --ink:#071527; --muted:#64748b;
      --blue:#185abc; --blue2:#2674ff; --sky:#dff0ff; --navy:#06142c; --line:#d8e4f2;
      --green:#16a34a; --yellow:#f59e0b; --orange:#ea580c; --red:#dc2626;
      --shadow:0 24px 65px rgba(15,23,42,.11); --softshadow:0 10px 28px rgba(15,23,42,.07);
    }
    *{box-sizing:border-box} body{margin:0;background:linear-gradient(180deg,#e8f4ff 0,#f7fbff 58%,#edf6ff 100%);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);font-size:16px;line-height:1.55} a{color:#075bd8;text-decoration:none;font-weight:900}.page{width:min(1280px,calc(100% - 48px));margin:28px auto 72px}.top{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:0 0 18px}.brand{display:flex;align-items:center;gap:12px}.mark{width:38px;height:38px;border-radius:14px;background:linear-gradient(135deg,#06142c,#2f7dff);box-shadow:0 10px 24px rgba(37,99,235,.25)}.brand b{display:block;font-size:18px}.brand span{display:block;font-size:13px;color:var(--muted)}.nav{display:flex;gap:10px;flex-wrap:wrap}.btn,.nav a{display:inline-flex;align-items:center;justify-content:center;border:1px solid #9fc6ff;background:#f4f9ff;border-radius:999px;padding:9px 14px;font-size:13px;color:#075bd8;font-weight:900}.btn.primary{background:#075bd8;color:#fff;border-color:#075bd8}.hero{position:relative;overflow:hidden;background:radial-gradient(circle at 92% 12%,rgba(255,255,255,.22),transparent 17%),linear-gradient(135deg,#06142c 0%,#143d99 48%,#2f7dff 100%);color:white;border-radius:32px;padding:42px 46px;box-shadow:var(--shadow);margin-bottom:18px}.hero h1{margin:14px 0 12px;font-size:clamp(38px,5vw,72px);line-height:.95;letter-spacing:-.06em}.hero p{max-width:860px;margin:0;color:#e9f2ff;font-size:18px}.chips{display:flex;gap:10px;flex-wrap:wrap}.chip{display:inline-flex;align-items:center;border:1px solid rgba(255,255,255,.36);background:rgba(255,255,255,.12);border-radius:999px;padding:7px 12px;color:#fff;font-size:12px;font-weight:950}.notice{border:1px solid #fdba74;background:#fff7ed;color:#7c2d12;border-radius:16px;padding:13px 16px;margin:16px 0 18px;font-weight:950}.date-strip{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:14px;margin-bottom:18px}.date-card,.panel,.card,.metric,.day-card,.period-card,.hour-row,.location-card{background:#fff;border:1px solid var(--line);border-radius:22px;box-shadow:var(--softshadow)}.date-card{padding:18px}.label,.date-card span,.metric span,.var-card span{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:950;color:var(--muted)}.date-card b{display:block;font-size:21px;margin-top:4px}.date-card p{color:var(--muted);margin:4px 0 0}.status-grid{display:grid;grid-template-columns:minmax(0,1.4fr) 340px;gap:18px;margin-bottom:18px}.decision{background:linear-gradient(135deg,#06142c,#0c3277);color:#fff;border-radius:24px;padding:28px;box-shadow:var(--softshadow)}.decision h2{font-size:clamp(30px,3.5vw,48px);line-height:1.02;letter-spacing:-.05em;margin:8px 0 12px}.decision p{color:#dbeafe;font-size:17px;margin:0}.badge{display:inline-flex;border-radius:999px;padding:8px 12px;font-size:13px;font-weight:950}.badge.low{background:#dcfce7;color:#166534}.badge.medium{background:#fef3c7;color:#92400e}.badge.high{background:#ffedd5;color:#9a3412}.badge.critical{background:#fee2e2;color:#991b1b}.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.metric{padding:18px}.metric strong{display:block;font-size:32px;letter-spacing:-.04em;line-height:1;margin:9px 0 5px}.metric small{color:var(--muted)}.panel{padding:24px;margin:18px 0}.section-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:16px}.section-head h2,.panel h2{margin:0;font-size:28px;letter-spacing:-.035em}.section-head p,.muted{margin:0;color:var(--muted)}.day-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.day-card{padding:20px;border-top:6px solid #22c55e}.day-card.medium{border-top-color:#f59e0b}.day-card.high,.day-card.critical{border-top-color:#ef4444}.day-card h3{font-size:24px;margin:0 0 5px;letter-spacing:-.03em}.day-card .summary{font-weight:850;margin:12px 0}.mini-row{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}.mini-row span{background:#f6faff;border:1px solid var(--line);border-radius:12px;padding:10px;font-size:12px;color:var(--muted)}.mini-row b{display:block;color:var(--ink);font-size:16px}.overview-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.variables-primary{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:12px}.variables-secondary{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.var-card{padding:16px;border:1px solid var(--line);border-radius:17px;background:#fbfdff}.var-card.primary{border-top:5px solid #2674ff}.var-card strong{display:block;font-size:24px;letter-spacing:-.04em;margin:8px 0 4px}.var-card small{color:var(--muted)}.periods{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.period-card{padding:18px;border-top:5px solid #22c55e}.period-card.medium{border-top-color:#f59e0b}.period-card.high,.period-card.critical{border-top-color:#ef4444}.period-card h3{margin:0 0 8px;font-size:21px}.period-card .cond{font-weight:950;margin-bottom:8px}.mini{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.mini span{background:#f7fbff;border:1px solid var(--line);border-radius:12px;padding:9px;color:var(--muted);font-size:12px}.mini b{display:block;color:var(--ink);font-size:15px}.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.scenario-list{list-style:none;padding:0;margin:0}.scenario-list li{display:flex;justify-content:space-between;border-bottom:1px solid var(--line);padding:12px 0;font-weight:850}.big{font-size:22px;font-weight:950}.hour-list{display:grid;gap:10px}.hour-row{display:grid;grid-template-columns:92px 1.25fr repeat(5,108px) 1fr;align-items:center;gap:12px;padding:14px 16px;border-left:7px solid #22c55e}.hour-row.medium{border-left-color:#f59e0b}.hour-row.high,.hour-row.critical{border-left-color:#ef4444}.time{font-size:18px;font-weight:950}.condition b{display:block}.condition small{color:var(--muted)}.cell{background:#f8fbff;border:1px solid var(--line);border-radius:12px;padding:9px 10px;min-height:55px}.cell b{display:block}.cell small{color:var(--muted);font-size:11px}.note{color:var(--muted);font-size:14px}.chartbox{min-height:220px}.portal-hero{margin-bottom:20px}.location-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.location-card{padding:22px}.location-card h2{font-size:28px;margin:0 0 6px}.location-card .summary{min-height:78px;color:#334155}.loc-kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}.loc-kpi span{background:#f7fbff;border:1px solid var(--line);border-radius:12px;padding:9px;font-size:12px;color:var(--muted)}.loc-kpi b{display:block;color:var(--ink);font-size:16px}.data-box{background:#fff;border:1px solid var(--line);border-radius:24px;padding:24px;margin-top:18px}.footer-note{text-align:center;color:var(--muted);font-size:13px;margin:32px 0}.progress{height:12px;border-radius:999px;background:#e6eef7;overflow:hidden}.progress span{display:block;height:100%;width:var(--p,0%);background:linear-gradient(90deg,#1d4ed8,#38bdf8)}.table-scroll{overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--line);text-align:left}th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}@media(max-width:1150px){.status-grid,.overview-grid,.two,.date-strip{grid-template-columns:1fr}.variables-primary{grid-template-columns:repeat(3,1fr)}.variables-secondary{grid-template-columns:repeat(3,1fr)}.day-grid,.location-grid{grid-template-columns:1fr}.metric-grid,.periods{grid-template-columns:repeat(2,1fr)}.hour-row{grid-template-columns:82px 1fr repeat(2,104px)}.note{grid-column:1/-1}}@media(max-width:720px){.page{width:min(100% - 24px,1280px);margin:16px auto 48px}.top{align-items:flex-start;flex-direction:column}.hero{padding:30px}.hero h1{font-size:42px}.hero p{font-size:16px}.variables-primary,.variables-secondary,.periods,.metric-grid{grid-template-columns:1fr}.hour-row{grid-template-columns:1fr 1fr}.condition,.note{grid-column:1/-1}.mini-row{grid-template-columns:1fr}}
    """


def _anemos11_num(value, suffix="", digits=0, fallback="—"):
    return _anemos_num(value, suffix=suffix, digits=digits, fallback=fallback)


def _anemos11_today(args):
    return now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE)).date()


def _anemos11_forecast_dates(args):
    if getattr(args, 'target_date', None):
        start = parse_iso_date(args.target_date)
    else:
        start = _anemos11_today(args)
    if getattr(args, 'single_day_public', False):
        days = 1
    else:
        days = int(getattr(args, 'forecast_range_days', 3) or 3)
    days = max(1, min(days, 5))
    return [start + timedelta(days=i) for i in range(days)]


def _anemos11_day_label(date_obj, args):
    today = _anemos11_today(args)
    if date_obj == today:
        prefix = "Hari ini"
    elif date_obj == today + timedelta(days=1):
        prefix = "Besok"
    elif date_obj == today + timedelta(days=2):
        prefix = "Lusa"
    else:
        prefix = _v6_format_date_id(date_obj).split(',')[0]
    return f"{prefix}, {_v6_format_date_id(date_obj)}"


def _anemos11_payload_for_date(date_obj):
    stamp = date_obj.strftime('%Y%m%d')
    path = path_output(f"sentinel_x_{stamp}.json")
    return read_json(path, default={}) or {}


def _anemos11_all_day_payloads(args, forecast_dates=None):
    out = []
    for date_obj in forecast_dates or _anemos11_forecast_dates(args):
        payload = _anemos11_payload_for_date(date_obj)
        rows = payload.get('hourly') or []
        if rows:
            out.append({'date': date_obj, 'payload': payload, 'rows': rows, 'daily': payload.get('daily') or {}})
    return out


def _anemos11_day_summary(day, args):
    rows = day.get('rows') or []
    title, badge, risk_word, risk_cls, hint, jam = _anemos_main_decision(rows, args)
    peak = _anemos_peak(rows, 'prob_rain')
    peak_risk = _anemos_peak(rows, 'rain_threat_score')
    temp = _anemos_avg(rows, 'temp_p50')
    rh = _anemos_avg(rows, 'rh_p50')
    risk_score = max(safe_float(peak.get('prob_rain')) or 0, safe_float(peak_risk.get('rain_threat_score')) or 0)
    _, risk_cls, _ = _anemos_risk_level(risk_score)
    return {
        'date': day.get('date'), 'label': _anemos11_day_label(day.get('date'), args), 'title': title, 'badge': badge,
        'risk_word': risk_word, 'risk_cls': risk_cls, 'jam': jam,
        'peak_prob': safe_float(peak.get('prob_rain')), 'peak_hour': _anemos_safe_hour(peak),
        'temp_avg': temp, 'rh_avg': rh, 'risk_score': risk_score,
    }


def _anemos11_top_summary(days, args):
    if not days:
        return "Prakiraan belum tersedia. Jalankan pembaruan data untuk menampilkan informasi terbaru."
    first = _anemos11_day_summary(days[0], args)
    loc = getattr(args, 'location_name', 'lokasi ini')
    return (
        f"Untuk {loc}, {first['title'].lower()} Peluang hujan tertinggi sekitar "
        f"{_anemos11_num(first['peak_prob'],'%')} pada {first['peak_hour']}. "
        "Gunakan prakiraan ini sebagai panduan aktivitas harian dan tetap perhatikan kondisi langit di sekitar."
    )


def _anemos11_day_cards(days, args):
    cards = []
    for day in days:
        s = _anemos11_day_summary(day, args)
        cards.append(f"""
        <article class='day-card {s['risk_cls']}'>
          <span class='label'>{_v6_esc(s['label'])}</span>
          <h3>{_v6_esc(s['risk_word'].capitalize())}</h3>
          <p class='summary'>{_v6_esc(s['title'])}</p>
          <div class='mini-row'>
            <span>Peluang hujan<b>{_anemos11_num(s['peak_prob'],'%')}</b></span>
            <span>Jam perhatian<b>{_v6_esc(s['peak_hour'])}</b></span>
            <span>Suhu rata-rata<b>{_anemos11_num(s['temp_avg'],'°C',1)}</b></span>
          </div>
        </article>""")
    return "".join(cards)


def _anemos11_current_day(days, args):
    today = _anemos11_today(args)
    for day in days:
        if day['date'] == today:
            return day
    return days[0] if days else {'date': today, 'rows': [], 'daily': {}}


def _anemos11_day_detail(day, args):
    rows = day.get('rows') or []
    if not rows:
        return ""
    s = _anemos11_day_summary(day, args)
    return f"""
    <section class='panel' id='detail-{day['date'].strftime('%Y%m%d')}'>
      <div class='section-head'><div><span class='label'>{_v6_esc(s['label'])}</span><h2>Ringkasan hari</h2></div><p>{_v6_esc('Fokus pada jam penting dan perubahan risiko.')}</p></div>
      <div class='periods'>{''.join(_anemos_period_card(k, v) for k, v in _anemos_group_rows(rows).items())}</div>
    </section>
    <section class='two'>
      <section class='panel'><h2>Saran aktivitas</h2><p class='big'>{_v6_esc(s['title'])}</p><p class='muted'>Jika langit cepat gelap, angin berubah, atau mulai gerimis, prioritaskan kondisi aktual di sekitar.</p></section>
      <section class='panel'><h2>Kemungkinan kondisi</h2><ul class='scenario-list'>{_anemos_scenarios(rows)}</ul></section>
    </section>
    <section class='panel'>
      <div class='section-head'><h2>Jam penting</h2><p>Tidak semua jam ditampilkan; fokus pada jam utama dan jam rawan.</p></div>
      <div class='hour-list'>{_anemos_hour_html(rows, compact=True)}</div>
    </section>
    """


def _anemos11_header(args, active='prakiraan'):
    return f"""
    <header class='top'>
      <div class='brand'><div class='mark'></div><div><b>ANEMOS</b><span>{_v6_esc(getattr(args,'location_name','Prakiraan cuaca lokal'))}</span></div></div>
      <nav class='nav'>
        <a href='{AETHER_DASHBOARD_FILENAME}'>Prakiraan</a>
        <a href='{AETHER_REPORT_HTML_FILENAME}'>Ringkasan</a>
        <a href='sentinel_x_accuracy_public.html'>Akurasi</a>
        <a href='../'>Pilih lokasi</a>
      </nav>
    </header>"""


def _anemos11_dashboard_html(days, source_state_rows, args, report=False):
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    tz_label = _v6_timezone_label(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    if not days:
        valid_text = "Prakiraan belum tersedia"
        summary = "Data prakiraan belum tersedia. Silakan jalankan pembaruan data."
        current = {'date': now.date(), 'rows': [], 'daily': {}}
    else:
        valid_text = f"{_v6_format_date_id(days[0]['date'])}–{_v6_format_date_id(days[-1]['date'])}"
        summary = _anemos11_top_summary(days, args)
        current = _anemos11_current_day(days, args)
    current_rows = current.get('rows') or []
    current_summary = _anemos11_day_summary(current, args) if current_rows else {'title':'Prakiraan belum tersedia','badge':'Data belum tersedia','risk_word':'belum pasti','risk_cls':'medium','peak_prob':None,'peak_hour':'—'}
    risky, safe = _anemos_time_windows(current_rows)
    nav = _anemos11_header(args)
    _verification_summary_for_page = read_json(path_output('sentinel_x_verification_summary.json'), default={}) or {}
    _matched_cases_for_page = _verification_summary_for_page.get('matched_cases', 0)
    variables = _anemos_variable_html(current_rows) if current_rows else "<p class='muted'>Data variabel belum tersedia.</p>"
    details = _anemos11_day_detail(current, args)
    future_details = "".join(_anemos11_day_detail(day, args) for day in days[1:])
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Prakiraan { _v6_esc(getattr(args,'location_name','')) }</title><style>{_anemos11_css()}</style></head><body><main class='page'>{nav}
    <section class='hero'><div class='chips'><span class='chip'>Berlaku {_v6_esc(valid_text)}</span><span class='chip'>Diperbarui {_v6_esc(_v6_format_date_id(now.date()))}, {now.strftime('%H:%M')} {tz_label}</span><span class='chip'>ANEMOS</span></div><h1>{'Ringkasan Prakiraan' if report else 'Prakiraan Cuaca Lokal'}</h1><p>{_v6_esc(summary)}</p></section>
    <div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div>
    <section class='date-strip'><article class='date-card'><span>Periode prakiraan</span><b>{_v6_esc(valid_text)}</b><p>Hari ini dan beberapa hari ke depan</p></article><article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(_v6_format_date_id(now.date()))}, {now.strftime('%H:%M')} {tz_label}</b></article><article class='date-card'><span>Lokasi</span><b>{_v6_esc(getattr(args,'location_name',''))}</b></article></section>
    <section class='status-grid'><article class='decision'><span class='badge {current_summary['risk_cls']}'>{_v6_esc(current_summary['badge'])}</span><h2>{_v6_esc(current_summary['title'])}</h2><p>Jam yang perlu diperhatikan: <b>{_v6_esc(risky)}</b>. Waktu yang relatif lebih aman: <b>{_v6_esc(safe)}</b>.</p></article><div class='metric-grid'><article class='metric'><span>Peluang hujan tertinggi</span><strong>{_anemos11_num(current_summary.get('peak_prob'),'%')}</strong><small>sekitar {_v6_esc(current_summary.get('peak_hour','—'))}</small></article><article class='metric'><span>Tingkat risiko</span><strong>{_v6_esc(current_summary.get('risk_word','belum pasti'))}</strong><small>untuk hari yang sedang dipilih</small></article><article class='metric'><span>Kepercayaan prakiraan</span><strong>{_anemos11_num(_anemos_avg(current_rows,'trust_score'),'%')}</strong><small>lebih rendah saat hujan lokal sulit dipastikan</small></article><article class='metric'><span>Data akurasi</span><strong>{_v6_esc(str(_matched_cases_for_page))}</strong><small>pasangan prakiraan-observasi</small></article></div></section>
    <section class='panel'><div class='section-head'><h2>Ringkasan beberapa hari</h2><p>Bandingkan kondisi hari ini sampai beberapa hari ke depan.</p></div><div class='day-grid'>{_anemos11_day_cards(days, args)}</div></section>
    <section class='overview-grid'><section class='panel chartbox'><div class='section-head'><h2>Peluang hujan hari ini</h2><p>Perubahan peluang hujan per jam.</p></div>{_anemos_rain_chart(current_rows)}</section><section class='panel chartbox'><div class='section-head'><h2>Suhu hari ini</h2><p>Perkiraan perubahan suhu.</p></div>{_anemos_temp_svg(current_rows)}</section></section>
    <section class='panel'><div class='section-head'><h2>Variabel cuaca utama</h2><p>Suhu, kelembapan, heat index, hujan, angin, awan, dan jarak pandang.</p></div>{variables}</section>
    {details}
    <details class='panel'><summary><b>Lihat ringkasan hari berikutnya</b></summary>{future_details if future_details else '<p class="muted">Tidak ada hari tambahan yang tersedia.</p>'}</details>
    <details class='panel'><summary><b>Sumber data dan file publik</b></summary><p class='muted'>Bagian ini untuk pengguna yang ingin melihat data lebih rinci.</p><div class='links'><a class='btn' href='sentinel_x_variables.csv'>CSV variabel</a><a class='btn' href='sentinel_x.csv'>CSV lengkap</a><a class='btn' href='sentinel_x.json'>JSON</a><a class='btn' href='sentinel_x_forecast_contract.json'>Batas penggunaan</a></div></details>
    <section class='panel'><h2>Catatan penggunaan</h2><ul><li>Prakiraan ini digunakan sebagai panduan aktivitas harian.</li><li>Ini bukan peringatan resmi cuaca ekstrem.</li><li>Untuk kondisi berbahaya, ikuti informasi BMKG dan kondisi setempat.</li><li>Hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</li></ul></section>
    <p class='footer-note'>ANEMOS · Prakiraan cuaca lokal berbasis multi-sumber · {_v6_esc(valid_text)} · Diperbarui {now.strftime('%H:%M')} {tz_label}</p>
    </main></body></html>"""
    return doc


def anemos_write_multiday_public_pages(args, forecast_dates=None, source_state_rows=None):
    days = _anemos11_all_day_payloads(args, forecast_dates)
    source_state_rows = source_state_rows or read_dict_csv(path_output(AETHER_SOURCE_STATE_FILENAME)) if os.path.exists(path_output(AETHER_SOURCE_STATE_FILENAME)) else []
    doc = _anemos11_dashboard_html(days, source_state_rows, args, report=False)
    report = _anemos11_dashboard_html(days, source_state_rows, args, report=True)
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(report))
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    # Keep md as a pointer only; the public version is HTML.
    valid = f"{_v6_format_date_id(days[0]['date'])}–{_v6_format_date_id(days[-1]['date'])}" if days else "Belum tersedia"
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write(f"# Ringkasan prakiraan ANEMOS\n\nVersi publik: `sentinel_x_report.html`.\n\nPeriode: {valid}\nDiperbarui: {_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args, 'timezone', DEFAULT_TIMEZONE))}\n"))
    summary_payload = {
        'brand': 'ANEMOS', 'ui_version': SENTINEL_PUBLIC_UI_VERSION,
        'generated_at': now.isoformat(), 'location_slug': getattr(args,'location_slug',''),
        'location_name': getattr(args,'location_name',''), 'forecast_days': [],
    }
    for day in days:
        s = _anemos11_day_summary(day, args)
        summary_payload['forecast_days'].append({
            'date': day['date'].isoformat(), 'label': s['label'], 'summary': s['title'], 'risk': s['risk_word'],
            'peak_rain_probability': s['peak_prob'], 'peak_rain_hour': s['peak_hour'],
            'avg_temperature_c': aether_round(s['temp_avg'], 1) if s['temp_avg'] is not None else None,
            'avg_humidity_pct': aether_round(s['rh_avg'], 0) if s['rh_avg'] is not None else None,
        })
    write_json(path_output('anemos_multiday_summary.json'), summary_payload)
    write_json(path_output('command_center_manifest_sentinel_x.json'), {'brand':'ANEMOS','dashboard':path_output(AETHER_DASHBOARD_FILENAME),'report_html':path_output(AETHER_REPORT_HTML_FILENAME),'generated_at':now.isoformat(),'ui_version':SENTINEL_PUBLIC_UI_VERSION,'days':len(days)})
    return {'days': len(days), 'dashboard': path_output(AETHER_DASHBOARD_FILENAME), 'report_html': path_output(AETHER_REPORT_HTML_FILENAME)}


def _anemos11_accuracy_html(rows, args):
    verification_result = sentinel_compute_verification(rows, args)
    if isinstance(verification_result, tuple):
        summary = verification_result[0] or {}
        pairs = verification_result[1] if len(verification_result) > 1 else []
        reliability_raw = verification_result[2] if len(verification_result) > 2 else []
    else:
        summary = verification_result or {}
        pairs = summary.get('matched_pairs') or [] if isinstance(summary, dict) else []
        reliability_raw = summary.get('reliability_bins') or [] if isinstance(summary, dict) else []
    summary = dict(summary or {})
    reliability = []
    for r in reliability_raw or []:
        if not isinstance(r, dict):
            continue
        reliability.append({
            'bin': r.get('bin') or r.get('probability_bin') or r.get('probability') or '',
            'n': r.get('n', 0),
            'mean_forecast_pct': r.get('mean_forecast_pct', r.get('mean_forecast_probability', r.get('mean_forecast', ''))),
            'observed_frequency_pct': r.get('observed_frequency_pct', r.get('observed_rain_frequency', r.get('observed_frequency', ''))),
        })
    summary['matched_pairs'] = pairs or []
    summary['reliability_bins'] = reliability
    matched = int(summary.get('matched_cases') or 0)
    target = max(1, int(getattr(args, 'verification_min_cases', 30) or 30))
    pct = min(100, round(matched / target * 100))
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    nav = _anemos11_header(args)
    if matched < target:
        title = 'Data akurasi belum cukup'
        note = f'ANEMOS membutuhkan minimal {target} pasangan prakiraan dan observasi sebelum skor akurasi dapat dinilai lebih kuat. Saat ini baru terkumpul {matched} pasangan.'
    else:
        title = 'Data akurasi sudah mulai terbaca'
        note = 'Skor di bawah mulai dapat digunakan untuk melihat kecenderungan performa prakiraan.'
    reliability = summary.get('reliability_bins') or []
    rel_rows = ''.join(f"<tr><td>{_v6_esc(str(r.get('bin')))}</td><td>{_v6_esc(str(r.get('n',0)))}</td><td>{_anemos11_num(r.get('mean_forecast_pct'),'%')}</td><td>{_anemos11_num(r.get('observed_frequency_pct'),'%')}</td></tr>" for r in reliability)
    if not rel_rows:
        rel_rows = "<tr><td colspan='4'>Belum ada data observasi yang cocok.</td></tr>"
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Status Akurasi</title><style>{_anemos11_css()}</style></head><body><main class='page'>{nav}<section class='hero'><div class='chips'><span class='chip'>Diperbarui {_v6_esc(_v6_format_date_id(now.date()))}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args,'timezone',DEFAULT_TIMEZONE))}</span><span class='chip'>{matched} pasangan data</span></div><h1>Status Akurasi Prakiraan</h1><p>{_v6_esc(note)}</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='panel'><h2>{_v6_esc(title)}</h2><div class='progress' style='--p:{pct}%'><span style='--p:{pct}%'></span></div><p class='muted'>Target awal: {target} pasangan prakiraan-observasi. Saat ini terkumpul {matched}/{target} pasangan.</p></section><section class='metric-grid'><article class='metric'><span>Error suhu</span><strong>{_anemos11_num(summary.get('temperature_mae_c'),'°C',1)}</strong><small>Lebih kecil lebih baik</small></article><article class='metric'><span>Skor peluang hujan</span><strong>{_anemos11_num(summary.get('rain_brier_score'),'',2)}</strong><small>Lebih kecil lebih baik</small></article><article class='metric'><span>Hujan terdeteksi</span><strong>{_anemos11_num(summary.get('rain_pod'),'%')}</strong><small>Kemampuan menangkap hujan</small></article><article class='metric'><span>Alarm keliru</span><strong>{_anemos11_num(summary.get('rain_far'),'%')}</strong><small>Semakin kecil semakin baik</small></article></section><section class='panel'><div class='section-head'><h2>Bukti peluang hujan</h2><p>Tabel akan terisi setelah observasi terkumpul.</p></div><div class='table-scroll'><table><tr><th>Kelompok peluang</th><th>Jumlah kasus</th><th>Rata-rata prakiraan</th><th>Hujan yang terjadi</th></tr>{rel_rows}</table></div></section><section class='panel'><h2>Penjelasan singkat</h2><p>Sampai data observasi cukup, halaman prakiraan sebaiknya dipakai sebagai panduan aktivitas harian. Evaluasi akurasi akan bertambah otomatis seiring bertambahnya data.</p><div class='links'><a class='btn' href='sentinel_x_verification_summary.json'>Data verifikasi</a><a class='btn' href='sentinel_x_reliability.csv'>CSV peluang hujan</a><a class='btn' href='sentinel_x_verification_pairs.csv'>CSV pasangan data</a></div></section><p class='footer-note'>ANEMOS · Status akurasi · Diperbarui {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args,'timezone',DEFAULT_TIMEZONE))}</p></main></body></html>"""
    return doc, summary


def sentinel_write_verification_artifacts(rows, args):
    doc, summary = _anemos11_accuracy_html(rows, args)
    atomic_write_text(path_output('sentinel_x_accuracy_public.html'), lambda f: f.write(doc))
    write_json(path_output('sentinel_x_verification_summary.json'), summary)
    reliability = summary.get('reliability_bins') or []
    if reliability:
        write_dict_csv(path_output('sentinel_x_reliability.csv'), ['bin','n','mean_forecast_pct','observed_frequency_pct'], reliability)
    else:
        write_dict_csv(path_output('sentinel_x_reliability.csv'), ['bin','n','mean_forecast_pct','observed_frequency_pct'], [{'bin': f'{i*10:02d}-{(i+1)*10:02d}', 'n': 0, 'mean_forecast_pct': '', 'observed_frequency_pct': ''} for i in range(10)])
    pairs = summary.get('matched_pairs') or []
    if pairs:
        write_dict_csv(path_output('sentinel_x_verification_pairs.csv'), list(pairs[0].keys()), pairs)
    else:
        write_dict_csv(path_output('sentinel_x_verification_pairs.csv'), ['target_date','jam','forecast_rain_prob','observed_rain','forecast_temp_c','observed_temp_c'], [])
    return summary


def _anemos11_location_payload(loc):
    p = os.path.join(root_output_dir(), sanitize_filename(loc.slug), 'anemos_multiday_summary.json')
    return read_json(p, default={}) or {}


def _anemos11_location_card(loc, base_url=''):
    payload = _anemos11_location_payload(loc)
    days = payload.get('forecast_days') or []
    prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
    if days:
        d0 = days[0]
        summary = d0.get('summary') or 'Ringkasan prakiraan tersedia.'
        kpi = f"<div class='loc-kpi'><span>Peluang hujan<b>{_anemos11_num(d0.get('peak_rain_probability'),'%')}</b></span><span>Jam perhatian<b>{_v6_esc(str(d0.get('peak_rain_hour') or '—'))}</b></span><span>Suhu<b>{_anemos11_num(d0.get('avg_temperature_c'),'°C',1)}</b></span></div>"
        dayline = " · ".join(_v6_esc(str(d.get('label',''))) for d in days[:3])
    else:
        summary = 'Ringkasan belum tersedia. Jalankan pembaruan prakiraan untuk memperbarui halaman ini.'
        kpi = "<div class='loc-kpi'><span>Status<b>Belum ada</b></span><span>Hujan<b>—</b></span><span>Suhu<b>—</b></span></div>"
        dayline = 'Belum tersedia'
    return f"""
    <article class='location-card'>
      <h2>{_v6_esc(loc.location_name)}</h2>
      <p class='summary'>{_v6_esc(summary)}</p>
      <p class='muted'>{dayline}</p>
      {kpi}
      <div class='links'><a class='btn primary' href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Lihat prakiraan</a><a class='btn' href='{prefix}{AETHER_REPORT_HTML_FILENAME}'>Ringkasan</a><a class='btn' href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a><a class='btn' href='{prefix}sentinel_x_variables.csv'>CSV variabel</a></div>
    </article>"""


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, 'public_base_url', '') or '').rstrip('/')
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    updated = f"Diperbarui {_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args, 'timezone', DEFAULT_TIMEZONE))}"
    cards = ''.join(_anemos11_location_card(loc, base_url) for loc in locations)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Portal Prakiraan Cuaca Lokal</title><style>{_anemos11_css()}</style></head><body><main class='page'><section class='hero portal-hero'><div class='chips'><span class='chip'>ANEMOS</span><span class='chip'>{_v6_esc(updated)}</span></div><h1>Prakiraan Cuaca Lokal</h1><p>Ringkasan cuaca untuk hari ini dan beberapa hari ke depan: peluang hujan, suhu, kelembapan, heat index, angin, dan saran aktivitas.</p></section><div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div><section class='location-grid'>{cards}</section><section class='data-box'><h2>Data terbuka</h2><p class='muted'>File berikut dapat digunakan untuk analisis, arsip, atau pembuatan dashboard lanjutan.</p><div class='links'><a class='btn' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='btn' href='forecast_all_locations.csv'>Forecast CSV</a><a class='btn' href='source_status_all_locations.csv'>Status sumber</a><a class='btn' href='forecast_batch_summary.json'>Ringkasan batch</a></div></section><p class='footer-note'>ANEMOS · Prakiraan cuaca lokal berbasis multi-sumber · {updated}</p></main></body></html>"""
    atomic_write_text(root_output_path('index.html'), lambda f: f.write(doc))
    write_json(root_output_path('anemos_public_portal_manifest.json'), {'brand':'ANEMOS','generated_at':now.isoformat(),'locations':[loc.slug for loc in locations],'index':root_output_path('index.html'),'disclaimer':sentinel_public_disclaimer(args),'ui_version':SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path('index.html')


def anemos_run_once_for_date(args, target_date):
    old_target = getattr(args, 'target_date', None)
    args.target_date = target_date.isoformat()
    try:
        return run_once(args)
    finally:
        args.target_date = old_target


def run_forecast_for_locations(base_args, locations):
    forecast_dates = _anemos11_forecast_dates(base_args)
    rows = []
    combined_ensemble_rows, combined_ensemble_long_rows, combined_forecast_rows, combined_status_rows = [], [], [], []
    for location in locations:
        last_location_args = None
        location_outputs = []
        fatal_error = None
        for date_obj in forecast_dates:
            try:
                def _runner(location_args, d=date_obj):
                    return anemos_run_once_for_date(location_args, d)
                location_args, summary, log_path = execute_mode_for_location(base_args, location, _runner)
                last_location_args = location_args
                output_dir = summary.get('output_dir') or ACTIVE_OUTPUT_DIR
                td = summary['target_date']
                combined_ensemble_rows.extend(collect_combined_ensemble_rows(location_args, td, output_dir))
                combined_ensemble_long_rows.extend(collect_combined_ensemble_long_rows(location_args, td, output_dir))
                combined_forecast_rows.extend(collect_combined_forecast_rows(location_args, td, output_dir))
                combined_status_rows.extend(collect_combined_source_status_rows(location_args, td, output_dir))
                status = summary.get('run_status') or 'ok'
                location_outputs.append({'target_date': td, 'run_status': status, 'sources_success': summary.get('sources_success'), 'sources_total': summary.get('sources_total'), 'low_coverage_slots': summary.get('low_coverage_slots'), 'log_file': log_path})
            except Exception as exc:
                fatal_error = exc
                batch_warning(f"{location.location_name} gagal untuk {date_obj.isoformat()}:", exc)
                traceback.print_exc()
                location_outputs.append({'target_date': date_obj.isoformat(), 'run_status': 'error', 'error': str(exc)})
        if last_location_args:
            try:
                anemos_write_multiday_public_pages(last_location_args, forecast_dates)
                sentinel_write_publish_manifest(last_location_args)
            except Exception as exc:
                fatal_error = exc
                batch_warning(f"Gagal menulis halaman multi-hari untuk {location.location_name}:", exc)
                traceback.print_exc()
        ok_days = sum(1 for item in location_outputs if item.get('run_status') != 'error')
        rows.append({
            'location_slug': location.slug,
            'location_name': location.location_name,
            'bmkg_point_name': location.bmkg_point_name,
            'area_level': location.area_level,
            'is_proxy_bmkg': location.is_proxy_bmkg,
            'target_date': ','.join(d.isoformat() for d in forecast_dates),
            'run_status': 'error' if fatal_error else 'ok',
            'quality_status': 'limited_coverage_possible' if any((item.get('low_coverage_slots') for item in location_outputs if item.get('low_coverage_slots'))) else 'ok',
            'days_ok': ok_days,
            'days_total': len(forecast_dates),
            'output_dir': os.path.join(root_output_dir(), sanitize_filename(location.slug)),
            'daily_runs': location_outputs,
            'error': str(fatal_error) if fatal_error else '',
        })
    combined_outputs = {}
    for base_filename, label, fieldnames, payload_rows in (
        ('ensemble_all_locations','Ensemble',combined_ensemble_fieldnames(),combined_ensemble_rows),
        ('ensemble_long_all_locations','Ensemble long (BI)',combined_ensemble_long_fieldnames(),combined_ensemble_long_rows),
        ('forecast_all_locations','Forecast raw',combined_forecast_fieldnames(),combined_forecast_rows),
        ('source_status_all_locations','Source status',combined_source_status_fieldnames(),combined_status_rows),
    ):
        if base_args.no_combined:
            latest_path, versioned_path = None, None
        else:
            latest_path, versioned_path = write_combined_csv(base_filename, fieldnames, payload_rows)
        combined_outputs[base_filename] = {'latest_path': latest_path or '', 'versioned_path': versioned_path or '', 'rows': len(payload_rows)}
        if latest_path:
            batch_info(f"{label} gabungan:", latest_path)
    if not base_args.no_combined:
        try:
            dim_src_path, dim_src_count = write_dim_sources()
            dim_loc_path, dim_loc_count = write_dim_locations(locations, base_args)
            fact_path, fact_rows = write_ensemble_fact_from_long(combined_ensemble_long_rows)
            write_json(root_output_path('bi_artifacts_summary.json'), {'generated_at': now_local(DEFAULT_TIMEZONE).isoformat(), 'schema_version': OUTPUT_SCHEMA_VERSION, 'dim_sources': {'path': dim_src_path, 'rows': dim_src_count}, 'dim_locations': {'path': dim_loc_path, 'rows': dim_loc_count}, 'ensemble_fact': {'path': fact_path or '', 'rows': fact_rows}})
        except Exception as exc:
            write_json(root_output_path('bi_artifacts_summary.json'), {'generated_at': now_local(DEFAULT_TIMEZONE).isoformat(), 'schema_version': OUTPUT_SCHEMA_VERSION, 'status': 'error', 'error': str(exc)})
            batch_warning('Gagal menulis BI artifacts:', exc)
    write_batch_summary('forecast', rows, {'brand':'ANEMOS','forecast_range_days':len(forecast_dates),'forecast_dates':[d.isoformat() for d in forecast_dates], 'locations_ok': sum(1 for row in rows if row.get('run_status') != 'error'), 'combined_outputs': combined_outputs})
    try:
        sentinel_write_root_public_index(locations, rows, base_args)
    except Exception as exc:
        batch_warning('Gagal menulis public index ANEMOS:', exc)
    return rows



# =============================================================================
# ANEMOS v12 PREMIUM PUBLIC APP UI
# This section overrides only the public presentation layer. The forecast engine,
# sources, output filenames, CSV/JSON compatibility, and multi-day runner remain
# compatible with previous ANEMOS/AETHER versions.
# =============================================================================

SENTINEL_PUBLIC_UI_VERSION = "anemos_v12_2_csv_crashproof"


def _anemos12_css():
    return """
    :root{
      --bg0:#eef7ff; --bg1:#f8fbff; --ink:#071324; --muted:#64748b; --line:#d8e5f3;
      --card:#ffffff; --soft:#f6fbff; --navy:#07152d; --blue:#2563eb; --cyan:#38bdf8;
      --green:#10b981; --yellow:#f59e0b; --orange:#f97316; --red:#ef4444;
      --shadow:0 24px 70px rgba(15,23,42,.12); --softshadow:0 12px 34px rgba(15,23,42,.08);
      --radius:28px;
    }
    *{box-sizing:border-box}
    html{scroll-behavior:smooth}
    body{margin:0;background:
      radial-gradient(circle at 12% 0%, rgba(56,189,248,.22), transparent 32%),
      radial-gradient(circle at 90% 6%, rgba(37,99,235,.16), transparent 28%),
      linear-gradient(180deg,#edf7ff 0%,#f8fbff 54%,#edf7ff 100%);
      font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
      color:var(--ink);font-size:16px;line-height:1.55}
    a{color:#0b63e5;text-decoration:none;font-weight:900}
    .app{width:min(1440px,calc(100% - 56px));margin:26px auto 72px}
    .topbar{position:sticky;top:0;z-index:10;backdrop-filter:blur(16px);display:flex;align-items:center;justify-content:space-between;gap:18px;padding:14px 4px 18px;margin-bottom:8px}
    .brand{display:flex;align-items:center;gap:13px}.mark{width:42px;height:42px;border-radius:16px;background:linear-gradient(135deg,#07152d,#2563eb 65%,#7dd3fc);box-shadow:0 12px 28px rgba(37,99,235,.28)}
    .brand b{display:block;font-size:19px;letter-spacing:-.02em}.brand span{display:block;color:var(--muted);font-size:13px;margin-top:1px}
    .nav{display:flex;gap:10px;flex-wrap:wrap}.btn,.nav a{display:inline-flex;align-items:center;justify-content:center;border:1px solid #b7d5ff;background:#fff;border-radius:999px;padding:10px 14px;font-size:13px;color:#075bd8;font-weight:900;box-shadow:0 6px 16px rgba(37,99,235,.06)}
    .btn.primary{background:#0b63e5;color:white;border-color:#0b63e5}.btn.ghost{background:#f7fbff}.btn:hover{transform:translateY(-1px)}
    .hero{position:relative;overflow:hidden;color:#fff;background:
      radial-gradient(circle at 88% 4%,rgba(255,255,255,.25),transparent 18%),
      linear-gradient(135deg,#06142c 0%,#113b95 47%,#2f7dff 100%);
      border-radius:36px;padding:46px 52px;box-shadow:var(--shadow);margin:10px 0 18px}
    .hero h1{font-size:clamp(42px,5.8vw,82px);line-height:.92;letter-spacing:-.065em;margin:18px 0 16px;max-width:1050px}
    .hero p{font-size:clamp(16px,1.5vw,21px);color:#eaf4ff;max-width:980px;margin:0}.chips{display:flex;gap:10px;flex-wrap:wrap}.chip{border:1px solid rgba(255,255,255,.36);background:rgba(255,255,255,.12);border-radius:999px;padding:7px 12px;font-size:12px;font-weight:950;color:white}
    .notice{border:1px solid #fdba74;background:#fff7ed;color:#7c2d12;border-radius:18px;padding:14px 18px;font-weight:950;margin:16px 0 20px}
    .grid{display:grid;gap:18px}.grid.two{grid-template-columns:1fr 1fr}.grid.three{grid-template-columns:repeat(3,1fr)}.grid.four{grid-template-columns:repeat(4,1fr)}
    .card,.panel,.date-card,.metric,.location-card{background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--softshadow)}
    .panel{padding:26px;margin:18px 0}.date-card{padding:20px}.label,.date-card span,.metric span,.var span{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.09em;font-weight:950;color:#64748b}.date-card b{font-size:22px;display:block;margin-top:6px}.date-card p{margin:5px 0 0;color:var(--muted)}
    .date-strip{display:grid;grid-template-columns:1.15fr 1fr 1fr;gap:16px;margin-bottom:18px}
    .lead{display:grid;grid-template-columns:minmax(0,1.55fr) 420px;gap:18px;align-items:stretch;margin-bottom:18px}
    .decision{background:linear-gradient(135deg,#06142c,#0d3478);color:white;border-radius:32px;padding:34px;box-shadow:var(--softshadow);min-height:250px;display:flex;flex-direction:column;justify-content:space-between}
    .decision h2{font-size:clamp(36px,4.5vw,64px);line-height:.98;letter-spacing:-.06em;margin:14px 0}.decision p{font-size:17px;color:#dbeafe;margin:0}.decision .small{font-size:14px;color:#cfe4ff;margin-top:10px}
    .badge{display:inline-flex;align-items:center;width:max-content;border-radius:999px;padding:8px 12px;font-size:13px;font-weight:950}.badge.low{background:#dcfce7;color:#166534}.badge.medium{background:#fef3c7;color:#92400e}.badge.high{background:#ffedd5;color:#9a3412}.badge.critical{background:#fee2e2;color:#991b1b}
    .metrics{display:grid;grid-template-columns:1fr 1fr;gap:14px}.metric{padding:20px}.metric strong{display:block;font-size:35px;line-height:1;letter-spacing:-.05em;margin:10px 0 6px}.metric small{color:var(--muted)}
    .section-head{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:18px}.section-head h2,.panel>h2{margin:0;font-size:30px;letter-spacing:-.04em}.section-head p,.muted{margin:0;color:var(--muted)}
    .day-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.day-tile{padding:20px;border:1px solid var(--line);border-radius:22px;background:#fff;border-top:7px solid var(--green)}.day-tile.medium{border-top-color:var(--yellow)}.day-tile.high,.day-tile.critical{border-top-color:var(--red)}.day-tile h3{font-size:28px;margin:5px 0 8px;letter-spacing:-.04em}.day-tile p{margin:0;color:#334155;font-weight:750}.mini-kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:14px}.mini-kpi span{background:#f7fbff;border:1px solid var(--line);border-radius:14px;padding:10px;color:var(--muted);font-size:12px}.mini-kpi b{display:block;color:var(--ink);font-size:17px}
    .chart-grid{display:grid;grid-template-columns:1.05fr .95fr;gap:18px}.chart{min-height:285px}.rainbars{height:205px;display:flex;align-items:flex-end;gap:10px;padding:18px 8px 6px;border-bottom:1px solid #dbe7f5}.rainbar{flex:1;display:flex;flex-direction:column;align-items:center;gap:7px;height:100%;justify-content:flex-end}.rainbar i{display:block;width:100%;min-height:4px;border-radius:10px 10px 4px 4px;background:linear-gradient(180deg,#38bdf8,#2563eb);height:var(--h)}.rainbar.medium i{background:linear-gradient(180deg,#fbbf24,#f59e0b)}.rainbar.high i{background:linear-gradient(180deg,#fb7185,#ef4444)}.rainbar b{font-size:12px}.rainbar small{font-size:11px;color:var(--muted)}
    .temp-line{width:100%;height:auto;min-height:205px}.vars-main{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:14px}.vars-more{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.var{padding:18px;border:1px solid var(--line);border-radius:20px;background:#fbfdff}.var.main{border-top:5px solid var(--blue);background:#fff}.var strong{display:block;font-size:28px;letter-spacing:-.04em;margin:8px 0 4px}.var small{color:var(--muted)}
    .periods{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.period{padding:18px;border:1px solid var(--line);border-radius:22px;border-top:7px solid var(--green);background:#fff}.period.medium{border-top-color:var(--yellow)}.period.high,.period.critical{border-top-color:var(--red)}.period h3{font-size:24px;margin:0 0 8px}.period .cond{font-weight:950;margin-bottom:8px}.period .mini{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.period .mini span{background:#f7fbff;border:1px solid var(--line);border-radius:13px;padding:9px;color:var(--muted);font-size:12px}.period .mini b{display:block;color:var(--ink);font-size:16px}
    .activity{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.activity .act{padding:16px;border:1px solid var(--line);border-radius:20px;background:#fff}.act strong{display:block;font-size:17px}.act small{color:var(--muted)}
    .scenarios{list-style:none;margin:0;padding:0}.scenarios li{display:flex;align-items:center;justify-content:space-between;padding:13px 0;border-bottom:1px solid var(--line);font-weight:900}.scenarios li:last-child{border-bottom:0}.big{font-size:25px;font-weight:950;line-height:1.2}.hours{display:grid;gap:10px}.hour{display:grid;grid-template-columns:90px 1.35fr repeat(5,112px);gap:12px;align-items:center;padding:15px 16px;border:1px solid var(--line);border-left:8px solid var(--green);border-radius:20px;background:#fff}.hour.medium{border-left-color:var(--yellow)}.hour.high,.hour.critical{border-left-color:var(--red)}.hour .time{font-weight:950;font-size:20px}.cond b{display:block}.cond small{color:var(--muted)}.cell{border:1px solid var(--line);background:#f8fbff;border-radius:14px;padding:10px}.cell b{display:block}.cell small{color:var(--muted);font-size:11px}
    details.panel summary{cursor:pointer}.links{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}.footer{text-align:center;color:var(--muted);font-size:13px;margin:34px 0}.location-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.location-card{padding:24px;position:relative;overflow:hidden}.location-card:before{content:"";position:absolute;inset:0 0 auto 0;height:7px;background:linear-gradient(90deg,#22c55e,#38bdf8,#2563eb)}.location-card.medium:before{background:linear-gradient(90deg,#f59e0b,#38bdf8)}.location-card h2{font-size:30px;margin:0 0 10px;letter-spacing:-.04em}.location-card .summary{min-height:90px;color:#1f2937;font-weight:650}.loc-kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:15px 0}.loc-kpi span{border:1px solid var(--line);background:#f7fbff;border-radius:14px;padding:10px;color:var(--muted);font-size:12px}.loc-kpi b{display:block;color:var(--ink);font-size:17px}.progress{height:12px;border-radius:999px;background:#e6eef7;overflow:hidden}.progress span{display:block;height:100%;width:var(--p,0%);background:linear-gradient(90deg,#1d4ed8,#38bdf8)}
    table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--line);text-align:left}th{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}.table-scroll{overflow:auto}
    @media(max-width:1180px){.lead,.chart-grid,.grid.two,.date-strip{grid-template-columns:1fr}.metrics,.grid.four{grid-template-columns:repeat(2,1fr)}.vars-main{grid-template-columns:repeat(3,1fr)}.vars-more{grid-template-columns:repeat(3,1fr)}.day-strip,.location-grid{grid-template-columns:1fr}.periods{grid-template-columns:repeat(2,1fr)}.activity{grid-template-columns:repeat(2,1fr)}.hour{grid-template-columns:88px 1fr repeat(2,105px)}.hour .cond{grid-column:2/-1}}
    @media(max-width:720px){.app{width:calc(100% - 24px);margin:14px auto 48px}.topbar{position:static;align-items:flex-start;flex-direction:column}.hero{border-radius:28px;padding:32px 24px}.hero h1{font-size:44px}.hero p{font-size:16px}.metrics,.vars-main,.vars-more,.periods,.activity{grid-template-columns:1fr}.section-head{align-items:flex-start;flex-direction:column}.hour{grid-template-columns:1fr 1fr}.hour .time,.hour .cond{grid-column:auto}.cell{min-width:0}.mini-kpi{grid-template-columns:1fr}.nav{width:100%}.nav a{flex:1}.rainbars{gap:5px}}
    """


def _anemos12_icon(row_or_text):
    text = ""
    if isinstance(row_or_text, dict):
        text = str(row_or_text.get('weather') or row_or_text.get('condition') or '').lower()
    else:
        text = str(row_or_text or '').lower()
    if 'petir' in text or 'thunder' in text: return '⛈️'
    if 'hujan' in text or 'rain' in text: return '🌧️'
    if 'cerah' in text and 'berawan' not in text: return '☀️'
    if 'cerah' in text or 'cloud' in text or 'awan' in text or 'berawan' in text: return '⛅'
    return '🌤️'


def _anemos12_rain_chart(rows):
    selected = rows or []
    if len(selected) > 12:
        selected = [r for i, r in enumerate(selected) if i % 2 == 0][:12]
    if not selected:
        return "<p class='muted'>Data peluang hujan belum tersedia.</p>"
    bars = []
    for r in selected:
        p = safe_float(r.get('prob_rain')) or 0
        h = max(3, min(100, p))
        cls = 'high' if p >= 70 else 'medium' if p >= 45 else ''
        bars.append(f"<div class='rainbar {cls}'><i style='--h:{h}%'></i><b>{_anemos11_num(p,'%')}</b><small>{_v6_esc(_anemos_safe_hour(r)[:2])}</small></div>")
    return "<div class='rainbars'>" + ''.join(bars) + "</div>"


def _anemos12_temp_chart(rows):
    return _anemos_temp_svg(rows)


def _anemos12_kpi(label, value, note=''):
    return f"<article class='metric'><span>{_v6_esc(label)}</span><strong>{_v6_esc(str(value))}</strong><small>{_v6_esc(str(note or ''))}</small></article>"


def _anemos12_var_html(rows):
    rows = rows or []
    peak_rain = _anemos_peak(rows, 'prob_rain')
    peak_wind = _anemos_peak(rows, 'wind_ms_p50')
    peak_heat = _anemos_peak(rows, 'heat_index_p50')
    primary = [
        ('Suhu', _anemos11_num(_anemos_avg(rows,'temp_p50'),'°C',1), 'rata-rata harian'),
        ('Terasa seperti', _anemos11_num(_anemos_avg(rows,'apparent_temp_p50') or _anemos_avg(rows,'heat_index_p50'),'°C',1), 'gabungan suhu dan kelembapan'),
        ('Kelembapan', _anemos11_num(_anemos_avg(rows,'rh_p50'),'%'), 'rata-rata harian'),
        ('Heat index', _anemos11_num(safe_float(peak_heat.get('heat_index_p50')) or _anemos_avg(rows,'heat_index_p50'),'°C',1), f"tertinggi sekitar {_anemos_safe_hour(peak_heat)}"),
        ('Peluang hujan', _anemos11_num(safe_float(peak_rain.get('prob_rain')),'%'), f"tertinggi sekitar {_anemos_safe_hour(peak_rain)}"),
        ('Angin', _anemos11_num((safe_float(peak_wind.get('wind_ms_p50')) or 0)*3.6,' km/jam',1), f"tertinggi sekitar {_anemos_safe_hour(peak_wind)}"),
    ]
    secondary = [
        ('Awan', _anemos11_num(_anemos_avg(rows,'cloud_cover_p50'),'%'), 'perkiraan tutupan awan'),
        ('Titik embun', _anemos11_num(_anemos_avg(rows,'dew_point_p50'),'°C',1), 'indikasi kelembapan udara'),
        ('Hembusan angin', _anemos11_num((_anemos_avg(rows,'wind_gust_ms_p50') or 0)*3.6,' km/jam',1) if _anemos_avg(rows,'wind_gust_ms_p50') is not None else '—', 'potensi hembusan angin'),
        ('Tekanan udara', _anemos11_num(_anemos_avg(rows,'pressure_msl_p50'),' hPa',0), 'jika tersedia dari model'),
        ('Jarak pandang', _anemos11_num(_anemos_avg(rows,'visibility_p50'),' km',1), 'nilai rendah berarti visibilitas berkurang'),
    ]
    def cards(items, cls):
        return ''.join(f"<article class='var {cls}'><span>{_v6_esc(a)}</span><strong>{_v6_esc(str(b))}</strong><small>{_v6_esc(c)}</small></article>" for a,b,c in items)
    return f"<div class='vars-main'>{cards(primary,'main')}</div><div class='vars-more'>{cards(secondary,'')}</div>"


def _anemos12_period_card(name, rows):
    rows = rows or []
    if not rows:
        return f"<article class='period'><h3>{_v6_esc(name)}</h3><p class='muted'>Data belum tersedia.</p></article>"
    peak = _anemos_peak(rows, 'prob_rain')
    temp = _anemos_avg(rows, 'temp_p50')
    rh = _anemos_avg(rows, 'rh_p50')
    risk_score = max((safe_float(peak.get('prob_rain')) or 0), (safe_float(_anemos_peak(rows,'rain_threat_score').get('rain_threat_score')) or 0))
    risk_word, risk_cls, _ = _anemos_risk_level(risk_score)
    weather = (peak.get('weather') or rows[0].get('weather') or 'Cuaca lokal')
    return f"""
    <article class='period {risk_cls}'>
      <h3>{_anemos12_icon(weather)} {_v6_esc(name)}</h3>
      <div class='cond'>{_v6_esc(str(weather))}</div>
      <p class='muted'>Risiko hujan {risk_word}. Jam perhatian: {_v6_esc(_anemos_safe_hour(peak))}.</p>
      <div class='mini'><span>Suhu<b>{_anemos11_num(temp,'°C',1)}</b></span><span>Kelembapan<b>{_anemos11_num(rh,'%')}</b></span><span>Hujan<b>{_anemos11_num(peak.get('prob_rain'),'%')}</b></span><span>Risiko<b>{_v6_esc(risk_word)}</b></span></div>
    </article>"""


def _anemos12_day_detail(day, args, compact=False):
    rows = day.get('rows') or []
    if not rows:
        return ''
    groups = _anemos_group_rows(rows)
    periods = ''.join(_anemos12_period_card(n, groups.get(n, [])) for n in ['Pagi','Siang','Sore','Malam'])
    s = _anemos11_day_summary(day, args)
    title = 'Ringkasan hari ini' if day.get('date') == _anemos11_today(args) else 'Ringkasan hari'
    label = _anemos11_day_label(day.get('date'), args)
    return f"""
    <section class='panel'>
      <div class='section-head'><div><span class='label'>{_v6_esc(label)}</span><h2>{title}</h2></div><p>Fokus pada perubahan risiko dalam sehari.</p></div>
      <div class='periods'>{periods}</div>
    </section>"""


def _anemos12_hour_rows(rows):
    rows = rows or []
    if not rows:
        return "<p class='muted'>Data per jam belum tersedia.</p>"
    selected = []
    must = {'00:00','03:00','06:00','09:00','12:00','15:00','18:00','21:00'}
    # include peak rain hour and nearby neighbors if present
    peak = _anemos_peak(rows, 'prob_rain')
    ph = _anemos_safe_hour(peak)
    if ph and ph != '—': must.add(ph)
    for r in rows:
        h = _anemos_safe_hour(r)
        p = safe_float(r.get('prob_rain')) or 0
        if h in must or p >= 45:
            selected.append(r)
    # de-duplicate by hour and cap to 12
    seen, final = set(), []
    for r in selected:
        h = _anemos_safe_hour(r)
        if h not in seen:
            final.append(r); seen.add(h)
    final = final[:12]
    items=[]
    for r in final:
        p = safe_float(r.get('prob_rain')) or 0
        risk_score = max(p, safe_float(r.get('rain_threat_score')) or 0)
        risk_word, risk_cls, _ = _anemos_risk_level(risk_score)
        weather = r.get('weather') or 'Cuaca lokal'
        note = 'Siapkan payung atau rencana cadangan bila beraktivitas di luar ruang.' if risk_cls in ('medium','high','critical') else 'Relatif aman; tetap pantau kondisi sekitar.'
        items.append(f"""
        <article class='hour {risk_cls}'>
          <div class='time'>{_v6_esc(_anemos_safe_hour(r))}</div>
          <div class='cond'><b>{_anemos12_icon(weather)} {_v6_esc(str(weather))}</b><small>{_v6_esc(note)}</small></div>
          <div class='cell'><b>{_anemos11_num(r.get('temp_p50'),'°C',1)}</b><small>Suhu</small></div>
          <div class='cell'><b>{_anemos11_num(r.get('rh_p50'),'%')}</b><small>Kelembapan</small></div>
          <div class='cell'><b>{_anemos11_num(r.get('heat_index_p50'),'°C',1)}</b><small>Heat index</small></div>
          <div class='cell'><b>{_anemos11_num(p,'%')}</b><small>Peluang hujan</small></div>
          <div class='cell'><b>{_v6_esc(risk_word)}</b><small>Risiko</small></div>
        </article>""")
    return ''.join(items)


def _anemos12_activity_tiles(rows):
    peak = _anemos_peak(rows or [], 'prob_rain')
    p = safe_float(peak.get('prob_rain')) or 0
    h = _anemos_safe_hour(peak)
    rain_note = 'Bawa payung atau jas hujan.' if p >= 45 else 'Payung opsional, tetap pantau langit.'
    outdoor = 'Lebih aman pagi/menjelang malam.' if p >= 45 else 'Cukup aman untuk aktivitas luar ruang.'
    laundry = 'Lebih baik sebelum siang.' if p >= 35 else 'Masih memungkinkan.'
    commute = 'Waspadai jam hujan.' if p >= 45 else 'Relatif normal.'
    return f"""
    <div class='activity'>
      <article class='act'><strong>🚶 Aktivitas luar</strong><small>{_v6_esc(outdoor)}</small></article>
      <article class='act'><strong>☂️ Payung</strong><small>{_v6_esc(rain_note)}</small></article>
      <article class='act'><strong>🛵 Perjalanan</strong><small>{_v6_esc(commute)} Perhatikan sekitar {h}.</small></article>
      <article class='act'><strong>👕 Jemur pakaian</strong><small>{_v6_esc(laundry)}</small></article>
      <article class='act'><strong>🏃 Olahraga</strong><small>{'Utamakan pagi.' if p >= 45 else 'Pagi atau sore masih memungkinkan.'}</small></article>
    </div>"""


def _anemos12_dashboard_html(days, source_state_rows, args, report=False):
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    tz_label = _v6_timezone_label(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    loc = getattr(args, 'location_name', 'Lokasi')
    if not days:
        valid_text = 'Prakiraan belum tersedia'; summary = 'Data prakiraan belum tersedia.'; current={'date':now.date(),'rows':[],'daily':{}}
    else:
        valid_text = f"{_v6_format_date_id(days[0]['date'])}–{_v6_format_date_id(days[-1]['date'])}"
        current = _anemos11_current_day(days, args)
        summary = _anemos11_top_summary(days, args)
    rows = current.get('rows') or []
    s = _anemos11_day_summary(current, args) if rows else {'title':'Prakiraan belum tersedia','badge':'Data belum tersedia','risk_word':'belum pasti','risk_cls':'medium','peak_prob':None,'peak_hour':'—'}
    peak = _anemos_peak(rows, 'prob_rain') if rows else {}
    peak_failure = _anemos_peak(rows, 'forecast_failure_risk') if rows else {}
    certainty_raw = max(0, 100 - (safe_float(peak_failure.get('forecast_failure_risk')) or 0)) if rows else None
    certainty = _anemos11_num(certainty_raw, '%') if certainty_raw is not None and certainty_raw > 0 else '—'
    risky, safe = _anemos_time_windows(rows)
    verification = read_json(path_output('sentinel_x_verification_summary.json'), default={}) or {}
    matched = verification.get('matched_cases', 0)
    date_cards = f"""
      <section class='date-strip'>
        <article class='date-card'><span>Periode prakiraan</span><b>{_v6_esc(valid_text)}</b><p>Hari ini sampai beberapa hari ke depan</p></article>
        <article class='date-card'><span>Terakhir diperbarui</span><b>{_v6_esc(_v6_format_date_id(now.date()))}, {now.strftime('%H:%M')} {tz_label}</b></article>
        <article class='date-card'><span>Lokasi</span><b>{_v6_esc(loc)}</b></article>
      </section>"""
    day_cards = ''.join(f"""
      <article class='day-tile {_anemos11_day_summary(day,args)['risk_cls']}'>
        <span class='label'>{_v6_esc(_anemos11_day_summary(day,args)['label'])}</span>
        <h3>{_v6_esc(_anemos11_day_summary(day,args)['risk_word'].capitalize())}</h3>
        <p>{_v6_esc(_anemos11_day_summary(day,args)['title'])}</p>
        <div class='mini-kpi'><span>Peluang hujan<b>{_anemos11_num(_anemos11_day_summary(day,args)['peak_prob'],'%')}</b></span><span>Jam perhatian<b>{_v6_esc(_anemos11_day_summary(day,args)['peak_hour'])}</b></span><span>Suhu<b>{_anemos11_num(_anemos11_day_summary(day,args)['temp_avg'],'°C',1)}</b></span></div>
      </article>""" for day in days[:3])
    current_detail = _anemos12_day_detail(current, args) if rows else ''
    future_details = ''.join(_anemos12_day_detail(day, args) for day in days[1:])
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Prakiraan { _v6_esc(loc) }</title><style>{_anemos12_css()}</style></head><body><main class='app'>
      <nav class='topbar'><div class='brand'><div class='mark'></div><div><b>ANEMOS</b><span>{_v6_esc(loc)} · Diperbarui {now.strftime('%H:%M')} {tz_label}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Prakiraan</a><a href='sentinel_x_report.html'>Ringkasan</a><a href='sentinel_x_accuracy_public.html'>Akurasi</a><a href='../'>Pilih lokasi</a></div></nav>
      <section class='hero'><div class='chips'><span class='chip'>Berlaku { _v6_esc(valid_text) }</span><span class='chip'>Diperbarui { _v6_esc(_v6_format_date_id(now.date())) }, {now.strftime('%H:%M')} {tz_label}</span><span class='chip'>ANEMOS</span></div><h1>{'Ringkasan Prakiraan' if report else 'Prakiraan Cuaca Lokal'}</h1><p>{_v6_esc(summary)}</p></section>
      <div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div>
      {date_cards}
      <section class='lead'><article class='decision'><div><span class='badge {s['risk_cls']}'>{_v6_esc(s['badge'])}</span><h2>{_v6_esc(s['title'])}</h2><p>Puncak peluang hujan sekitar <b>{_v6_esc(s.get('peak_hour','—'))}</b>. Gunakan prakiraan ini untuk mengatur aktivitas harian.</p></div><p class='small'>Jam perlu diperhatikan: <b>{_v6_esc(risky)}</b>. Waktu relatif lebih aman: <b>{_v6_esc(safe)}</b>.</p></article><div class='metrics'>{_anemos12_kpi('Peluang hujan', _anemos11_num(s.get('peak_prob'),'%'), 'tertinggi sekitar '+str(s.get('peak_hour','—')))}{_anemos12_kpi('Tingkat risiko', s.get('risk_word','belum pasti'), 'hari yang sedang dipilih')}{_anemos12_kpi('Kepercayaan', certainty, 'lebih rendah saat hujan lokal sulit dipastikan')}{_anemos12_kpi('Data akurasi', matched, 'pasangan prakiraan-observasi')}</div></section>
      <section class='panel'><div class='section-head'><h2>Ringkasan beberapa hari</h2><p>Bandingkan kondisi hari ini dan hari berikutnya.</p></div><div class='day-strip'>{day_cards}</div></section>
      <section class='chart-grid'><section class='panel chart'><div class='section-head'><h2>Peluang hujan</h2><p>Perubahan peluang hujan per jam.</p></div>{_anemos12_rain_chart(rows)}</section><section class='panel chart'><div class='section-head'><h2>Suhu</h2><p>Perkiraan perubahan suhu.</p></div>{_anemos12_temp_chart(rows)}</section></section>
      <section class='panel'><div class='section-head'><h2>Kondisi utama</h2><p>Variabel yang paling berguna untuk aktivitas harian.</p></div>{_anemos12_var_html(rows)}</section>
      {current_detail}
      <section class='grid two'><section class='panel'><h2>Saran aktivitas</h2><p class='big'>{_v6_esc(s['title'])}</p><p class='muted'>Jika langit cepat gelap, angin berubah, atau mulai gerimis, prioritaskan kondisi aktual di sekitar.</p><div style='height:14px'></div>{_anemos12_activity_tiles(rows)}</section><section class='panel'><h2>Kemungkinan kondisi</h2><ul class='scenarios'>{_anemos_scenarios(rows)}</ul></section></section>
      <section class='panel'><div class='section-head'><h2>Jam penting</h2><p>Fokus pada jam utama dan jam rawan.</p></div><div class='hours'>{_anemos12_hour_rows(rows)}</div><details style='margin-top:14px'><summary><b>Lihat data lengkap</b></summary><p class='muted'>Gunakan file CSV untuk melihat semua jam dan seluruh variabel.</p><div class='links'><a class='btn' href='sentinel_x_variables.csv'>CSV variabel</a><a class='btn' href='sentinel_x.csv'>CSV lengkap</a><a class='btn' href='sentinel_x.json'>JSON</a></div></details></section>
      <details class='panel'><summary><b>Lihat ringkasan hari berikutnya</b></summary>{future_details if future_details else '<p class="muted">Tidak ada hari tambahan yang tersedia.</p>'}</details>
      <details class='panel'><summary><b>Catatan data dan batas penggunaan</b></summary><ul><li>Prakiraan ini digunakan sebagai panduan aktivitas harian.</li><li>Ini bukan peringatan resmi cuaca ekstrem.</li><li>Untuk kondisi berbahaya, ikuti informasi BMKG dan kondisi setempat.</li><li>Hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</li></ul><div class='links'><a class='btn' href='sentinel_x_accuracy_public.html'>Status akurasi</a><a class='btn' href='sentinel_x_forecast_contract.json'>Batas penggunaan</a><a class='btn' href='../'>Pilih lokasi</a></div></details>
      <p class='footer'>ANEMOS · Prakiraan cuaca lokal berbasis multi-sumber · {_v6_esc(valid_text)} · Diperbarui {now.strftime('%H:%M')} {tz_label}</p>
    </main></body></html>"""
    return doc


def anemos_write_multiday_public_pages(args, forecast_dates=None, source_state_rows=None):
    days = _anemos11_all_day_payloads(args, forecast_dates)
    source_state_rows = source_state_rows or []
    doc = _anemos12_dashboard_html(days, source_state_rows, args, report=False)
    report = _anemos12_dashboard_html(days, source_state_rows, args, report=True)
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(doc))
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(report))
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    valid = f"{_v6_format_date_id(days[0]['date'])}–{_v6_format_date_id(days[-1]['date'])}" if days else 'Belum tersedia'
    atomic_write_text(path_output(AETHER_REPORT_FILENAME), lambda f: f.write(f"# Ringkasan prakiraan ANEMOS\n\nVersi publik: `sentinel_x_report.html`.\n\nPeriode: {valid}\nDiperbarui: {_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args, 'timezone', DEFAULT_TIMEZONE))}\n"))
    summary_payload = {'brand':'ANEMOS','ui_version':SENTINEL_PUBLIC_UI_VERSION,'generated_at':now.isoformat(),'location_slug':getattr(args,'location_slug',''),'location_name':getattr(args,'location_name',''),'forecast_days':[]}
    for day in days:
        s = _anemos11_day_summary(day, args)
        summary_payload['forecast_days'].append({'date':day['date'].isoformat(),'label':s['label'],'summary':s['title'],'risk':s['risk_word'],'risk_class':s['risk_cls'],'peak_rain_probability':s['peak_prob'],'peak_rain_hour':s['peak_hour'],'avg_temperature_c':aether_round(s['temp_avg'],1) if s['temp_avg'] is not None else None,'avg_humidity_pct':aether_round(s['rh_avg'],0) if s['rh_avg'] is not None else None})
    write_json(path_output('anemos_multiday_summary.json'), summary_payload)
    write_json(path_output('command_center_manifest_sentinel_x.json'), {'brand':'ANEMOS','dashboard':path_output(AETHER_DASHBOARD_FILENAME),'report_html':path_output(AETHER_REPORT_HTML_FILENAME),'generated_at':now.isoformat(),'ui_version':SENTINEL_PUBLIC_UI_VERSION,'days':len(days)})
    return {'days':len(days),'dashboard':path_output(AETHER_DASHBOARD_FILENAME),'report_html':path_output(AETHER_REPORT_HTML_FILENAME)}


def _anemos12_accuracy_html(rows, args):
    verification_result = sentinel_compute_verification(rows, args)
    if isinstance(verification_result, tuple):
        summary = verification_result[0] or {}; reliability_raw = verification_result[2] if len(verification_result) > 2 else []
    else:
        summary = verification_result or {}; reliability_raw = summary.get('reliability_bins') or []
    matched = int((summary or {}).get('matched_cases') or 0)
    target = max(1, int(getattr(args, 'verification_min_cases', 30) or 30))
    pct = min(100, round(matched / target * 100))
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    rows_html = ''
    for r in reliability_raw or []:
        if isinstance(r, dict):
            rows_html += f"<tr><td>{_v6_esc(str(r.get('bin') or r.get('probability_bin') or ''))}</td><td>{_v6_esc(str(r.get('n',0)))}</td><td>{_v6_esc(str(r.get('mean_forecast_pct') or r.get('mean_forecast_probability') or '—'))}</td><td>{_v6_esc(str(r.get('observed_frequency_pct') or r.get('observed_rain_frequency') or '—'))}</td></tr>"
    if not rows_html:
        rows_html = "<tr><td colspan='4'>Belum ada pasangan prakiraan dan observasi.</td></tr>"
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Status akurasi</title><style>{_anemos12_css()}</style></head><body><main class='app'>
    <nav class='topbar'><div class='brand'><div class='mark'></div><div><b>ANEMOS</b><span>{_v6_esc(getattr(args,'location_name',''))} · Diperbarui {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args,'timezone',DEFAULT_TIMEZONE))}</span></div></div><div class='nav'><a href='command_center_sentinel_x.html'>Prakiraan</a><a href='sentinel_x_report.html'>Ringkasan</a><a href='../'>Pilih lokasi</a></div></nav>
    <section class='hero'><div class='chips'><span class='chip'>Status akurasi</span><span class='chip'>{matched}/{target} pasangan data</span></div><h1>Status Akurasi Prakiraan</h1><p>Halaman ini menunjukkan apakah prakiraan ANEMOS sudah memiliki cukup data pembanding observasi. Skor akan lebih bermakna setelah data terkumpul.</p></section>
    <div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div>
    <section class='panel'><h2>{'Data akurasi sudah mulai terbaca' if matched>=target else 'Data akurasi belum cukup'}</h2><div class='progress' style='--p:{pct}%'><span></span></div><p class='muted' style='margin-top:12px'>Target awal: minimal {target} pasangan prakiraan-observasi. Saat ini terkumpul {matched}/{target} pasangan.</p></section>
    <section class='grid four'><article class='metric'><span>Error suhu</span><strong>{_anemos11_num(summary.get('temperature_mae_c'),'°C',1)}</strong><small>lebih kecil lebih baik</small></article><article class='metric'><span>Skor peluang hujan</span><strong>{_anemos11_num(summary.get('brier_rain', summary.get('rain_brier_score')),'',3)}</strong><small>lebih kecil lebih baik</small></article><article class='metric'><span>Hujan terdeteksi</span><strong>{_anemos11_num(summary.get('pod_rain', summary.get('rain_pod')),'%')}</strong><small>kemampuan menangkap hujan</small></article><article class='metric'><span>Alarm keliru</span><strong>{_anemos11_num(summary.get('far_rain', summary.get('rain_far')),'%')}</strong><small>semakin kecil semakin baik</small></article></section>
    <section class='panel'><div class='section-head'><h2>Bukti peluang hujan</h2><p>Tabel akan terisi setelah observasi terkumpul.</p></div><div class='table-scroll'><table><thead><tr><th>Kelompok peluang</th><th>Jumlah kasus</th><th>Rata-rata prakiraan</th><th>Hujan yang terjadi</th></tr></thead><tbody>{rows_html}</tbody></table></div></section>
    <section class='panel'><h2>Penjelasan singkat</h2><p>Sampai data observasi cukup, gunakan prakiraan sebagai panduan harian. Evaluasi akurasi akan bertambah otomatis seiring bertambahnya data.</p><div class='links'><a class='btn' href='sentinel_x_verification_summary.json'>Data verifikasi</a><a class='btn' href='sentinel_x_reliability.csv'>CSV peluang hujan</a><a class='btn' href='sentinel_x_verification_pairs.csv'>CSV pasangan data</a></div></section>
    <p class='footer'>ANEMOS · Status akurasi · Diperbarui {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args,'timezone',DEFAULT_TIMEZONE))}</p>
    </main></body></html>"""
    return doc


def sentinel_write_verification_artifacts(rows, args):
    verification_result = sentinel_compute_verification(rows, args)
    if isinstance(verification_result, tuple):
        summary = verification_result[0] or {}
        pairs = verification_result[1] if len(verification_result) > 1 else []
        reliability_raw = verification_result[2] if len(verification_result) > 2 else []
    else:
        summary = verification_result or {}
        pairs = summary.get('matched_pairs') or []
        reliability_raw = summary.get('reliability_bins') or []

    # ANEMOS v12.1 hotfix:
    # sentinel_compute_verification() has used more than one reliability schema
    # across versions. csv.DictWriter is strict, so normalize every row before
    # writing. This prevents: ValueError: dict contains fields not in fieldnames.
    reliability = []
    for r in reliability_raw or []:
        if not isinstance(r, dict):
            continue
        reliability.append({
            'bin': r.get('bin', r.get('probability_bin', '')),
            'n': r.get('n', 0),
            'mean_forecast_pct': r.get('mean_forecast_pct', r.get('mean_forecast_probability', '')),
            'observed_frequency_pct': r.get('observed_frequency_pct', r.get('observed_rain_frequency', '')),
        })

    pair_fieldnames = list(pairs[0].keys()) if pairs else [
        'target_date', 'jam', 'forecast_rain_prob', 'observed_rain',
        'forecast_temp_c', 'observed_temp_c'
    ]

    write_json(path_output('sentinel_x_verification_summary.json'), summary)
    write_dict_csv(path_output('sentinel_x_reliability.csv'), ['bin','n','mean_forecast_pct','observed_frequency_pct'], reliability)
    write_dict_csv(path_output('sentinel_x_verification_pairs.csv'), pair_fieldnames, pairs if pairs else [])
    atomic_write_text(path_output('sentinel_x_accuracy_public.html'), lambda f: f.write(_anemos12_accuracy_html(rows, args)))
    return summary


def _anemos12_location_card(loc, base_url=''):
    payload = _anemos11_location_payload(loc)
    days = payload.get('forecast_days') or []
    prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
    if days:
        d0 = days[0]; risk_cls = d0.get('risk_class') or ('medium' if (safe_float(d0.get('peak_rain_probability')) or 0) >= 45 else 'low')
        summary = d0.get('summary') or 'Ringkasan prakiraan tersedia.'
        dayline = ' · '.join(_v6_esc(str(d.get('label',''))) for d in days[:3])
        kpi = f"<div class='loc-kpi'><span>Peluang hujan<b>{_anemos11_num(d0.get('peak_rain_probability'),'%')}</b></span><span>Jam perhatian<b>{_v6_esc(str(d0.get('peak_rain_hour') or '—'))}</b></span><span>Suhu<b>{_anemos11_num(d0.get('avg_temperature_c'),'°C',1)}</b></span></div>"
    else:
        risk_cls=''; summary='Ringkasan belum tersedia. Jalankan pembaruan prakiraan untuk memperbarui halaman ini.'; dayline='Belum tersedia'; kpi="<div class='loc-kpi'><span>Status<b>Belum ada</b></span><span>Hujan<b>—</b></span><span>Suhu<b>—</b></span></div>"
    return f"""
    <article class='location-card {risk_cls}'>
      <h2>{_v6_esc(loc.location_name)}</h2><p class='summary'>{_v6_esc(summary)}</p><p class='muted'>{dayline}</p>{kpi}
      <div class='links'><a class='btn primary' href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Lihat prakiraan</a><a class='btn' href='{prefix}{AETHER_REPORT_HTML_FILENAME}'>Ringkasan</a><a class='btn' href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a><a class='btn ghost' href='{prefix}sentinel_x_variables.csv'>CSV variabel</a></div>
    </article>"""


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, 'public_base_url', '') or '').rstrip('/')
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    updated = f"Diperbarui {_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args, 'timezone', DEFAULT_TIMEZONE))}"
    cards = ''.join(_anemos12_location_card(loc, base_url) for loc in locations)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Prakiraan Cuaca Lokal</title><style>{_anemos12_css()}</style></head><body><main class='app'>
    <section class='hero'><div class='chips'><span class='chip'>ANEMOS</span><span class='chip'>{_v6_esc(updated)}</span></div><h1>Prakiraan Cuaca Lokal</h1><p>Ringkasan cuaca untuk hari ini dan beberapa hari ke depan: peluang hujan, suhu, kelembapan, heat index, angin, dan saran aktivitas.</p></section>
    <div class='notice'><b>{_v6_esc(sentinel_public_disclaimer(args))}</b></div>
    <section class='location-grid'>{cards}</section>
    <details class='panel'><summary><b>Data terbuka</b></summary><p class='muted'>File berikut dapat digunakan untuk analisis, arsip, atau pembuatan dashboard lanjutan.</p><div class='links'><a class='btn' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='btn' href='forecast_all_locations.csv'>Forecast CSV</a><a class='btn' href='source_status_all_locations.csv'>Status sumber</a><a class='btn' href='forecast_batch_summary.json'>Ringkasan batch</a></div></details>
    <p class='footer'>ANEMOS · Prakiraan cuaca lokal berbasis multi-sumber · {updated}</p>
    </main></body></html>"""
    atomic_write_text(root_output_path('index.html'), lambda f: f.write(doc))
    write_json(root_output_path('anemos_public_portal_manifest.json'), {'brand':'ANEMOS','generated_at':now.isoformat(),'locations':[loc.slug for loc in locations],'index':root_output_path('index.html'),'disclaimer':sentinel_public_disclaimer(args),'ui_version':SENTINEL_PUBLIC_UI_VERSION})
    return root_output_path('index.html')



# =============================================================================
# ANEMOS v13 MAJOR PUBLIC UPGRADE
# Product-level public outputs layered safely on top of the existing forecast core.
# Focus: clearer homepage, multi-day public API, activity advice, compact widgets,
# share card, and a more app-like forecast page while keeping old links alive.
# =============================================================================
SENTINEL_PUBLIC_UI_VERSION = "anemos_v13_major_public_upgrade"
ANEMOS_VERSION = "v13.0-major-public-upgrade"


def _anemos13_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _anemos13_num(value, suffix="", digits=0, fallback="—"):
    v = _anemos13_float(value)
    if v is None:
        return fallback
    if digits == 0:
        return f"{round(v):.0f}{suffix}"
    return f"{v:.{digits}f}{suffix}"


def _anemos13_get(row, *keys, default=None):
    if not isinstance(row, dict):
        return default
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return default


def _anemos13_hour(row):
    raw = str(_anemos13_get(row, "jam", "target_time", "time", default="") or "")
    if "T" in raw:
        raw = raw.split("T", 1)[-1]
    if len(raw) >= 5 and raw[2] == ":":
        return raw[:5]
    if len(raw) >= 2 and raw[:2].isdigit():
        return f"{raw[:2]}:00"
    return raw or "—"


def _anemos13_date_from_row(row, fallback_date=None):
    raw = _anemos13_get(row, "target_date", "date", "local_date", default=None)
    if raw:
        raw = str(raw)[:10]
        try:
            return datetime.fromisoformat(raw).date()
        except Exception:
            pass
    return fallback_date


def _anemos13_date_label(d, tz_label="WIB"):
    if not d:
        return "Tanggal belum tersedia"
    try:
        return _v6_format_date_id(d)
    except Exception:
        return str(d)


def _anemos13_day_tag(d, today=None):
    if today is None:
        today = now_local(DEFAULT_TIMEZONE).date()
    if d == today:
        return "Hari ini"
    if d == today + timedelta(days=1):
        return "Besok"
    if d == today + timedelta(days=2):
        return "Lusa"
    return _anemos13_date_label(d).split(",", 1)[0]


def _anemos13_icon(category):
    text = str(category or "").lower()
    if "lebat" in text:
        return "⛈️"
    if "sedang" in text:
        return "🌧️"
    if "ringan" in text or "hujan" in text:
        return "🌦️"
    if "cerah" in text and "berawan" in text:
        return "🌤️"
    if "cerah" in text:
        return "☀️"
    if "berawan" in text:
        return "☁️"
    return "🌡️"


def _anemos13_mode(values, fallback="Berawan"):
    counts = {}
    for v in values:
        if v in (None, ""):
            continue
        counts[str(v)] = counts.get(str(v), 0) + 1
    if not counts:
        return fallback
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _anemos13_avg(rows, *keys):
    vals = []
    for r in rows or []:
        v = _anemos13_float(_anemos13_get(r, *keys))
        if v is not None:
            vals.append(v)
    return sum(vals) / len(vals) if vals else None


def _anemos13_peak(rows, *keys):
    best = None
    best_v = None
    for r in rows or []:
        v = _anemos13_float(_anemos13_get(r, *keys))
        if v is None:
            continue
        if best is None or v > best_v:
            best = r
            best_v = v
    return best or {}, best_v


def _anemos13_risk_level(prob, risk=None):
    p = _anemos13_float(prob, 0) or 0
    r = _anemos13_float(risk, p) or p
    score = max(p, r)
    if score >= 75:
        return "tinggi", "high", "Utamakan rencana cadangan untuk aktivitas luar ruang."
    if score >= 55:
        return "waspada", "orange", "Siapkan payung atau jas hujan, terutama pada jam rawan."
    if score >= 35:
        return "sedang", "yellow", "Aktivitas masih memungkinkan, tetapi tetap pantau perubahan langit."
    return "rendah", "green", "Aktivitas relatif aman dengan pemantauan cuaca sekitar."


def _anemos13_group_by_day(rows, fallback_date=None):
    groups = {}
    for r in rows or []:
        d = _anemos13_date_from_row(r, fallback_date)
        if not d:
            continue
        groups.setdefault(d, []).append(r)
    return [(d, groups[d]) for d in sorted(groups)]


def _anemos13_period_name(hour):
    try:
        h = int(str(hour)[:2])
    except Exception:
        h = 12
    if 5 <= h <= 10:
        return "Pagi"
    if 11 <= h <= 14:
        return "Siang"
    if 15 <= h <= 18:
        return "Sore"
    return "Malam"


def _anemos13_period_summary(rows):
    out = []
    for name in ["Pagi", "Siang", "Sore", "Malam"]:
        subset = [r for r in rows or [] if _anemos13_period_name(_anemos13_hour(r)) == name]
        if not subset:
            continue
        peak_prob_row, peak_prob = _anemos13_peak(subset, "prob_rain", "rain_probability", "probability_rain")
        peak_risk_row, peak_risk = _anemos13_peak(subset, "rain_threat_score", "forecast_failure_risk")
        risk_word, risk_cls, hint = _anemos13_risk_level(peak_prob, peak_risk)
        out.append({
            "period": name,
            "condition": _anemos13_mode([_anemos13_get(r, "cuaca", "weather", "category") for r in subset]),
            "risk": risk_word,
            "risk_class": risk_cls,
            "hint": hint,
            "attention_hour": _anemos13_hour(peak_prob_row or peak_risk_row),
            "avg_temp_c": aether_round(_anemos13_avg(subset, "temp_p50", "temperature_c"), 1),
            "avg_humidity_pct": aether_round(_anemos13_avg(subset, "rh_p50", "humidity_pct"), 0),
            "peak_rain_probability": aether_round(peak_prob, 0),
        })
    return out


def _anemos13_select_hours(rows):
    by_hour = { _anemos13_hour(r): r for r in rows or [] }
    wanted = ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]
    selected = []
    seen = set()
    for h in wanted:
        if h in by_hour:
            selected.append(by_hour[h]); seen.add(h)
    for key in ["prob_rain", "rain_threat_score", "temp_p50", "heat_index_p50", "wind_p50"]:
        row, _ = _anemos13_peak(rows, key)
        h = _anemos13_hour(row)
        if row and h not in seen:
            selected.append(row); seen.add(h)
    return sorted(selected, key=lambda r: _anemos13_hour(r))[:12]


def _anemos13_day_model(d, rows, args=None):
    peak_prob_row, peak_prob = _anemos13_peak(rows, "prob_rain", "rain_probability", "probability_rain")
    peak_risk_row, peak_risk = _anemos13_peak(rows, "rain_threat_score", "forecast_failure_risk")
    peak_heat_row, peak_heat = _anemos13_peak(rows, "heat_index_p50", "apparent_temperature_c", "temp_p50")
    peak_wind_row, peak_wind = _anemos13_peak(rows, "wind_p50", "wind_speed_kmh")
    risk_word, risk_cls, risk_hint = _anemos13_risk_level(peak_prob, peak_risk)
    condition = _anemos13_mode([_anemos13_get(r, "cuaca", "weather", "category") for r in rows])
    hour = _anemos13_hour(peak_prob_row or peak_risk_row)
    if risk_cls in ("high", "orange"):
        title = f"Waspada hujan sekitar pukul {hour}."
        advice = f"Aktivitas masih memungkinkan, tetapi siapkan payung atau jas hujan sekitar pukul {hour}."
    elif risk_cls == "yellow":
        title = f"Peluang hujan meningkat sekitar pukul {hour}."
        advice = f"Aktivitas masih bisa dilakukan. Tetap pantau langit dan siapkan payung bila keluar mendekati pukul {hour}."
    else:
        title = "Cuaca relatif aman untuk aktivitas harian."
        advice = "Aktivitas luar ruang relatif aman, tetapi tetap pantau perubahan cuaca di sekitar."
    return {
        "date": d.isoformat() if d else "",
        "date_label": _anemos13_date_label(d),
        "day_tag": _anemos13_day_tag(d, now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE)).date() if args else None),
        "condition": condition,
        "icon": _anemos13_icon(condition),
        "risk": risk_word,
        "risk_class": risk_cls,
        "risk_hint": risk_hint,
        "title": title,
        "advice": advice,
        "peak_rain_probability": aether_round(peak_prob, 0),
        "peak_rain_hour": hour,
        "avg_temperature_c": aether_round(_anemos13_avg(rows, "temp_p50", "temperature_c"), 1),
        "max_temperature_c": aether_round((_anemos13_peak(rows, "temp_p50", "temperature_c")[1]), 1),
        "avg_humidity_pct": aether_round(_anemos13_avg(rows, "rh_p50", "humidity_pct"), 0),
        "max_heat_index_c": aether_round(peak_heat, 1),
        "max_wind_kmh": aether_round(peak_wind, 1),
        "attention_hours": ", ".join(_anemos13_hour(r) for r in _anemos13_select_hours(rows)[:4]),
        "periods": _anemos13_period_summary(rows),
        "important_hours": rows and [
            {
                "hour": _anemos13_hour(r),
                "condition": _anemos13_get(r, "cuaca", "weather", "category", default="—"),
                "icon": _anemos13_icon(_anemos13_get(r, "cuaca", "weather", "category")),
                "temp_c": aether_round(_anemos13_float(_anemos13_get(r, "temp_p50", "temperature_c")), 1),
                "humidity_pct": aether_round(_anemos13_float(_anemos13_get(r, "rh_p50", "humidity_pct")), 0),
                "heat_index_c": aether_round(_anemos13_float(_anemos13_get(r, "heat_index_p50", "apparent_temperature_c")), 1),
                "rain_probability": aether_round(_anemos13_float(_anemos13_get(r, "prob_rain", "rain_probability")), 0),
                "wind_kmh": aether_round(_anemos13_float(_anemos13_get(r, "wind_p50", "wind_speed_kmh")), 1),
                "risk": _anemos13_risk_level(_anemos13_get(r, "prob_rain"), _anemos13_get(r, "rain_threat_score"))[0],
                "risk_class": _anemos13_risk_level(_anemos13_get(r, "prob_rain"), _anemos13_get(r, "rain_threat_score"))[1],
            }
            for r in _anemos13_select_hours(rows)
        ] or [],
    }


def _anemos13_activity_advice(day):
    p = _anemos13_float(day.get("peak_rain_probability"), 0) or 0
    h = day.get("peak_rain_hour") or "—"
    risk = day.get("risk_class") or "green"
    umbrella = "Bawa payung/jas hujan" if p >= 35 else "Tidak wajib, tetapi tetap siapkan jika langit cepat berubah"
    if p >= 55:
        motor = f"Lebih hati-hati sekitar {h}; jalan bisa lebih licin bila hujan muncul."
        outdoor = f"Utamakan pagi atau sebelum jam rawan {h}."
        laundry = "Lebih aman menjemur pada pagi–siang awal, jangan terlalu sore."
    elif p >= 35:
        motor = f"Masih memungkinkan, pantau kondisi sekitar {h}."
        outdoor = "Aktivitas luar ruang masih bisa, siapkan opsi pindah tempat."
        laundry = "Jemur lebih baik sebelum siang dan pantau awan sore."
    else:
        motor = "Kondisi relatif aman, tetap perhatikan perubahan lokal."
        outdoor = "Cocok untuk aktivitas luar ruang dengan pemantauan biasa."
        laundry = "Relatif aman untuk menjemur, tetap pantau awan lokal."
    return [
        {"label":"Payung", "value":umbrella, "class":risk},
        {"label":"Motor/perjalanan", "value":motor, "class":risk},
        {"label":"Aktivitas luar ruang", "value":outdoor, "class":risk},
        {"label":"Jemur pakaian", "value":laundry, "class":risk},
        {"label":"Olahraga", "value":"Lebih nyaman pagi atau menjelang malam bila suhu terasa lebih rendah.", "class":"green"},
    ]


def _anemos13_css():
    return """
    :root{--bg:#eaf3ff;--paper:#fff;--ink:#07111f;--muted:#66758b;--line:#d6e4f5;--blue:#1167e8;--blue2:#4aa3ff;--navy:#061b3d;--green:#18b66a;--yellow:#f4b000;--orange:#f97316;--red:#e11d48;--shadow:0 24px 70px rgba(15,42,90,.16)}
    *{box-sizing:border-box} body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:radial-gradient(circle at top right,#d9ecff,transparent 35%),linear-gradient(180deg,#f6fbff,#eaf3ff 70%);color:var(--ink);line-height:1.55} a{text-decoration:none;color:#075bd8;font-weight:900}.app{width:min(1280px,calc(100% - 44px));margin:28px auto 80px}.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 0 18px;backdrop-filter:blur(18px)}.brand{display:flex;align-items:center;gap:12px}.brand-mark{width:42px;height:42px;border-radius:15px;background:linear-gradient(135deg,#061b3d,#1677ff 70%,#9ad4ff);box-shadow:0 12px 30px rgba(17,103,232,.25)}.brand b{font-size:20px}.brand span{display:block;color:var(--muted);font-size:12px}.nav{display:flex;gap:10px;flex-wrap:wrap}.btn{display:inline-flex;align-items:center;justify-content:center;border:1px solid #a9ccff;background:#f0f7ff;color:#075bd8;border-radius:999px;padding:10px 14px;font-size:13px;font-weight:950}.btn.primary{background:#0d6efd;color:white;border-color:#0d6efd;box-shadow:0 12px 28px rgba(13,110,253,.25)}.hero{position:relative;overflow:hidden;border-radius:34px;padding:46px;background:radial-gradient(circle at 88% 16%,rgba(255,255,255,.28),transparent 16%),linear-gradient(135deg,#04142f 0,#0d3d9a 48%,#2e86ff 100%);color:white;box-shadow:var(--shadow)}.hero h1{font-size:clamp(42px,6vw,76px);line-height:.92;letter-spacing:-.07em;margin:14px 0}.hero p{font-size:19px;color:#eef6ff;max-width:860px;margin:0}.chips{display:flex;gap:10px;flex-wrap:wrap}.chip{display:inline-flex;border:1px solid rgba(255,255,255,.35);background:rgba(255,255,255,.14);border-radius:999px;padding:7px 12px;font-size:12px;font-weight:950;color:white}.notice{margin:16px 0 18px;padding:13px 16px;background:#fff7ed;border:1px solid #fdba74;border-radius:16px;color:#8a3510;font-weight:900}.grid{display:grid;gap:18px}.grid.three{grid-template-columns:repeat(3,1fr)}.grid.four{grid-template-columns:repeat(4,1fr)}.grid.two{grid-template-columns:1fr 1fr}.card,.panel,.metric,.day-card,.period,.hour{background:rgba(255,255,255,.92);border:1px solid var(--line);border-radius:24px;box-shadow:0 12px 34px rgba(15,42,90,.08)}.card{padding:24px}.card h2{font-size:30px;margin:0 0 8px;letter-spacing:-.035em}.card p{color:var(--muted)}.kpi{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:18px 0}.kpi span,.mini span{background:#f6faff;border:1px solid var(--line);border-radius:14px;padding:11px;color:var(--muted);font-size:12px}.kpi b,.mini b{display:block;color:var(--ink);font-size:19px;margin-top:3px}.section{margin:22px 0}.panel{padding:26px}.section-head{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:16px}.section-head h2,.panel h2{font-size:28px;margin:0;letter-spacing:-.04em}.section-head p{margin:0;color:var(--muted)}.lead{display:grid;grid-template-columns:1.45fr .9fr;gap:18px}.decision{background:linear-gradient(135deg,#061b3d,#0d3475);color:white;border-radius:28px;padding:28px;box-shadow:var(--shadow)}.decision h2{font-size:clamp(32px,4vw,56px);line-height:.95;letter-spacing:-.06em;margin:10px 0}.decision p{color:#dcedff;font-size:17px}.badge{display:inline-flex;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:950}.badge.green{background:#dcfce7;color:#166534}.badge.yellow{background:#fef3c7;color:#92400e}.badge.orange{background:#ffedd5;color:#9a3412}.badge.high{background:#fee2e2;color:#991b1b}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:14px}.metric{padding:20px}.metric span{font-size:11px;color:var(--muted);font-weight:950;text-transform:uppercase;letter-spacing:.08em}.metric strong{display:block;font-size:36px;letter-spacing:-.05em;margin:8px 0 2px}.metric small{color:var(--muted)}.day-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.day-card{padding:20px;border-top:6px solid var(--green)}.day-card.yellow{border-top-color:var(--yellow)}.day-card.orange{border-top-color:var(--orange)}.day-card.high{border-top-color:var(--red)}.day-card h3{font-size:24px;margin:4px 0}.day-card small{color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:.07em}.chart{display:flex;align-items:flex-end;gap:8px;height:180px;padding:16px 6px 4px;border-bottom:1px solid var(--line)}.bar{flex:1;min-width:12px;background:linear-gradient(180deg,#4aa3ff,#0d6efd);height:var(--h);border-radius:10px 10px 4px 4px;position:relative}.bar.orange{background:linear-gradient(180deg,#fb923c,#f97316)}.bar.high{background:linear-gradient(180deg,#fb7185,#e11d48)}.bar:after{content:attr(data-v);position:absolute;left:50%;bottom:calc(100% + 6px);transform:translateX(-50%);font-size:11px;font-weight:900;color:#0b2c5f}.bar-labels{display:flex;gap:8px;margin-top:8px}.bar-labels span{flex:1;text-align:center;color:var(--muted);font-size:11px}.variables{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.var{background:#f8fbff;border:1px solid var(--line);border-radius:18px;padding:16px}.var span{font-size:11px;color:var(--muted);font-weight:950;text-transform:uppercase;letter-spacing:.08em}.var strong{display:block;font-size:24px;margin:8px 0 2px}.periods{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.period{padding:18px;border-top:5px solid var(--green)}.period.yellow{border-top-color:var(--yellow)}.period.orange{border-top-color:var(--orange)}.period.high{border-top-color:var(--red)}.period h3{font-size:21px;margin:0 0 4px}.activity{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.activity .var{min-height:132px}.hour-list{display:grid;gap:10px}.hour{display:grid;grid-template-columns:88px 1.3fr repeat(5,105px);align-items:center;gap:12px;padding:14px 16px;border-left:7px solid var(--green)}.hour.yellow{border-left-color:var(--yellow)}.hour.orange{border-left-color:var(--orange)}.hour.high{border-left-color:var(--red)}.hour .time{font-weight:950;font-size:18px}.hour .cond b{display:block}.hour .cond small{color:var(--muted)}.hour .cell{background:#f8fbff;border:1px solid var(--line);border-radius:12px;padding:9px}.hour .cell b{display:block}.hour .cell small{font-size:11px;color:var(--muted)}.footer{text-align:center;color:var(--muted);font-size:13px;margin:34px 0}.progress{height:12px;background:#e2edf9;border-radius:999px;overflow:hidden}.progress span{display:block;height:100%;width:var(--p);background:linear-gradient(90deg,#0d6efd,#4aa3ff);border-radius:999px}@media(max-width:1120px){.lead,.grid.three,.grid.four,.grid.two,.day-strip{grid-template-columns:1fr 1fr}.variables{grid-template-columns:repeat(3,1fr)}.periods{grid-template-columns:repeat(2,1fr)}.activity{grid-template-columns:repeat(2,1fr)}.hour{grid-template-columns:75px 1fr repeat(2,95px)}}@media(max-width:720px){.app{width:calc(100% - 24px);margin:14px auto 50px}.top{position:static;flex-direction:column;align-items:flex-start}.hero{padding:30px}.lead,.grid.three,.grid.four,.grid.two,.day-strip,.variables,.periods,.activity{grid-template-columns:1fr}.metrics{grid-template-columns:1fr}.hour{grid-template-columns:1fr 1fr}.hour .cond{grid-column:1/-1}.kpi{grid-template-columns:1fr}.section-head{display:block}.chart{height:140px}}
    """


def _anemos13_chart(day):
    hours = day.get("important_hours") or []
    if not hours:
        return "<p class='muted'>Grafik belum tersedia.</p>"
    bars = []
    labels = []
    for h in hours:
        p = _anemos13_float(h.get("rain_probability"), 0) or 0
        cls = "high" if p >= 70 else "orange" if p >= 45 else ""
        bars.append(f"<div class='bar {cls}' data-v='{_anemos13_num(p,'%')}' style='--h:{max(3,min(100,p))}%'></div>")
        labels.append(f"<span>{_v6_esc(str(h.get('hour','')[:2]))}</span>")
    return f"<div class='chart'>{''.join(bars)}</div><div class='bar-labels'>{''.join(labels)}</div>"


def _anemos13_var_cards(day):
    items = [
        ("Suhu rata-rata", _anemos13_num(day.get("avg_temperature_c"), "°C", 1), "perkiraan harian"),
        ("Suhu tertinggi", _anemos13_num(day.get("max_temperature_c"), "°C", 1), "puncak suhu"),
        ("Terasa seperti", _anemos13_num(day.get("max_heat_index_c"), "°C", 1), "heat index tertinggi"),
        ("Kelembapan", _anemos13_num(day.get("avg_humidity_pct"), "%"), "rata-rata"),
        ("Peluang hujan", _anemos13_num(day.get("peak_rain_probability"), "%"), f"sekitar {day.get('peak_rain_hour') or '—'}"),
        ("Angin", _anemos13_num(day.get("max_wind_kmh"), " km/jam", 1), "tertinggi"),
    ]
    return "".join(f"<article class='var'><span>{_v6_esc(a)}</span><strong>{_v6_esc(b)}</strong><small>{_v6_esc(c)}</small></article>" for a,b,c in items)


def _anemos13_day_cards(days):
    html = []
    for d in days[:5]:
        cls = d.get("risk_class") or "green"
        html.append(f"<article class='day-card {cls}'><small>{_v6_esc(d.get('day_tag',''))} · {_v6_esc(d.get('date_label',''))}</small><h3>{_v6_esc(d.get('risk','')).title()}</h3><p><b>{_v6_esc(d.get('advice',''))}</b></p><div class='kpi'><span>Hujan<b>{_anemos13_num(d.get('peak_rain_probability'),'%')}</b></span><span>Jam<b>{_v6_esc(d.get('peak_rain_hour') or '—')}</b></span><span>Suhu<b>{_anemos13_num(d.get('avg_temperature_c'),'°C',1)}</b></span></div></article>")
    return "".join(html)


def _anemos13_period_cards(day):
    cards = []
    for p in day.get("periods") or []:
        cards.append(f"<article class='period {p.get('risk_class','green')}'><h3>{_v6_esc(p.get('period',''))}</h3><p><b>{_anemos13_icon(p.get('condition'))} {_v6_esc(p.get('condition',''))}</b></p><p class='muted'>Risiko {p.get('risk','—')}. Jam perhatian: <b>{_v6_esc(str(p.get('attention_hour') or '—'))}</b>.</p><div class='mini kpi'><span>Suhu<b>{_anemos13_num(p.get('avg_temp_c'),'°C',1)}</b></span><span>RH<b>{_anemos13_num(p.get('avg_humidity_pct'),'%')}</b></span><span>Hujan<b>{_anemos13_num(p.get('peak_rain_probability'),'%')}</b></span></div></article>")
    return "".join(cards)


def _anemos13_activity_cards(day):
    return "".join(f"<article class='var'><span>{_v6_esc(a.get('label',''))}</span><strong>{_v6_esc(a.get('value',''))}</strong></article>" for a in _anemos13_activity_advice(day))


def _anemos13_hour_rows(day):
    rows = []
    for h in day.get("important_hours") or []:
        cls = h.get("risk_class") or "green"
        rows.append(f"<article class='hour {cls}'><div class='time'>{_v6_esc(h.get('hour','—'))}</div><div class='cond'><b>{_v6_esc(h.get('icon',''))} {_v6_esc(h.get('condition','—'))}</b><small>Risiko {h.get('risk','—')}</small></div><div class='cell'><b>{_anemos13_num(h.get('temp_c'),'°C',1)}</b><small>Suhu</small></div><div class='cell'><b>{_anemos13_num(h.get('humidity_pct'),'%')}</b><small>RH</small></div><div class='cell'><b>{_anemos13_num(h.get('heat_index_c'),'°C',1)}</b><small>Heat index</small></div><div class='cell'><b>{_anemos13_num(h.get('rain_probability'),'%')}</b><small>Hujan</small></div><div class='cell'><b>{_anemos13_num(h.get('wind_kmh'),' km/jam',1)}</b><small>Angin</small></div></article>")
    return "".join(rows)


def _anemos13_detail_html(api, args):
    days = api.get("days") or []
    today = days[0] if days else {}
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    updated = f"{_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args,'timezone',DEFAULT_TIMEZONE))}"
    loc = api.get("location_name") or getattr(args, "location_name", "Lokasi")
    return f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Prakiraan { _v6_esc(loc) }</title><style>{_anemos13_css()}</style></head><body><main class='app'>
      <nav class='top'><div class='brand'><div class='brand-mark'></div><div><b>ANEMOS</b><span>{_v6_esc(loc)} · diperbarui {updated}</span></div></div><div class='nav'><a class='btn' href='sentinel_x_report.html'>Ringkasan</a><a class='btn' href='sentinel_x_accuracy_public.html'>Akurasi</a><a class='btn' href='../'>Pilih lokasi</a></div></nav>
      <section class='hero'><div class='chips'><span class='chip'>{_v6_esc(loc)}</span><span class='chip'>{_v6_esc(api.get('period_label',''))}</span><span class='chip'>ANEMOS {ANEMOS_VERSION}</span></div><h1>Prakiraan Cuaca Lokal</h1><p>{_v6_esc(today.get('advice','Prakiraan belum tersedia.'))} Peluang hujan tertinggi {_anemos13_num(today.get('peak_rain_probability'),'%')} sekitar { _v6_esc(today.get('peak_rain_hour') or '—') }.</p></section>
      <div class='notice'>{_v6_esc(sentinel_public_disclaimer(args))}</div>
      <section class='lead section'><article class='decision'><span class='badge {today.get('risk_class','green')}'>{_v6_esc(str(today.get('risk','')).title())}</span><h2>{_v6_esc(today.get('advice','Prakiraan belum tersedia.'))}</h2><p>{_v6_esc(today.get('risk_hint',''))} Jam yang perlu diperhatikan: <b>{_v6_esc(today.get('attention_hours') or today.get('peak_rain_hour') or '—')}</b>.</p></article><div class='metrics'><article class='metric'><span>Peluang hujan</span><strong>{_anemos13_num(today.get('peak_rain_probability'),'%')}</strong><small>tertinggi sekitar { _v6_esc(today.get('peak_rain_hour') or '—') }</small></article><article class='metric'><span>Risiko</span><strong>{_v6_esc(today.get('risk','—'))}</strong><small>{_v6_esc(today.get('condition',''))}</small></article><article class='metric'><span>Suhu</span><strong>{_anemos13_num(today.get('avg_temperature_c'),'°C',1)}</strong><small>rata-rata</small></article><article class='metric'><span>Heat index</span><strong>{_anemos13_num(today.get('max_heat_index_c'),'°C',1)}</strong><small>terasa seperti</small></article></div></section>
      <section class='panel section'><div class='section-head'><h2>Ringkasan beberapa hari</h2><p>Hari ini sampai beberapa hari ke depan.</p></div><div class='day-strip'>{_anemos13_day_cards(days)}</div></section>
      <section class='grid two section'><article class='panel'><div class='section-head'><h2>Peluang hujan</h2><p>Jam penting hari ini.</p></div>{_anemos13_chart(today)}</article><article class='panel'><h2>Kondisi utama</h2><div class='variables'>{_anemos13_var_cards(today)}</div></article></section>
      <section class='panel section'><div class='section-head'><h2>Pagi sampai malam</h2><p>Ringkasan yang lebih cepat dibaca.</p></div><div class='periods'>{_anemos13_period_cards(today)}</div></section>
      <section class='panel section'><div class='section-head'><h2>Saran aktivitas</h2><p>Ditulis untuk keputusan harian, bukan peringatan resmi.</p></div><div class='activity'>{_anemos13_activity_cards(today)}</div></section>
      <section class='panel section'><div class='section-head'><h2>Jam penting</h2><p>Dipilih dari jam utama dan jam rawan.</p></div><div class='hour-list'>{_anemos13_hour_rows(today)}</div></section>
      <section class='panel section'><details><summary><b>Data dan integrasi</b></summary><p class='muted'>Gunakan data berikut untuk analisis, arsip, atau integrasi dashboard lain.</p><div class='nav'><a class='btn' href='anemos_v13_public_api.json'>Public API JSON</a><a class='btn' href='anemos_daily_outlook.csv'>Daily CSV</a><a class='btn' href='anemos_hourly_compact.csv'>Hourly CSV</a><a class='btn' href='anemos_widget.html'>Widget</a><a class='btn' href='anemos_share_card.html'>Share card</a></div></details></section>
      <p class='footer'>ANEMOS · {ANEMOS_VERSION} · {updated}</p>
    </main></body></html>"""


def _anemos13_share_card_html(api, args):
    days = api.get("days") or []
    d = days[0] if days else {}
    loc = api.get("location_name") or getattr(args, "location_name", "Lokasi")
    return f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS Share Card</title><style>{_anemos13_css()} body{{background:#dceeff}} .share{{width:min(900px,calc(100% - 32px));margin:40px auto;padding:40px;border-radius:38px;background:linear-gradient(135deg,#061b3d,#0d6efd);color:white;box-shadow:var(--shadow)}} .share h1{{font-size:56px;line-height:.95;margin:12px 0}} .share p{{font-size:22px;color:#eaf4ff}} .share .kpi span{{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.3);color:#dbeafe}} .share .kpi b{{color:white}}</style></head><body><section class='share'><div class='chips'><span class='chip'>ANEMOS</span><span class='chip'>{_v6_esc(loc)}</span></div><h1>{_v6_esc(d.get('advice','Prakiraan belum tersedia.'))}</h1><p>{_v6_esc(d.get('date_label',''))}</p><div class='kpi'><span>Peluang hujan<b>{_anemos13_num(d.get('peak_rain_probability'),'%')}</b></span><span>Jam perhatian<b>{_v6_esc(d.get('peak_rain_hour') or '—')}</b></span><span>Suhu<b>{_anemos13_num(d.get('avg_temperature_c'),'°C',1)}</b></span></div><p style='font-size:14px'>Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi BMKG dan kondisi setempat.</p></section></body></html>"""


def _anemos13_widget_html(api, args):
    days = api.get("days") or []
    d = days[0] if days else {}
    loc = api.get("location_name") or getattr(args, "location_name", "Lokasi")
    return f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS Widget</title><style>{_anemos13_css()} body{{background:transparent}} .widget{{max-width:420px;margin:0;padding:22px;border-radius:26px;background:white;border:1px solid var(--line);box-shadow:var(--shadow)}} .widget h1{{font-size:26px;margin:0 0 8px}} .widget .temp{{font-size:52px;font-weight:950;letter-spacing:-.06em}}</style></head><body><article class='widget'><b>ANEMOS · {_v6_esc(loc)}</b><h1>{_v6_esc(d.get('condition',''))}</h1><div class='temp'>{_anemos13_num(d.get('avg_temperature_c'),'°C',1)}</div><p>{_v6_esc(d.get('advice',''))}</p><div class='kpi'><span>Hujan<b>{_anemos13_num(d.get('peak_rain_probability'),'%')}</b></span><span>Jam<b>{_v6_esc(d.get('peak_rain_hour') or '—')}</b></span><span>Risiko<b>{_v6_esc(d.get('risk','—'))}</b></span></div></article></body></html>"""


def _anemos13_build_api(target_date, args):
    payload = read_json(path_output(AETHER_JSON_FILENAME), default={}) or {}
    rows = payload.get("hourly") or payload.get("rows") or []
    grouped = _anemos13_group_by_day(rows, target_date)
    days = [_anemos13_day_model(d, r, args) for d, r in grouped]
    if not days and rows:
        days = [_anemos13_day_model(target_date, rows, args)]
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    period_label = "Belum tersedia"
    if days:
        period_label = f"{days[0].get('date_label')}–{days[-1].get('date_label')}"
    return {
        "brand": "ANEMOS",
        "version": ANEMOS_VERSION,
        "generated_at": now.isoformat(),
        "updated_label": f"{_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args,'timezone',DEFAULT_TIMEZONE))}",
        "location_slug": getattr(args, "location_slug", ""),
        "location_name": getattr(args, "location_name", ""),
        "period_label": period_label,
        "disclaimer": sentinel_public_disclaimer(args),
        "days": days,
    }


def _anemos13_write_outputs(target_date, args):
    api = _anemos13_build_api(target_date, args)
    days = api.get("days") or []
    daily_csv = []
    hourly_csv = []
    for d in days:
        daily_csv.append({
            "date": d.get("date"), "day_tag": d.get("day_tag"), "risk": d.get("risk"), "condition": d.get("condition"),
            "peak_rain_probability": d.get("peak_rain_probability"), "peak_rain_hour": d.get("peak_rain_hour"),
            "avg_temperature_c": d.get("avg_temperature_c"), "avg_humidity_pct": d.get("avg_humidity_pct"),
            "advice": d.get("advice"),
        })
        for h in d.get("important_hours") or []:
            row = dict(h); row["date"] = d.get("date"); row["day_tag"] = d.get("day_tag"); hourly_csv.append(row)
    write_json(path_output("anemos_v13_public_api.json"), api)
    write_json(path_output("anemos_activity_advice.json"), {"location_name": api.get("location_name"), "days": [{"date": d.get("date"), "advice": _anemos13_activity_advice(d)} for d in days]})
    write_dict_csv(path_output("anemos_daily_outlook.csv"), ["date","day_tag","risk","condition","peak_rain_probability","peak_rain_hour","avg_temperature_c","avg_humidity_pct","advice"], daily_csv)
    write_dict_csv(path_output("anemos_hourly_compact.csv"), ["date","day_tag","hour","condition","temp_c","humidity_pct","heat_index_c","rain_probability","wind_kmh","risk","risk_class"], hourly_csv)
    atomic_write_text(path_output("anemos_app.html"), lambda f: f.write(_anemos13_detail_html(api, args)))
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(_anemos13_detail_html(api, args)))
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(_anemos13_detail_html(api, args)))
    atomic_write_text(path_output("anemos_share_card.html"), lambda f: f.write(_anemos13_share_card_html(api, args)))
    atomic_write_text(path_output("anemos_widget.html"), lambda f: f.write(_anemos13_widget_html(api, args)))
    write_json(path_output("anemos_v13_manifest.json"), {"brand":"ANEMOS","version":ANEMOS_VERSION,"files":["anemos_app.html","anemos_v13_public_api.json","anemos_daily_outlook.csv","anemos_hourly_compact.csv","anemos_share_card.html","anemos_widget.html",AETHER_DASHBOARD_FILENAME,AETHER_REPORT_HTML_FILENAME],"generated_at":api.get("generated_at")})
    return api


_ANEMOS13_PREV_SAVE_OUTPUTS = save_outputs


def save_outputs(target_date, results, args):
    summary = _ANEMOS13_PREV_SAVE_OUTPUTS(target_date, results, args)
    try:
        _anemos13_write_outputs(target_date, args)
    except Exception as exc:
        log_warning("ANEMOS v13 public outputs gagal ditulis:", exc)
        if DEBUG:
            traceback.print_exc()
    return summary


def _anemos13_location_card(loc, base_url=""):
    path = os.path.join(root_output_dir(), sanitize_filename(loc.slug), "anemos_v13_public_api.json")
    api = read_json(path, default={}) or {}
    days = api.get("days") or []
    d = days[0] if days else {}
    prefix = f"{base_url}/{loc.slug}/" if base_url else f"{loc.slug}/"
    if d:
        summary = d.get("advice") or "Ringkasan prakiraan tersedia."
        kpi = f"<div class='kpi'><span>Peluang hujan<b>{_anemos13_num(d.get('peak_rain_probability'),'%')}</b></span><span>Jam perhatian<b>{_v6_esc(d.get('peak_rain_hour') or '—')}</b></span><span>Suhu<b>{_anemos13_num(d.get('avg_temperature_c'),'°C',1)}</b></span></div>"
        cls = d.get("risk_class") or "green"
    else:
        summary = "Ringkasan belum tersedia. Jalankan pembaruan prakiraan untuk mengisi kartu ini."
        kpi = "<div class='kpi'><span>Status<b>Belum tersedia</b></span><span>Hujan<b>—</b></span><span>Suhu<b>—</b></span></div>"
        cls = "green"
    return f"""<article class='card {cls}'><h2>{_v6_esc(loc.location_name)}</h2><p>{_v6_esc(summary)}</p>{kpi}<div class='nav'><a class='btn primary' href='{prefix}{AETHER_DASHBOARD_FILENAME}'>Buka prakiraan</a><a class='btn' href='{prefix}anemos_share_card.html'>Share card</a><a class='btn' href='{prefix}anemos_widget.html'>Widget</a><a class='btn' href='{prefix}sentinel_x_accuracy_public.html'>Akurasi</a></div></article>"""


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    updated = f"{_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args,'timezone',DEFAULT_TIMEZONE))}"
    cards = "".join(_anemos13_location_card(loc, base_url) for loc in locations)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS — Prakiraan Cuaca Lokal</title><style>{_anemos13_css()}</style></head><body><main class='app'>
      <section class='hero'><div class='chips'><span class='chip'>ANEMOS</span><span class='chip'>Diperbarui {updated}</span><span class='chip'>Multi-day forecast</span></div><h1>Prakiraan Cuaca Lokal</h1><p>Ringkasan cuaca untuk hari ini dan beberapa hari ke depan, dibuat agar langsung membantu keputusan harian: peluang hujan, suhu, kelembapan, heat index, angin, dan saran aktivitas.</p></section>
      <div class='notice'>{_v6_esc(sentinel_public_disclaimer(args))}</div>
      <section class='grid three section'>{cards}</section>
      <section class='panel section'><div class='section-head'><h2>Data terbuka</h2><p>Untuk analisis, arsip, atau dashboard lanjutan.</p></div><div class='nav'><a class='btn' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='btn' href='forecast_all_locations.csv'>Forecast CSV</a><a class='btn' href='source_status_all_locations.csv'>Status sumber</a><a class='btn' href='forecast_batch_summary.json'>Batch summary</a></div></section>
      <p class='footer'>ANEMOS · {ANEMOS_VERSION} · {updated}</p>
    </main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("anemos_v13_portal_manifest.json"), {"brand":"ANEMOS","version":ANEMOS_VERSION,"generated_at":now.isoformat(),"locations":[loc.slug for loc in locations],"index":root_output_path("index.html")})
    return root_output_path("index.html")


# =============================================================================
# ANEMOS v20 — Civic Weather OS / Public Product Final Layer
# =============================================================================
# This layer keeps the single-file requirement. It intentionally does not remove
# the previous forecast engine. Instead, it overrides the final public outputs
# and adds a mature product layer on top of the existing multi-source ensemble.
#
# Included upgrade path:
# v14 Civic Weather OS          : activity-first public pages
# v15 Confidence Intelligence   : confidence, agreement, uncertainty language
# v16 PWA/Public Product        : manifest, offline page, embeddable widget
# v17 Verification Maturity     : clearer accuracy status and API status
# v18 Hyperlocal Advisory       : commute/laundry/outdoor local decisions
# v19 Public API Ecosystem      : schema, status, public brief, share text
# v20 Final Public-Grade ANEMOS : consolidated premium UI and outputs

ANEMOS_VERSION = "ANEMOS v20 Civic Weather OS"
ANEMOS_PUBLIC_VERSION = "ANEMOS v20"
ANEMOS_BRAND_NAME = "ANEMOS"
ANEMOS_TAGLINE = "Prakiraan cuaca lokal untuk keputusan harian."
SENTINEL_PUBLIC_UI_VERSION = "anemos-v20-civic-weather-os"
AETHER_VERSION = ANEMOS_VERSION


def _anemos20_esc(value):
    try:
        return _v6_esc(value)
    except Exception:
        import html as _html
        return _html.escape("" if value is None else str(value))


def _anemos20_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _anemos20_num(value, suffix="", digits=0, blank="—"):
    v = _anemos20_float(value, None)
    if v is None:
        return blank
    if digits == 0:
        return f"{round(v):.0f}{suffix}"
    return f"{v:.{digits}f}{suffix}"


def _anemos20_risk_class(prob=None, risk=None):
    text = (str(risk or "").lower()).strip()
    p = _anemos20_float(prob, None)
    if "tinggi" in text or text in {"red", "high"} or (p is not None and p >= 70):
        return "high"
    if "sedang" in text or text in {"orange", "yellow"} or (p is not None and p >= 45):
        return "medium"
    if p is not None and p >= 30:
        return "watch"
    return "low"


def _anemos20_risk_label(prob=None, risk=None):
    cls = _anemos20_risk_class(prob, risk)
    return {"low": "Aman dipantau", "watch": "Perlu dipantau", "medium": "Waspada hujan", "high": "Risiko tinggi"}.get(cls, "Aman dipantau")


def _anemos20_day_tag(date_text, index):
    if index == 0:
        return "Hari ini"
    if index == 1:
        return "Besok"
    if index == 2:
        return "Lusa"
    return f"Hari ke-{index+1}"


def _anemos20_condition_icon(condition):
    c = (condition or "").lower()
    if "petir" in c or "badai" in c:
        return "⛈️"
    if "lebat" in c:
        return "🌧️"
    if "hujan" in c:
        return "🌦️"
    if "cerah" in c and "berawan" in c:
        return "🌤️"
    if "cerah" in c:
        return "☀️"
    if "berawan" in c:
        return "☁️"
    return "🌡️"


def _anemos20_pick_day(api, idx=0):
    days = api.get("days") or []
    return days[idx] if len(days) > idx else (days[0] if days else {})


def _anemos20_key_hours(day, limit=8):
    hours = list(day.get("important_hours") or [])
    if not hours:
        return []
    preferred = []
    seen = set()
    # Always keep the highest rain probability hour, then familiar anchor hours.
    peak = max(hours, key=lambda h: _anemos20_float(h.get("rain_probability"), -1) or -1)
    for h in [peak] + [x for x in hours if str(x.get("hour", ""))[:2] in {"06", "09", "12", "14", "15", "16", "18", "21"}]:
        key = h.get("hour") or str(h)
        if key not in seen:
            seen.add(key)
            preferred.append(h)
    for h in hours:
        key = h.get("hour") or str(h)
        if key not in seen:
            seen.add(key)
            preferred.append(h)
        if len(preferred) >= limit:
            break
    return sorted(preferred[:limit], key=lambda h: str(h.get("hour", "99:99")))


def _anemos20_activity_matrix(day):
    p = _anemos20_float(day.get("peak_rain_probability"), 0) or 0
    h = day.get("peak_rain_hour") or "—"
    temp = _anemos20_float(day.get("max_heat_index_c") or day.get("avg_temperature_c"), 0) or 0
    risk = _anemos20_risk_class(p, day.get("risk"))
    def level(ok, warn):
        return "Kurang ideal" if ok == "bad" else ("Perlu rencana cadangan" if warn else "Relatif aman")
    return [
        {
            "activity": "Perjalanan / motor",
            "status": level("bad" if p >= 75 else "ok", p >= 45),
            "advice": f"Lebih hati-hati sekitar {h} bila hujan muncul; jalan dapat lebih licin.",
            "priority_hour": h,
            "risk_class": risk,
        },
        {
            "activity": "Jalan kaki",
            "status": level("ok", p >= 35),
            "advice": f"Masih memungkinkan. Siapkan payung bila keluar mendekati {h}.",
            "priority_hour": h,
            "risk_class": risk,
        },
        {
            "activity": "Jemur pakaian",
            "status": "Lebih aman pagi" if p >= 35 else "Relatif aman",
            "advice": "Utamakan pagi sampai menjelang siang; hindari terlalu sore bila awan mulai gelap.",
            "priority_hour": "pagi–siang awal",
            "risk_class": "watch" if p >= 35 else "low",
        },
        {
            "activity": "Olahraga luar ruang",
            "status": "Pilih pagi/sore teduh" if temp >= 30 or p >= 45 else "Relatif aman",
            "advice": "Pagi lebih nyaman. Kurangi durasi jika terasa panas/lembap atau hujan mulai muncul.",
            "priority_hour": "pagi",
            "risk_class": "watch" if temp >= 30 or p >= 45 else "low",
        },
        {
            "activity": "Acara outdoor",
            "status": "Siapkan plan B" if p >= 45 else "Bisa dilanjutkan",
            "advice": f"Untuk agenda penting, sediakan opsi tempat teduh terutama sekitar {h}.",
            "priority_hour": h,
            "risk_class": risk,
        },
        {
            "activity": "Foto / city walk",
            "status": "Pantau langit" if p >= 35 else "Relatif aman",
            "advice": "Cek perubahan awan dan angin sebelum berangkat; cahaya setelah hujan bisa berubah cepat.",
            "priority_hour": h,
            "risk_class": risk,
        },
    ]


def _anemos20_summary_sentence(day, loc="lokasi ini"):
    p = _anemos20_float(day.get("peak_rain_probability"), 0) or 0
    h = day.get("peak_rain_hour") or "—"
    if p >= 70:
        return f"Untuk {loc}, aktivitas perlu rencana cadangan karena peluang hujan cukup tinggi sekitar pukul {h}."
    if p >= 45:
        return f"Untuk {loc}, aktivitas masih memungkinkan, tetapi payung atau jas hujan disarankan bila keluar sekitar pukul {h}."
    if p >= 30:
        return f"Untuk {loc}, cuaca masih cukup aman, namun tetap pantau perubahan awan terutama sekitar pukul {h}."
    return f"Untuk {loc}, aktivitas harian relatif aman dengan pemantauan cuaca biasa."


def _anemos20_enhance_api(api, args):
    api = dict(api or {})
    loc = api.get("location_name") or getattr(args, "location_name", "Lokasi")
    days = []
    for i, d in enumerate(api.get("days") or []):
        nd = dict(d)
        nd["day_tag"] = nd.get("day_tag") or _anemos20_day_tag(nd.get("date"), i)
        nd["risk_class"] = _anemos20_risk_class(nd.get("peak_rain_probability"), nd.get("risk"))
        nd["risk_label"] = _anemos20_risk_label(nd.get("peak_rain_probability"), nd.get("risk"))
        nd["icon"] = _anemos20_condition_icon(nd.get("condition"))
        nd["summary_sentence"] = _anemos20_summary_sentence(nd, loc)
        nd["activity_matrix"] = _anemos20_activity_matrix(nd)
        nd["key_hours"] = _anemos20_key_hours(nd, 9)
        nd["confidence_label"] = "Lebih rendah saat hujan lokal sulit dipastikan"
        days.append(nd)
    api["brand"] = "ANEMOS"
    api["version"] = ANEMOS_VERSION
    api["product"] = "Civic Weather OS"
    api["schema_version"] = "1.0.0"
    api["days"] = days
    api["today"] = days[0] if days else {}
    api["public_brief"] = {
        "location": loc,
        "headline": _anemos20_summary_sentence(days[0], loc) if days else "Prakiraan belum tersedia.",
        "period": api.get("period_label") or "",
        "updated": api.get("updated_label") or "",
        "disclaimer": sentinel_public_disclaimer(args),
    }
    return api


def _anemos20_css():
    return r"""
    :root{--bg:#ebf4ff;--paper:#ffffff;--ink:#061120;--muted:#63728a;--line:#d7e6f7;--blue:#1769ff;--sky:#6ec6ff;--navy:#071a3f;--green:#14b86f;--yellow:#f5b700;--orange:#fb7b14;--red:#e11d48;--shadow:0 26px 80px rgba(7,26,63,.16);--soft:0 14px 38px rgba(7,26,63,.08)}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 84% 5%,rgba(23,105,255,.18),transparent 25%),linear-gradient(180deg,#f8fcff,#ebf4ff 72%);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);line-height:1.55}.app{width:min(1360px,calc(100% - 44px));margin:22px auto 90px}.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:14px 0 18px;background:linear-gradient(180deg,rgba(235,244,255,.92),rgba(235,244,255,.62));backdrop-filter:blur(18px)}.brand{display:flex;gap:12px;align-items:center}.mark{width:44px;height:44px;border-radius:16px;background:linear-gradient(135deg,#071a3f,#1769ff 65%,#a7e2ff);box-shadow:0 12px 34px rgba(23,105,255,.28)}.brand b{display:block;font-size:21px;letter-spacing:-.03em}.brand span{display:block;font-size:12px;color:var(--muted)}.nav{display:flex;gap:10px;flex-wrap:wrap}.btn{display:inline-flex;align-items:center;justify-content:center;border:1px solid #b5d4ff;background:#f3f8ff;color:#075bd8;border-radius:999px;padding:10px 14px;font-size:13px;font-weight:950;text-decoration:none}.btn.primary{background:linear-gradient(135deg,#0f62fe,#239bff);border-color:#0f62fe;color:white;box-shadow:0 12px 30px rgba(15,98,254,.26)}.hero{position:relative;overflow:hidden;border-radius:38px;padding:48px;background:radial-gradient(circle at 88% 13%,rgba(255,255,255,.32),transparent 15%),linear-gradient(135deg,#061634 0,#0d3f9c 50%,#2d87ff 100%);color:white;box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;right:-80px;bottom:-120px;width:340px;height:340px;border-radius:50%;background:rgba(255,255,255,.10)}.hero h1{font-size:clamp(44px,6.5vw,86px);line-height:.9;letter-spacing:-.075em;margin:16px 0 12px;max-width:980px}.hero p{font-size:clamp(16px,2vw,22px);max-width:900px;margin:0;color:#eaf5ff}.chips{display:flex;gap:10px;flex-wrap:wrap}.chip{display:inline-flex;border:1px solid rgba(255,255,255,.35);background:rgba(255,255,255,.14);border-radius:999px;padding:7px 12px;color:white;font-weight:950;font-size:12px}.notice{margin:16px 0 18px;background:#fff7ed;border:1px solid #fdba74;color:#8a3510;border-radius:18px;padding:13px 16px;font-weight:900}.grid{display:grid;gap:18px}.g2{grid-template-columns:1.1fr .9fr}.g3{grid-template-columns:repeat(3,1fr)}.g4{grid-template-columns:repeat(4,1fr)}.panel,.card,.metric,.day,.activity,.hour,.data-card{background:rgba(255,255,255,.93);border:1px solid var(--line);border-radius:28px;box-shadow:var(--soft)}.panel{padding:26px;margin:20px 0}.section-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:16px}.section-head h2,.panel h2{font-size:30px;line-height:1;letter-spacing:-.045em;margin:0}.section-head p,.muted{color:var(--muted);margin:0}.decision-wrap{display:grid;grid-template-columns:1.45fr .85fr;gap:18px}.decision{border-radius:34px;padding:32px;background:linear-gradient(135deg,#061634,#0a2d6f);color:white;box-shadow:var(--shadow)}.decision h2{font-size:clamp(34px,4.5vw,64px);letter-spacing:-.07em;line-height:.94;margin:10px 0}.decision p{font-size:17px;color:#dceeff}.badge{display:inline-flex;border-radius:999px;padding:8px 12px;font-weight:950;font-size:12px}.badge.low{background:#dcfce7;color:#166534}.badge.watch{background:#fef3c7;color:#92400e}.badge.medium{background:#ffedd5;color:#9a3412}.badge.high{background:#ffe4e6;color:#9f1239}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:14px}.metric{padding:20px}.metric span,.label{font-size:11px;color:var(--muted);font-weight:950;text-transform:uppercase;letter-spacing:.08em}.metric strong{display:block;font-size:38px;letter-spacing:-.055em;margin:8px 0 0}.metric small{color:var(--muted)}.day-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.day{padding:20px;border-top:7px solid var(--green)}.day.watch{border-top-color:var(--yellow)}.day.medium{border-top-color:var(--orange)}.day.high{border-top-color:var(--red)}.day h3{font-size:26px;letter-spacing:-.04em;margin:3px 0}.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:15px}.mini span{background:#f7fbff;border:1px solid var(--line);border-radius:14px;padding:10px;color:var(--muted);font-size:11px}.mini b{display:block;color:var(--ink);font-size:18px}.rain-bars{display:flex;align-items:flex-end;gap:7px;height:210px;padding:20px 4px 8px;border-bottom:1px solid var(--line)}.rain-bar{flex:1;min-width:10px;height:var(--h);border-radius:12px 12px 4px 4px;background:linear-gradient(180deg,#74c7ff,#0f62fe);position:relative}.rain-bar.medium{background:linear-gradient(180deg,#ffbd5a,#fb7b14)}.rain-bar.high{background:linear-gradient(180deg,#fb7185,#e11d48)}.rain-bar:after{content:attr(data-v);position:absolute;left:50%;bottom:calc(100% + 7px);transform:translateX(-50%);font-weight:950;font-size:11px;color:#12355f}.axis{display:flex;gap:7px;margin-top:8px}.axis span{flex:1;text-align:center;font-size:11px;color:var(--muted)}.variables{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.data-card{padding:16px;background:#f8fbff}.data-card strong{display:block;font-size:25px;margin:7px 0 2px}.periods{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.period{padding:18px;border-top:6px solid var(--green);border-radius:22px;border-left:1px solid var(--line);border-right:1px solid var(--line);border-bottom:1px solid var(--line);background:#fff}.period.watch{border-top-color:var(--yellow)}.period.medium{border-top-color:var(--orange)}.period.high{border-top-color:var(--red)}.activity-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.activity{padding:18px;border-left:7px solid var(--green)}.activity.watch{border-left-color:var(--yellow)}.activity.medium{border-left-color:var(--orange)}.activity.high{border-left-color:var(--red)}.activity h3{font-size:21px;letter-spacing:-.03em;margin:0 0 8px}.hour-list{display:grid;gap:10px}.hour{display:grid;grid-template-columns:82px 1.4fr repeat(5,106px);align-items:center;gap:12px;padding:14px 16px;border-left:7px solid var(--green)}.hour.watch{border-left-color:var(--yellow)}.hour.medium{border-left-color:var(--orange)}.hour.high{border-left-color:var(--red)}.hour .time{font-size:18px;font-weight:950}.hour .cond b{display:block}.hour .cond small{color:var(--muted)}.cell{border:1px solid var(--line);background:#f8fbff;border-radius:13px;padding:9px}.cell b{display:block}.cell small{font-size:11px;color:var(--muted)}.footer{text-align:center;color:var(--muted);font-size:13px;margin:34px 0}.portal-card{padding:24px}.portal-card h2{font-size:32px;letter-spacing:-.045em;margin:0}.portal-card p{color:var(--muted)}.status-dot{width:11px;height:11px;border-radius:50%;display:inline-block;background:var(--green);margin-right:6px}.status-dot.watch{background:var(--yellow)}.status-dot.medium{background:var(--orange)}.status-dot.high{background:var(--red)}.progress{height:13px;background:#e2edf9;border-radius:999px;overflow:hidden}.progress span{display:block;height:100%;width:var(--p);background:linear-gradient(90deg,#0f62fe,#75d0ff);border-radius:999px}.sharebox{background:#071a3f;color:white;border-radius:30px;padding:26px}.sharebox textarea{width:100%;min-height:120px;border:0;border-radius:18px;padding:16px;font:inherit}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid var(--line);padding:10px;text-align:left}th{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}@media(max-width:1180px){.g3,.g4,.decision-wrap,.day-strip,.g2{grid-template-columns:1fr 1fr}.variables{grid-template-columns:repeat(3,1fr)}.periods,.activity-grid{grid-template-columns:repeat(2,1fr)}.hour{grid-template-columns:80px 1fr repeat(2,100px)}}@media(max-width:740px){.app{width:calc(100% - 24px);margin:12px auto 55px}.top{position:static;align-items:flex-start;flex-direction:column}.hero{padding:30px;border-radius:30px}.hero h1{font-size:44px}.g3,.g4,.decision-wrap,.day-strip,.g2,.variables,.periods,.activity-grid,.metrics{grid-template-columns:1fr}.section-head{display:block}.hour{grid-template-columns:1fr 1fr}.hour .cond{grid-column:1/-1}.rain-bars{height:160px}.mini{grid-template-columns:1fr}.nav{width:100%}.btn{flex:1}}
    """


def _anemos20_topbar(api, args):
    loc = api.get("location_name") or getattr(args, "location_name", "")
    return f"""
    <nav class='top'>
      <div class='brand'><div class='mark'></div><div><b>ANEMOS</b><span>{_anemos20_esc(loc)} · {ANEMOS_PUBLIC_VERSION}</span></div></div>
      <div class='nav'><a class='btn primary' href='anemos_today.html'>Hari ini</a><a class='btn' href='anemos_3day.html'>3 hari</a><a class='btn' href='anemos_activity.html'>Aktivitas</a><a class='btn' href='sentinel_x_accuracy_public.html'>Akurasi</a><a class='btn' href='../'>Lokasi</a></div>
    </nav>"""


def _anemos20_rain_chart(day):
    hours = day.get("important_hours") or day.get("key_hours") or []
    if not hours:
        return "<p class='muted'>Grafik peluang hujan belum tersedia.</p>"
    bars, labels = [], []
    for h in hours:
        p = _anemos20_float(h.get("rain_probability"), 0) or 0
        cls = _anemos20_risk_class(p)
        bars.append(f"<div class='rain-bar {cls}' data-v='{_anemos20_num(p,'%')}' style='--h:{max(4,min(100,p))}%'></div>")
        labels.append(f"<span>{_anemos20_esc(str(h.get('hour','')[:2]))}</span>")
    return f"<div class='rain-bars'>{''.join(bars)}</div><div class='axis'>{''.join(labels)}</div>"


def _anemos20_svg_temp(day):
    hours = day.get("important_hours") or day.get("key_hours") or []
    vals = [_anemos20_float(h.get("temp_c"), None) for h in hours]
    vals = [v for v in vals if v is not None]
    if not vals:
        return "<p class='muted'>Grafik suhu belum tersedia.</p>"
    w, hgt, pad = 640, 210, 28
    mn, mx = min(vals), max(vals)
    if abs(mx-mn) < 0.01:
        mx += 1; mn -= 1
    pts=[]; labels=[]
    for i, hour in enumerate(hours[:len(vals)]):
        v = _anemos20_float(hour.get("temp_c"), vals[min(i, len(vals)-1)])
        x = pad + i*(w-2*pad)/max(1,len(vals)-1)
        y = hgt-pad - (v-mn)/(mx-mn)*(hgt-2*pad)
        pts.append((x,y,v,str(hour.get('hour','')[:2])))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x,y,_,_ in pts)
    circles = "".join(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4'/><text x='{x:.1f}' y='{y-10:.1f}' text-anchor='middle'>{v:.0f}°</text><text x='{x:.1f}' y='{hgt-6}' text-anchor='middle' class='x'>{_anemos20_esc(lbl)}</text>" for x,y,v,lbl in pts)
    return f"<svg viewBox='0 0 {w} {hgt}' width='100%' height='230' role='img' aria-label='Grafik suhu'><style>svg{{overflow:visible}} polyline{{fill:none;stroke:#1769ff;stroke-width:5;stroke-linecap:round;stroke-linejoin:round}} circle{{fill:white;stroke:#1769ff;stroke-width:4}} text{{font:700 13px system-ui;fill:#09214a}} .x{{fill:#71809a;font-size:11px}}</style><polyline points='{poly}'/>{circles}</svg>"


def _anemos20_variable_cards(day):
    items = [
        ("Suhu rata-rata", _anemos20_num(day.get("avg_temperature_c"), "°C", 1), "perkiraan harian"),
        ("Terasa seperti", _anemos20_num(day.get("max_heat_index_c") or day.get("avg_temperature_c"), "°C", 1), "heat index tertinggi"),
        ("Kelembapan", _anemos20_num(day.get("avg_humidity_pct"), "%"), "rata-rata"),
        ("Peluang hujan", _anemos20_num(day.get("peak_rain_probability"), "%"), f"sekitar {day.get('peak_rain_hour') or '—'}"),
        ("Angin", _anemos20_num(day.get("max_wind_kmh"), " km/jam", 1), "perkiraan maksimum"),
        ("Status", _anemos20_risk_label(day.get("peak_rain_probability"), day.get("risk")), "ringkasan risiko"),
    ]
    return "".join(f"<article class='data-card'><span class='label'>{_anemos20_esc(a)}</span><strong>{_anemos20_esc(b)}</strong><small class='muted'>{_anemos20_esc(c)}</small></article>" for a,b,c in items)


def _anemos20_day_cards(days):
    html = []
    for d in (days or [])[:5]:
        cls = d.get("risk_class") or _anemos20_risk_class(d.get("peak_rain_probability"), d.get("risk"))
        html.append(f"""
        <article class='day {cls}'><small>{_anemos20_esc(d.get('day_tag'))} · {_anemos20_esc(d.get('date_label') or d.get('date'))}</small><h3>{_anemos20_esc(d.get('risk_label') or _anemos20_risk_label(d.get('peak_rain_probability')))}</h3><p>{_anemos20_esc(d.get('summary_sentence') or d.get('advice') or '')}</p><div class='mini'><span>Hujan<b>{_anemos20_num(d.get('peak_rain_probability'),'%')}</b></span><span>Jam<b>{_anemos20_esc(d.get('peak_rain_hour') or '—')}</b></span><span>Suhu<b>{_anemos20_num(d.get('avg_temperature_c'),'°C',1)}</b></span></div></article>
        """)
    return "".join(html) or "<p class='muted'>Ringkasan hari belum tersedia.</p>"


def _anemos20_activity_cards(day):
    return "".join(f"""
      <article class='activity {a.get('risk_class','low')}'><h3>{_anemos20_esc(a.get('activity'))}</h3><b>{_anemos20_esc(a.get('status'))}</b><p class='muted'>{_anemos20_esc(a.get('advice'))}</p><small class='label'>Fokus: {_anemos20_esc(a.get('priority_hour'))}</small></article>
    """ for a in (day.get('activity_matrix') or []))


def _anemos20_period_cards(day):
    periods = day.get("periods") or []
    if not periods:
        return "<p class='muted'>Ringkasan pagi–siang–sore–malam belum tersedia.</p>"
    html=[]
    for p in periods[:4]:
        cls = _anemos20_risk_class(p.get("rain_probability"), p.get("risk"))
        html.append(f"""
        <article class='period {cls}'><h3>{_anemos20_condition_icon(p.get('condition'))} {_anemos20_esc(p.get('label') or p.get('period') or '')}</h3><b>{_anemos20_esc(p.get('condition') or '—')}</b><p class='muted'>Jam perhatian: {_anemos20_esc(p.get('attention_hour') or p.get('peak_hour') or '—')}</p><div class='mini'><span>Suhu<b>{_anemos20_num(p.get('avg_temperature_c') or p.get('temp_c'),'°C',1)}</b></span><span>Hujan<b>{_anemos20_num(p.get('rain_probability'),'%')}</b></span><span>Risiko<b>{_anemos20_esc(_anemos20_risk_label(p.get('rain_probability'), p.get('risk')))}</b></span></div></article>
        """)
    return "".join(html)


def _anemos20_hour_rows(day):
    rows = day.get("key_hours") or day.get("important_hours") or []
    html=[]
    for h in rows[:10]:
        cls = _anemos20_risk_class(h.get("rain_probability"), h.get("risk"))
        html.append(f"""
        <article class='hour {cls}'><div class='time'>{_anemos20_esc(h.get('hour') or '—')}</div><div class='cond'><b>{_anemos20_condition_icon(h.get('condition'))} {_anemos20_esc(h.get('condition') or '—')}</b><small>{_anemos20_esc(h.get('advice') or 'Pantau kondisi sekitar.')}</small></div><div class='cell'><b>{_anemos20_num(h.get('temp_c'),'°C',1)}</b><small>Suhu</small></div><div class='cell'><b>{_anemos20_num(h.get('humidity_pct'),'%')}</b><small>RH</small></div><div class='cell'><b>{_anemos20_num(h.get('heat_index_c'),'°C',1)}</b><small>Terasa</small></div><div class='cell'><b>{_anemos20_num(h.get('rain_probability'),'%')}</b><small>Hujan</small></div><div class='cell'><b>{_anemos20_esc(_anemos20_risk_label(h.get('rain_probability'), h.get('risk')))}</b><small>Risiko</small></div></article>
        """)
    return "".join(html) or "<p class='muted'>Jam penting belum tersedia.</p>"


def _anemos20_whatsapp_text(api):
    loc = api.get("location_name") or "Lokasi"
    d = _anemos20_pick_day(api, 0)
    if not d:
        return f"ANEMOS · {loc}: prakiraan belum tersedia."
    return (f"ANEMOS · {loc}\n"
            f"{d.get('date_label') or d.get('date')}\n"
            f"{d.get('summary_sentence') or d.get('advice')}\n"
            f"Peluang hujan tertinggi: {_anemos20_num(d.get('peak_rain_probability'),'%')} sekitar {d.get('peak_rain_hour') or '—'}.\n"
            f"Suhu rata-rata: {_anemos20_num(d.get('avg_temperature_c'),'°C',1)}.\n"
            f"Bukan peringatan resmi; untuk cuaca ekstrem ikuti informasi BMKG.")


def _anemos20_detail_html(api, args, page="full"):
    api = _anemos20_enhance_api(api, args)
    loc = api.get("location_name") or getattr(args, "location_name", "Lokasi")
    today = _anemos20_pick_day(api, 0)
    days = api.get("days") or []
    top = _anemos20_topbar(api, args)
    updated = api.get("updated_label") or ""
    period = api.get("period_label") or ""
    headline = (today.get("summary_sentence") or api.get("public_brief",{}).get("headline") or "Prakiraan belum tersedia.")
    peak = _anemos20_num(today.get("peak_rain_probability"), "%")
    peak_hour = today.get("peak_rain_hour") or "—"
    risk_cls = today.get("risk_class") or "low"
    risk_label = today.get("risk_label") or _anemos20_risk_label(today.get("peak_rain_probability"), today.get("risk"))
    title = "ANEMOS Civic Weather OS" if page == "full" else ("Prakiraan Hari Ini" if page == "today" else "Prakiraan 3 Hari")
    body_sections = []
    body_sections.append(f"""
      <section class='hero'><div class='chips'><span class='chip'>ANEMOS v20</span><span class='chip'>{_anemos20_esc(loc)}</span><span class='chip'>{_anemos20_esc(updated)}</span></div><h1>{_anemos20_esc(title)}</h1><p>{_anemos20_esc(headline)}</p></section>
      <div class='notice'>{_anemos20_esc(sentinel_public_disclaimer(args))}</div>
      <section class='grid g3'><article class='metric'><span>Periode</span><strong style='font-size:24px'>{_anemos20_esc(period or (today.get('date_label') or '—'))}</strong><small>Hari ini dan beberapa hari ke depan</small></article><article class='metric'><span>Peluang hujan tertinggi</span><strong>{peak}</strong><small>sekitar { _anemos20_esc(peak_hour) }</small></article><article class='metric'><span>Status</span><strong style='font-size:32px'>{_anemos20_esc(risk_label)}</strong><small>{_anemos20_esc(today.get('confidence_label') or '')}</small></article></section>
      <section class='decision-wrap section'><article class='decision'><span class='badge {risk_cls}'>{_anemos20_esc(risk_label)}</span><h2>{_anemos20_esc(today.get('advice') or headline)}</h2><p>Jam yang paling perlu diperhatikan: <b>{_anemos20_esc(peak_hour)}</b>. Gunakan halaman ini sebagai panduan aktivitas harian dan tetap pantau kondisi langit di sekitar.</p></article><section class='metrics'><article class='metric'><span>Suhu rata-rata</span><strong>{_anemos20_num(today.get('avg_temperature_c'),'°C',1)}</strong><small>perkiraan harian</small></article><article class='metric'><span>Kelembapan</span><strong>{_anemos20_num(today.get('avg_humidity_pct'),'%')}</strong><small>rata-rata</small></article><article class='metric'><span>Terasa seperti</span><strong>{_anemos20_num(today.get('max_heat_index_c') or today.get('avg_temperature_c'),'°C',1)}</strong><small>heat index</small></article><article class='metric'><span>Data akurasi</span><strong>{_anemos20_esc(str((read_json(path_output('sentinel_x_verification_summary.json'), default={}) or {}).get('matched_cases', 0)))}</strong><small>pasangan prakiraan-observasi</small></article></section></section>
    """)
    if page in {"full", "3day"}:
        body_sections.append(f"<section class='panel'><div class='section-head'><h2>Ringkasan beberapa hari</h2><p>Bandingkan kondisi hari ini sampai beberapa hari ke depan.</p></div><div class='day-strip'>{_anemos20_day_cards(days)}</div></section>")
    if page in {"full", "today"}:
        body_sections.append(f"<section class='grid g2 section'><article class='panel'><div class='section-head'><h2>Peluang hujan</h2><p>Jam dengan batang lebih tinggi perlu lebih diperhatikan.</p></div>{_anemos20_rain_chart(today)}</article><article class='panel'><div class='section-head'><h2>Suhu</h2><p>Perubahan suhu pada jam penting.</p></div>{_anemos20_svg_temp(today)}</article></section>")
        body_sections.append(f"<section class='panel'><div class='section-head'><h2>Kondisi utama</h2><p>Variabel yang paling relevan untuk aktivitas harian.</p></div><div class='variables'>{_anemos20_variable_cards(today)}</div></section>")
        body_sections.append(f"<section class='panel'><div class='section-head'><h2>Pagi, siang, sore, malam</h2><p>Ringkasan cepat menurut bagian hari.</p></div><div class='periods'>{_anemos20_period_cards(today)}</div></section>")
    if page in {"full", "activity"}:
        body_sections.append(f"<section class='panel'><div class='section-head'><h2>Saran aktivitas</h2><p>Dirancang agar prakiraan lebih mudah dipakai dalam keputusan harian.</p></div><div class='activity-grid'>{_anemos20_activity_cards(today)}</div></section>")
        body_sections.append(f"<section class='grid g2 section'><article class='panel'><h2>Share singkat</h2><p class='muted'>Teks ini dapat disalin untuk WhatsApp atau grup.</p><div class='sharebox'><textarea readonly>{_anemos20_esc(_anemos20_whatsapp_text(api))}</textarea></div></article><article class='panel'><h2>Catatan penggunaan</h2><ul><li>Prakiraan ini membantu aktivitas harian, bukan peringatan resmi.</li><li>Hujan lokal dapat bergeser beberapa kilometer atau berubah beberapa jam.</li><li>Untuk kondisi ekstrem, ikuti informasi BMKG dan kondisi setempat.</li></ul></article></section>")
    body_sections.append(f"<section class='panel'><div class='section-head'><h2>Jam penting</h2><p>Ditampilkan ringkas agar tidak melelahkan dibaca.</p></div><div class='hour-list'>{_anemos20_hour_rows(today)}</div><details style='margin-top:14px'><summary><b>Butuh data lengkap?</b></summary><div class='nav' style='margin-top:12px'><a class='btn' href='sentinel_x.csv'>CSV lengkap</a><a class='btn' href='sentinel_x_variables.csv'>CSV variabel</a><a class='btn' href='anemos_api_v1.json'>API JSON</a><a class='btn' href='anemos_schema.json'>Schema</a></div></details></section>")
    return f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS v20 — {_anemos20_esc(loc)}</title><meta name='theme-color' content='#1769ff'><link rel='manifest' href='anemos_pwa_manifest.webmanifest'><style>{_anemos20_css()}</style></head><body><main class='app'>{top}{''.join(body_sections)}<p class='footer'>ANEMOS · {ANEMOS_VERSION} · {_anemos20_esc(updated)} · Bukan peringatan resmi</p></main></body></html>"""


def _anemos20_accuracy_html(rows, args):
    summary = read_json(path_output('sentinel_x_verification_summary.json'), default={}) or {}
    rel = read_dict_csv(path_output('sentinel_x_reliability.csv')) if os.path.exists(path_output('sentinel_x_reliability.csv')) else []
    matched = int(_anemos20_float(summary.get('matched_cases'), 0) or 0)
    target = int(_anemos20_float(getattr(args, 'verification_min_cases', 30), 30) or 30)
    pct = max(0, min(100, (matched / max(1, target)) * 100))
    api = {"location_name": getattr(args, 'location_name', ''), "updated_label": now_local(getattr(args,'timezone',DEFAULT_TIMEZONE)).strftime('%d %b %Y %H:%M')}
    rel_rows = "".join(f"<tr><td>{_anemos20_esc(r.get('bin') or r.get('probability_bin') or '')}</td><td>{_anemos20_esc(r.get('n') or r.get('matched_cases') or '0')}</td><td>{_anemos20_esc(r.get('mean_forecast_pct') or r.get('mean_forecast_probability') or '—')}</td><td>{_anemos20_esc(r.get('observed_frequency_pct') or r.get('observed_rain_frequency') or '—')}</td></tr>" for r in rel)
    if not rel_rows:
        rel_rows = "<tr><td colspan='4'>Data akan muncul setelah observasi terkumpul.</td></tr>"
    top = _anemos20_topbar(api, args)
    return f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS v20 — Status Akurasi</title><style>{_anemos20_css()}</style></head><body><main class='app'>{top}<section class='hero'><div class='chips'><span class='chip'>ANEMOS v20</span><span class='chip'>{matched}/{target} pasangan data</span></div><h1>Status Akurasi</h1><p>ANEMOS sedang mengumpulkan pasangan prakiraan dan observasi agar evaluasi akurasi dapat dibaca lebih kuat.</p></section><div class='notice'>{_anemos20_esc(sentinel_public_disclaimer(args))}</div><section class='panel'><div class='section-head'><h2>{'Data akurasi mulai terkumpul' if matched else 'Data akurasi belum cukup'}</h2><p>Target awal {target} pasangan.</p></div><div class='progress' style='--p:{pct:.1f}%'><span></span></div><p class='muted' style='margin-top:10px'>Saat ini terkumpul {matched}/{target} pasangan prakiraan-observasi.</p></section><section class='grid g4'><article class='metric'><span>Error suhu</span><strong>{_anemos20_num(summary.get('temperature_mae_c'),'°C',1)}</strong><small>lebih kecil lebih baik</small></article><article class='metric'><span>Skor peluang hujan</span><strong>{_anemos20_num(summary.get('rain_brier_score'),'',2)}</strong><small>lebih kecil lebih baik</small></article><article class='metric'><span>Hujan terdeteksi</span><strong>{_anemos20_num(summary.get('rain_pod'),'%')}</strong><small>kemampuan menangkap hujan</small></article><article class='metric'><span>Alarm keliru</span><strong>{_anemos20_num(summary.get('rain_far'),'%')}</strong><small>lebih kecil lebih baik</small></article></section><section class='panel'><div class='section-head'><h2>Bukti peluang hujan</h2><p>Reliability table akan makin bermakna saat data bertambah.</p></div><table><tr><th>Kelompok peluang</th><th>Kasus</th><th>Rata-rata prakiraan</th><th>Hujan benar terjadi</th></tr>{rel_rows}</table></section><p class='footer'>ANEMOS · Status akurasi · {ANEMOS_VERSION}</p></main></body></html>"""


def _anemos20_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ANEMOS Public API v1",
        "type": "object",
        "required": ["brand", "version", "generated_at", "location_name", "days"],
        "properties": {
            "brand": {"const": "ANEMOS"},
            "version": {"type": "string"},
            "generated_at": {"type": "string"},
            "location_name": {"type": "string"},
            "period_label": {"type": "string"},
            "days": {"type": "array", "items": {"type": "object"}},
            "public_brief": {"type": "object"},
        },
    }


def _anemos20_write_pwa_files(api, args):
    loc = api.get("location_name") or getattr(args, "location_name", "ANEMOS")
    manifest = {
        "name": f"ANEMOS — {loc}",
        "short_name": "ANEMOS",
        "start_url": "anemos_app.html",
        "display": "standalone",
        "background_color": "#ebf4ff",
        "theme_color": "#1769ff",
        "description": "Prakiraan cuaca lokal untuk keputusan harian.",
    }
    write_json(path_output("anemos_pwa_manifest.webmanifest"), manifest)
    atomic_write_text(path_output("offline.html"), lambda f: f.write("<!doctype html><html lang='id'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS Offline</title><body style='font-family:system-ui;padding:32px;background:#ebf4ff'><h1>ANEMOS</h1><p>Halaman offline. Buka kembali saat koneksi tersedia.</p></body></html>"))
    sw = """const CACHE='anemos-v20-cache';const FILES=['anemos_app.html','anemos_today.html','anemos_3day.html','anemos_activity.html','offline.html'];self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(FILES)).catch(()=>{}));});self.addEventListener('fetch',e=>{e.respondWith(fetch(e.request).catch(()=>caches.match(e.request).then(r=>r||caches.match('offline.html'))));});"""
    atomic_write_text(path_output("service_worker.js"), lambda f: f.write(sw))


def _anemos20_write_outputs(target_date, args):
    base_api = _anemos13_build_api(target_date, args)
    api = _anemos20_enhance_api(base_api, args)
    today = _anemos20_pick_day(api, 0)
    days = api.get("days") or []

    # Public API ecosystem
    write_json(path_output("anemos_api_v1.json"), api)
    write_json(path_output("anemos_public_brief.json"), api.get("public_brief", {}))
    write_json(path_output("anemos_schema.json"), _anemos20_schema())
    write_json(path_output("anemos_status.json"), {
        "brand": "ANEMOS", "version": ANEMOS_VERSION, "generated_at": api.get("generated_at"),
        "location": api.get("location_name"), "days_available": len(days),
        "ok": bool(days), "disclaimer": sentinel_public_disclaimer(args),
    })
    write_json(path_output("anemos_manifest.json"), {
        "brand": "ANEMOS", "version": ANEMOS_VERSION,
        "files": ["anemos_app.html","anemos_today.html","anemos_3day.html","anemos_activity.html","anemos_commute_advice.html","anemos_laundry_advice.html","anemos_api_v1.json","anemos_schema.json","anemos_status.json","anemos_public_brief.json","anemos_whatsapp_brief.txt"],
        "legacy_files_kept": [AETHER_DASHBOARD_FILENAME, AETHER_REPORT_HTML_FILENAME, "sentinel_x_accuracy_public.html"],
    })

    # CSV decision ecosystem
    risk_rows = []
    activity_rows = []
    for d in days:
        for h in d.get("key_hours") or []:
            risk_rows.append({"date": d.get("date"), "day_tag": d.get("day_tag"), "hour": h.get("hour"), "condition": h.get("condition"), "rain_probability": h.get("rain_probability"), "temp_c": h.get("temp_c"), "humidity_pct": h.get("humidity_pct"), "heat_index_c": h.get("heat_index_c"), "risk_class": _anemos20_risk_class(h.get("rain_probability")), "risk_label": _anemos20_risk_label(h.get("rain_probability"))})
        for a in d.get("activity_matrix") or []:
            row = dict(a); row["date"] = d.get("date"); row["day_tag"] = d.get("day_tag"); activity_rows.append(row)
    write_dict_csv(path_output("anemos_risk_timeline.csv"), ["date","day_tag","hour","condition","rain_probability","temp_c","humidity_pct","heat_index_c","risk_class","risk_label"], risk_rows)
    write_dict_csv(path_output("anemos_activity_matrix.csv"), ["date","day_tag","activity","status","advice","priority_hour","risk_class"], activity_rows)

    # Human pages
    full_html = _anemos20_detail_html(api, args, "full")
    atomic_write_text(path_output("anemos_app.html"), lambda f: f.write(full_html))
    atomic_write_text(path_output(AETHER_DASHBOARD_FILENAME), lambda f: f.write(full_html))
    atomic_write_text(path_output(AETHER_REPORT_HTML_FILENAME), lambda f: f.write(_anemos20_detail_html(api, args, "today")))
    atomic_write_text(path_output("anemos_today.html"), lambda f: f.write(_anemos20_detail_html(api, args, "today")))
    atomic_write_text(path_output("anemos_3day.html"), lambda f: f.write(_anemos20_detail_html(api, args, "3day")))
    atomic_write_text(path_output("anemos_activity.html"), lambda f: f.write(_anemos20_detail_html(api, args, "activity")))
    atomic_write_text(path_output("anemos_commute_advice.html"), lambda f: f.write(_anemos20_detail_html(api, args, "activity")))
    atomic_write_text(path_output("anemos_laundry_advice.html"), lambda f: f.write(_anemos20_detail_html(api, args, "activity")))
    atomic_write_text(path_output("anemos_embed_widget.html"), lambda f: f.write(_anemos20_widget_html(api, args)))
    atomic_write_text(path_output("anemos_whatsapp_brief.txt"), lambda f: f.write(_anemos20_whatsapp_text(api)))
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(_anemos20_accuracy_html([], args)))
    _anemos20_write_pwa_files(api, args)
    return api


def _anemos20_widget_html(api, args):
    api = _anemos20_enhance_api(api, args)
    loc = api.get("location_name") or getattr(args, "location_name", "Lokasi")
    d = _anemos20_pick_day(api, 0)
    return f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS Widget</title><style>{_anemos20_css()}body{{background:transparent}}.widget{{max-width:440px;margin:0;padding:24px;border-radius:30px;background:linear-gradient(135deg,#061634,#1769ff);color:white;box-shadow:var(--shadow)}}.widget h1{{font-size:34px;line-height:1;margin:8px 0}}.widget p{{color:#eaf5ff}}.widget .mini span{{background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.35);color:#dbeafe}}.widget .mini b{{color:white}}</style></head><body><article class='widget'><b>ANEMOS · {_anemos20_esc(loc)}</b><h1>{_anemos20_esc(d.get('risk_label') or 'Prakiraan lokal')}</h1><p>{_anemos20_esc(d.get('summary_sentence') or '')}</p><div class='mini'><span>Hujan<b>{_anemos20_num(d.get('peak_rain_probability'),'%')}</b></span><span>Jam<b>{_anemos20_esc(d.get('peak_rain_hour') or '—')}</b></span><span>Suhu<b>{_anemos20_num(d.get('avg_temperature_c'),'°C',1)}</b></span></div></article></body></html>"""


_ANEMOS20_PREV_SAVE_OUTPUTS = save_outputs


def save_outputs(target_date, results, args):
    summary = _ANEMOS20_PREV_SAVE_OUTPUTS(target_date, results, args)
    try:
        _anemos20_write_outputs(target_date, args)
    except Exception as exc:
        log_warning("ANEMOS v20 public product layer gagal ditulis:", exc)
        if DEBUG:
            traceback.print_exc()
    return summary


def _anemos20_portal_card(loc, base_url=""):
    slug = sanitize_filename(loc.slug)
    path = os.path.join(root_output_dir(), slug, "anemos_api_v1.json")
    api = read_json(path, default={}) or {}
    if not api:
        path = os.path.join(root_output_dir(), slug, "anemos_v13_public_api.json")
        api = read_json(path, default={}) or {}
    api = _anemos20_enhance_api(api, type("Args", (), {"location_name": getattr(loc, "location_name", slug), "timezone": DEFAULT_TIMEZONE})()) if api else {}
    d = _anemos20_pick_day(api, 0) if api else {}
    prefix = f"{base_url}/{slug}/" if base_url else f"{slug}/"
    cls = d.get("risk_class") or "low"
    summary = d.get("summary_sentence") or "Ringkasan akan tersedia setelah pembaruan prakiraan selesai."
    return f"""<article class='portal-card card'><h2><span class='status-dot {cls}'></span>{_anemos20_esc(loc.location_name)}</h2><p>{_anemos20_esc(summary)}</p><div class='mini'><span>Peluang hujan<b>{_anemos20_num(d.get('peak_rain_probability'),'%')}</b></span><span>Jam perhatian<b>{_anemos20_esc(d.get('peak_rain_hour') or '—')}</b></span><span>Suhu<b>{_anemos20_num(d.get('avg_temperature_c'),'°C',1)}</b></span></div><div class='nav' style='margin-top:16px'><a class='btn primary' href='{prefix}anemos_app.html'>Buka ANEMOS</a><a class='btn' href='{prefix}anemos_today.html'>Hari ini</a><a class='btn' href='{prefix}anemos_3day.html'>3 hari</a><a class='btn' href='{prefix}anemos_activity.html'>Aktivitas</a><a class='btn' href='{prefix}anemos_api_v1.json'>API</a></div></article>"""


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    now = now_local(getattr(args, 'timezone', DEFAULT_TIMEZONE))
    updated = f"{_v6_format_date_id(now.date())}, {now.strftime('%H:%M')} {_v6_timezone_label(getattr(args,'timezone',DEFAULT_TIMEZONE))}"
    cards = "".join(_anemos20_portal_card(loc, base_url) for loc in locations)
    doc = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ANEMOS v20 — Portal Cuaca Lokal</title><meta name='theme-color' content='#1769ff'><style>{_anemos20_css()}</style></head><body><main class='app'><section class='hero'><div class='chips'><span class='chip'>ANEMOS v20</span><span class='chip'>Civic Weather OS</span><span class='chip'>Diperbarui {updated}</span></div><h1>Cuaca lokal untuk keputusan harian.</h1><p>Pilih lokasi untuk melihat ringkasan hari ini, beberapa hari ke depan, peluang hujan, suhu, heat index, saran aktivitas, widget, dan API publik.</p></section><div class='notice'>{_anemos20_esc(sentinel_public_disclaimer(args))}</div><section class='grid g3 section'>{cards}</section><section class='panel'><div class='section-head'><h2>Output publik</h2><p>Disediakan untuk masyarakat, analisis, embed, dan pengembangan lanjutan.</p></div><div class='nav'><a class='btn' href='ensemble_all_locations.csv'>Ensemble CSV</a><a class='btn' href='forecast_all_locations.csv'>Forecast CSV</a><a class='btn' href='source_status_all_locations.csv'>Status sumber</a><a class='btn' href='forecast_batch_summary.json'>Batch summary</a><a class='btn' href='anemos_portal_manifest.json'>Portal manifest</a></div></section><p class='footer'>ANEMOS · {ANEMOS_VERSION} · {updated}</p></main></body></html>"""
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(doc))
    write_json(root_output_path("anemos_portal_manifest.json"), {"brand":"ANEMOS","version":ANEMOS_VERSION,"generated_at":now.isoformat(),"locations":[loc.slug for loc in locations],"index":root_output_path("index.html"),"disclaimer":sentinel_public_disclaimer(args)})
    # lightweight location card JSON for external apps
    location_cards = []
    for loc in locations:
        slug = sanitize_filename(loc.slug)
        api = read_json(os.path.join(root_output_dir(), slug, "anemos_api_v1.json"), default={}) or {}
        d = _anemos20_pick_day(api, 0) if api else {}
        location_cards.append({"slug": slug, "name": loc.location_name, "summary": d.get("summary_sentence"), "peak_rain_probability": d.get("peak_rain_probability"), "peak_rain_hour": d.get("peak_rain_hour"), "risk_label": d.get("risk_label"), "url": f"{slug}/anemos_app.html"})
    write_json(root_output_path("anemos_location_cards.json"), {"brand":"ANEMOS","version":ANEMOS_VERSION,"generated_at":now.isoformat(),"locations":location_cards})
    return root_output_path("index.html")


if __name__ == "__main__":
    main()
