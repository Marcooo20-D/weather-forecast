#!/usr/bin/env python3
"""
LANGIT v64.3 — Full Variable Wind Field Map Engine

Perbaikan dari v64.2:
1. Data bridge diperbaiki total: bisa membaca JSON row-based, JSON columnar-array, CSV, dan nested forecast.
2. Semua variabel penting disalurkan ke payload peta, bukan hanya risk/rain/temp/rh/wind.
3. Layer peta menjadi dinamis: Risiko, Hujan, Suhu, Terasa, Lembap, Awan, Tekanan, Angin,
   Gust, UV, Visibilitas, Confidence.
4. Popup dan panel membaca variabel aktual dari data jam aktif.
5. Peta portal regional dan peta per lokasi memakai struktur payload yang sama.

Pakai di root repo:
  python langit_v64_3_full_variable_map_engine.py --root outputs --public-base-url https://marcooo20-d.github.io/weather-forecast

Verify:
  python langit_v64_3_full_variable_map_engine.py --root outputs --verify-only
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

VERSION = "v64.3"
ENGINE_NAME = "LANGIT v64.3 Full Variable Wind Field Map Engine"
JAKARTA = ZoneInfo("Asia/Jakarta")

MONTH_ID = [
    "", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember"
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
    "location",
    "geojson",
    "source",
    "model_court",
    "accuracy",
    "diagnostic",
    "debug",
    "raw",
]


@dataclass
class HourPoint:
    iso: str
    date_label: str
    hour: str
    condition: str
    status: str
    risk: int
    note: str
    variables: dict[str, float | int | str | None] = field(default_factory=dict)


@dataclass
class LocationPack:
    slug: str
    name: str
    short_name: str
    admin: str
    lat: float
    lon: float
    updated_label: str
    hours: list[HourPoint]


LAYER_DEFS: dict[str, dict[str, Any]] = {
    "risk": {"label": "Risiko", "unit": "/100", "min": 0, "max": 100, "palette": "risk"},
    "rain_prob": {"label": "Hujan", "unit": "%", "min": 0, "max": 100, "palette": "risk"},
    "temp": {"label": "Suhu", "unit": "°C", "min": 18, "max": 38, "palette": "heat"},
    "feels": {"label": "Terasa", "unit": "°C", "min": 20, "max": 44, "palette": "heat"},
    "humidity": {"label": "Lembap", "unit": "%", "min": 35, "max": 100, "palette": "humidity"},
    "cloud": {"label": "Awan", "unit": "%", "min": 0, "max": 100, "palette": "cloud"},
    "pressure": {"label": "Tekanan", "unit": "hPa", "min": 990, "max": 1025, "palette": "pressure"},
    "wind": {"label": "Angin", "unit": "m/s", "min": 0, "max": 12, "palette": "wind"},
    "gust": {"label": "Gust", "unit": "m/s", "min": 0, "max": 20, "palette": "wind"},
    "uv": {"label": "UV", "unit": "", "min": 0, "max": 12, "palette": "uv"},
    "visibility": {"label": "Visibilitas", "unit": "km", "min": 0, "max": 25, "palette": "visibility"},
    "confidence": {"label": "Confidence", "unit": "%", "min": 0, "max": 100, "palette": "confidence"},
}


ALIASES: dict[str, list[str]] = {
    "time": [
        "time", "datetime", "valid_time", "date_time", "timestamp", "target_time",
        "forecast_time", "local_time", "jam", "hour"
    ],
    "date": ["date", "tanggal", "local_date", "target_date", "forecast_date"],
    "temp": [
        "temperature_2m", "temperature", "temp", "suhu", "t2m", "air_temperature",
        "air_temp", "temp_c", "temperature_c"
    ],
    "feels": [
        "apparent_temperature", "feels_like", "heat_index", "terasa", "suhu_terasa",
        "apparent_temp", "felt_temperature"
    ],
    "dew_point": [
        "dew_point_2m", "dewpoint_2m", "dew_point", "dewpoint", "titik_embun"
    ],
    "humidity": [
        "relative_humidity_2m", "relative_humidity", "humidity", "rh", "kelembapan",
        "kelembaban", "rel_humidity"
    ],
    "rain_prob": [
        "precipitation_probability", "rain_probability", "rain_prob", "pop",
        "probability_of_precipitation", "hujan", "peluang_hujan", "rain_chance",
        "precip_prob", "precipitation_probability_max"
    ],
    "precip": [
        "precipitation", "precipitation_sum", "precipitation_mm", "rain", "rain_mm",
        "rainfall", "curah_hujan", "hujan_mm", "showers"
    ],
    "cloud": [
        "cloud_cover", "cloudcover", "clouds", "total_cloud_cover", "awan",
        "cloud_cover_total", "cloudiness"
    ],
    "pressure": [
        "pressure_msl", "msl_pressure", "surface_pressure", "pressure", "tekanan",
        "sea_level_pressure", "slp"
    ],
    "wind": [
        "wind_speed_10m", "wind_speed", "windspeed", "kecepatan_angin", "angin",
        "wind_10m", "wind"
    ],
    "wind_dir": [
        "wind_direction_10m", "winddirection_10m", "wind_direction", "wind_dir",
        "arah_angin", "direction"
    ],
    "gust": [
        "wind_gusts_10m", "wind_gust", "gust", "gusts", "hembusan", "windgusts"
    ],
    "uv": ["uv_index", "uv", "uvi", "indeks_uv"],
    "visibility": ["visibility", "vis", "jarak_pandang", "visibility_m", "visibility_km"],
    "shortwave": [
        "shortwave_radiation", "solar_radiation", "ghi", "radiasi", "irradiance",
        "global_horizontal_irradiance"
    ],
    "weather_code": ["weather_code", "weathercode", "kode_cuaca", "code"],
    "condition": ["condition", "weather", "summary", "cuaca", "description", "weather_desc"],
    "confidence": ["confidence", "confidence_pct", "kepercayaan", "data_confidence", "model_confidence"],
    "active_sources": ["active_sources", "sources_active", "source_count", "model_count"],
    "total_sources": ["total_sources", "sources_total", "model_total"],
}


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "—", "None", "null", "nan", "NaN"}:
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


def round_or_none(value: Any, ndigits: int = 1) -> float | None:
    num = as_float(value)
    if num is None:
        return None
    return round(num, ndigits)


def key_variants(key: str) -> set[str]:
    k = str(key).lower()
    compact = re.sub(r"[^a-z0-9]+", "", k)
    return {k, compact}


def pick(row: dict[str, Any], alias_name: str | list[str]) -> Any:
    if isinstance(alias_name, list):
        keys = alias_name
    else:
        keys = ALIASES.get(alias_name, [alias_name])
    lower = {str(k).lower(): v for k, v in row.items()}
    compact = {re.sub(r"[^a-z0-9]+", "", str(k).lower()): v for k, v in row.items()}
    for key in keys:
        lk = str(key).lower()
        ck = re.sub(r"[^a-z0-9]+", "", lk)
        if lk in lower:
            return lower[lk]
        if ck in compact:
            return compact[ck]
    for lk, v in lower.items():
        for key in keys:
            if str(key).lower() in lk:
                return v
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
        h = int(match.group(1))
        m = int(match.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    match = re.fullmatch(r"(\d{1,2})(?:\.00)?", text)
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
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(JAKARTA).date()
    except Exception:
        return fallback


def parse_dt(time_value: Any, date_value: Any, fallback_day: date, fallback_hour: str) -> datetime:
    text = "" if time_value is None else str(time_value).strip().replace("Z", "+00:00")
    dt: datetime | None = None
    if text:
        for candidate in [text, text.replace(" ", "T"), re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)]:
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except ValueError:
                pass
    if dt is None:
        d = parse_date_value(date_value, fallback_day)
        h, m = [int(x) for x in fallback_hour.split(":")]
        dt = datetime(d.year, d.month, d.day, h, m)
    if dt.tzinfo is not None:
        dt = dt.astimezone(JAKARTA).replace(tzinfo=None)
    return dt


def date_id(d: date) -> str:
    return f"{DAY_ID[d.weekday()]}, {d.day} {MONTH_ID[d.month]} {d.year}"


def title_from_slug(slug: str) -> tuple[str, str, str]:
    known = {
        "dago": ("Dago, Bandung", "Dago", "Bandung"),
        "jatinangor": ("Jatinangor, Sumedang", "Jatinangor", "Sumedang"),
        "arjawinangun": ("Arjawinangun, Cirebon", "Arjawinangun", "Cirebon"),
    }
    if slug in known:
        return known[slug]
    clean = slug.replace("_", " ").replace("-", " ").title()
    return clean, clean, ""


def sentence(value: Any, fallback: str = "Berawan") -> str:
    text = str(value or "").strip()
    if not text or text in {"-", "—", "None", "null"}:
        text = fallback
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1].upper() + text[1:]


def heat_index(temp: float | None, rh: float | None) -> float | None:
    if temp is None:
        return None
    if rh is None or temp < 27:
        return round(temp, 1)
    t_f = temp * 9 / 5 + 32
    r = clamp(rh, 1, 100)
    hi_f = (
        -42.379 + 2.04901523 * t_f + 10.14333127 * r
        - 0.22475541 * t_f * r - 0.00683783 * t_f * t_f
        - 0.05481717 * r * r + 0.00122874 * t_f * t_f * r
        + 0.00085282 * t_f * r * r - 0.00000199 * t_f * t_f * r * r
    )
    hi_c = (hi_f - 32) * 5 / 9
    return round(max(temp, hi_c), 1)


def risk_score(v: dict[str, float | int | str | None]) -> int:
    rain = float(v.get("rain_prob") or 0)
    feels = as_float(v.get("feels"))
    rh = as_float(v.get("humidity"))
    gust = as_float(v.get("gust"))
    uv = as_float(v.get("uv"))
    confidence = as_float(v.get("confidence")) or 74

    heat = 0
    if feels is not None:
        if feels >= 40:
            heat = 36
        elif feels >= 37:
            heat = 27
        elif feels >= 34:
            heat = 18
        elif feels >= 32:
            heat = 10
    wind = 0
    if gust is not None:
        if gust >= 17:
            wind = 36
        elif gust >= 12:
            wind = 22
        elif gust >= 8:
            wind = 10
    uv_risk = 0
    if uv is not None:
        if uv >= 11:
            uv_risk = 24
        elif uv >= 8:
            uv_risk = 16
        elif uv >= 6:
            uv_risk = 8
    moist = 6 if rh is not None and rh >= 88 else 0
    conf_penalty = max(0, 65 - confidence) * 0.20
    return int(round(clamp(max(rain, heat, wind, uv_risk) + moist + conf_penalty, 0, 100)))


def status_from_risk(risk: int) -> str:
    if risk >= 76:
        return "Tinggi"
    if risk >= 56:
        return "Waspada"
    if risk >= 31:
        return "Pantau"
    return "Aman"


def note_for(status: str, v: dict[str, Any], condition: str) -> str:
    rain = as_float(v.get("rain_prob")) or 0
    feels = as_float(v.get("feels"))
    gust = as_float(v.get("gust"))
    uv = as_float(v.get("uv"))
    if status == "Tinggi":
        return "Risiko tinggi pada jam ini. Hindari aktivitas luar ruang jika tidak mendesak."
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


def confidence_from(row: dict[str, Any]) -> int:
    explicit = as_percent(pick(row, "confidence"))
    if explicit is not None:
        return explicit
    active = as_float(pick(row, "active_sources"))
    total = as_float(pick(row, "total_sources"))
    if active is not None and total is not None and total > 0:
        return int(round(clamp(active / total * 100, 0, 100)))
    return 74


def is_scalar(v: Any) -> bool:
    return not isinstance(v, (dict, list, tuple))


def explode_columnar_dict(obj: dict[str, Any], path: str = "") -> list[dict[str, Any]]:
    """
    Membaca schema ala Open-Meteo:
      {"hourly": {"time": [...], "temperature_2m": [...], ...}}
    menjadi list row:
      [{"time": t0, "temperature_2m": x0, ...}, ...]
    Ini sumber bug terbesar di v64.2: array variabel tidak dipasangkan per jam.
    """
    list_keys = [k for k, v in obj.items() if isinstance(v, list) and v and not all(isinstance(x, dict) for x in v)]
    if not list_keys:
        return []
    time_key = None
    for k in list_keys:
        if any(alias in str(k).lower() for alias in ["time", "jam", "hour", "valid"]):
            time_key = k
            break
    if time_key is None:
        return []
    n = len(obj[time_key])
    if n <= 0:
        return []
    usable = [k for k in list_keys if len(obj[k]) == n]
    if len(usable) < 2:
        return []
    scalars = {k: v for k, v in obj.items() if is_scalar(v)}
    rows = []
    for i in range(n):
        row = dict(scalars)
        row["_path"] = path
        for k in usable:
            row[k] = obj[k][i]
        rows.append(row)
    return rows


def extract_records(obj: Any, path: str = "") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        records.extend(explode_columnar_dict(obj, path))
        # Row-like dict.
        if row_score(obj) >= 5:
            row = {k: v for k, v in obj.items() if is_scalar(v)}
            row["_path"] = path
            records.append(row)
        for k, v in obj.items():
            records.extend(extract_records(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            for i, item in enumerate(obj):
                if isinstance(item, dict):
                    row = {k: v for k, v in item.items() if is_scalar(v)}
                    row["_path"] = f"{path}[{i}]"
                    if row_score(row) >= 5:
                        records.append(row)
                    records.extend(extract_records(item, f"{path}[{i}]"))
        else:
            for i, item in enumerate(obj):
                records.extend(extract_records(item, f"{path}[{i}]"))
    return records


def row_score(row: dict[str, Any]) -> int:
    keys = " ".join(str(k).lower() for k in row)
    score = 0
    if any(x in keys for x in ["time", "hour", "jam", "datetime", "valid", "timestamp"]):
        score += 3
    if any(x in keys for x in ["temp", "suhu", "temperature", "apparent", "terasa"]):
        score += 3
    if any(x in keys for x in ["rain", "precip", "hujan", "pop", "probability"]):
        score += 3
    if any(x in keys for x in ["humidity", "rh", "kelembapan", "kelembaban"]):
        score += 2
    if any(x in keys for x in ["wind", "angin", "gust"]):
        score += 2
    if any(x in keys for x in ["cloud", "awan", "pressure", "uv", "visibility"]):
        score += 1
    return score


def csv_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row["_path"] = path.name
                out.append(dict(row))
    except Exception:
        pass
    return out


def candidate_rows(loc_dir: Path, api: Any) -> list[dict[str, Any]]:
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
        if any(skip in low for skip in ["source", "model", "accuracy", "diagnostic"]):
            continue
        rows.extend([r for r in csv_rows(path) if row_score(r) >= 5])

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        sig = "|".join(str(pick(r, k)) for k in ["time", "date", "temp", "rain_prob", "humidity", "wind"])
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(r)
    return unique


def normalize_visibility(value: Any) -> float | None:
    num = as_float(value)
    if num is None:
        return None
    # If value looks like meters, convert to km.
    if num > 100:
        num = num / 1000
    return round(clamp(num, 0, 80), 1)


def normalize_variables(row: dict[str, Any]) -> dict[str, float | int | str | None]:
    temp = round_or_none(pick(row, "temp"))
    humidity = as_percent(pick(row, "humidity"))
    rain_prob = as_percent(pick(row, "rain_prob"))
    precip = round_or_none(pick(row, "precip"))
    cloud = as_percent(pick(row, "cloud"))
    pressure = round_or_none(pick(row, "pressure"))
    wind = round_or_none(pick(row, "wind"))
    wind_dir = round_or_none(pick(row, "wind_dir"), 0)
    gust = round_or_none(pick(row, "gust"))
    uv = round_or_none(pick(row, "uv"))
    visibility = normalize_visibility(pick(row, "visibility"))
    dew_point = round_or_none(pick(row, "dew_point"))
    shortwave = round_or_none(pick(row, "shortwave"))

    feels = round_or_none(pick(row, "feels"))
    if feels is None:
        feels = heat_index(temp, humidity)

    confidence = confidence_from(row)

    variables: dict[str, float | int | str | None] = {
        "temp": temp,
        "feels": feels,
        "dew_point": dew_point,
        "humidity": humidity,
        "rain_prob": rain_prob if rain_prob is not None else 0,
        "precip": precip,
        "cloud": cloud,
        "pressure": pressure,
        "wind": wind if wind is not None else 2.2,
        "wind_dir": wind_dir if wind_dir is not None else 115,
        "gust": gust if gust is not None else wind,
        "uv": uv,
        "visibility": visibility,
        "shortwave": shortwave,
        "confidence": confidence,
        "weather_code": pick(row, "weather_code"),
    }
    variables["risk"] = risk_score(variables)
    return variables


def build_hours(rows: list[dict[str, Any]]) -> list[HourPoint]:
    today = datetime.now(JAKARTA).date()
    points: list[HourPoint] = []
    for row in rows:
        time_value = pick(row, "time")
        hour = normalize_hour(time_value) or normalize_hour(pick(row, ["jam", "hour"]))
        if hour is None:
            continue
        dt = parse_dt(time_value, pick(row, "date"), today, hour)
        variables = normalize_variables(row)
        condition = sentence(pick(row, "condition"), "Berawan")
        risk = int(variables.get("risk") or 0)
        status = status_from_risk(risk)
        points.append(HourPoint(
            iso=dt.isoformat(timespec="minutes"),
            date_label=date_id(dt.date()),
            hour=f"{dt.hour:02d}:{dt.minute:02d}",
            condition=condition,
            status=status,
            risk=risk,
            note=note_for(status, variables, condition),
            variables=variables,
        ))

    if not points:
        for i, hour in enumerate(["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]):
            dt = parse_dt(None, None, today, hour)
            temp = round(24 + max(0, math.sin(i / 7 * math.pi) * 6), 1)
            rh = int(round(82 - max(0, math.sin(i / 7 * math.pi) * 22)))
            variables = {
                "temp": temp,
                "feels": heat_index(temp, rh),
                "dew_point": None,
                "humidity": rh,
                "rain_prob": 0,
                "precip": None,
                "cloud": 55,
                "pressure": 1010,
                "wind": 2.0,
                "wind_dir": 115,
                "gust": 3.2,
                "uv": None,
                "visibility": None,
                "shortwave": None,
                "confidence": 45,
                "weather_code": None,
            }
            variables["risk"] = risk_score(variables)
            risk = int(variables["risk"])
            points.append(HourPoint(
                iso=dt.isoformat(timespec="minutes"),
                date_label=date_id(dt.date()),
                hour=hour,
                condition="Data terbatas",
                status=status_from_risk(risk),
                risk=risk,
                note="Data terbatas. Gunakan hanya sebagai visualisasi sementara.",
                variables=variables,
            ))

    points.sort(key=lambda p: p.iso)
    out: list[HourPoint] = []
    seen: set[tuple[str, str]] = set()
    for p in points:
        key = (p.date_label, p.hour)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
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
            feat = features[0]
            geom = feat.get("geometry", {}) if isinstance(feat, dict) else {}
            props = feat.get("properties", {}) if isinstance(feat, dict) else {}
            coords = geom.get("coordinates") if isinstance(geom, dict) else None
            if isinstance(coords, list) and len(coords) >= 2:
                return as_float(coords[1]), as_float(coords[0]), str(props.get("name") or props.get("title") or "") or None
        coords = data.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            return as_float(coords[1]), as_float(coords[0]), None
    return None, None, None


def find_lat_lon(obj: Any) -> tuple[float | None, float | None]:
    if isinstance(obj, dict):
        la = as_float(pick(obj, ["latitude", "lat"]))
        lo = as_float(pick(obj, ["longitude", "lon", "lng"]))
        if la is not None and lo is not None and -90 <= la <= 90 and -180 <= lo <= 180:
            return la, lo
        for v in obj.values():
            a, b = find_lat_lon(v)
            if a is not None and b is not None:
                return a, b
    elif isinstance(obj, list):
        for v in obj:
            a, b = find_lat_lon(v)
            if a is not None and b is not None:
                return a, b
    return None, None


def location_name(api: Any, slug: str, geo_name: str | None) -> tuple[str, str, str]:
    full, short, admin = title_from_slug(slug)
    candidates: list[str] = []
    if geo_name:
        candidates.append(geo_name)
    if isinstance(api, dict):
        for k in ["location_name", "display_name", "name", "title"]:
            v = api.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
        loc = api.get("location")
        if isinstance(loc, dict):
            parts = []
            for k in ["name", "adm4", "city", "regency", "admin", "province"]:
                v = loc.get(k)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
            if parts:
                candidates.append(", ".join(dict.fromkeys(parts)))
    if candidates:
        full = candidates[0]
        bits = [x.strip() for x in full.split(",")]
        short = bits[0] if bits else full
        admin = bits[1] if len(bits) > 1 else admin
    return full, short, admin


def load_api(loc_dir: Path) -> Any:
    for name in ["langit_api_v1.json", "anemos_api_v1.json", "forecast.json", "public_api.json"]:
        path = loc_dir / name
        if path.exists():
            data = read_json(path)
            if data is not None:
                return data
    return None


def read_location(loc_dir: Path) -> LocationPack | None:
    if not loc_dir.is_dir():
        return None
    api = load_api(loc_dir)
    glat, glon, gname = geo_from_dir(loc_dir)
    alat, alon = find_lat_lon(api)
    lat = glat if glat is not None else alat
    lon = glon if glon is not None else alon
    if lat is None or lon is None:
        fallback = {
            "dago": (-6.883, 107.613),
            "jatinangor": (-6.933, 107.771),
            "arjawinangun": (-6.646, 108.408),
        }.get(loc_dir.name, (-6.9, 107.6))
        lat, lon = fallback
    full, short, admin = location_name(api, loc_dir.name, gname)
    rows = candidate_rows(loc_dir, api)
    hours = build_hours(rows)
    now = datetime.now(JAKARTA)
    return LocationPack(
        slug=loc_dir.name,
        name=full,
        short_name=short,
        admin=admin,
        lat=float(lat),
        lon=float(lon),
        updated_label=f"{date_id(now.date())}, {now:%H:%M} WIB",
        hours=hours,
    )


def peak_hour(pack: LocationPack) -> HourPoint:
    return max(pack.hours, key=lambda h: (
        h.risk,
        as_float(h.variables.get("rain_prob")) or 0,
        as_float(h.variables.get("feels")) or 0,
    ))


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="generator" content="LANGIT v64.3 Full Variable Wind Field Map Engine">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{
  --bg:#020814;--panel:rgba(3,13,25,.78);--line:rgba(148,163,184,.24);
  --text:#f8fbff;--muted:#b8c7d9;--blue:#22a7ff;--cyan:#2dd4bf;
}
*{box-sizing:border-box}
html,body,#map{height:100%;margin:0}
body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);overflow:hidden}
#map{z-index:1;background:#08111d}.leaflet-container{font-family:inherit;background:#08111d}.leaflet-control-attribution{font-size:10px;opacity:.55}
.leaflet-control-zoom a{background:rgba(4,15,27,.88)!important;color:#eaf6ff!important;border-color:rgba(148,163,184,.20)!important}
.atmos-canvas,.wind-canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.atmos-canvas{z-index:405;mix-blend-mode:screen;opacity:.95}.wind-canvas{z-index:430;opacity:.74}
.map-vignette{position:absolute;inset:0;z-index:440;pointer-events:none;background:radial-gradient(circle at 50% 50%,transparent 0,transparent 42%,rgba(0,0,0,.22) 78%,rgba(0,0,0,.48) 100%),linear-gradient(90deg,rgba(2,8,20,.28),transparent 18%,transparent 78%,rgba(2,8,20,.24))}
.topbar{position:absolute;left:0;right:0;top:0;z-index:720;display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding:14px 18px;background:linear-gradient(180deg,rgba(2,8,20,.86),rgba(2,8,20,.10));pointer-events:none}
.brand{pointer-events:auto;display:flex;align-items:center;gap:10px;border:1px solid var(--line);background:rgba(3,13,25,.66);padding:8px 12px;border-radius:999px;box-shadow:0 18px 44px rgba(0,0,0,.28);backdrop-filter:blur(16px)}
.logo{width:24px;height:24px;border-radius:9px;background:linear-gradient(135deg,#31e6c3,#20a4ff 54%,#1d4ed8);box-shadow:0 0 22px rgba(37,169,255,.58)}
.brand b{font-size:13px;letter-spacing:-.03em}.brand small{display:block;color:var(--muted);font-size:10px;line-height:1}
.actions{pointer-events:auto;display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;max-width:min(920px,calc(100vw - 180px))}
.chip{border:1px solid var(--line);background:rgba(3,13,25,.72);color:var(--text);border-radius:999px;padding:8px 10px;font-weight:850;font-size:12px;cursor:pointer;backdrop-filter:blur(14px);box-shadow:0 14px 32px rgba(0,0,0,.22)}
.chip.active{border-color:rgba(125,211,252,.82);background:linear-gradient(135deg,#1d4ed8,#22a7ff)}
.side-panel{position:absolute;left:18px;top:78px;z-index:710;width:min(365px,calc(100vw - 36px));border:1px solid rgba(96,165,250,.40);background:linear-gradient(180deg,rgba(3,13,25,.90),rgba(6,21,38,.78));border-radius:22px;overflow:hidden;box-shadow:0 24px 64px rgba(0,0,0,.42);backdrop-filter:blur(20px)}
.panel-head{padding:18px 18px 14px;border-bottom:1px solid rgba(148,163,184,.15)}.panel-kicker{display:flex;align-items:center;gap:8px;color:#aee9ff;font-size:11px;font-weight:900;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--cyan);box-shadow:0 0 0 7px rgba(45,212,191,.12),0 0 22px rgba(45,212,191,.70)}
.panel-head h1{margin:0 0 8px;font-size:25px;line-height:1.02;letter-spacing:-.05em}.panel-head p{margin:0;color:var(--muted);font-size:12.5px;line-height:1.45}
.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:12px}.metric{min-height:76px;border:1px solid rgba(148,163,184,.18);background:rgba(15,42,70,.64);border-radius:15px;padding:11px}.metric span{display:block;color:var(--muted);font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.05em}.metric b{display:block;margin-top:5px;font-size:22px;line-height:1}
.panel-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 14px;border-top:1px solid rgba(148,163,184,.14);color:var(--muted);font-size:11px}
.legend{position:absolute;right:18px;bottom:118px;z-index:710;width:220px;border:1px solid var(--line);background:rgba(3,13,25,.76);border-radius:18px;padding:13px;backdrop-filter:blur(18px);box-shadow:0 22px 48px rgba(0,0,0,.32)}
.legend-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:12px;font-weight:900}.scale{height:9px;border-radius:999px;background:linear-gradient(90deg,#2dd4bf 0%,#38bdf8 30%,#facc15 58%,#fb923c 78%,#ef4444 100%);box-shadow:0 0 20px rgba(250,204,21,.20)}
.scale-labels{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;margin-top:5px}.legend-note{margin-top:10px;color:var(--muted);font-size:11px;line-height:1.35}
.timeline{position:absolute;left:50%;bottom:22px;transform:translateX(-50%);z-index:730;width:min(830px,calc(100vw - 40px));border:1px solid rgba(125,211,252,.25);background:rgba(3,13,25,.82);border-radius:22px;padding:12px;box-shadow:0 24px 70px rgba(0,0,0,.45);backdrop-filter:blur(22px)}
.timeline-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 4px 10px}.timeline-title{font-size:12px;color:var(--muted);font-weight:800}.play{border:1px solid rgba(125,211,252,.35);background:rgba(15,42,70,.70);color:var(--text);border-radius:999px;padding:8px 12px;font-weight:900;cursor:pointer}
.time-track{position:relative;display:flex;gap:7px;overflow-x:auto;padding-bottom:2px;scrollbar-color:#8aa4bd transparent}.time{flex:0 0 auto;min-width:68px;border:1px solid rgba(148,163,184,.22);background:rgba(15,42,70,.64);color:var(--text);border-radius:13px;padding:9px 8px;cursor:pointer;text-align:center}.time b{display:block;font-size:13px}.time span{display:block;margin-top:2px;color:var(--muted);font-size:10px;font-weight:800}.time.active{border-color:rgba(125,211,252,.9);background:linear-gradient(135deg,#1d4ed8,#22a7ff);box-shadow:0 0 0 4px rgba(34,167,255,.12)}
.inspect{position:absolute;left:18px;bottom:118px;z-index:710;width:min(340px,calc(100vw - 36px));border:1px solid var(--line);background:rgba(3,13,25,.76);border-radius:18px;padding:14px;backdrop-filter:blur(18px);box-shadow:0 22px 48px rgba(0,0,0,.32)}.inspect h2{margin:0 0 6px;font-size:16px;letter-spacing:-.03em}.inspect p{margin:0;color:var(--muted);font-size:12px;line-height:1.42}
.location-label{border:1px solid rgba(125,211,252,.45);background:rgba(3,13,25,.82);color:#f8fbff;border-radius:999px;padding:6px 9px;font-size:11px;font-weight:900;box-shadow:0 10px 24px rgba(0,0,0,.28)}.leaflet-marker-icon{filter:drop-shadow(0 10px 18px rgba(0,0,0,.42))}
.leaflet-popup-content-wrapper{border-radius:18px;background:#f8fbff;box-shadow:0 24px 60px rgba(0,0,0,.35)}.leaflet-popup-content{margin:14px;min-width:260px}.popup-title{font-weight:950;font-size:16px;letter-spacing:-.03em;color:#07111f}.popup-sub{color:#475569;font-size:12px;margin-top:3px}.popup-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:10px}.popup-cell{border:1px solid #dbe7f3;background:#eef6ff;border-radius:10px;padding:8px;color:#06111f}.popup-cell b{display:block;font-size:15px}.popup-cell span{font-size:10px;color:#64748b;text-transform:uppercase;font-weight:800}
@media(max-width:820px){.side-panel{top:68px;left:12px;width:calc(100vw - 24px);max-height:42vh;overflow:auto}.actions{max-width:calc(100vw - 128px);overflow-x:auto;flex-wrap:nowrap}.chip{white-space:nowrap}.legend{display:none}.inspect{display:none}.timeline{bottom:12px;width:calc(100vw - 24px)}}
</style>
</head>
<body>
<div id="map"></div>
<canvas id="atmos" class="atmos-canvas"></canvas>
<canvas id="wind" class="wind-canvas"></canvas>
<div class="map-vignette"></div>

<header class="topbar">
  <div class="brand"><span class="logo"></span><div><b>LANGIT</b><small>Full Variable Field</small></div></div>
  <div class="actions" id="layerButtons"></div>
</header>

<section class="side-panel">
  <div class="panel-head">
    <div class="panel-kicker"><span class="pulse"></span><span id="panelMode">Forecast variable field</span></div>
    <h1 id="panelTitle">__PANEL_TITLE__</h1>
    <p id="panelDesc">Peta memakai seluruh variabel jam aktif yang tersedia di payload publik.</p>
  </div>
  <div class="metric-grid">
    <div class="metric"><span>Layer aktif</span><b id="mLayer">—</b></div>
    <div class="metric"><span>Nilai</span><b id="mValue">—</b></div>
    <div class="metric"><span>Status</span><b id="mStatus">—</b></div>
    <div class="metric"><span>Jam aktif</span><b id="mHour">—</b></div>
  </div>
  <div class="panel-foot"><span id="mDate">—</span><span>LANGIT __VERSION__</span></div>
</section>

<aside class="inspect"><h2 id="inspectTitle">Field atmosfer</h2><p id="inspectText">Klik titik mana pun di peta untuk membaca estimasi layer aktif.</p></aside>

<aside class="legend">
  <div class="legend-title"><span id="legendTitle">Skala</span><span id="legendUnit">—</span></div>
  <div class="scale" id="legendScale"></div>
  <div class="scale-labels"><span>rendah</span><span>sedang</span><span>tinggi</span></div>
  <div class="legend-note" id="legendNote">Warna adalah interpolasi visual dari data lokasi, bukan radar observasi.</div>
</aside>

<section class="timeline">
  <div class="timeline-top"><button class="play" id="playBtn">Play</button><div class="timeline-title" id="timeTitle">—</div></div>
  <div class="time-track" id="timeTrack"></div>
</section>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const LANGIT = __PAYLOAD__;
const LAYERS = LANGIT.layers;
let currentLayer = 'risk';
let currentIndex = 0;
let baseIndex = 0;
let playing = false;
let playTimer = null;
let markers = [];
let labels = [];

const map = L.map('map', { zoomControl:true, attributionControl:true, preferCanvas:true });
const baseLayers = [
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {maxZoom:19, attribution:'&copy; OpenStreetMap & CARTO'}),
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'&copy; OpenStreetMap'}),
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {maxZoom:19, attribution:'Tiles &copy; Esri'})
];
baseLayers[baseIndex].addTo(map);

const locs = LANGIT.locations || [LANGIT.location];
if(locs.length > 1){
  map.fitBounds(L.latLngBounds(locs.map(l => [l.lat,l.lon])), {padding:[160,160], maxZoom:9});
}else{
  map.setView([LANGIT.location.lat, LANGIT.location.lon], 10);
}

const atmos = document.getElementById('atmos');
const actx = atmos.getContext('2d', {alpha:true});
const wind = document.getElementById('wind');
const wctx = wind.getContext('2d', {alpha:true});
let particles = [];

function layerDef(layer=currentLayer){ return LAYERS[layer] || LAYERS.risk; }
function fmt(v, layer=currentLayer){
  const d = layerDef(layer);
  if(v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  const rounded = Math.abs(n) >= 100 ? Math.round(n) : Math.round(n*10)/10;
  return `${rounded}${d.unit || ''}`;
}
function pointFor(loc, index=currentIndex){ return loc.hours[Math.min(index, loc.hours.length-1)] || loc.hours[0]; }
function val(p, layer=currentLayer){
  if(layer === 'risk') return Number(p.risk ?? p.variables?.risk ?? 0);
  return Number(p.variables?.[layer] ?? 0);
}
function normalizeValue(v, layer=currentLayer){
  const d = layerDef(layer);
  return Math.max(0, Math.min(1, (Number(v) - Number(d.min)) / Math.max(1e-6, Number(d.max) - Number(d.min))));
}
function statusColor(status){
  if(status === 'Tinggi') return '#ef4444';
  if(status === 'Waspada') return '#fb923c';
  if(status === 'Pantau') return '#facc15';
  return '#2dd4bf';
}
function colorRamp(n, palette='risk', alpha=0.55){
  n = Math.max(0, Math.min(1, n));
  let stops;
  if(palette === 'confidence') stops = [[0,'139,92,246'],[.45,'245,158,11'],[.72,'56,189,248'],[1,'45,212,191']];
  else if(palette === 'humidity') stops = [[0,'45,212,191'],[.42,'56,189,248'],[.78,'139,92,246'],[1,'236,72,153']];
  else if(palette === 'heat' || palette === 'uv') stops = [[0,'45,212,191'],[.35,'250,204,21'],[.68,'249,115,22'],[1,'239,68,68']];
  else if(palette === 'wind') stops = [[0,'56,189,248'],[.5,'45,212,191'],[.78,'250,204,21'],[1,'249,115,22']];
  else if(palette === 'pressure') stops = [[0,'139,92,246'],[.45,'56,189,248'],[.75,'45,212,191'],[1,'250,204,21']];
  else if(palette === 'visibility') stops = [[0,'239,68,68'],[.3,'249,115,22'],[.65,'56,189,248'],[1,'45,212,191']];
  else if(palette === 'cloud') stops = [[0,'45,212,191'],[.4,'56,189,248'],[.72,'148,163,184'],[1,'226,232,240']];
  else stops = [[0,'45,212,191'],[.36,'250,204,21'],[.62,'249,115,22'],[1,'239,68,68']];
  for(let i=1;i<stops.length;i++){
    if(n <= stops[i][0]){
      const [p0,c0] = stops[i-1], [p1,c1] = stops[i];
      const t = (n-p0)/(p1-p0 || 1);
      const a = c0.split(',').map(Number), b = c1.split(',').map(Number);
      const r = Math.round(a[0]+(b[0]-a[0])*t);
      const g = Math.round(a[1]+(b[1]-a[1])*t);
      const bb = Math.round(a[2]+(b[2]-a[2])*t);
      return `rgba(${r},${g},${bb},${alpha})`;
    }
  }
  return `rgba(239,68,68,${alpha})`;
}
function noise(lat, lon, t=0){
  const x = Math.sin(lat*18.9898 + lon*78.233 + t*0.013) * 43758.5453;
  return x - Math.floor(x);
}
function fieldAt(latlng){
  let num = 0, den = 0;
  const scaleKm = LANGIT.portal ? 70 : 30;
  for(const loc of locs){
    const p = pointFor(loc);
    const dKm = Math.max(0.3, map.distance(latlng, L.latLng(loc.lat,loc.lon)) / 1000);
    const w = Math.exp(-dKm/scaleKm) + 1 / Math.pow(dKm + 2, 1.35);
    num += val(p) * w;
    den += w;
  }
  let v = den ? num / den : 0;
  const n = noise(latlng.lat, latlng.lng, currentIndex);
  const d = layerDef();
  v += (n - .5) * Math.max(.6, (d.max - d.min) * .05);
  return Math.max(Number(d.min), Math.min(Number(d.max), v));
}
function drawAtmosphere(){
  const w = window.innerWidth, h = window.innerHeight;
  actx.clearRect(0,0,w,h);
  const step = LANGIT.portal ? 8 : 7;
  const d = layerDef();
  for(let y=0; y<h; y+=step){
    for(let x=0; x<w; x+=step){
      const ll = map.containerPointToLatLng([x+step/2,y+step/2]);
      const v = fieldAt(ll);
      const n = normalizeValue(v);
      const alpha = 0.05 + n * 0.48;
      actx.fillStyle = colorRamp(n, d.palette, alpha);
      actx.fillRect(x,y,step+1,step+1);
    }
  }
}
function popupHtml(loc){
  const p = pointFor(loc);
  const v = p.variables || {};
  const cells = [
    ['Status', p.status],
    ['Risiko', `${p.risk}/100`],
    ['Hujan', fmt(v.rain_prob, 'rain_prob')],
    ['Suhu', fmt(v.temp, 'temp')],
    ['Terasa', fmt(v.feels, 'feels')],
    ['RH', fmt(v.humidity, 'humidity')],
    ['Awan', fmt(v.cloud, 'cloud')],
    ['Angin', fmt(v.wind, 'wind')],
    ['Gust', fmt(v.gust, 'gust')],
    ['Tekanan', fmt(v.pressure, 'pressure')],
    ['UV', fmt(v.uv, 'uv')],
    ['Conf.', fmt(v.confidence, 'confidence')],
  ].map(([k,val]) => `<div class="popup-cell"><b>${val}</b><span>${k}</span></div>`).join('');
  return `<div class="popup-title">${loc.name}</div>
  <div class="popup-sub">${p.date_label} · ${p.hour} WIB · ${p.condition}</div>
  <div class="popup-grid">${cells}</div>
  <p style="color:#475569;font-size:12px;line-height:1.45;margin:10px 0 0">${p.note}</p>`;
}
function drawMarkers(){
  markers.forEach(m => map.removeLayer(m)); labels.forEach(l => map.removeLayer(l));
  markers = []; labels = [];
  for(const loc of locs){
    const p = pointFor(loc);
    const c = statusColor(p.status);
    const marker = L.circleMarker([loc.lat, loc.lon], {radius: LANGIT.portal ? 7 : 9, color:'#eaffff', weight:2, fillColor:c, fillOpacity:.96})
      .bindPopup(popupHtml(loc)).addTo(map);
    markers.push(marker);
    labels.push(L.marker([loc.lat, loc.lon], {
      icon:L.divIcon({className:'', html:`<div class="location-label">${loc.short_name}</div>`, iconSize:[120,24], iconAnchor:[-14,8]}),
      interactive:false
    }).addTo(map));
  }
}
function activeMain(){
  if(LANGIT.portal){
    let best = locs[0];
    for(const loc of locs){ if(pointFor(loc).risk > pointFor(best).risk) best = loc; }
    return {loc:best, p:pointFor(best)};
  }
  return {loc:LANGIT.location, p:pointFor(LANGIT.location)};
}
function refreshPanel(){
  const {loc,p} = activeMain();
  const d = layerDef();
  const v = currentLayer === 'risk' ? p.risk : p.variables?.[currentLayer];
  document.getElementById('panelTitle').textContent = LANGIT.portal ? 'Regional field' : loc.name;
  document.getElementById('mLayer').textContent = d.label;
  document.getElementById('mValue').textContent = fmt(v);
  document.getElementById('mStatus').textContent = p.status;
  document.getElementById('mHour').textContent = p.hour;
  document.getElementById('mDate').textContent = p.date_label;
  document.getElementById('timeTitle').textContent = `${p.date_label} · ${d.label}`;
  document.getElementById('inspectTitle').textContent = d.label + ' field';
  document.getElementById('inspectText').textContent = `${loc.name}, ${p.hour} WIB. ${p.note}`;
  document.getElementById('legendTitle').textContent = d.label;
  document.getElementById('legendUnit').textContent = `${d.min}–${d.max}${d.unit || ''}`;
  document.getElementById('legendNote').textContent = 'Layer ini memakai variabel aktual dari payload jam aktif.';
}
function buildLayerButtons(){
  const box = document.getElementById('layerButtons');
  box.innerHTML = '';
  Object.entries(LAYERS).forEach(([key, d]) => {
    const b = document.createElement('button');
    b.className = 'chip' + (key === currentLayer ? ' active' : '');
    b.textContent = d.label;
    b.dataset.layer = key;
    b.onclick = () => {
      currentLayer = key;
      buildLayerButtons();
      refreshAll();
    };
    box.appendChild(b);
  });
  const base = document.createElement('button');
  base.className = 'chip';
  base.textContent = 'Mode peta';
  base.onclick = () => {
    map.removeLayer(baseLayers[baseIndex]);
    baseIndex = (baseIndex + 1) % baseLayers.length;
    baseLayers[baseIndex].addTo(map);
  };
  box.appendChild(base);
}
function buildTimeline(){
  const track = document.getElementById('timeTrack');
  track.innerHTML = '';
  const hours = LANGIT.location.hours;
  hours.forEach((p,i) => {
    const b = document.createElement('button');
    b.className = 'time' + (i === currentIndex ? ' active' : '');
    const activeVal = currentLayer === 'risk' ? p.risk : p.variables?.[currentLayer];
    b.innerHTML = `<b>${p.hour}</b><span>${fmt(activeVal)}</span>`;
    b.onclick = () => { currentIndex = i; buildTimeline(); refreshAll(); };
    track.appendChild(b);
  });
}
function refreshAll(){ refreshPanel(); drawAtmosphere(); drawMarkers(); buildTimeline(); }

function resizeCanvas(){
  const dpr = window.devicePixelRatio || 1;
  for(const c of [atmos, wind]){
    c.width = Math.max(1, Math.floor(window.innerWidth * dpr));
    c.height = Math.max(1, Math.floor(window.innerHeight * dpr));
    c.style.width = window.innerWidth + 'px';
    c.style.height = window.innerHeight + 'px';
  }
  actx.setTransform(dpr,0,0,dpr,0,0);
  wctx.setTransform(dpr,0,0,dpr,0,0);
  seedParticles();
  drawAtmosphere();
}
window.addEventListener('resize', resizeCanvas);
map.on('moveend zoomend resize', () => { drawAtmosphere(); drawMarkers(); });

map.on('click', e => {
  const estimate = fieldAt(e.latlng);
  L.popup().setLatLng(e.latlng).setContent(`<div class="popup-title">Estimasi titik</div>
    <div class="popup-sub">Layer ${layerDef().label}</div>
    <div class="popup-grid"><div class="popup-cell"><b>${fmt(estimate)}</b><span>Nilai</span></div><div class="popup-cell"><b>${Math.round(normalizeValue(estimate)*100)}%</b><span>Intensitas</span></div></div>
    <p style="color:#475569;font-size:12px;line-height:1.45;margin:10px 0 0">Estimasi visual dari interpolasi lokasi, bukan observasi titik.</p>`).openOn(map);
});

function windVectorAt(x,y){
  const ll = map.containerPointToLatLng([x,y]);
  let sx = 0, sy = 0, den = 0;
  for(const loc of locs){
    const p = pointFor(loc);
    const dKm = Math.max(0.3, map.distance(ll, L.latLng(loc.lat,loc.lon)) / 1000);
    const w = Math.exp(-dKm/(LANGIT.portal ? 75 : 34)) + 1 / Math.pow(dKm + 2, 1.22);
    const dir = Number(p.variables?.wind_dir || 115) * Math.PI / 180;
    const sp = Math.max(.7, Number(p.variables?.wind || 2.2));
    sx += Math.sin(dir) * sp * w;
    sy += -Math.cos(dir) * sp * w;
    den += w;
  }
  if(!den) return {x:1,y:0,s:1};
  const nx = sx / den, ny = sy / den;
  return {x:nx,y:ny,s:Math.sqrt(nx*nx+ny*ny)};
}
function seedParticles(){
  const count = Math.min(1300, Math.max(330, Math.floor(window.innerWidth * window.innerHeight / 1450)));
  particles = Array.from({length:count}, () => ({x:Math.random()*window.innerWidth,y:Math.random()*window.innerHeight,age:Math.random()*120,life:80+Math.random()*100}));
}
function animateWind(){
  wctx.clearRect(0,0,window.innerWidth,window.innerHeight);
  wctx.lineWidth = 1;
  for(const pt of particles){
    const v = windVectorAt(pt.x, pt.y);
    const speed = Math.max(.45, Math.min(3.8, v.s * .72));
    const wobble = Math.sin((pt.x + pt.y + pt.age) * .008) * .45;
    const vx = v.x * speed + Math.cos(wobble) * .05;
    const vy = v.y * speed + Math.sin(wobble) * .05;
    const alpha = Math.max(.08, Math.min(.42, .12 + v.s * .05));
    wctx.strokeStyle = `rgba(190,235,255,${alpha})`;
    wctx.beginPath(); wctx.moveTo(pt.x, pt.y);
    pt.x += vx; pt.y += vy; pt.age += 1;
    wctx.lineTo(pt.x, pt.y); wctx.stroke();
    if(pt.x < -40 || pt.x > window.innerWidth+40 || pt.y < -40 || pt.y > window.innerHeight+40 || pt.age > pt.life){
      pt.x = Math.random()*window.innerWidth; pt.y = Math.random()*window.innerHeight; pt.age = 0; pt.life = 80 + Math.random()*100;
    }
  }
  requestAnimationFrame(animateWind);
}
document.getElementById('playBtn').onclick = () => {
  playing = !playing;
  document.getElementById('playBtn').textContent = playing ? 'Pause' : 'Play';
  if(playing){
    playTimer = setInterval(() => {
      currentIndex = (currentIndex + 1) % LANGIT.location.hours.length;
      refreshAll();
    }, 950);
  }else clearInterval(playTimer);
};
document.addEventListener('keydown', e => {
  if(e.key === 'ArrowRight') currentIndex = Math.min(LANGIT.location.hours.length-1, currentIndex+1);
  if(e.key === 'ArrowLeft') currentIndex = Math.max(0, currentIndex-1);
  refreshAll();
});
buildLayerButtons();
resizeCanvas();
refreshAll();
animateWind();
</script>
</body>
</html>
"""


def build_html(pack: LocationPack, locations: list[LocationPack], portal: bool) -> str:
    if portal:
        selected = max(locations, key=lambda p: peak_hour(p).risk)
        title = "LANGIT Portal Map"
        panel = "Regional field"
    else:
        selected = pack
        title = f"LANGIT Map — {pack.name}"
        panel = pack.name

    payload = {
        "version": VERSION,
        "engine": ENGINE_NAME,
        "portal": portal,
        "layers": LAYER_DEFS,
        "location": asdict(selected),
        "locations": [asdict(x) for x in locations] if portal else [asdict(selected)],
    }
    return (
        HTML_TEMPLATE
        .replace("__TITLE__", esc(title))
        .replace("__PANEL_TITLE__", esc(panel))
        .replace("__VERSION__", esc(VERSION))
        .replace("__PAYLOAD__", compact_json(payload))
    )


def redirect_html(target: str) -> str:
    return f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0; url={esc(target)}">
<title>LANGIT Map</title>
<style>
body{{margin:0;height:100vh;display:grid;place-items:center;background:#020814;color:#f8fbff;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
a{{color:#38bdf8}}
</style>
</head>
<body><p>Membuka <a href="{esc(target)}">LANGIT Map</a>...</p></body>
</html>"""


def write_outputs(root: Path, public_base_url: str = "", debug: bool = False) -> list[LocationPack]:
    if not root.exists():
        raise FileNotFoundError(f"Folder output tidak ditemukan: {root}")

    locations: list[LocationPack] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name in {"assets", "logs", "raw_payloads"}:
            continue
        if not ((d / "langit_api_v1.json").exists() or (d / "langit_location.geojson").exists() or list(d.glob("*.csv")) or list(d.glob("*.json"))):
            continue
        pack = read_location(d)
        if pack is not None:
            locations.append(pack)

    if not locations:
        raise RuntimeError("Tidak ada lokasi valid di outputs/.")

    for pack in locations:
        d = root / pack.slug
        html_text = build_html(pack, locations, portal=False)
        write_text(d / "langit_map_room.html", html_text)
        write_text(d / "langit_map.html", html_text)
        write_text(d / "anemos_map.html", html_text)
        manifest = {
            "version": VERSION,
            "engine": ENGINE_NAME,
            "slug": pack.slug,
            "name": pack.name,
            "url": f"{public_base_url.rstrip('/')}/{pack.slug}/langit_map_room.html" if public_base_url else f"{pack.slug}/langit_map_room.html",
            "peak": asdict(peak_hour(pack)),
            "hours": len(pack.hours),
            "layers": sorted(LAYER_DEFS.keys()),
            "first_hour_variables": pack.hours[0].variables if pack.hours else {},
        }
        write_text(d / "langit_v64_3_map_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if debug:
            print(f"OK: {d / 'langit_map_room.html'}")

    write_text(root / "langit_portal_map.html", build_html(locations[0], locations, portal=True))
    write_text(root / "map.html", redirect_html("langit_portal_map.html"))

    index_patch = root / "index.html"
    if index_patch.exists():
        text = index_patch.read_text(encoding="utf-8", errors="ignore")
        if "langit_portal_map.html" not in text:
            text = text.replace("</body>", '<a href="langit_portal_map.html" style="display:none">LANGIT Portal Map</a></body>')
            index_patch.write_text(text, encoding="utf-8")

    root_manifest = {
        "version": VERSION,
        "engine": ENGINE_NAME,
        "generated": datetime.now(JAKARTA).isoformat(timespec="seconds"),
        "layers": LAYER_DEFS,
        "locations": [
            {
                "slug": p.slug,
                "name": p.name,
                "lat": p.lat,
                "lon": p.lon,
                "peak_rain": peak_hour(p).variables.get("rain_prob"),
                "peak_hour": peak_hour(p).hour,
                "risk": peak_hour(p).risk,
                "status": peak_hour(p).status,
                "sample_variables": p.hours[0].variables if p.hours else {},
            }
            for p in locations
        ],
    }
    write_text(root / "langit_v64_3_manifest.json", json.dumps(root_manifest, ensure_ascii=False, indent=2))
    return locations


def verify(root: Path) -> None:
    errors: list[str] = []
    if not root.exists():
        errors.append(f"Root tidak ditemukan: {root}")
    if not (root / "langit_portal_map.html").exists():
        errors.append("outputs/langit_portal_map.html belum ada.")
    if not (root / "langit_v64_3_manifest.json").exists():
        errors.append("outputs/langit_v64_3_manifest.json belum ada.")

    loc_dirs = []
    if root.exists():
        for d in root.iterdir():
            if d.is_dir() and not d.name.startswith(".") and d.name not in {"assets", "logs", "raw_payloads"}:
                if (d / "langit_api_v1.json").exists() or (d / "langit_location.geojson").exists() or list(d.glob("*.csv")) or list(d.glob("*.json")):
                    loc_dirs.append(d)

    for d in loc_dirs:
        for name in ["langit_map_room.html", "langit_map.html", "anemos_map.html", "langit_v64_3_map_manifest.json"]:
            if not (d / name).exists():
                errors.append(f"{d / name} belum ada.")

    html_files = [root / "langit_portal_map.html"]
    for d in loc_dirs:
        html_files += [d / "langit_map_room.html", d / "langit_map.html", d / "anemos_map.html"]

    required = [
        "LANGIT v64.3", "Full Variable Wind Field Map Engine", "atmos-canvas", "wind-canvas",
        "variables", "rain_prob", "humidity", "cloud", "pressure", "wind_dir", "uv",
        "fieldAt", "LAYERS"
    ]
    for path in html_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in BAD_PUBLIC_TOKENS:
            if token in text:
                errors.append(f"{path} masih mengandung token lama: {token}")
        for token in required:
            if token not in text:
                errors.append(f"{path} tidak memuat elemen wajib: {token}")

    # Verify manifests contain full variable bridge.
    for d in loc_dirs:
        man = read_json(d / "langit_v64_3_map_manifest.json")
        if isinstance(man, dict):
            vars0 = man.get("first_hour_variables") or {}
            for key in ["temp", "humidity", "rain_prob", "wind", "wind_dir", "confidence", "risk"]:
                if key not in vars0:
                    errors.append(f"{d.name}: manifest tidak membawa variable {key}")

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        raise SystemExit(3)
    print(f"OK: {ENGINE_NAME} verified. lokasi={len(loc_dirs)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=ENGINE_NAME)
    parser.add_argument("--root", default="outputs")
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if args.verify_only:
        verify(root)
        return 0
    locations = write_outputs(root, args.public_base_url, args.debug)
    print(f"OK: {ENGINE_NAME} selesai. lokasi={len(locations)}")
    verify(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
