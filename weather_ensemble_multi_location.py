import argparse
import csv
import json
import logging
import math
import os
import ssl
import tempfile
import time
import traceback
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


TARGET_TIMES = ["10:00", "13:00", "16:00", "19:00", "22:00"]
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
MAX_CONSECUTIVE_FAILURE_PENALTY = 5

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
    "User-Agent": "weather-ensemble-multi-location/3.0 (+https://data.bmkg.go.id/prakiraan-cuaca/)",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
}
BMKG_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BMKG_PORTAL_URL,
    "Origin": "https://data.bmkg.go.id",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
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
    # Jatinangor does not have a single village named "Jatinangor" in BMKG adm4 format,
    # so the preset uses Hegarmanah as a representative BMKG point for the district area.
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
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    atomic_write_text(path, writer_fn, newline="")


def write_dict_csv(path, fieldnames, rows):
    def writer_fn(f):
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    atomic_write_text(path, writer_fn, newline="")


def write_json(path, payload):
    def writer_fn(f):
        json.dump(payload, f, ensure_ascii=False, indent=2)

    atomic_write_text(path, writer_fn)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_dict_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


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


def http_get_json(url, headers=None, timeout=HTTP_TIMEOUT_SECONDS):
    effective_headers = dict(DEFAULT_HTTP_HEADERS)
    if headers:
        effective_headers.update(headers)
    request = urllib.request.Request(url, headers=effective_headers)
    ssl_context = build_ssl_context() if url.lower().startswith("https://") else None
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset)
        return json.loads(payload)


def fetch_json_with_retry(url, headers=None, source_id="UNKNOWN"):
    last_error = None
    for attempt in range(1, MAX_RETRY_HTTP + 1):
        try:
            log_debug(source_id, "HTTP attempt", attempt, url)
            return http_get_json(url, headers=headers)
        except urllib.error.HTTPError as exc:
            last_error = exc
            log_debug(source_id, "attempt gagal:", exc)
            if exc.code in (400, 401, 403, 404):
                raise
            if attempt < MAX_RETRY_HTTP:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            log_debug(source_id, "attempt gagal:", exc)
            if attempt < MAX_RETRY_HTTP:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
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


def load_weight_config():
    global ACTIVE_SOURCE_WEIGHTS
    path = path_output(WEIGHTS_FILENAME)
    payload = read_json(path, default=None)
    ACTIVE_SOURCE_WEIGHTS = dict(SOURCE_BASE_WEIGHTS)
    if not payload:
        return

    for source_id, value in (payload.get("weights") or {}).items():
        parsed = safe_float(value)
        if parsed is not None and parsed > 0:
            ACTIVE_SOURCE_WEIGHTS[source_id] = round(parsed, 4)


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
    base = source_active_weight(point.source_id) * source_health_factor(point.source_id)
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
        sum(source_active_weight(item["source_id"]) for item in ALL_SOURCE_CONFIGS), 4
    )


def compute_confidence(bucket, total_weight, dominant_weight, temp_std, rh_std, rain_std):
    if not bucket:
        return 0.0, "Rendah"

    expected_sources = max(len(ALL_SOURCE_CONFIGS), 1)
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
            candidates.append(
                {
                    "dt": dt_local,
                    "temp_c": safe_float(item.get("t")),
                    "rh_pct": safe_float(item.get("hu")),
                    "rain_mm": bmkg_rain_proxy_mm(item.get("weather_desc")),
                    "wind_kmh": safe_float(item.get("ws")),
                    "raw_condition": item.get("weather_desc") or "",
                    "category": bmkg_to_kategori(item.get("weather_desc")),
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


def load_cached_bmkg_payload(target_date, args):
    raw_dir = path_output(RAW_PAYLOAD_DIRNAME)
    if not os.path.isdir(raw_dir):
        return None

    file_stub = sanitize_filename("bmkg")
    preferred_paths = [
        os.path.join(raw_dir, f"{file_stub}_latest_success.json"),
        os.path.join(raw_dir, f"{file_stub}_latest.json"),
    ]
    versioned_paths = []
    ignored_names = {
        f"{file_stub}_latest.json",
        f"{file_stub}_latest_success.json",
        f"{file_stub}_latest_failure.json",
    }
    for entry in os.scandir(raw_dir):
        if not entry.is_file():
            continue
        lower_name = entry.name.lower()
        if not lower_name.startswith(f"{file_stub}_") or not lower_name.endswith(".json"):
            continue
        if lower_name in ignored_names:
            continue
        versioned_paths.append(entry.path)

    candidate_paths = []
    for path in preferred_paths:
        if os.path.exists(path):
            candidate_paths.append(path)
    versioned_paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    candidate_paths.extend(versioned_paths)

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


def fetch_bmkg_forecast(target_date, config, args):
    params = {"adm4": args.adm4}
    url = build_url(BMKG_API_URL, params)
    try:
        payload = fetch_json_with_retry(
            url,
            headers=BMKG_HTTP_HEADERS,
            source_id=config["source_id"],
        )
        points = extract_bmkg_points(target_date, payload, args)
        return {"points": points, "raw_payload": payload, "request_url": url}
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
        }


def fetch_open_meteo_forecast(target_date, config, args):
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
    url = build_url(config["endpoint"], params)
    payload = fetch_json_with_retry(url, source_id=config["source_id"])
    hourly = payload.get("hourly") or {}

    times = hourly.get("time") or []
    if not times:
        raise ValueError("Open-Meteo response tidak memiliki hourly.time")

    temperatures = hourly.get("temperature_2m") or []
    humidities = hourly.get("relative_humidity_2m") or []
    precipitations = hourly.get("precipitation") or []
    weather_codes = hourly.get("weather_code") or []
    wind_speeds = hourly.get("wind_speed_10m") or []

    candidates = []
    for idx, time_text in enumerate(times):
        dt_local = parse_open_meteo_time(time_text, args.timezone)
        if dt_local.date() != target_date:
            continue
        candidates.append(
            {
                "dt": dt_local,
                "temp_c": safe_float(temperatures[idx] if idx < len(temperatures) else None),
                "rh_pct": safe_float(humidities[idx] if idx < len(humidities) else None),
                "rain_mm": safe_float(precipitations[idx] if idx < len(precipitations) else None),
                "wind_kmh": safe_float(wind_speeds[idx] if idx < len(wind_speeds) else None),
                "weather_code": weather_codes[idx] if idx < len(weather_codes) else None,
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
        )
    return {"points": points, "raw_payload": payload, "request_url": url}


def metno_precipitation_amount(data):
    for bucket_name, divisor in (("next_1_hours", 1), ("next_6_hours", 6), ("next_12_hours", 12)):
        bucket = data.get(bucket_name) or {}
        details = bucket.get("details") or {}
        value = safe_float(details.get("precipitation_amount"))
        if value is not None:
            return round(value / divisor, 2) if divisor > 1 else value
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
        "User-Agent": "weather-ensemble-multi-location/3.0 (contact: local-script)",
        "Accept": "application/json",
    }
    url = build_url(MET_NO_URL, params)
    payload = fetch_json_with_retry(url, headers=headers, source_id=config["source_id"])
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
        candidates.append(
            {
                "dt": dt_local,
                "temp_c": safe_float(instant_details.get("air_temperature")),
                "rh_pct": safe_float(instant_details.get("relative_humidity")),
                "rain_mm": rain_mm,
                "wind_kmh": round(wind_ms * 3.6, 2) if wind_ms is not None else None,
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
    return {"points": points, "raw_payload": payload, "request_url": url}


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


def save_raw_payload_snapshot(target_date, result, tz_name):
    raw_dir = path_output(RAW_PAYLOAD_DIRNAME)
    ensure_directory(raw_dir)
    stamp = target_date.strftime("%Y%m%d")
    created_stamp = now_local(tz_name).strftime("%Y%m%d_%H%M%S")
    file_stub = sanitize_filename(result.source_id.lower())
    path_versioned = os.path.join(raw_dir, f"{file_stub}_{stamp}_{created_stamp}.json")
    path_latest = os.path.join(raw_dir, f"{file_stub}_latest.json")
    path_latest_success = os.path.join(raw_dir, f"{file_stub}_latest_success.json")
    path_latest_failure = os.path.join(raw_dir, f"{file_stub}_latest_failure.json")
    document = {
        "generated_at": now_local(tz_name).isoformat(),
        "target_date": target_date.isoformat(),
        "source_id": result.source_id,
        "provider": result.provider,
        "success": result.success,
        "base_weight": result.base_weight,
        "request_url": result.request_url,
        "points_collected": len(result.points),
        "error": result.error,
        "payload": result.raw_payload,
    }
    write_json(path_versioned, document)
    write_json(path_latest, document)
    if result.success and result.raw_payload is not None:
        write_json(path_latest_success, document)
    else:
        write_json(path_latest_failure, document)
    return path_versioned


def fetch_source(target_date, config, args):
    source_id = config["source_id"]
    provider = config["provider"]
    kind = config["kind"]
    request_url = preview_request_url(config, args)
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
        )
        if args.save_raw_payloads:
            result.payload_saved_path = save_raw_payload_snapshot(
                target_date, result, args.timezone
            )
        return result
    except Exception as exc:
        log_info(f"{source_id} gagal:", exc)
        if DEBUG:
            traceback.print_exc()
        result = SourceResult(
            source_id=source_id,
            provider=provider,
            success=False,
            points={},
            error=str(exc),
            request_url=request_url,
            base_weight=source_active_weight(source_id),
        )
        if args.save_raw_payloads:
            result.payload_saved_path = save_raw_payload_snapshot(
                target_date, result, args.timezone
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

    return {
        "tanggal": tanggal,
        "jam": jam,
        "observed_datetime": row.get("observed_datetime") or "",
        "temp_c": round_or_blank(safe_float(row.get("temp_c"))),
        "rh_pct": round_or_blank(safe_float(row.get("rh_pct"))),
        "rain_mm": round_or_blank(safe_float(row.get("rain_mm"))),
        "wind_kmh": round_or_blank(safe_float(row.get("wind_kmh"))),
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

    candidates = []
    for idx, time_text in enumerate(times):
        dt_local = parse_open_meteo_time(time_text, tz_name)
        if dt_local.date() != target_date:
            continue
        candidates.append(
            {
                "dt": dt_local,
                "temp_c": safe_float(temperatures[idx] if idx < len(temperatures) else None),
                "rh_pct": safe_float(humidities[idx] if idx < len(humidities) else None),
                "rain_mm": safe_float(precipitations[idx] if idx < len(precipitations) else None),
                "wind_kmh": safe_float(wind_speeds[idx] if idx < len(wind_speeds) else None),
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
    url = build_url(OBSERVATION_ARCHIVE_URL, params)
    payload = fetch_json_with_retry(url, source_id="OBSERVATION_ARCHIVE")
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
                f"archive_payload_{target_date.strftime('%Y%m%d')}.json",
            )
            write_json(
                payload_path,
                {
                    "request_url": url,
                    "target_date": target_date.isoformat(),
                    "payload": payload,
                },
            )
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


def cleanup_old_outputs(args):
    total_deleted = 0
    for folder_name in (
        RAW_PAYLOAD_DIRNAME,
        LOG_DIRNAME,
        OBSERVATION_DIRNAME,
        REPORT_DIRNAME,
    ):
        total_deleted += cleanup_old_files_in_directory(
            path_output(folder_name), args.retention_days
        )
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

            temp_error = absolute_error(
                safe_float(row.get("suhu_C")),
                safe_float(observed.get("temp_c")),
            )
            rh_error = absolute_error(
                safe_float(row.get("RH_%")),
                safe_float(observed.get("rh_pct")),
            )
            rain_error = absolute_error(
                safe_float(row.get("rain_mm")),
                safe_float(observed.get("rain_mm")),
            )
            category_score = category_match_score(row.get("kategori"), observed.get("category"))

            temp_score = metric_score(temp_error, 8)
            rh_score = metric_score(rh_error, 1.5)
            rain_score = metric_score(rain_error, 20)
            overall_score = round(
                temp_score * 0.35
                + rh_score * 0.20
                + rain_score * 0.20
                + category_score * 0.25,
                2,
            )

            detail_rows.append(
                {
                    "target_date": target_date.isoformat(),
                    "source_id": row.get("source_id"),
                    "jam": row.get("target_jam"),
                    "temp_error": round_or_blank(temp_error),
                    "rh_error": round_or_blank(rh_error),
                    "rain_error": round_or_blank(rain_error),
                    "category_score": category_score,
                    "overall_score": overall_score,
                }
            )

            bucket = per_source.setdefault(
                row.get("source_id"),
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
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(ALL_SOURCE_CONFIGS))) as executor:
        future_map = {
            executor.submit(fetch_source, target_date, config, args): config
            for config in ALL_SOURCE_CONFIGS
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
        "weights_file": path_output(WEIGHTS_FILENAME),
        "health_file": path_output(HEALTH_FILENAME),
        "output_dir": ACTIVE_OUTPUT_DIR,
        "retention_days": args.retention_days,
        "low_coverage_slots": low_coverage_slots,
        "run_status": "warning" if low_coverage_slots else "ok",
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

    load_weight_config()
    load_health_config()
    log_info("Mulai proses untuk lokasi", args.location_name)
    log_info("Target date:", target_date.isoformat())
    log_info("Target hours:", ", ".join(TARGET_TIMES))
    log_info("Sumber aktif:", ", ".join(item["source_id"] for item in ALL_SOURCE_CONFIGS))
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
        log_info(
            f"{result.source_id}:",
            "OK" if result.success else "FAIL",
            f"({len(result.points)}/{len(TARGET_TIMES)} point)",
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
    return rows


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


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Multi-location multi-source weather ensemble collector."
    )
    parser.add_argument(
        "--mode",
        choices=["forecast", "sync-observations", "evaluate", "import-observations", "self-test"],
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
    parser.add_argument("--debug", action="store_true", default=DEBUG)
    parser.add_argument("--no-debug", action="store_false", dest="debug")
    return parser


def main():
    global DEBUG
    parser = build_arg_parser()
    args = parser.parse_args()
    DEBUG = args.debug
    refresh_location_presets(args.locations_file)

    if args.list_locations:
        print_available_locations()
        return

    validate_common_args(args)
    locations = resolve_requested_locations(args)
    for location in locations:
        validate_location_config(location)

    if args.mode == "forecast":
        if args.run_daily:
            loop_daily(args, locations)
        else:
            run_forecast_for_locations(args, locations)
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
    else:
        raise ValueError(f"Mode tidak dikenali: {args.mode}")


if __name__ == "__main__":
    main()
