#!/usr/bin/env python3
"""
LANGIT v64.4.2 — Real Atmospheric Field Map Engine

Drop-in replacement untuk: langit_v64_3_full_variable_map_engine.py

Fungsi utama:
1. Membaca payload publik hasil generator v63/v63.1 dari folder outputs.
2. Menormalisasi semua variabel cuaca penting ke schema tunggal.
3. Membuat peta field atmosfer yang lebih benar: multi-layer, timeline, marker,
   local-radius field untuk peta per lokasi, IDW field untuk portal multi-lokasi.
4. Membuat manifest validasi coverage variabel.
5. Verify-only yang ketat untuk core data, tetapi tidak memblokir field angin saat arah angin memang tidak tersedia di payload publik.

Tidak mengubah weather_ensemble_multi_location.py dan tidak merusak layer v63.1.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

VERSION = "v64.4.2"
ENGINE_NAME = "LANGIT v64.4.2 Real Atmospheric Field Map Engine"
JAKARTA = ZoneInfo("Asia/Jakarta")

MONTH_ID = [
    "",
    "Januari",
    "Februari",
    "Maret",
    "April",
    "Mei",
    "Juni",
    "Juli",
    "Agustus",
    "September",
    "Oktober",
    "November",
    "Desember",
]
DAY_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

BAD_PUBLIC_TOKENS = [
    "visual-first",
    "ANEMOS sedang",
    "AETHER Sentinel",
    "[.new Set",
    "const hours=[.new",
    "Data confidence",
]

JSON_SKIP_PATTERNS = [
    "manifest",
    "geojson",
    "location",
    "accuracy",
    "diagnostic",
    "debug",
    "raw",
    "model_court",
]

CSV_SKIP_PATTERNS = ["source", "model", "accuracy", "diagnostic", "debug", "raw"]

KNOWN_LOCATIONS: dict[str, tuple[str, str, str, float, float]] = {
    "dago": ("Dago, Bandung", "Dago", "Bandung", -6.8830, 107.6130),
    "jatinangor": ("Jatinangor, Sumedang", "Jatinangor", "Sumedang", -6.9330, 107.7710),
    "arjawinangun": ("Arjawinangun, Cirebon", "Arjawinangun", "Cirebon", -6.6460, 108.4080),
}

ALIASES: dict[str, list[str]] = {
    "time": [
        "time",
        "datetime",
        "valid_time",
        "date_time",
        "timestamp",
        "target_time",
        "forecast_time",
        "local_time",
        "jam",
        "hour",
        "valid",
    ],
    "date": ["date", "tanggal", "local_date", "target_date", "forecast_date", "day"],
    "temperature_c": [
        "temperature_2m",
        "temperature",
        "temp",
        "temp_c",
        "temperature_c",
        "suhu",
        "t2m",
        "air_temperature",
        "air_temp",
        "temperature_mean",
    ],
    "apparent_temperature_c": [
        "apparent_temperature",
        "apparent_temperature_c",
        "feels_like",
        "feelslike",
        "heat_index",
        "terasa",
        "suhu_terasa",
        "apparent_temp",
        "felt_temperature",
    ],
    "humidity_pct": [
        "relative_humidity_2m",
        "relative_humidity",
        "relative_humidity_pct",
        "humidity",
        "humidity_pct",
        "rh",
        "kelembapan",
        "kelembaban",
        "rel_humidity",
    ],
    "rain_probability_pct": [
        "precipitation_probability",
        "precipitation_probability_max",
        "rain_probability",
        "rain_probability_pct",
        "rain_prob",
        "pop",
        "probability_of_precipitation",
        "hujan",
        "peluang_hujan",
        "rain_chance",
        "precip_prob",
    ],
    "precipitation_mm": [
        "precipitation",
        "precipitation_sum",
        "precipitation_mm",
        "rain",
        "rain_mm",
        "rainfall",
        "curah_hujan",
        "hujan_mm",
        "showers",
    ],
    "cloud_cover_pct": [
        "cloud_cover",
        "cloudcover",
        "clouds",
        "total_cloud_cover",
        "cloud_cover_total",
        "cloudiness",
        "awan",
    ],
    "pressure_hpa": [
        "pressure_msl",
        "msl_pressure",
        "surface_pressure",
        "surface_pressure_hpa",
        "pressure",
        "pressure_hpa",
        "tekanan",
        "sea_level_pressure",
        "slp",
    ],
    "wind_speed_ms": [
        "wind_speed_10m",
        "wind_speed",
        "windspeed",
        "wind_speed_ms",
        "wind_speed_mps",
        "kecepatan_angin",
        "angin",
        "wind_10m",
        "wind",
    ],
    "wind_direction_deg": [
        "wind_direction_10m",
        "winddirection_10m",
        "wind_direction_10m_dominant",
        "winddirection_10m_dominant",
        "wind_direction",
        "winddirection",
        "wind_dir",
        "wind_dir_10m",
        "wind_direction_deg",
        "wind_dir_deg",
        "wind_deg",
        "wind_bearing",
        "bearing",
        "arah_angin",
        "arah_angin_derajat",
        "wd",
        "wd10m",
        "direction",
    ],
    "wind_gust_ms": [
        "wind_gusts_10m",
        "wind_gust",
        "wind_gust_ms",
        "gust",
        "gusts",
        "hembusan",
        "windgusts",
    ],
    "uv_index": ["uv_index", "uv", "uvi", "indeks_uv"],
    "visibility_km": ["visibility", "visibility_km", "visibility_m", "vis", "jarak_pandang"],
    "dew_point_c": ["dew_point_2m", "dewpoint_2m", "dew_point", "dewpoint", "titik_embun"],
    "shortwave_radiation": [
        "shortwave_radiation",
        "solar_radiation",
        "ghi",
        "radiasi",
        "irradiance",
        "global_horizontal_irradiance",
    ],
    "weather_code": ["weather_code", "weathercode", "kode_cuaca", "code"],
    "condition": ["condition", "weather", "summary", "cuaca", "description", "weather_desc"],
    "confidence_pct": ["confidence", "confidence_pct", "kepercayaan", "model_confidence", "data_confidence"],
    "active_sources": ["active_sources", "sources_active", "source_count", "model_count"],
    "total_sources": ["total_sources", "sources_total", "model_total"],
}

LAYER_DEFS: list[dict[str, Any]] = [
    {"key": "risk", "label": "Risiko", "field": "risk_score", "unit": "/100", "min": 0, "max": 100, "palette": "risk"},
    {"key": "rain", "label": "Hujan", "field": "rain_probability_pct", "unit": "%", "min": 0, "max": 100, "palette": "rain"},
    {"key": "temp", "label": "Suhu", "field": "temperature_c", "unit": "°C", "min": 18, "max": 38, "palette": "temp"},
    {"key": "feels", "label": "Terasa", "field": "apparent_temperature_c", "unit": "°C", "min": 20, "max": 44, "palette": "temp"},
    {"key": "humidity", "label": "Lembap", "field": "humidity_pct", "unit": "%", "min": 35, "max": 100, "palette": "humidity"},
    {"key": "cloud", "label": "Awan", "field": "cloud_cover_pct", "unit": "%", "min": 0, "max": 100, "palette": "cloud"},
    {"key": "pressure", "label": "Tekanan", "field": "pressure_hpa", "unit": "hPa", "min": 990, "max": 1025, "palette": "pressure"},
    {"key": "wind", "label": "Angin", "field": "wind_speed_ms", "unit": "m/s", "min": 0, "max": 12, "palette": "wind"},
    {"key": "gust", "label": "Gust", "field": "wind_gust_ms", "unit": "m/s", "min": 0, "max": 20, "palette": "wind"},
    {"key": "uv", "label": "UV", "field": "uv_index", "unit": "", "min": 0, "max": 12, "palette": "uv"},
    {"key": "visibility", "label": "Visibilitas", "field": "visibility_km", "unit": "km", "min": 0, "max": 25, "palette": "visibility"},
    {"key": "confidence", "label": "Confidence", "field": "confidence_pct", "unit": "%", "min": 0, "max": 100, "palette": "confidence"},
]

LAYER_BY_KEY = {layer["key"]: layer for layer in LAYER_DEFS}


@dataclass
class HourPoint:
    iso: str
    date: str
    date_label: str
    hour: str
    condition: str
    status: str
    risk_score: int
    note: str
    variables: dict[str, float | int | str | None] = field(default_factory=dict)


@dataclass
class LocationPack:
    slug: str
    name: str
    short_name: str
    admin: str
    latitude: float
    longitude: float
    updated_label: str
    coordinate_source: str
    hours: list[HourPoint]
    coverage: dict[str, int]
    available_layers: list[str]
    disabled_layers: list[str]
    missing_variables: list[str]
    wind_field_valid: bool


class BuildError(RuntimeError):
    pass


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).lower())


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    text = str(value).strip()
    if not text or text.lower() in {"-", "none", "null", "nan", "n/a"} or text == "—":
        return None
    text = text.replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def as_percent(value: Any) -> int | None:
    num = as_float(value)
    if num is None:
        return None
    if 0 <= num <= 1:
        num *= 100
    return int(round(clamp(num, 0, 100)))


def as_round(value: Any, ndigits: int = 1) -> float | None:
    num = as_float(value)
    if num is None:
        return None
    return round(num, ndigits)


def pick(row: dict[str, Any], alias_name: str | Iterable[str]) -> Any:
    aliases = list(alias_name) if not isinstance(alias_name, str) else ALIASES.get(alias_name, [alias_name])
    lower = {str(k).lower(): v for k, v in row.items()}
    compact = {normalize_key(str(k)): v for k, v in row.items()}

    for alias in aliases:
        alias_l = str(alias).lower()
        alias_c = normalize_key(alias_l)
        if alias_l in lower:
            return lower[alias_l]
        if alias_c in compact:
            return compact[alias_c]

    # Soft contains fallback, but avoid one-letter aliases causing false positives.
    for key_l, value in lower.items():
        key_c = normalize_key(key_l)
        for alias in aliases:
            alias_l = str(alias).lower()
            alias_c = normalize_key(alias_l)
            if len(alias_c) >= 4 and (alias_l in key_l or alias_c in key_c):
                return value
    return None


def normalize_hour(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        h = int(value)
        if 0 <= h <= 23:
            return f"{h:02d}:00"
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if match:
        h, m = int(match.group(1)), int(match.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    match = re.fullmatch(r"(\d{1,2})(?:\.0+)?", text)
    if match:
        h = int(match.group(1))
        if 0 <= h <= 23:
            return f"{h:02d}:00"
    return None


def parse_date_value(value: Any, fallback: date) -> date:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(JAKARTA)
        return dt.date()
    except Exception:
        return fallback


def parse_datetime(time_value: Any, date_value: Any, fallback_date: date, fallback_hour: str) -> datetime:
    text = "" if time_value is None else str(time_value).strip().replace("Z", "+00:00")
    if text:
        for candidate in [text, text.replace(" ", "T"), re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)]:
            try:
                dt = datetime.fromisoformat(candidate)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(JAKARTA).replace(tzinfo=None)
                return dt
            except Exception:
                pass
    d = parse_date_value(date_value, fallback_date)
    h, m = [int(x) for x in fallback_hour.split(":")]
    return datetime(d.year, d.month, d.day, h, m)


def date_label_id(d: date) -> str:
    return f"{DAY_ID[d.weekday()]}, {d.day} {MONTH_ID[d.month]} {d.year}"


def sentence(value: Any, fallback: str = "Berawan") -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"-", "none", "null", "n/a"} or text == "—":
        text = fallback
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1].upper() + text[1:]


def heat_index_c(temp_c: float | None, rh_pct: float | None) -> float | None:
    if temp_c is None:
        return None
    if rh_pct is None or temp_c < 27:
        return round(temp_c, 1)
    t_f = temp_c * 9 / 5 + 32
    rh = clamp(float(rh_pct), 1, 100)
    hi_f = (
        -42.379
        + 2.04901523 * t_f
        + 10.14333127 * rh
        - 0.22475541 * t_f * rh
        - 0.00683783 * t_f * t_f
        - 0.05481717 * rh * rh
        + 0.00122874 * t_f * t_f * rh
        + 0.00085282 * t_f * rh * rh
        - 0.00000199 * t_f * t_f * rh * rh
    )
    hi_c = (hi_f - 32) * 5 / 9
    return round(max(temp_c, hi_c), 1)


def normalize_visibility(value: Any) -> float | None:
    num = as_float(value)
    if num is None:
        return None
    # If source is meters, convert to km.
    if num > 100:
        num = num / 1000
    return round(clamp(num, 0, 80), 1)


def normalize_wind_speed(value: Any) -> float | None:
    num = as_float(value)
    if num is None:
        return None
    # Very common API unit mismatch: km/h accidentally labelled as speed.
    # If it is unrealistically high for the local low-level field, interpret as km/h.
    if num > 45:
        num = num / 3.6
    return round(max(0, num), 1)


def direction_from_compass(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    raw = raw.replace("°", "").replace("derajat", "").strip()
    n = as_float(raw)
    if n is not None:
        return float(n) % 360
    aliases = {
        "n": 0, "utara": 0, "north": 0,
        "nne": 22.5, "ne": 45, "timur laut": 45, "northeast": 45,
        "ene": 67.5, "e": 90, "timur": 90, "east": 90,
        "ese": 112.5, "se": 135, "tenggara": 135, "southeast": 135,
        "sse": 157.5, "s": 180, "selatan": 180, "south": 180,
        "ssw": 202.5, "sw": 225, "barat daya": 225, "southwest": 225,
        "wsw": 247.5, "w": 270, "barat": 270, "west": 270,
        "wnw": 292.5, "nw": 315, "barat laut": 315, "northwest": 315,
        "nnw": 337.5,
    }
    return aliases.get(raw)


def normalize_pressure(value: Any) -> float | None:
    num = as_float(value)
    if num is None:
        return None
    # Pa to hPa.
    if num > 2000:
        num = num / 100
    return round(num, 1)


def confidence_from(row: dict[str, Any]) -> int | None:
    explicit = as_percent(pick(row, "confidence_pct"))
    if explicit is not None:
        return explicit
    active = as_float(pick(row, "active_sources"))
    total = as_float(pick(row, "total_sources"))
    if active is not None and total is not None and total > 0:
        return int(round(clamp(active / total * 100, 0, 100)))
    return None


def compute_risk(variables: dict[str, Any]) -> int:
    rain = float(variables.get("rain_probability_pct") or 0)
    feels = as_float(variables.get("apparent_temperature_c"))
    rh = as_float(variables.get("humidity_pct"))
    gust = as_float(variables.get("wind_gust_ms"))
    uv = as_float(variables.get("uv_index"))
    confidence = as_float(variables.get("confidence_pct"))

    heat = 0
    if feels is not None:
        if feels >= 41:
            heat = 42
        elif feels >= 38:
            heat = 32
        elif feels >= 35:
            heat = 22
        elif feels >= 32:
            heat = 12

    wind = 0
    if gust is not None:
        if gust >= 18:
            wind = 42
        elif gust >= 13:
            wind = 28
        elif gust >= 9:
            wind = 14

    uv_risk = 0
    if uv is not None:
        if uv >= 11:
            uv_risk = 30
        elif uv >= 8:
            uv_risk = 20
        elif uv >= 6:
            uv_risk = 10

    moist = 8 if rh is not None and rh >= 90 else 0
    conf_penalty = 0 if confidence is None else max(0, 65 - confidence) * 0.20
    return int(round(clamp(max(rain, heat, wind, uv_risk) + moist + conf_penalty, 0, 100)))


def status_from_risk(risk: int) -> str:
    if risk >= 76:
        return "Tinggi"
    if risk >= 56:
        return "Waspada"
    if risk >= 31:
        return "Pantau"
    return "Aman"


def note_for(status: str, variables: dict[str, Any], condition: str) -> str:
    rain = as_float(variables.get("rain_probability_pct")) or 0
    feels = as_float(variables.get("apparent_temperature_c"))
    gust = as_float(variables.get("wind_gust_ms"))
    uv = as_float(variables.get("uv_index"))
    if status == "Tinggi":
        return "Risiko tinggi pada jam ini. Kurangi aktivitas luar ruang jika tidak mendesak."
    if status == "Waspada":
        return "Perlu persiapan dan pantau perubahan lokal."
    if rain >= 30:
        return "Ada peluang hujan yang perlu dipantau."
    if feels is not None and feels >= 34:
        return "Panas terasa cukup kuat; pilih waktu yang lebih teduh."
    if gust is not None and gust >= 8:
        return "Angin terasa aktif; perhatikan aktivitas luar ruang ringan."
    if uv is not None and uv >= 6:
        return "UV cukup tinggi; siapkan perlindungan matahari."
    if "cerah" in condition.lower():
        return "Kondisi cukup baik untuk aktivitas luar ruang."
    return "Kondisi relatif aman, tetap pantau lokal."


def normalize_variables(row: dict[str, Any]) -> dict[str, float | int | str | None]:
    temp = as_round(pick(row, "temperature_c"), 1)
    humidity = as_percent(pick(row, "humidity_pct"))
    rain_prob = as_percent(pick(row, "rain_probability_pct"))
    precip = as_round(pick(row, "precipitation_mm"), 2)
    cloud = as_percent(pick(row, "cloud_cover_pct"))
    pressure = normalize_pressure(pick(row, "pressure_hpa"))
    wind = normalize_wind_speed(pick(row, "wind_speed_ms"))
    wind_dir = direction_from_compass(pick(row, "wind_direction_deg"))
    gust = normalize_wind_speed(pick(row, "wind_gust_ms"))
    uv = as_round(pick(row, "uv_index"), 1)
    visibility = normalize_visibility(pick(row, "visibility_km"))
    dew_point = as_round(pick(row, "dew_point_c"), 1)
    shortwave = as_round(pick(row, "shortwave_radiation"), 1)
    feels = as_round(pick(row, "apparent_temperature_c"), 1)
    if feels is None:
        feels = heat_index_c(temp, humidity)
    confidence = confidence_from(row)

    if wind_dir is not None:
        wind_dir = float(wind_dir) % 360
    if gust is None and wind is not None:
        gust = round(wind * 1.45, 1)

    variables: dict[str, float | int | str | None] = {
        "temperature_c": temp,
        "apparent_temperature_c": feels,
        "dew_point_c": dew_point,
        "humidity_pct": humidity,
        "rain_probability_pct": rain_prob,
        "precipitation_mm": precip,
        "cloud_cover_pct": cloud,
        "pressure_hpa": pressure,
        "wind_speed_ms": wind,
        "wind_direction_deg": wind_dir,
        "wind_gust_ms": gust,
        "uv_index": uv,
        "visibility_km": visibility,
        "shortwave_radiation": shortwave,
        "confidence_pct": confidence,
        "weather_code": pick(row, "weather_code"),
    }
    variables["risk_score"] = compute_risk(variables)
    return variables


def row_score(row: dict[str, Any]) -> int:
    keys = " ".join(str(k).lower() for k in row)
    score = 0
    if any(x in keys for x in ["time", "hour", "jam", "datetime", "valid", "timestamp"]):
        score += 4
    if any(x in keys for x in ["temp", "suhu", "temperature", "apparent", "terasa"]):
        score += 4
    if any(x in keys for x in ["rain", "precip", "hujan", "pop", "probability"]):
        score += 4
    if any(x in keys for x in ["humidity", "rh", "kelembapan", "kelembaban"]):
        score += 2
    if any(x in keys for x in ["wind", "angin", "gust"]):
        score += 2
    if any(x in keys for x in ["cloud", "awan", "pressure", "uv", "visibility"]):
        score += 1
    return score


def explode_columnar_dict(obj: dict[str, Any], path: str = "") -> list[dict[str, Any]]:
    list_keys = [
        k
        for k, v in obj.items()
        if isinstance(v, list) and v and not all(isinstance(x, dict) for x in v)
    ]
    if not list_keys:
        return []

    time_key = None
    for key in list_keys:
        key_l = str(key).lower()
        if any(alias in key_l for alias in ["time", "jam", "hour", "valid"]):
            time_key = key
            break
    if time_key is None:
        return []

    n = len(obj[time_key])
    usable = [key for key in list_keys if len(obj.get(key, [])) == n]
    if n <= 0 or len(usable) < 2:
        return []

    scalars = {k: v for k, v in obj.items() if is_scalar(v)}
    rows: list[dict[str, Any]] = []
    for i in range(n):
        row = dict(scalars)
        row["_path"] = path
        for key in usable:
            row[key] = obj[key][i]
        rows.append(row)
    return rows


def extract_records(obj: Any, path: str = "") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        records.extend(explode_columnar_dict(obj, path))
        if row_score(obj) >= 7:
            row = {k: v for k, v in obj.items() if is_scalar(v)}
            if row:
                row["_path"] = path
                records.append(row)
        for key, value in obj.items():
            records.extend(extract_records(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            item_path = f"{path}[{i}]"
            if isinstance(item, dict):
                row = {k: v for k, v in item.items() if is_scalar(v)}
                if row_score(row) >= 7:
                    row["_path"] = item_path
                    records.append(row)
            records.extend(extract_records(item, item_path))
    return records


def csv_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row_score(row) >= 7:
                    clean = dict(row)
                    clean["_path"] = path.name
                    out.append(clean)
    except Exception:
        return []
    return out


def load_api(loc_dir: Path) -> Any | None:
    for name in ["langit_api_v1.json", "anemos_api_v1.json", "forecast.json", "public_api.json"]:
        path = loc_dir / name
        if path.exists():
            data = read_json(path)
            if data is not None:
                return data
    return None


def candidate_rows(loc_dir: Path, api: Any | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if api is not None:
        rows.extend(extract_records(api, "api"))

    for path in sorted(loc_dir.glob("*.json")):
        low = path.name.lower()
        if any(skip in low for skip in JSON_SKIP_PATTERNS):
            continue
        data = read_json(path)
        if data is not None:
            rows.extend(extract_records(data, path.name))

    for path in sorted(loc_dir.glob("*.csv")):
        low = path.name.lower()
        if any(skip in low for skip in CSV_SKIP_PATTERNS):
            continue
        rows.extend(csv_rows(path))

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        signature = "|".join(
            str(pick(row, key))
            for key in ["time", "date", "temperature_c", "rain_probability_pct", "humidity_pct", "wind_speed_ms"]
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(row)
    return unique


def build_hours(rows: list[dict[str, Any]]) -> list[HourPoint]:
    today = datetime.now(JAKARTA).date()
    points: list[HourPoint] = []

    for row in rows:
        time_value = pick(row, "time")
        hour = normalize_hour(time_value) or normalize_hour(pick(row, ["jam", "hour"]))
        if hour is None:
            continue
        dt = parse_datetime(time_value, pick(row, "date"), today, hour)
        variables = normalize_variables(row)
        # Reject records that do not really contain core weather values.
        if variables.get("temperature_c") is None and variables.get("rain_probability_pct") is None:
            continue
        condition = sentence(pick(row, "condition"), "Berawan")
        risk = int(variables.get("risk_score") or 0)
        status = status_from_risk(risk)
        points.append(
            HourPoint(
                iso=dt.isoformat(timespec="minutes"),
                date=dt.date().isoformat(),
                date_label=date_label_id(dt.date()),
                hour=f"{dt.hour:02d}:{dt.minute:02d}",
                condition=condition,
                status=status,
                risk_score=risk,
                note=note_for(status, variables, condition),
                variables=variables,
            )
        )

    points.sort(key=lambda p: p.iso)
    out: list[HourPoint] = []
    seen: set[tuple[str, str]] = set()
    for point in points:
        key = (point.date, point.hour)
        if key in seen:
            continue
        seen.add(key)
        out.append(point)
        if len(out) >= 96:
            break
    return out


def geo_from_dir(loc_dir: Path) -> tuple[float | None, float | None, str | None]:
    for name in ["langit_location.geojson", "location.geojson"]:
        path = loc_dir / name
        if not path.exists():
            continue
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        features = data.get("features")
        if isinstance(features, list) and features:
            feature = features[0]
            if isinstance(feature, dict):
                geometry = feature.get("geometry") or {}
                props = feature.get("properties") or {}
                coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
                if isinstance(coords, list) and len(coords) >= 2:
                    name_value = props.get("name") or props.get("title") or props.get("location")
                    return as_float(coords[1]), as_float(coords[0]), str(name_value) if name_value else None
        coords = data.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            return as_float(coords[1]), as_float(coords[0]), None
    return None, None, None


def find_lat_lon(obj: Any) -> tuple[float | None, float | None]:
    if isinstance(obj, dict):
        lat = as_float(pick(obj, ["latitude", "lat"]))
        lon = as_float(pick(obj, ["longitude", "lon", "lng"]))
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
        for value in obj.values():
            lat2, lon2 = find_lat_lon(value)
            if lat2 is not None and lon2 is not None:
                return lat2, lon2
    elif isinstance(obj, list):
        for value in obj:
            lat2, lon2 = find_lat_lon(value)
            if lat2 is not None and lon2 is not None:
                return lat2, lon2
    return None, None


def location_name(api: Any | None, slug: str, geo_name: str | None) -> tuple[str, str, str]:
    known = KNOWN_LOCATIONS.get(slug)
    if known:
        full, short, admin, _, _ = known
    else:
        clean = slug.replace("_", " ").replace("-", " ").title()
        full, short, admin = clean, clean, ""

    candidates: list[str] = []
    if geo_name:
        candidates.append(geo_name)
    if isinstance(api, dict):
        for key in ["location_name", "display_name", "name", "title"]:
            value = api.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        loc = api.get("location")
        if isinstance(loc, dict):
            parts = []
            for key in ["name", "adm4", "city", "regency", "admin", "province"]:
                value = loc.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
            if parts:
                candidates.append(", ".join(dict.fromkeys(parts)))

    if candidates:
        full = candidates[0]
        bits = [part.strip() for part in full.split(",") if part.strip()]
        short = bits[0] if bits else full
        admin = bits[1] if len(bits) > 1 else admin
    return full, short, admin


def coverage_from_hours(hours: list[HourPoint]) -> tuple[dict[str, int], list[str], list[str], list[str], bool]:
    coverage: dict[str, int] = {}
    for layer in LAYER_DEFS:
        field_name = layer["field"]
        count = 0
        for hour in hours:
            value = hour.variables.get(field_name)
            if as_float(value) is not None:
                count += 1
        coverage[layer["key"]] = count

    # risk is always computed if any hour is valid.
    if hours:
        coverage["risk"] = len(hours)

    available_layers = [layer["key"] for layer in LAYER_DEFS if coverage.get(layer["key"], 0) > 0]
    disabled_layers = [layer["key"] for layer in LAYER_DEFS if layer["key"] not in available_layers]
    missing_variables = [LAYER_BY_KEY[key]["field"] for key in disabled_layers]
    wind_valid = (
        coverage.get("wind", 0) > 0
        and sum(1 for hour in hours if as_float(hour.variables.get("wind_direction_deg")) is not None) > 0
    )
    return coverage, available_layers, disabled_layers, missing_variables, wind_valid


def read_location(loc_dir: Path) -> LocationPack | None:
    if not loc_dir.is_dir():
        return None
    api = load_api(loc_dir)
    geo_lat, geo_lon, geo_name = geo_from_dir(loc_dir)
    api_lat, api_lon = find_lat_lon(api)
    coordinate_source = "payload"
    lat = geo_lat if geo_lat is not None else api_lat
    lon = geo_lon if geo_lon is not None else api_lon
    if geo_lat is not None and geo_lon is not None:
        coordinate_source = "geojson"
    elif api_lat is not None and api_lon is not None:
        coordinate_source = "api"
    elif loc_dir.name in KNOWN_LOCATIONS:
        _, _, _, lat_f, lon_f = KNOWN_LOCATIONS[loc_dir.name]
        lat, lon = lat_f, lon_f
        coordinate_source = "known-location-fallback"
    else:
        return None

    assert lat is not None and lon is not None
    name, short, admin = location_name(api, loc_dir.name, geo_name)
    rows = candidate_rows(loc_dir, api)
    hours = build_hours(rows)
    if not hours:
        return None

    coverage, available, disabled, missing, wind_valid = coverage_from_hours(hours)
    now = datetime.now(JAKARTA)
    return LocationPack(
        slug=loc_dir.name,
        name=name,
        short_name=short,
        admin=admin,
        latitude=float(lat),
        longitude=float(lon),
        updated_label=f"{date_label_id(now.date())}, {now:%H:%M} WIB",
        coordinate_source=coordinate_source,
        hours=hours,
        coverage=coverage,
        available_layers=available,
        disabled_layers=disabled,
        missing_variables=missing,
        wind_field_valid=wind_valid,
    )


def load_locations(root: Path) -> list[LocationPack]:
    packs: list[LocationPack] = []
    for loc_dir in sorted(root.iterdir() if root.exists() else []):
        if not loc_dir.is_dir():
            continue
        pack = read_location(loc_dir)
        if pack is not None:
            packs.append(pack)
    return packs


def best_hour(pack: LocationPack) -> HourPoint:
    return max(
        pack.hours,
        key=lambda hour: (
            hour.risk_score,
            as_float(hour.variables.get("rain_probability_pct")) or 0,
            as_float(hour.variables.get("apparent_temperature_c")) or 0,
        ),
    )


def first_safe_hour(pack: LocationPack) -> HourPoint:
    return min(pack.hours, key=lambda hour: (hour.risk_score, hour.iso))


def hour_summary(pack: LocationPack) -> dict[str, Any]:
    peak = best_hour(pack)
    safe = first_safe_hour(pack)
    return {
        "slug": pack.slug,
        "name": pack.name,
        "status": peak.status,
        "risk_score": peak.risk_score,
        "peak_hour": peak.hour,
        "safe_hour": safe.hour,
        "rain_probability_pct": peak.variables.get("rain_probability_pct"),
        "temperature_c": peak.variables.get("temperature_c"),
        "wind_speed_ms": peak.variables.get("wind_speed_ms"),
        "wind_direction_deg": peak.variables.get("wind_direction_deg"),
    }


def manifest_for(pack: LocationPack | None, packs: list[LocationPack], portal: bool) -> dict[str, Any]:
    all_packs = packs if portal else ([pack] if pack else [])
    total_hours = sum(len(p.hours) for p in all_packs)
    global_coverage: dict[str, int] = {}
    for layer in LAYER_DEFS:
        global_coverage[layer["key"]] = sum(p.coverage.get(layer["key"], 0) for p in all_packs)
    available = [layer["key"] for layer in LAYER_DEFS if global_coverage.get(layer["key"], 0) > 0]
    disabled = [layer["key"] for layer in LAYER_DEFS if layer["key"] not in available]
    wind_direction_coverage = sum(
        1
        for p in all_packs
        for h in p.hours
        if as_float(h.variables.get("wind_direction_deg")) is not None
    )
    warnings = []
    if global_coverage.get("wind", 0) > 0 and wind_direction_coverage <= 0:
        warnings.append(
            "wind_speed tersedia, tetapi wind_direction tidak tersedia di payload publik; layer warna angin tetap aktif, animasi partikel angin dinonaktifkan agar tidak membuat arah palsu."
        )
    return {
        "version": VERSION,
        "engine": ENGINE_NAME,
        "generated_at": datetime.now(JAKARTA).isoformat(timespec="seconds"),
        "portal": portal,
        "location_count": len(all_packs),
        "hour_count": total_hours,
        "coverage": global_coverage,
        "available_layers": available,
        "disabled_layers": disabled,
        "missing_variables": [LAYER_BY_KEY[key]["field"] for key in disabled],
        "wind_field_valid": any(p.wind_field_valid for p in all_packs),
        "wind_direction_coverage": wind_direction_coverage,
        "warnings": warnings,
        "locations": [hour_summary(p) for p in all_packs],
        "notes": [
            "Peta adalah estimasi field dari titik prakiraan publik, bukan radar observasi.",
            "Portal memakai IDW interpolation antar lokasi aktif.",
            "Peta per lokasi memakai local-radius field agar tidak mewarnai area terlalu luas.",
        ],
    }


HTML_TEMPLATE = r'''<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="preconnect" href="https://unpkg.com">
<link rel="preconnect" href="https://basemaps.cartocdn.com">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
<style>
:root{
  --bg:#050b13;--panel:#06172a;--panel2:rgba(8,24,43,.82);--line:rgba(132,190,255,.28);
  --text:#f5fbff;--muted:#a8bfd5;--blue:#28a8ff;--cyan:#38f4df;--yellow:#f8ca4f;--orange:#f68d3b;--red:#ff4d6d;
  --shadow:0 20px 70px rgba(0,0,0,.42);--radius:20px;
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
}
*{box-sizing:border-box}html,body,#map{height:100%;margin:0;background:var(--bg);color:var(--text)}
body{overflow:hidden}.leaflet-container{background:#07111c}.leaflet-control-attribution{font-size:10px;background:rgba(3,8,14,.62)!important;color:#8aa1b8!important}.leaflet-control-zoom a{background:#071829!important;color:#fff!important;border-color:rgba(122,182,255,.25)!important}
.canvas-layer{position:absolute;inset:0;pointer-events:none;z-index:430}.wind-layer{position:absolute;inset:0;pointer-events:none;z-index:440}.grain{position:absolute;inset:0;z-index:450;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.022) 1px,transparent 1px);background-size:8px 8px;mix-blend-mode:screen;opacity:.45}.vignette{position:absolute;inset:0;z-index:451;pointer-events:none;background:radial-gradient(circle at 50% 45%,transparent 0,rgba(0,0,0,.10) 45%,rgba(0,0,0,.38) 100%)}
.brand{position:absolute;left:18px;top:18px;z-index:700;display:flex;align-items:center;gap:9px;background:rgba(4,14,25,.76);border:1px solid rgba(96,167,255,.25);border-radius:999px;padding:8px 12px;box-shadow:var(--shadow);backdrop-filter:blur(14px)}.logo{width:28px;height:28px;border-radius:10px;background:linear-gradient(135deg,#1b7dff,#4cf5df);box-shadow:0 0 22px rgba(48,202,255,.42)}.brand b{font-size:13px;letter-spacing:.02em}.brand small{display:block;color:var(--muted);font-size:10px;margin-top:1px}
.panel{position:absolute;left:18px;top:74px;z-index:700;width:270px;background:var(--panel2);border:1px solid rgba(108,180,255,.35);box-shadow:var(--shadow);border-radius:var(--radius);backdrop-filter:blur(18px);padding:16px}.eyebrow{font-size:10px;color:#79fff0;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.panel h1{font-size:22px;line-height:1.05;margin:7px 0 8px}.panel p{font-size:12px;line-height:1.45;color:#c4d7e8;margin:0 0 14px}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px}.metric{border:1px solid rgba(119,184,255,.30);background:rgba(13,42,73,.72);border-radius:12px;padding:10px}.metric small{display:block;color:#91aac2;font-size:10px}.metric b{display:block;font-size:18px;margin-top:4px}.footline{display:flex;justify-content:space-between;color:#92a9c1;font-size:10px;border-top:1px solid rgba(150,200,255,.15);margin-top:14px;padding-top:10px}.pill-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--cyan);box-shadow:0 0 12px var(--cyan);margin-right:6px}
.layerbar{position:absolute;top:18px;right:18px;z-index:710;display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end;max-width:min(760px,calc(100vw - 340px))}.layerbtn,.modebtn,.timebtn,.playbtn,.backbtn{border:1px solid rgba(130,190,255,.35);background:rgba(5,15,29,.82);color:#eaf7ff;border-radius:999px;font-weight:800;cursor:pointer;box-shadow:0 10px 28px rgba(0,0,0,.22);backdrop-filter:blur(12px);transition:.18s ease}.layerbtn{font-size:12px;padding:8px 12px}.layerbtn.active,.modebtn.active,.timebtn.active,.playbtn.active{background:#208dff;border-color:#6cc8ff;color:#fff}.layerbtn.disabled{opacity:.34;cursor:not-allowed;text-decoration:line-through}.modebtn{position:absolute;right:18px;top:62px;z-index:710;font-size:12px;padding:9px 13px}.backbtn{display:inline-block;text-decoration:none;margin-top:10px;padding:8px 12px;font-size:12px;background:#1fa6ff;color:white}
.note{position:absolute;left:18px;bottom:84px;z-index:700;width:270px;background:rgba(5,16,28,.78);border:1px solid rgba(112,180,255,.25);border-radius:16px;padding:14px;box-shadow:var(--shadow);backdrop-filter:blur(14px)}.note b{font-size:13px}.note p{margin:6px 0 0;color:#c2d8eb;font-size:12px;line-height:1.4}
.timeline{position:absolute;left:50%;bottom:24px;transform:translateX(-50%);z-index:720;width:min(620px,calc(100vw - 360px));background:rgba(4,15,28,.88);border:1px solid rgba(113,184,255,.35);border-radius:18px;padding:12px 14px;box-shadow:var(--shadow);backdrop-filter:blur(18px)}.timeline-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px}.timeline-title{font-size:11px;color:#c7dcef;font-weight:800}.timewrap{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;scrollbar-color:#6bbdff rgba(255,255,255,.12)}.timebtn{min-width:62px;padding:8px 8px;border-radius:12px}.timebtn span{display:block;font-size:12px}.timebtn small{display:block;color:#a7bed4;font-size:10px;margin-top:3px}.playbtn{font-size:12px;padding:8px 12px}.rangebar{height:7px;border-radius:99px;background:rgba(255,255,255,.16);overflow:hidden;margin-top:5px}.rangebar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#34e9d2,#28a8ff);border-radius:inherit}
.legend{position:absolute;right:18px;bottom:84px;z-index:700;width:168px;background:rgba(4,14,25,.82);border:1px solid rgba(115,183,255,.28);border-radius:16px;padding:13px;box-shadow:var(--shadow);backdrop-filter:blur(16px)}.legend-title{display:flex;justify-content:space-between;gap:10px;align-items:baseline;font-size:12px;font-weight:900}.legend-title small{color:#bad2e8;font-size:10px}.scale{height:8px;border-radius:99px;margin:12px 0 7px;background:linear-gradient(90deg,#32e7cf,#f7cd4f,#ff4f70)}.legend-labels{display:flex;justify-content:space-between;color:#a8bfd5;font-size:10px}.legend p{font-size:10px;color:#b5cada;line-height:1.35;margin:10px 0 0}
.location-label{background:rgba(5,17,31,.86);color:#fff;border:1px solid rgba(80,220,212,.75);padding:6px 10px;border-radius:999px;font-size:11px;font-weight:900;box-shadow:0 0 20px rgba(56,244,223,.35)}.leaflet-tooltip{background:transparent;border:0;box-shadow:none}.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#06172a;color:#eef8ff;border:1px solid rgba(122,192,255,.32)}.leaflet-popup-content{font-size:12px;line-height:1.45}.popup-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}.popup-grid div{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.10);border-radius:8px;padding:6px}.popup-grid small{display:block;color:#9fb7ce}.popup-grid b{font-size:13px}
@media(max-width:860px){.panel{width:calc(100vw - 36px);top:70px}.layerbar{left:18px;right:18px;top:auto;bottom:176px;max-width:none;justify-content:flex-start}.timeline{width:calc(100vw - 36px);bottom:18px}.legend{display:none}.note{display:none}.modebtn{display:none}.brand small{display:none}}
</style>
</head>
<body>
<div id="map"></div>
<canvas id="fieldCanvas" class="canvas-layer"></canvas>
<canvas id="windCanvas" class="wind-layer"></canvas>
<div class="grain"></div><div class="vignette"></div>
<div class="brand"><div class="logo"></div><div><b>LANGIT</b><small>Real Atmospheric Field</small></div></div>
<section class="panel">
  <div class="eyebrow"><span class="pill-dot"></span>Forecast variable field</div>
  <h1 id="panelTitle">LANGIT Map</h1>
  <p id="panelDesc">Peta memakai variabel jam aktif dari payload publik.</p>
  <div class="metrics">
    <div class="metric"><small>Layer aktif</small><b id="mLayer">Risiko</b></div>
    <div class="metric"><small>Nilai</small><b id="mValue">Data belum tersedia</b></div>
    <div class="metric"><small>Status</small><b id="mStatus">Aman</b></div>
    <div class="metric"><small>Jam aktif</small><b id="mHour">00:00</b></div>
  </div>
  <div class="footline"><span id="mDate"></span><span>LANGIT v64.4</span></div>
  <a id="backLink" class="backbtn" href="__BACK_URL__">Kembali</a>
</section>
<div class="layerbar" id="layerBar"></div>
<button class="modebtn" id="modeBtn" type="button">Mode peta</button>
<section class="note"><b id="noteTitle">Field atmosfer</b><p id="noteText">Pilih layer dan jam untuk melihat perubahan.</p></section>
<section class="legend"><div class="legend-title"><span id="legendTitle">Risiko</span><small id="legendUnit">0-100/100</small></div><div class="scale" id="legendScale"></div><div class="legend-labels"><span>rendah</span><span>sedang</span><span>tinggi</span></div><p id="legendNote">Warna adalah estimasi field dari titik prakiraan, bukan radar observasi.</p></section>
<section class="timeline"><div class="timeline-top"><button class="playbtn" id="playBtn" type="button">Play</button><div class="timeline-title" id="timelineTitle">Jam aktif</div></div><div class="timewrap" id="timeWrap"></div><div class="rangebar"><i id="rangeFill"></i></div></section>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<script>
const PAYLOAD = __PAYLOAD__;
const layers = PAYLOAD.layers;
const layerByKey = Object.fromEntries(layers.map(l => [l.key, l]));
const packs = PAYLOAD.locations || [];
const portal = !!PAYLOAD.portal;
const primary = PAYLOAD.location || packs[0];
let activeLayer = PAYLOAD.defaultLayer || (primary.available_layers && primary.available_layers.includes('risk') ? 'risk' : (primary.available_layers || ['risk'])[0]);
let activeTimeIndex = 0;
let playing = false;
let playTimer = null;
let mapMode = 'dark';

const centerLat = packs.length ? packs.reduce((a,p)=>a+p.latitude,0)/packs.length : primary.latitude;
const centerLon = packs.length ? packs.reduce((a,p)=>a+p.longitude,0)/packs.length : primary.longitude;
const map = L.map('map', {zoomControl:true, preferCanvas:true, attributionControl:true}).setView([centerLat, centerLon], portal ? 8 : 9);
const tilesDark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:19, attribution:'&copy; OpenStreetMap &copy; CARTO'}).addTo(map);
const tilesLight = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19, attribution:'&copy; OpenStreetMap'});

const fieldCanvas = document.getElementById('fieldCanvas');
const windCanvas = document.getElementById('windCanvas');
const fctx = fieldCanvas.getContext('2d');
const wctx = windCanvas.getContext('2d');
const layerBar = document.getElementById('layerBar');
const timeWrap = document.getElementById('timeWrap');
const rangeFill = document.getElementById('rangeFill');

function resizeCanvas(){
  const s = map.getSize();
  const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  for (const c of [fieldCanvas, windCanvas]) {
    c.style.width = s.x + 'px'; c.style.height = s.y + 'px';
    c.width = Math.round(s.x*dpr); c.height = Math.round(s.y*dpr);
    const ctx = c.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  }
}
function num(v){ const n = Number(v); return Number.isFinite(n) ? n : null; }
function clamp(v,a,b){return Math.max(a,Math.min(b,v));}
function norm(v, min, max){ const n=num(v); if(n===null) return null; return clamp((n-min)/(max-min),0,1); }
function hexToRgb(hex){ const h=hex.replace('#',''); return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)]; }
function mix(a,b,t){return [Math.round(a[0]+(b[0]-a[0])*t),Math.round(a[1]+(b[1]-a[1])*t),Math.round(a[2]+(b[2]-a[2])*t)];}
const palettes={
  risk:['#2ee6c9','#f4cd4c','#f06b3a','#ff486b'], rain:['#35d9ff','#2f8dff','#8554ff','#ff4f8b'], temp:['#1a7cff','#27e2d1','#f6cf48','#ff6c3e'], humidity:['#26dfc7','#377cff','#7b5cff'], cloud:['#0fa7ff','#9aa7b7','#f0f5ff'], pressure:['#5c54ff','#22d4c4','#f0c746'], wind:['#21d8d0','#f0ca45','#ff5b45'], uv:['#19d3cf','#f5d64e','#ff7a38','#c84dff'], visibility:['#ff526b','#f4c64c','#38e2ce'], confidence:['#ff526b','#f4c64c','#38e2ce']
};
function colorFor(layerKey, value){
  const layer=layerByKey[layerKey]; const t=norm(value, layer.min, layer.max); if(t===null) return null;
  const pal=(palettes[layer.palette]||palettes.risk).map(hexToRgb);
  const x=t*(pal.length-1); const i=Math.min(pal.length-2, Math.floor(x)); const f=x-i;
  const c=mix(pal[i],pal[i+1],f); return `rgb(${c[0]},${c[1]},${c[2]})`;
}
function valueLabel(layerKey,value){
  const layer=layerByKey[layerKey]; const n=num(value);
  if(n===null) return 'Data belum tersedia';
  const rounded = Math.abs(n)>=100 ? Math.round(n) : (Math.round(n*10)/10);
  return `${rounded}${layer.unit||''}`;
}
function allHours(){
  const set=new Set(); packs.forEach(p=>(p.hours||[]).forEach(h=>set.add(h.iso)));
  return Array.from(set).sort();
}
const hourList = allHours();
function hourFor(pack, idx=activeTimeIndex){
  if(!pack || !pack.hours || !pack.hours.length) return null;
  if(!hourList.length) return pack.hours[0];
  const target = hourList[Math.min(idx, hourList.length-1)];
  let exact = pack.hours.find(h=>h.iso===target); if(exact) return exact;
  const targetMs = Date.parse(target); let best=pack.hours[0], bd=Infinity;
  for(const h of pack.hours){ const d=Math.abs(Date.parse(h.iso)-targetMs); if(d<bd){bd=d;best=h;} }
  return best;
}
function fieldValueAtPoint(layerKey, x, y){
  const layer=layerByKey[layerKey]; if(!layer) return {value:null, alpha:0, windDir:null, windSpeed:null};
  let total=0, wsum=0, nearestKm=Infinity, windX=0, windY=0, windW=0;
  const ll = map.containerPointToLatLng([x,y]);
  for(const p of packs){
    const h=hourFor(p); if(!h) continue;
    const value=num(h.variables[layer.field]); if(value===null) continue;
    const d = map.distance(ll, [p.latitude,p.longitude]) / 1000;
    nearestKm = Math.min(nearestKm, d);
    let w;
    if(portal && packs.length > 1){ w = 1 / Math.pow(d + 12, 2.15); }
    else { const radius=72; if(d>radius) continue; w = Math.pow(1 - d/radius, 2.2); }
    total += value*w; wsum += w;

    const ws=num(h.variables.wind_speed_ms), wd=num(h.variables.wind_direction_deg);
    if(ws!==null && wd!==null){
      const rad=(wd+180)*Math.PI/180; // meteorological from-direction to movement-direction
      windX += Math.sin(rad)*ws*w; windY += -Math.cos(rad)*ws*w; windW += w;
    }
  }
  if(wsum<=0) return {value:null, alpha:0, windDir:null, windSpeed:null};
  let alpha = portal ? 0.55 : clamp(1 - nearestKm/72, 0, .62);
  if(activeLayer==='wind' || activeLayer==='gust') alpha += .08;
  const wx = windW>0 ? windX/windW : null, wy = windW>0 ? windY/windW : null;
  const speed = wx===null ? null : Math.sqrt(wx*wx + wy*wy);
  const dir = wx===null ? null : (Math.atan2(wx, -wy)*180/Math.PI + 360) % 360;
  return {value:total/wsum, alpha, windDir:dir, windSpeed:speed};
}
function drawField(){
  resizeCanvas();
  const s=map.getSize(); fctx.clearRect(0,0,s.x,s.y);
  const layer=layerByKey[activeLayer]; if(!layer) return;
  const step = map.getZoom() >= 10 ? 7 : 9;
  for(let y=0;y<s.y;y+=step){
    for(let x=0;x<s.x;x+=step){
      const fv=fieldValueAtPoint(activeLayer,x+step/2,y+step/2);
      if(fv.value===null || fv.alpha<=.02) continue;
      fctx.fillStyle=colorFor(activeLayer,fv.value);
      fctx.globalAlpha=fv.alpha;
      fctx.fillRect(x,y,step+1,step+1);
    }
  }
  fctx.globalAlpha=1;
}
let particles=[];
function resetParticles(){
  const s=map.getSize(); particles=[];
  const count = Math.round(clamp((s.x*s.y)/14500, 55, 160));
  for(let i=0;i<count;i++) particles.push({x:Math.random()*s.x,y:Math.random()*s.y,age:Math.random()*90});
}
function drawWind(){
  const s=map.getSize();
  wctx.clearRect(0,0,s.x,s.y);
  const anyWind = packs.some(p => (p.wind_field_valid));
  if(!anyWind){ requestAnimationFrame(drawWind); return; }
  wctx.lineWidth = 1.1; wctx.strokeStyle='rgba(188,230,255,.26)';
  for(const p of particles){
    const fv=fieldValueAtPoint('wind',p.x,p.y); const sp=num(fv.windSpeed), dir=num(fv.windDir);
    if(sp===null || dir===null || fv.alpha<=.02){ p.x=Math.random()*s.x; p.y=Math.random()*s.y; p.age=0; continue; }
    const rad=dir*Math.PI/180; const scale=0.22+Math.min(1.5,sp)*0.05;
    const vx=Math.sin(rad)*(1.8+sp)*scale, vy=-Math.cos(rad)*(1.8+sp)*scale;
    const px=p.x-vx*5, py=p.y-vy*5;
    wctx.globalAlpha=clamp(.08+fv.alpha*.35,0,.44);
    wctx.beginPath(); wctx.moveTo(px,py); wctx.lineTo(p.x,p.y); wctx.stroke();
    p.x+=vx; p.y+=vy; p.age++;
    if(p.x<0||p.y<0||p.x>s.x||p.y>s.y||p.age>180){p.x=Math.random()*s.x;p.y=Math.random()*s.y;p.age=0;}
  }
  wctx.globalAlpha=1;
  requestAnimationFrame(drawWind);
}
function statusColor(status){return status==='Tinggi'?'#ff4d6d':status==='Waspada'?'#f68d3b':status==='Pantau'?'#f8ca4f':'#38f4df';}
function markerColor(pack){const h=hourFor(pack); return statusColor(h?h.status:'Aman');}
const markers=[];
function setupMarkers(){
  packs.forEach(p=>{
    const color=markerColor(p);
    const marker=L.circleMarker([p.latitude,p.longitude],{radius:9,color,fillColor:color,weight:2,fillOpacity:.78,opacity:.95}).addTo(map);
    marker.bindTooltip(p.short_name || p.name, {permanent:true, direction:'right', className:'location-label', offset:[10,0]});
    marker.on('click',()=>{
      const h=hourFor(p); if(!h) return;
      const vars=h.variables;
      const html=`<b>${p.name}</b><br>${h.date_label} ${h.hour}<br><b>${h.status}</b> - Risiko ${h.risk_score}/100
      <div class="popup-grid">
        <div><small>Hujan</small><b>${valueLabel('rain',vars.rain_probability_pct)}</b></div>
        <div><small>Suhu</small><b>${valueLabel('temp',vars.temperature_c)}</b></div>
        <div><small>Terasa</small><b>${valueLabel('feels',vars.apparent_temperature_c)}</b></div>
        <div><small>Angin</small><b>${valueLabel('wind',vars.wind_speed_ms)}</b></div>
        <div><small>Lembap</small><b>${valueLabel('humidity',vars.humidity_pct)}</b></div>
        <div><small>Awan</small><b>${valueLabel('cloud',vars.cloud_cover_pct)}</b></div>
      </div><br>${h.note}`;
      marker.bindPopup(html).openPopup();
    });
    markers.push({pack:p, marker});
  });
}
function refreshMarkers(){
  for(const item of markers){ const c=markerColor(item.pack); item.marker.setStyle({color:c,fillColor:c}); }
}
function coverageFor(layerKey){
  return packs.reduce((a,p)=>a+(p.coverage&&p.coverage[layerKey]||0),0);
}
function setupLayers(){
  layerBar.innerHTML='';
  for(const layer of layers){
    const btn=document.createElement('button'); btn.className='layerbtn'; btn.textContent=layer.label; btn.type='button';
    if(coverageFor(layer.key)<=0){btn.classList.add('disabled'); btn.title='Data belum tersedia di payload publik';}
    if(layer.key===activeLayer) btn.classList.add('active');
    btn.onclick=()=>{ if(btn.classList.contains('disabled')) return; activeLayer=layer.key; updateAll(); };
    layerBar.appendChild(btn);
  }
}
function setupTimeline(){
  timeWrap.innerHTML='';
  hourList.forEach((iso,idx)=>{
    const d=new Date(iso); const h=hourFor(primary,idx); const layer=layerByKey[activeLayer];
    const value=h ? h.variables[layer.field] : null;
    const btn=document.createElement('button'); btn.type='button'; btn.className='timebtn';
    if(idx===activeTimeIndex) btn.classList.add('active');
    btn.innerHTML=`<span>${String(d.getHours()).padStart(2,'0')}:00</span><small>${valueLabel(activeLayer,value)}</small>`;
    btn.onclick=()=>{activeTimeIndex=idx; updateAll();};
    timeWrap.appendChild(btn);
  });
  const pct=hourList.length>1 ? activeTimeIndex/(hourList.length-1)*100 : 0;
  rangeFill.style.width=pct+'%';
}
function updatePanel(){
  const h=hourFor(primary); const layer=layerByKey[activeLayer]; if(!h||!layer) return;
  const value=h.variables[layer.field];
  document.getElementById('panelTitle').textContent = portal ? 'LANGIT Portal Map' : (primary.name || 'LANGIT Map');
  document.getElementById('panelDesc').textContent = portal ? 'Regional field dari seluruh lokasi aktif. Pilih layer dan jam.' : 'Peta memakai seluruh variabel jam aktif dari payload publik.';
  document.getElementById('mLayer').textContent=layer.label;
  document.getElementById('mValue').textContent=valueLabel(activeLayer,value);
  document.getElementById('mStatus').textContent=h.status;
  document.getElementById('mHour').textContent=h.hour;
  document.getElementById('mDate').textContent=h.date_label;
  document.getElementById('noteTitle').textContent=`${layer.label} field`;
  document.getElementById('noteText').textContent=`${primary.name}, ${h.hour}. ${h.note}`;
  document.getElementById('legendTitle').textContent=layer.label;
  document.getElementById('legendUnit').textContent=`${layer.min}-${layer.max}${layer.unit||''}`;
  document.getElementById('timelineTitle').textContent=`${h.date_label} - ${layer.label}`;
  document.getElementById('legendScale').style.background = `linear-gradient(90deg, ${(palettes[layer.palette]||palettes.risk).join(',')})`;
}
function updateLayerButtons(){
  [...document.querySelectorAll('.layerbtn')].forEach(btn=>btn.classList.toggle('active',btn.textContent===layerByKey[activeLayer].label));
}
function updateAll(){
  setupTimeline(); updatePanel(); updateLayerButtons(); refreshMarkers(); drawField();
}
document.getElementById('playBtn').onclick=()=>{
  playing=!playing; document.getElementById('playBtn').textContent=playing?'Pause':'Play'; document.getElementById('playBtn').classList.toggle('active',playing);
  if(playTimer) clearInterval(playTimer);
  if(playing){ playTimer=setInterval(()=>{activeTimeIndex=(activeTimeIndex+1)%Math.max(1,hourList.length); updateAll();},900); }
};
document.getElementById('modeBtn').onclick=()=>{
  if(mapMode==='dark'){ map.removeLayer(tilesDark); tilesLight.addTo(map); mapMode='light'; document.body.style.setProperty('--bg','#f4f8fc'); }
  else{ map.removeLayer(tilesLight); tilesDark.addTo(map); mapMode='dark'; document.body.style.setProperty('--bg','#050b13'); }
};
map.on('move zoom resize',()=>{ drawField(); resetParticles(); });
window.addEventListener('resize',()=>{ drawField(); resetParticles(); });
setupLayers(); setupMarkers(); setupTimeline(); updatePanel(); resizeCanvas(); drawField(); resetParticles(); drawWind();
</script>
</body>
</html>
'''


def build_html(pack: LocationPack | None, packs: list[LocationPack], portal: bool, public_base_url: str) -> str:
    selected = max(packs, key=lambda p: best_hour(p).risk_score) if portal else pack
    if selected is None:
        raise BuildError("Tidak ada lokasi valid untuk dibuat menjadi peta.")
    payload = {
        "version": VERSION,
        "engine": ENGINE_NAME,
        "portal": portal,
        "defaultLayer": "risk",
        "layers": LAYER_DEFS,
        "location": asdict(selected),
        "locations": [asdict(p) for p in packs] if portal else [asdict(selected)],
        "publicBaseUrl": public_base_url,
    }
    title = "LANGIT Portal Map" if portal else f"LANGIT Map - {selected.name}"
    back_url = "index.html" if portal else "../index.html"
    return (
        HTML_TEMPLATE.replace("__TITLE__", esc(title))
        .replace("__PAYLOAD__", compact_json(payload))
        .replace("__BACK_URL__", esc(back_url))
    )


def redirect_html(target: str, label: str = "LANGIT Map") -> str:
    return f'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="0; url={esc(target)}"><title>{esc(label)}</title><style>body{{font-family:system-ui;margin:40px;background:#061321;color:#eef8ff}}a{{color:#43c7ff}}</style></head><body><p>Membuka <a href="{esc(target)}">{esc(label)}</a>.</p></body></html>'''


def build_outputs(root: Path, public_base_url: str, debug: bool = False) -> None:
    packs = load_locations(root)
    if not packs:
        raise BuildError("Tidak ada lokasi valid di folder outputs. Jalankan forecast engine terlebih dahulu.")

    # Portal map.
    write_text(root / "langit_portal_map.html", build_html(None, packs, True, public_base_url))
    write_text(root / "langit_v64_4_manifest.json", pretty_json(manifest_for(None, packs, True)))

    # Per-location map.
    for pack in packs:
        loc_dir = root / pack.slug
        html_map = build_html(pack, packs, False, public_base_url)
        write_text(loc_dir / "langit_map_room.html", html_map)
        write_text(loc_dir / "langit_map.html", redirect_html("langit_map_room.html", "LANGIT Map"))
        write_text(loc_dir / "anemos_map.html", redirect_html("langit_map_room.html", "LANGIT Map"))
        write_text(loc_dir / "langit_v64_4_map_manifest.json", pretty_json(manifest_for(pack, [pack], False)))
        # Backward compatible manifest name expected by v64.3 workflows.
        write_text(loc_dir / "langit_v64_3_map_manifest.json", pretty_json(manifest_for(pack, [pack], False)))

    if debug:
        print(f"{ENGINE_NAME}: built {len(packs)} location(s)")
        for pack in packs:
            print(f"- {pack.slug}: {len(pack.hours)} hours, available={','.join(pack.available_layers)}")


def verify_manifest(manifest: dict[str, Any], source: Path) -> list[str]:
    errors: list[str] = []
    if int(manifest.get("location_count") or 0) <= 0:
        errors.append(f"{source}: location_count kosong")
    if int(manifest.get("hour_count") or 0) <= 0:
        errors.append(f"{source}: hour_count kosong")
    coverage = manifest.get("coverage") or {}
    if not isinstance(coverage, dict):
        errors.append(f"{source}: coverage tidak valid")
        return errors
    if sum(int(v or 0) for v in coverage.values()) <= 0:
        errors.append(f"{source}: semua layer bernilai kosong")
    if int(coverage.get("temp") or 0) <= 0:
        errors.append(f"{source}: temperature tidak terbaca")
    if int(coverage.get("rain") or 0) <= 0:
        errors.append(f"{source}: rain_probability tidak terbaca")
    # Wind speed can still be rendered as a scalar field even when direction is absent.
    # Do not fail the build here; v64.4.2 treats missing wind direction as non-fatal and disables wind particles automatically instead of faking directions.
    if int(coverage.get("wind") or 0) > 0 and not manifest.get("wind_field_valid"):
        print(f"::warning::{source}: wind_speed tersedia, tetapi wind_direction belum tersedia; layer angin tetap valid sebagai scalar field dan partikel dinonaktifkan.")
    return errors


def verify_outputs(root: Path) -> None:
    errors: list[str] = []
    portal = root / "langit_portal_map.html"
    portal_manifest = root / "langit_v64_4_manifest.json"
    if not portal.exists():
        errors.append("outputs/langit_portal_map.html tidak ditemukan")
    if not portal_manifest.exists():
        errors.append("outputs/langit_v64_4_manifest.json tidak ditemukan")
    else:
        data = read_json(portal_manifest)
        if isinstance(data, dict):
            errors.extend(verify_manifest(data, portal_manifest))
        else:
            errors.append("outputs/langit_v64_4_manifest.json tidak valid")

    for loc_dir in sorted(root.iterdir() if root.exists() else []):
        if not loc_dir.is_dir():
            continue
        for required in ["langit_map_room.html", "langit_map.html", "anemos_map.html", "langit_v64_4_map_manifest.json"]:
            if not (loc_dir / required).exists():
                errors.append(f"{loc_dir.name}/{required} tidak ditemukan")
        manifest_path = loc_dir / "langit_v64_4_map_manifest.json"
        data = read_json(manifest_path) if manifest_path.exists() else None
        if isinstance(data, dict):
            errors.extend(verify_manifest(data, manifest_path))

    html_files = list(root.glob("*.html")) + list(root.glob("*/*.html"))
    for path in html_files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for token in BAD_PUBLIC_TOKENS:
            if token in text:
                errors.append(f"{path}: mengandung token lama {token!r}")
        if "__PAYLOAD__" in text or "__TITLE__" in text:
            errors.append(f"{path}: placeholder template belum terganti")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(3)
    print(f"OK: {ENGINE_NAME} public output valid.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=ENGINE_NAME)
    parser.add_argument("--root", default="outputs", help="Folder output publik")
    parser.add_argument("--public-base-url", default="", help="Base URL GitHub Pages")
    parser.add_argument("--verify-only", action="store_true", help="Hanya validasi output")
    parser.add_argument("--debug", action="store_true", help="Tampilkan log detail")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if args.verify_only:
        verify_outputs(root)
        return 0

    build_outputs(root, args.public_base_url, args.debug)
    verify_outputs(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
