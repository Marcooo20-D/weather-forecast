#!/usr/bin/env python3
"""
LANGIT CORE — Local Weather Intelligence Infrastructure
======================================================

Post-processor after weather_ensemble_multi_location.py.
It turns existing forecast outputs into a deeper system layer:

1. Forecast Archive       -> outputs/core/langit_core.sqlite
2. Risk Engine            -> rain/heat/humidity/wind/data-freshness risk
3. Local Correction       -> location-specific microclimate profile
4. Confidence Engine      -> source/data/temporal consistency scoring
5. Risk Field Map         -> GeoJSON grid surface, not only markers
6. Public Console         -> map-first, minimal public UI
7. Analyst Mode           -> model/source confidence and evidence
8. Operations Monitor     -> build health and output diagnostics

Usage:
  python langit_core.py --root outputs --public-base-url https://marcooo20-d.github.io/weather-forecast
  python langit_core.py --root outputs --verify-only
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

BRAND = "LANGIT"
VERSION = "LANGIT CORE"
TZ_NAME = "Asia/Jakarta"
DISCLAIMER = "Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi BMKG dan kondisi setempat."
ID_BOUNDS = [[-11.25, 94.0], [6.45, 141.25]]
MONTH_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
DAY_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    out = str(value).strip()
    if not out or out.lower() in {"none", "nan", "null", "undefined"} or out in {"—", "-", "–"}:
        return default
    return out


def num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            v = value.strip().replace("%", "").replace("°C", "").replace("km/jam", "").replace("km/h", "").replace(",", ".")
            if not v or v in {"—", "-", "–"}:
                return default
            value = v
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def clamp(value: Any, lo: float = 0, hi: float = 100, default: float = 0) -> float:
    x = num(value, default)
    if x is None:
        x = default
    return max(lo, min(hi, x))


def prob(value: Any, default: Optional[float] = None) -> Optional[float]:
    x = num(value, default)
    if x is None:
        return default
    if 0 < x < 1:
        x *= 100.0
    return clamp(x)


def hour(value: Any, default: str = "00:00") -> str:
    raw = text(value, default)
    m = re.search(r"(\d{1,2})(?::(\d{2}))?", raw)
    if not m:
        return default
    hh = max(0, min(23, int(m.group(1))))
    mm = (m.group(2) or "00")[:2]
    return f"{hh:02d}:{mm}"


def hlabel(value: Any) -> str:
    return hour(value).replace(":", ".")


def hint(value: Any) -> int:
    try:
        return int(hour(value)[:2])
    except Exception:
        return 0


def slugify(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text(value, "location").lower()).strip("-")
    return out or "location"


def now_local() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(TZ_NAME))


def parse_date(value: Any) -> Optional[dt.date]:
    raw = text(value)
    if not raw:
        return None
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](20\d{2})", raw)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except Exception:
            pass
    return None


def fmt_date(d: Optional[dt.date], long: bool = True) -> str:
    if d is None:
        return "Tanggal belum terbaca"
    if long:
        return f"{DAY_ID[d.weekday()]}, {d.day} {MONTH_ID[d.month - 1]} {d.year}"
    return f"{d.day} {MONTH_ID[d.month - 1]}"


def fmt_update(value: Any = None) -> str:
    raw = text(value)
    d = parse_date(raw)
    if d:
        return f"Diperbarui {fmt_date(d, False)}, {hlabel(raw)} WIB"
    return now_local().strftime("Diperbarui %d/%m/%Y, %H.%M WIB")


def pct(value: Any) -> str:
    x = prob(value, None)
    return "—" if x is None else f"{round(x):.0f}%"


def deg(value: Any) -> str:
    x = num(value, None)
    return "—" if x is None else f"{x:.1f}°C"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_csv_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return [dict(row) for row in csv.DictReader(f)]
    except Exception:
        return []


def write_csv_rows(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = sanitize_public_text(content)
    path.write_text(content, encoding="utf-8")


def pick(row: Dict[str, Any], *names: str, default: Any = None) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
        key = name.lower()
        if key in lower and lower[key] not in (None, ""):
            return lower[key]
    return default


def mean(values: Iterable[Any]) -> Optional[float]:
    xs = [num(v, None) for v in values]
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def maximum(values: Iterable[Any]) -> Optional[float]:
    xs = [num(v, None) for v in values]
    xs = [x for x in xs if x is not None]
    return max(xs) if xs else None

# ---------------------------------------------------------------------------
# Local atmospheric intelligence
# ---------------------------------------------------------------------------

MICROCLIMATE = {
    "dago": {
        "label": "Urban highland",
        "note": "Area lebih sejuk; awan sore dan kelembapan malam lebih sensitif.",
        "rain_afternoon": 1.12,
        "heat_weight": 0.82,
        "humidity_weight": 1.12,
        "wind_weight": 0.92,
    },
    "jatinangor": {
        "label": "Open campus basin",
        "note": "Area terbuka; siang lebih panas dan perubahan awan sore dapat cepat.",
        "rain_afternoon": 1.07,
        "heat_weight": 1.05,
        "humidity_weight": 1.03,
        "wind_weight": 1.05,
    },
    "arjawinangun": {
        "label": "Lowland heat corridor",
        "note": "Dataran rendah; suhu terasa siang lebih dominan daripada hujan ringan.",
        "rain_afternoon": 0.96,
        "heat_weight": 1.22,
        "humidity_weight": 0.96,
        "wind_weight": 1.02,
    },
}


def profile_for(slug: str, name: str) -> Dict[str, Any]:
    key = slug.lower()
    if key in MICROCLIMATE:
        return MICROCLIMATE[key]
    lname = name.lower()
    if "dago" in lname or "bandung" in lname:
        return MICROCLIMATE["dago"]
    if "jatinangor" in lname or "sumedang" in lname:
        return MICROCLIMATE["jatinangor"]
    if "arjawinangun" in lname or "cirebon" in lname:
        return MICROCLIMATE["arjawinangun"]
    return {
        "label": "Local atmosphere",
        "note": "Kondisi lokal dapat berubah lebih cepat dibanding prakiraan umum.",
        "rain_afternoon": 1.0,
        "heat_weight": 1.0,
        "humidity_weight": 1.0,
        "wind_weight": 1.0,
    }


def heat_risk(heat: Any, temp: Any = None, rh: Any = None) -> float:
    hi = num(heat, num(temp, None))
    humidity = num(rh, None)
    if hi is None:
        return 0.0
    if hi >= 41:
        score = 88
    elif hi >= 39:
        score = 76
    elif hi >= 37:
        score = 62
    elif hi >= 35:
        score = 48
    elif hi >= 33:
        score = 30
    else:
        score = 0
    if humidity is not None and humidity >= 82 and hi >= 32:
        score += 8
    return clamp(score)


def humidity_risk(rh: Any, hour_value: Any) -> float:
    r = num(rh, None)
    if r is None:
        return 0.0
    h = hint(hour_value)
    base = max(0, (r - 78) * 1.2)
    if h <= 8 or h >= 19:
        base *= 1.18
    return clamp(base)


def wind_risk(wind: Any) -> float:
    w = num(wind, None)
    if w is None:
        return 0.0
    if w <= 18:
        return 0.0
    return clamp((w - 18) * 3.2)


def data_quality_score(valid: bool, source_health: Dict[str, Any]) -> float:
    if not valid:
        return 35.0
    total = max(1, int(source_health.get("total", 0) or 0))
    active = int(source_health.get("active", 0) or 0)
    if active <= 0:
        return 55.0
    return clamp(100 - (active / total * 100), 0, 60)


def classify(score: Any, valid: bool = True) -> str:
    if not valid:
        return "limited"
    x = clamp(score)
    if x >= 75:
        return "danger"
    if x >= 55:
        return "rain"
    if x >= 27:
        return "watch"
    return "safe"


def label(cls: str) -> str:
    return {
        "safe": "Aman",
        "watch": "Dipantau",
        "rain": "Waspada",
        "danger": "Tinggi",
        "limited": "Data terbatas",
    }.get(cls, "Dipantau")


def color(cls: str) -> str:
    return {
        "safe": "#188a50",
        "watch": "#c88410",
        "rain": "#d45a18",
        "danger": "#c82842",
        "limited": "#6b7280",
    }.get(cls, "#2163d3")


def condition_from_values(hh: str, rain: Any, temp: Any, rh: Any, heat: Any, valid: bool) -> str:
    if not valid:
        return "Data terbatas"
    p = prob(rain, 0) or 0
    hi = num(heat, num(temp, None))
    r = num(rh, None)
    h = hint(hh)
    if p >= 75:
        return "Hujan lebat"
    if p >= 55:
        return "Hujan lokal"
    if p >= 35:
        return "Potensi hujan"
    if hi is not None and hi >= 36 and 10 <= h <= 16:
        return "Panas terasa"
    if r is not None and r >= 88 and (h <= 8 or h >= 19):
        return "Lembap"
    if 10 <= h <= 15:
        return "Cerah berawan"
    return "Berawan"


def source_health_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"active": 0, "total": 0, "availability": 0, "level": "belum tersedia", "rows": []}
    active = 0
    for row in rows:
        joined = " ".join(str(v).lower() for v in row.values())
        if any(token in joined for token in ["aktif", "active", "ok", "success", "true", "yes", "200"]):
            active += 1
    total = len(rows)
    availability = round(active / max(1, total) * 100)
    if availability >= 75:
        level = "tinggi"
    elif availability >= 45:
        level = "sedang"
    elif availability > 0:
        level = "rendah"
    else:
        level = "belum tersedia"
    return {"active": active, "total": total, "availability": availability, "level": level, "rows": rows}


def confidence_for(hours: List[Dict[str, Any]], source_health: Dict[str, Any]) -> Dict[str, Any]:
    valid_ratio = len([h for h in hours if h.get("valid")]) / max(1, len(hours))
    source_ratio = (source_health.get("active", 0) or 0) / max(1, source_health.get("total", 0) or 1)
    risks = [num(h.get("risk_total"), None) for h in hours if num(h.get("risk_total"), None) is not None]
    spread = 0
    if len(risks) >= 3:
        avg = sum(risks) / len(risks)
        spread = math.sqrt(sum((x - avg) ** 2 for x in risks) / len(risks))
    stability = clamp(100 - spread * 2.2)
    score = round(clamp(valid_ratio * 40 + source_ratio * 35 + stability * 0.25))
    level = "tinggi" if score >= 75 else "sedang" if score >= 50 else "rendah"
    return {"score": score, "level": level, "valid_ratio": round(valid_ratio, 3), "source_ratio": round(source_ratio, 3), "stability": round(stability, 1)}


def point_from_row(row: Dict[str, Any], fallback_date: dt.date, relative: str, profile: Dict[str, Any], source_health: Dict[str, Any]) -> Dict[str, Any]:
    hh = hour(pick(row, "hour", "jam", "time", "local_time", "target_hour", "datetime", "timestamp", default="00:00"))
    date_value = parse_date(pick(row, "date", "date_iso", "tanggal", "target_date", "valid_date", "forecast_date", "datetime", "timestamp")) or fallback_date
    temp = num(pick(row, "temp_c", "temperature_c", "temperature_2m_c", "avg_temperature_c", "t2m", "suhu"))
    rh = num(pick(row, "humidity_pct", "relative_humidity", "relative_humidity_2m", "rh", "kelembapan"))
    heat = num(pick(row, "heat_index_c", "apparent_temperature_c", "feels_like_c", "terasa"), temp)
    rain = prob(pick(row, "rain_probability", "rain_probability_raw", "precip_probability", "precipitation_probability", "pop", "hujan"))
    wind = num(pick(row, "wind_kmh", "wind_speed_kmh", "wind_speed_10m_kmh", "angin"))
    valid = any(v is not None for v in [temp, rh, heat, rain, wind])

    h = hint(hh)
    rain_r = prob(rain, 0) or 0
    if 12 <= h <= 18:
        rain_r *= float(profile.get("rain_afternoon", 1.0))
    heat_r = heat_risk(heat, temp, rh) * float(profile.get("heat_weight", 1.0))
    hum_r = humidity_risk(rh, hh) * float(profile.get("humidity_weight", 1.0))
    wind_r = wind_risk(wind) * float(profile.get("wind_weight", 1.0))
    data_r = data_quality_score(valid, source_health)
    risk_total = clamp(max(rain_r, heat_r, hum_r * 0.70, wind_r) + (data_r * 0.16 if valid else data_r * 0.70))
    cls = classify(risk_total, valid)
    drivers = {
        "hujan": round(clamp(rain_r), 1),
        "panas": round(clamp(heat_r), 1),
        "lembap": round(clamp(hum_r), 1),
        "angin": round(clamp(wind_r), 1),
        "data": round(clamp(data_r), 1),
    }
    primary = sorted(drivers.items(), key=lambda x: x[1], reverse=True)[0][0]

    return {
        "date_iso": date_value.isoformat(),
        "date_label": fmt_date(date_value),
        "date_short": fmt_date(date_value, False),
        "relative": relative,
        "hour": hh,
        "hour_label": hlabel(hh),
        "temp_c": temp,
        "rh_pct": rh,
        "heat_index_c": heat,
        "rain_probability": prob(rain, None),
        "wind_kmh": wind,
        "rain_risk": round(clamp(rain_r), 1),
        "heat_risk": round(clamp(heat_r), 1),
        "humidity_risk": round(clamp(hum_r), 1),
        "wind_risk": round(clamp(wind_r), 1),
        "data_risk": round(clamp(data_r), 1),
        "risk_total": round(risk_total, 1),
        "risk_class": cls,
        "risk_label": label(cls),
        "risk_driver": primary,
        "drivers": drivers,
        "condition": text(pick(row, "condition", "weather", "cuaca", "summary"), "") or condition_from_values(hh, rain, temp, rh, heat, valid),
        "valid": bool(valid),
    }

# ---------------------------------------------------------------------------
# Load existing outputs
# ---------------------------------------------------------------------------

def metadata_by_slug(root: Path) -> Dict[str, Dict[str, Any]]:
    meta: Dict[str, Dict[str, Any]] = {}
    for name in ["dim_locations.csv", "locations.csv", "dim_location.csv"]:
        for row in read_csv_rows(root / name):
            slug = text(pick(row, "slug", "location_slug"), "") or slugify(text(pick(row, "location_name", "name"), "location"))
            meta.setdefault(slug, {}).update(row)
    for name in ["langit_all_locations.geojson", "anemos_all_locations.geojson"]:
        gj = read_json(root / name, {}) or {}
        for feat in gj.get("features", []) if isinstance(gj, dict) else []:
            props = feat.get("properties") or {}
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            slug = text(props.get("slug") or props.get("location_slug") or slugify(props.get("location_name") or props.get("name") or ""))
            if slug:
                meta.setdefault(slug, {}).update({
                    "slug": slug,
                    "location_name": props.get("location_name") or props.get("name") or meta.get(slug, {}).get("location_name"),
                    "longitude": coords[0] if len(coords) >= 1 else meta.get(slug, {}).get("longitude"),
                    "latitude": coords[1] if len(coords) >= 2 else meta.get(slug, {}).get("latitude"),
                })
    return meta


def location_dirs(root: Path) -> List[Path]:
    if not root.exists():
        return []
    sentinel = ["anemos_app.html", "langit_app.html", "langit_api_v1.json", "anemos_api_v1.json", "langit_hourly_intelligence.csv", "anemos_hourly_compact.csv", "forecast.csv"]
    out = []
    for child in root.iterdir():
        if child.is_dir() and any((child / s).exists() for s in sentinel):
            out.append(child)
    return sorted(out, key=lambda p: p.name)


def rows_from_api(api: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if isinstance(api.get("days"), list):
        for day in api["days"][:3]:
            if not isinstance(day, dict):
                continue
            date_val = day.get("date") or day.get("date_iso") or day.get("target_date")
            rel = day.get("relative") or day.get("day_tag") or day.get("label")
            for key in ["hours", "hourly", "key_hours", "rows", "forecast"]:
                items = day.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            x = dict(item)
                            x.setdefault("date", date_val)
                            x.setdefault("relative", rel)
                            rows.append(x)
                    break
    for key in ["hours", "hourly", "key_hours", "forecast"]:
        if isinstance(api.get(key), list):
            rows.extend([dict(x) for x in api[key] if isinstance(x, dict)])
            break
    return rows


def raw_rows_for_location(directory: Path, api: Dict[str, Any]) -> List[Dict[str, Any]]:
    for name in ["langit_hourly_intelligence.csv", "anemos_hourly_compact.csv", "anemos_risk_timeline.csv", "forecast.csv"]:
        rows = read_csv_rows(directory / name)
        if rows:
            return rows
    return rows_from_api(api)


def split_day_chunks(rows: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    if not rows:
        return []
    dated: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        d = parse_date(pick(row, "date", "date_iso", "tanggal", "target_date", "valid_date", "forecast_date", "datetime", "timestamp"))
        if d:
            dated.setdefault(d.isoformat(), []).append(row)
    if dated:
        return [dated[k] for k in sorted(dated.keys())[:3]]
    tagged: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        tag = text(pick(row, "relative", "relative_day", "day_tag", "hari", "day"), "")
        if tag:
            tagged.setdefault(tag.lower(), []).append(row)
    if len(tagged) > 1:
        order = ["hari ini", "today", "besok", "tomorrow", "lusa"]
        keys = sorted(tagged.keys(), key=lambda k: order.index(k) if k in order else 99)
        return [tagged[k] for k in keys[:3]]
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    last_h = -1
    for row in rows:
        h = hint(pick(row, "hour", "jam", "time", "local_time", "datetime", "timestamp", default="00:00"))
        if current and h < last_h:
            chunks.append(current)
            current = []
        current.append(row)
        last_h = h
    if current:
        chunks.append(current)
    return chunks[:3]


def default_rows(date_value: dt.date, relative: str) -> List[Dict[str, Any]]:
    return [{"date": date_value.isoformat(), "relative": relative, "hour": h} for h in ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]]


def summarize_day(relative: str, date_value: dt.date, points: List[Dict[str, Any]]) -> Dict[str, Any]:
    points = sorted(points, key=lambda p: hint(p["hour"]))
    valid = [p for p in points if p.get("valid")]
    basis = valid or points
    peak = max(basis, key=lambda p: prob(p.get("rain_probability"), -1) if prob(p.get("rain_probability"), None) is not None else -1) if basis else {}
    worst = max(basis, key=lambda p: num(p.get("risk_total"), 0) or 0) if basis else {}
    cls = "limited" if not valid else worst.get("risk_class", "watch")
    safe_hours = sorted({hint(p["hour"]) for p in points if p.get("risk_class") == "safe" and p.get("valid")})
    if not safe_hours:
        safe_hours = sorted({hint(p["hour"]) for p in points if p.get("risk_class") in {"safe", "watch"}})
    windows = compact_windows(safe_hours)
    confidence = confidence_for(points, {"active": 0, "total": 0})  # overwritten later at location level
    return {
        "relative": relative,
        "date_iso": date_value.isoformat(),
        "date_label": fmt_date(date_value),
        "date_short": fmt_date(date_value, False),
        "points": points,
        "peak_rain_probability": prob(peak.get("rain_probability"), None),
        "peak_rain_hour": peak.get("hour", "—"),
        "risk_total": round(num(worst.get("risk_total"), 35 if cls == "limited" else 0) or 0, 1),
        "risk_class": cls,
        "risk_label": label(cls),
        "risk_driver": worst.get("risk_driver", "data" if cls == "limited" else "hujan"),
        "condition": worst.get("condition", "Data terbatas" if cls == "limited" else "Berawan"),
        "avg_temp_c": mean(p.get("temp_c") for p in valid),
        "avg_rh_pct": mean(p.get("rh_pct") for p in valid),
        "max_heat_index_c": maximum(p.get("heat_index_c") for p in valid),
        "max_wind_kmh": maximum(p.get("wind_kmh") for p in valid),
        "safe_windows": windows,
        "valid_points": len(valid),
        "confidence": confidence,
    }


def compact_windows(hours: List[int]) -> List[str]:
    if not hours:
        return []
    groups: List[Tuple[int, int]] = []
    start = prev = hours[0]
    for h in hours[1:]:
        if h <= prev + 3:
            prev = h
        else:
            groups.append((start, prev))
            start = prev = h
    groups.append((start, prev))
    return [f"{a:02d}.00" if a == b else f"{a:02d}.00–{b:02d}.00" for a, b in groups[:3]]


def load_location(directory: Path, meta: Dict[str, Any]) -> Dict[str, Any]:
    api: Dict[str, Any] = {}
    for name in ["langit_api_v1.json", "anemos_api_v1.json", "api.json"]:
        obj = read_json(directory / name, {})
        if isinstance(obj, dict) and obj:
            api = obj
            break
    slug = text(api.get("location_slug"), text(meta.get("slug"), directory.name))
    name = text(api.get("location_name"), text(meta.get("location_name"), slug.replace("-", " ").title()))
    lat = num(api.get("latitude"), num(meta.get("latitude"), num(meta.get("lat"))))
    lon = num(api.get("longitude"), num(meta.get("longitude"), num(meta.get("lon"))))
    if lat is None or lon is None:
        gj = read_json(directory / "langit_location.geojson", {}) or {}
        feats = gj.get("features") or []
        if feats:
            coords = (feats[0].get("geometry") or {}).get("coordinates") or []
            if len(coords) >= 2:
                lon = num(coords[0], lon)
                lat = num(coords[1], lat)
    source_rows: List[Dict[str, Any]] = []
    for fname in ["source_status.csv", "source_status_all_locations.csv", "langit_source_status.csv"]:
        source_rows = read_csv_rows(directory / fname)
        if source_rows:
            break
    source_health = source_health_from_rows(source_rows)
    profile = profile_for(slug, name)
    raw_rows = raw_rows_for_location(directory, api)
    base_date = parse_date(api.get("target_date") or api.get("date") or api.get("generated_at") or api.get("updated_at")) or now_local().date()
    chunks = split_day_chunks(raw_rows)
    relatives = ["Hari ini", "Besok", "Lusa"]
    days: List[Dict[str, Any]] = []
    for i in range(3):
        date_value = base_date + dt.timedelta(days=i)
        rows = chunks[i] if i < len(chunks) else default_rows(date_value, relatives[i])
        points = [point_from_row(row, date_value, relatives[i], profile, source_health) for row in rows]
        day = summarize_day(relatives[i], date_value, points)
        day["confidence"] = confidence_for(points, source_health)
        days.append(day)
    return {
        "brand": BRAND,
        "version": VERSION,
        "generated_at": fmt_update(api.get("generated_at") or api.get("updated_at")),
        "location_slug": slug,
        "location_name": name,
        "latitude": lat,
        "longitude": lon,
        "profile": profile,
        "source_health": source_health,
        "days": days,
        "today": days[0],
        "sources": source_rows,
    }

# ---------------------------------------------------------------------------
# Archive and data products
# ---------------------------------------------------------------------------

def run_id() -> str:
    return now_local().strftime("%Y%m%d_%H%M%S")


def create_archive(root: Path, locations: List[Dict[str, Any]], public_base_url: str) -> str:
    core_dir = root / "core"
    core_dir.mkdir(parents=True, exist_ok=True)
    db_path = core_dir / "langit_core.sqlite"
    rid = run_id()
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS run_info(
            run_id TEXT PRIMARY KEY, created_at TEXT, public_base_url TEXT, location_count INTEGER, version TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS forecast_point(
            run_id TEXT, slug TEXT, location_name TEXT, date_iso TEXT, hour TEXT,
            temp_c REAL, rh_pct REAL, heat_index_c REAL, rain_probability REAL, wind_kmh REAL,
            rain_risk REAL, heat_risk REAL, humidity_risk REAL, wind_risk REAL, data_risk REAL,
            risk_total REAL, risk_class TEXT, confidence_score REAL, confidence_level TEXT,
            risk_driver TEXT, valid INTEGER
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS source_status(
            run_id TEXT, slug TEXT, source TEXT, status TEXT, active INTEGER, raw_json TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS verification_placeholder(
            run_id TEXT, slug TEXT, required_pairs INTEGER, available_pairs INTEGER, status TEXT
        )""")
        cur.execute("INSERT OR REPLACE INTO run_info VALUES(?,?,?,?,?)", (rid, now_local().isoformat(), public_base_url, len(locations), VERSION))
        for loc in locations:
            sh = loc.get("source_health", {})
            for day in loc.get("days", []):
                conf = day.get("confidence", {})
                for p in day.get("points", []):
                    cur.execute("""INSERT INTO forecast_point VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        rid, loc["location_slug"], loc["location_name"], p.get("date_iso"), p.get("hour"),
                        p.get("temp_c"), p.get("rh_pct"), p.get("heat_index_c"), p.get("rain_probability"), p.get("wind_kmh"),
                        p.get("rain_risk"), p.get("heat_risk"), p.get("humidity_risk"), p.get("wind_risk"), p.get("data_risk"),
                        p.get("risk_total"), p.get("risk_class"), conf.get("score"), conf.get("level"), p.get("risk_driver"), int(bool(p.get("valid")))
                    ))
            for row in loc.get("sources", []):
                source = str(pick(row, "source", "source_id", "model", "name", default="unknown"))
                joined = " ".join(str(v).lower() for v in row.values())
                active = 1 if any(token in joined for token in ["aktif", "active", "ok", "success", "true", "yes", "200"]) else 0
                status = str(pick(row, "status", "verdict", "active", "ok", default=""))
                cur.execute("INSERT INTO source_status VALUES(?,?,?,?,?,?)", (rid, loc["location_slug"], source, status, active, json.dumps(row, ensure_ascii=False)))
            cur.execute("INSERT INTO verification_placeholder VALUES(?,?,?,?,?)", (rid, loc["location_slug"], 30, 0, "belum cukup data"))
        con.commit()
    finally:
        con.close()
    return rid


def square_polygon(lon: float, lat: float, half: float) -> List[List[float]]:
    return [[lon-half, lat-half], [lon+half, lat-half], [lon+half, lat+half], [lon-half, lat+half], [lon-half, lat-half]]


def risk_surface_for_location(loc: Dict[str, Any]) -> Dict[str, Any]:
    lat = num(loc.get("latitude"))
    lon = num(loc.get("longitude"))
    if lat is None or lon is None:
        lat, lon = -6.2, 106.8
    features: List[Dict[str, Any]] = []
    # 5x5 local field, about 1.2 km grid spacing near equator.
    offsets = [-2, -1, 0, 1, 2]
    half = 0.0055
    for day in loc.get("days", [])[:3]:
        for p in day.get("points", []):
            for iy in offsets:
                for ix in offsets:
                    dist = math.sqrt(ix*ix + iy*iy)
                    influence = max(0.55, 1.0 - dist * 0.12)
                    # synthetic surface from point risk, shaped by microclimate. It is explicitly a local risk field, not radar observation.
                    base = clamp(p.get("risk_total"), default=35)
                    rain_v = clamp(p.get("rain_risk"), default=0) * influence
                    heat_v = clamp(p.get("heat_risk"), default=0) * (1.05 if ix >= 0 else 0.96) * influence
                    hum_v = clamp(p.get("humidity_risk"), default=0) * (1.04 if iy >= 0 else 0.97) * influence
                    risk_v = clamp(max(rain_v, heat_v, hum_v, base * influence))
                    cls = classify(risk_v, bool(p.get("valid")))
                    c_lon = lon + ix * 0.012
                    c_lat = lat + iy * 0.012
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [square_polygon(c_lon, c_lat, half)]},
                        "properties": {
                            "slug": loc["location_slug"],
                            "location_name": loc["location_name"],
                            "relative": day.get("relative"),
                            "date": day.get("date_label"),
                            "date_iso": day.get("date_iso"),
                            "hour": p.get("hour"),
                            "hour_label": p.get("hour_label"),
                            "risk": round(risk_v, 1),
                            "rain": round(clamp(rain_v), 1),
                            "heat": round(clamp(heat_v), 1),
                            "humidity": round(clamp(hum_v), 1),
                            "confidence": day.get("confidence", {}).get("score"),
                            "risk_class": cls,
                            "risk_label": label(cls),
                            "condition": p.get("condition"),
                        }
                    })
    # Add point marker features.
    for day in loc.get("days", [])[:1]:
        for p in day.get("points", []):
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "kind": "center",
                    "slug": loc["location_slug"],
                    "location_name": loc["location_name"],
                    "relative": day.get("relative"),
                    "date": day.get("date_label"),
                    "hour": p.get("hour"),
                    "hour_label": p.get("hour_label"),
                    "risk": p.get("risk_total"),
                    "rain": p.get("rain_probability"),
                    "heat": p.get("heat_index_c"),
                    "confidence": day.get("confidence", {}).get("score"),
                    "risk_class": p.get("risk_class"),
                    "risk_label": p.get("risk_label"),
                    "condition": p.get("condition"),
                }
            })
    return {"type": "FeatureCollection", "features": features}


def regional_surface(locations: List[Dict[str, Any]]) -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []
    for loc in locations:
        lat = num(loc.get("latitude"))
        lon = num(loc.get("longitude"))
        if lat is None or lon is None:
            continue
        d = loc["today"]
        p = max(d.get("points", []), key=lambda x: num(x.get("risk_total"), 0) or 0) if d.get("points") else {}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "kind": "center",
                "slug": loc["location_slug"],
                "location_name": loc["location_name"],
                "date": d.get("date_label"),
                "hour": d.get("peak_rain_hour"),
                "hour_label": hlabel(d.get("peak_rain_hour")),
                "risk": d.get("risk_total"),
                "rain": d.get("peak_rain_probability"),
                "heat": d.get("max_heat_index_c"),
                "confidence": d.get("confidence", {}).get("score"),
                "risk_class": d.get("risk_class"),
                "risk_label": d.get("risk_label"),
                "condition": d.get("condition"),
            }
        })
        features.extend(risk_surface_for_location(loc)["features"][:25])
    return {"type": "FeatureCollection", "features": features}

# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

BASE_CSS = r'''
:root{--bg:#f3f6fa;--panel:#ffffff;--ink:#0b1e33;--muted:#637184;--line:#d9e3ee;--blue:#1657c8;--green:#188a50;--yellow:#c88410;--orange:#d45a18;--red:#c82842;--gray:#6b7280;--shadow:0 14px 40px rgba(11,30,51,.08);--radius:20px}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,Roboto,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.45}a{text-decoration:none;color:inherit}.top{height:66px;background:rgba(255,255,255,.94);border-bottom:1px solid var(--line);backdrop-filter:blur(12px);position:sticky;top:0;z-index:40}.topin{width:min(1440px,calc(100% - 28px));height:100%;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{display:flex;align-items:center;gap:12px}.mark{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#0b3f9f,#4bb8ff)}.brand b{display:block;letter-spacing:-.03em}.brand span{font-size:12px;color:var(--muted)}.nav{display:flex;gap:8px;flex-wrap:wrap}.nav a{font-weight:700;font-size:13px;padding:8px 11px;border-radius:999px;color:#334155}.nav a.on{background:#e7efff;color:var(--blue)}.shell{width:min(1440px,calc(100% - 28px));margin:0 auto;padding:22px 0 58px}.console{display:grid;grid-template-columns:minmax(0,1.42fr) 380px;gap:16px;align-items:stretch}.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}.mapbox{min-height:610px;overflow:hidden}.mapbox iframe{display:block;width:100%;height:100%;min-height:610px;border:0}.decision{padding:22px;display:flex;flex-direction:column;gap:14px}.kicker{font-size:13px;color:var(--muted);font-weight:700;margin:0}.decision h1{font-size:38px;line-height:1.02;margin:0;letter-spacing:-.05em}.status{display:inline-flex;align-items:center;width:max-content;border-radius:999px;padding:6px 10px;font-weight:800;font-size:13px}.safe{color:#11693b;background:#dcfce7}.watch{color:#7a5208;background:#fef3c7}.rain{color:#9a3412;background:#ffedd5}.danger{color:#991b1b;background:#fee2e2}.limited{color:#374151;background:#f3f4f6}.metricgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.metric{border:1px solid var(--line);border-radius:15px;padding:13px;background:#f8fbff}.metric small{display:block;color:var(--muted);font-weight:700}.metric b{font-size:26px;display:block;line-height:1.1;margin-top:5px}.strip{display:flex;gap:8px;overflow:auto;padding:14px}.stripcell{min-width:58px;text-align:center}.bar{height:var(--h);min-height:8px;background:var(--c);border-radius:8px 8px 3px 3px;margin:8px auto;width:100%}.stripcell b{font-size:13px}.stripcell small{display:block;color:var(--muted);font-size:12px}.section{margin-top:16px}.head{padding:18px 20px 0;display:flex;justify-content:space-between;gap:12px;align-items:flex-end}.head h2{margin:0;font-size:22px;letter-spacing:-.03em}.head p{margin:0;color:var(--muted);font-size:13px}.grid{display:grid;gap:12px;padding:18px 20px 20px}.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:repeat(3,1fr)}.g4{grid-template-columns:repeat(4,1fr)}.card{border:1px solid var(--line);border-radius:16px;background:#fff;padding:16px}.card h3{margin:0 0 8px}.card p{margin:0;color:#334155}.matrix{width:100%;border-collapse:collapse}.matrix th,.matrix td{padding:12px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}.matrix th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.drawer{padding:18px 20px 20px}.drawer details{border:1px solid var(--line);border-radius:14px;padding:13px;background:#fff}.drawer summary{font-weight:800;cursor:pointer}.evidence{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.ev{border-left:4px solid var(--blue);background:#f8fbff;border-radius:12px;padding:12px}.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:28px}.ops{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.btn{display:inline-flex;padding:9px 12px;border:1px solid var(--line);border-radius:10px;background:#fff;font-weight:750;color:#334155}.btn.primary{background:var(--blue);color:#fff;border-color:var(--blue)}@media(max-width:1050px){.console{grid-template-columns:1fr}.mapbox,.mapbox iframe{min-height:480px}.g2,.g3,.g4,.ops,.evidence{grid-template-columns:1fr}.top{height:auto}.topin{padding:12px 0;align-items:flex-start;flex-direction:column}.nav{justify-content:flex-start}}@media(max-width:640px){.shell,.topin{width:calc(100% - 20px)}.decision h1{font-size:31px}.metricgrid{grid-template-columns:1fr 1fr}.mapbox,.mapbox iframe{min-height:390px}.nav a{font-size:12px;padding:7px 9px}}
'''


def cls_name(cls: str) -> str:
    return cls if cls in {"safe", "watch", "rain", "danger", "limited"} else "watch"


def mini_status(cls: str, value: str = "") -> str:
    return f'<span class="status {esc(cls_name(cls))}">{esc(value or label(cls))}</span>'


def html_doc(title: str, nav_active: str, body: str, root_rel: str = "") -> str:
    def n(label_text: str, href: str, key: str) -> str:
        return f'<a class="{"on" if nav_active==key else ""}" href="{root_rel}{href}">{esc(label_text)}</a>'
    nav = n("Ringkasan", "index.html" if root_rel else "anemos_app.html", "overview")
    if not root_rel:
        nav += n("Peta", "langit_map_room.html", "map") + n("Outlook", "anemos_3day.html", "outlook") + n("Aktivitas", "anemos_activity.html", "activity") + n("Keandalan", "langit_model_court.html", "analyst")
    else:
        nav += n("Peta", "langit_portal_map.html", "map") + n("Operasional", "ops.html", "ops")
    return f'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><meta name="theme-color" content="#f3f6fa"><style>{BASE_CSS}</style></head><body><header class="top"><div class="topin"><a class="brand" href="{root_rel}index.html"><span class="mark"></span><span><b>LANGIT</b><span>Local Weather Intelligence</span></span></a><nav class="nav">{nav}</nav></div></header><main class="shell">{body}<p class="footer">LANGIT CORE · {esc(DISCLAIMER)}</p></main></body></html>'''


def decision_sentence(loc: Dict[str, Any], day: Dict[str, Any]) -> str:
    cls = day.get("risk_class", "watch")
    peak = hlabel(day.get("peak_rain_hour"))
    rain = pct(day.get("peak_rain_probability"))
    driver = day.get("risk_driver", "hujan")
    if cls == "limited":
        return "Data terbatas. Periksa kondisi setempat sebelum mengambil keputusan."
    if cls == "danger":
        return f"Risiko tinggi. Hindari aktivitas luar ruang pada sekitar {peak} WIB."
    if cls == "rain":
        return f"Waspada hujan lokal. Jam perhatian utama sekitar {peak} WIB."
    if cls == "watch":
        if driver == "panas":
            return "Aktivitas masih memungkinkan. Perhatikan panas siang."
        return f"Masih bisa digunakan untuk aktivitas. Pantau sekitar {peak} WIB."
    return f"Aman. Peluang hujan tertinggi {rain} sekitar {peak} WIB."


def weather_strip(day: Dict[str, Any]) -> str:
    cells = []
    for p in day.get("points", [])[:12]:
        cls = cls_name(p.get("risk_class", "watch"))
        risk = clamp(p.get("risk_total"), default=0)
        height = max(8, round(8 + risk * 0.9))
        cells.append(f'<div class="stripcell"><b>{pct(p.get("rain_probability"))}</b><div class="bar" style="--h:{height}px;--c:{color(cls)}"></div><small>{esc(p.get("hour_label"))}</small></div>')
    return f'<div class="strip">{"".join(cells)}</div>'


def evidence(day: Dict[str, Any]) -> str:
    p = max(day.get("points", []), key=lambda x: num(x.get("risk_total"), 0) or 0) if day.get("points") else {}
    items = [
        ("Driver", day.get("risk_driver", "—")),
        ("Confidence", day.get("confidence", {}).get("level", "—")),
        ("Data valid", f'{day.get("valid_points",0)}/{len(day.get("points",[]))} jam'),
        ("Risk", f'{round(clamp(day.get("risk_total"))):.0f}/100'),
    ]
    return '<div class="evidence">' + ''.join(f'<div class="ev"><b>{esc(a)}</b><p>{esc(b)}</p></div>' for a,b in items) + '</div>'


def activity_matrix(day: Dict[str, Any]) -> str:
    periods = {
        "Pagi": [p for p in day.get("points", []) if 5 <= hint(p.get("hour")) <= 10],
        "Siang": [p for p in day.get("points", []) if 11 <= hint(p.get("hour")) <= 14],
        "Sore": [p for p in day.get("points", []) if 15 <= hint(p.get("hour")) <= 18],
        "Malam": [p for p in day.get("points", []) if hint(p.get("hour")) >= 19 or hint(p.get("hour")) <= 4],
    }
    def pstat(kind: str, rows: List[Dict[str, Any]]) -> Tuple[str,str]:
        if not rows:
            return "limited", "—"
        worst = max(rows, key=lambda p: num(p.get("risk_total"), 0) or 0)
        base_cls = worst.get("risk_class", "watch")
        heat = maximum(p.get("heat_index_c") for p in rows) or 0
        rain = maximum(p.get("rain_probability") for p in rows) or 0
        if kind == "Jemur" and rain >= 25:
            return "watch", "Kurang ideal"
        if kind == "Olahraga" and heat >= 36:
            return "watch", "Batasi"
        if kind == "Motor" and rain >= 35:
            return "rain", "Siapkan jas"
        return base_cls, label(base_cls)
    acts = ["Motor", "Jalan kaki", "Jemur", "Olahraga", "Foto", "Outdoor"]
    rows_html = []
    for act in acts:
        tds = []
        for pname in ["Pagi", "Siang", "Sore", "Malam"]:
            c, v = pstat(act, periods[pname])
            tds.append(f'<td>{mini_status(c, v)}</td>')
        rows_html.append(f'<tr><th>{esc(act)}</th>{"".join(tds)}</tr>')
    return f'<table class="matrix"><thead><tr><th>Aktivitas</th><th>Pagi</th><th>Siang</th><th>Sore</th><th>Malam</th></tr></thead><tbody>{"".join(rows_html)}</tbody></table>'


def console_page(loc: Dict[str, Any]) -> str:
    day = loc["today"]
    cls = cls_name(day.get("risk_class"))
    body = f'''
<section class="console">
  <div class="panel mapbox"><iframe src="langit_map_room.html" loading="lazy"></iframe></div>
  <aside class="panel decision">
    <p class="kicker">{esc(day.get("date_label"))} · {esc(loc.get("generated_at"))}</p>
    <h1>{esc(loc["location_name"])}</h1>
    {mini_status(cls, day.get("risk_label"))}
    <p>{esc(decision_sentence(loc, day))}</p>
    <div class="metricgrid">
      <div class="metric"><small>Risk</small><b>{round(clamp(day.get("risk_total"))):.0f}/100</b></div>
      <div class="metric"><small>Hujan maks.</small><b>{pct(day.get("peak_rain_probability"))}</b></div>
      <div class="metric"><small>Jam perhatian</small><b>{esc(hlabel(day.get("peak_rain_hour")))}</b></div>
      <div class="metric"><small>Confidence</small><b>{esc(day.get("confidence",{}).get("level","—"))}</b></div>
    </div>
    <div class="card"><h3>{esc(loc.get("profile",{}).get("label","Local atmosphere"))}</h3><p>{esc(loc.get("profile",{}).get("note",""))}</p></div>
  </aside>
</section>
<section class="panel section"><div class="head"><h2>Weather strip</h2><p>{esc(day.get("date_label"))}</p></div>{weather_strip(day)}</section>
<section class="panel section"><div class="head"><h2>Activity matrix</h2><p>Keputusan per periode</p></div><div class="drawer">{activity_matrix(day)}</div></section>
<section class="panel section"><div class="head"><h2>Dasar keputusan</h2><p>Ringkasan evidence</p></div><div class="drawer">{evidence(day)}</div></section>
<section class="panel section"><div class="drawer"><details><summary>Detail per jam</summary>{hourly_table(day)}</details></div></section>
'''
    return html_doc(f"LANGIT — {loc['location_name']}", "overview", body)


def hourly_table(day: Dict[str, Any]) -> str:
    rows = []
    for p in day.get("points", []):
        rows.append(f'<tr><td>{esc(p.get("hour_label"))}</td><td>{esc(p.get("condition"))}</td><td>{deg(p.get("temp_c"))}</td><td>{deg(p.get("heat_index_c"))}</td><td>{pct(p.get("rain_probability"))}</td><td>{mini_status(p.get("risk_class"), p.get("risk_label"))}</td></tr>')
    return f'<div style="overflow:auto;margin-top:14px"><table class="matrix"><thead><tr><th>Jam</th><th>Kondisi</th><th>Suhu</th><th>Terasa</th><th>Hujan</th><th>Status</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def outlook_page(loc: Dict[str, Any]) -> str:
    cards = []
    strips = []
    for d in loc.get("days", []):
        c = cls_name(d.get("risk_class"))
        cards.append(f'<div class="card"><p class="kicker">{esc(d.get("relative"))} · {esc(d.get("date_label"))}</p><h3>{esc(d.get("risk_label"))}</h3><p>{esc(decision_sentence(loc,d))}</p><div class="metricgrid" style="margin-top:12px"><div class="metric"><small>Hujan</small><b>{pct(d.get("peak_rain_probability"))}</b></div><div class="metric"><small>Jam</small><b>{esc(hlabel(d.get("peak_rain_hour")))}</b></div></div></div>')
        strips.append(f'<section class="panel section"><div class="head"><h2>{esc(d.get("relative"))}</h2><p>{esc(d.get("date_label"))}</p></div>{weather_strip(d)}</section>')
    body = f'<section class="panel"><div class="head"><h2>Outlook 3 hari</h2><p>{esc(loc["location_name"])}</p></div><div class="grid g3">{"".join(cards)}</div></section>' + ''.join(strips)
    return html_doc(f"LANGIT Outlook — {loc['location_name']}", "outlook", body)


def activity_page(loc: Dict[str, Any]) -> str:
    d = loc["today"]
    body = f'<section class="panel"><div class="head"><h2>Activity matrix</h2><p>{esc(loc["location_name"])} · {esc(d.get("date_label"))}</p></div><div class="drawer">{activity_matrix(d)}</div></section><section class="panel section"><div class="head"><h2>Weather strip</h2><p>Jam risiko</p></div>{weather_strip(d)}</section><section class="panel section"><div class="drawer"><details open><summary>Detail per jam</summary>{hourly_table(d)}</details></div></section>'
    return html_doc(f"LANGIT Aktivitas — {loc['location_name']}", "activity", body)


def analyst_page(loc: Dict[str, Any]) -> str:
    sh = loc.get("source_health", {})
    d = loc["today"]
    rows = ''
    for r in loc.get("sources", [])[:30]:
        rows += f'<tr><td>{esc(pick(r,"source","source_id","model","name",default="—"))}</td><td>{esc(pick(r,"provider","origin",default="—"))}</td><td>{esc(pick(r,"status","verdict","active","ok",default="—"))}</td><td>{esc(pick(r,"duration_ms","latency_ms","ms",default="—"))}</td></tr>'
    if not rows:
        rows = '<tr><td colspan="4">Source table belum tersedia.</td></tr>'
    body = f'''
<section class="panel"><div class="head"><h2>Analyst mode</h2><p>{esc(loc["location_name"])}</p></div><div class="grid g4">
<div class="metric"><small>Sumber aktif</small><b>{sh.get("active",0)}/{sh.get("total",0) or "—"}</b></div>
<div class="metric"><small>Ketersediaan</small><b>{sh.get("availability",0) if sh.get("total") else "—"}%</b></div>
<div class="metric"><small>Confidence</small><b>{esc(d.get("confidence",{}).get("level","—"))}</b></div>
<div class="metric"><small>Stability</small><b>{esc(d.get("confidence",{}).get("stability","—"))}</b></div>
</div></section>
<section class="panel section"><div class="head"><h2>Model/source court</h2><p>Detail teknis</p></div><div class="drawer"><table class="matrix"><thead><tr><th>Model</th><th>Sumber</th><th>Status</th><th>Latency</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section class="panel section"><div class="head"><h2>Risk drivers</h2><p>{esc(d.get("date_label"))}</p></div><div class="drawer">{evidence(d)}</div></section>
'''
    return html_doc(f"LANGIT Analyst — {loc['location_name']}", "analyst", body)


def reliability_page(loc: Dict[str, Any]) -> str:
    body = f'<section class="panel"><div class="head"><h2>Keandalan prakiraan</h2><p>{esc(loc["location_name"])}</p></div><div class="grid g3"><div class="metric"><small>Observasi cocok</small><b>0/30</b></div><div class="metric"><small>Status</small><b>Belum cukup</b></div><div class="metric"><small>Mode</small><b>Aktif arsip</b></div></div><div class="drawer"><p>Sistem arsip sudah dibuat. Metrik verifikasi aktif setelah observasi terkumpul dan dicocokkan dengan prakiraan.</p></div></section>'
    return html_doc(f"LANGIT Keandalan — {loc['location_name']}", "analyst", body)

# ---------------------------------------------------------------------------
# Leaflet map
# ---------------------------------------------------------------------------

MAP_CSS = r'''
html,body,#map{height:100%;margin:0;background:#eef3f8;font-family:Inter,Roboto,system-ui,-apple-system,"Segoe UI",sans-serif;color:#0b1e33}.panel{position:absolute;z-index:900;left:18px;top:18px;width:min(380px,calc(100% - 36px));background:rgba(255,255,255,.96);border:1px solid #d8e3ee;border-radius:16px;padding:15px;box-shadow:0 12px 36px rgba(11,30,51,.14)}.panel h1{font-size:20px;margin:0 0 5px}.panel p{margin:0;color:#637184;font-size:13px}.btn{display:inline-block;margin-top:12px;padding:8px 11px;border-radius:10px;background:#1657c8;color:white;text-decoration:none;font-weight:800;font-size:13px}.timebar{position:absolute;z-index:900;left:50%;bottom:18px;transform:translateX(-50%);max-width:calc(100% - 32px);overflow:auto;display:flex;gap:7px;background:rgba(255,255,255,.96);border:1px solid #d8e3ee;border-radius:999px;padding:9px;box-shadow:0 12px 36px rgba(11,30,51,.12)}.timebar button,.layerbar button{border:1px solid #d8e3ee;border-radius:999px;background:white;padding:8px 11px;font-weight:800;cursor:pointer}.timebar button.active,.layerbar button.active{background:#1657c8;color:white;border-color:#1657c8}.layerbar{position:absolute;z-index:900;right:18px;top:18px;display:flex;gap:7px;background:rgba(255,255,255,.96);border:1px solid #d8e3ee;border-radius:999px;padding:9px}.legend{position:absolute;z-index:900;right:18px;bottom:78px;background:rgba(255,255,255,.96);border:1px solid #d8e3ee;border-radius:14px;padding:10px;font-size:12px}.legend div{display:flex;align-items:center;gap:7px;margin:4px 0}.dot{width:10px;height:10px;border-radius:50%;background:var(--c)}@media(max-width:680px){.panel{left:10px;top:10px}.layerbar{right:10px;top:auto;bottom:82px}.legend{display:none}.timebar{bottom:12px}}
'''


def leaflet_map(title: str, geo: Dict[str, Any], back_href: str, portal: bool = False) -> str:
    data = json.dumps(geo, ensure_ascii=False)
    center = [-6.8, 107.2]
    pts = [f for f in geo.get("features", []) if f.get("geometry", {}).get("type") == "Point"]
    if pts:
        c = pts[0]["geometry"]["coordinates"]
        center = [c[1], c[0]]
    zoom = 8 if portal else 12
    js = f'''
const DATA = {data};
const COLORS = {{safe:'#188a50', watch:'#c88410', rain:'#d45a18', danger:'#c82842', limited:'#6b7280'}};
let activeHour = null; let activeLayer = 'risk';
const features = (DATA && DATA.features) ? DATA.features : [];
const map = L.map('map', {{minZoom:5, maxBounds:{json.dumps(ID_BOUNDS)}, maxBoundsViscosity:.85, worldCopyJump:false}}).setView({json.dumps(center)}, {zoom});
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:19, noWrap:true, bounds:{json.dumps(ID_BOUNDS)}, attribution:'&copy; OpenStreetMap'}}).addTo(map);
const layer = L.layerGroup().addTo(map);
function val(p){{ return Number(p[activeLayer] ?? p.risk ?? 0); }}
function classFrom(v){{ if(v>=75)return 'danger'; if(v>=55)return 'rain'; if(v>=27)return 'watch'; return 'safe'; }}
function colorFor(p){{ return COLORS[p.risk_class] || COLORS[classFrom(val(p))] || '#1657c8'; }}
function popup(p){{ let rain=(p.rain==null?'—':Math.round(Number(p.rain))+'%'); let risk=(p.risk==null?'—':Math.round(Number(p.risk))); return `<b>${{p.location_name||'Lokasi'}}</b><br>${{p.date||''}} · ${{(p.hour_label||p.hour||'').replace(':','.')}} WIB<br>${{p.condition||''}}<br>Risk: ${{risk}}/100<br>Hujan: ${{rain}}<br>Status: ${{p.risk_label||'-'}}`; }}
function draw(){{ layer.clearLayers(); let selected = features.filter(f => !activeHour || !((f.properties||{{}}).hour) || (f.properties||{{}}).hour === activeHour); if(!selected.length) selected = features.slice(0,100); selected.forEach(f => {{ const p=f.properties||{{}}; const col=colorFor(p); if((f.geometry||{{}}).type === 'Polygon'){{ L.geoJSON(f, {{style:{{color:col,weight:.6,fillColor:col,fillOpacity:Math.min(.35,.08+val(p)/280)}}}}).bindPopup(popup(p)).addTo(layer); }} else if((f.geometry||{{}}).type === 'Point'){{ const c=f.geometry.coordinates; L.circleMarker([c[1],c[0]],{{radius:9,color:'#fff',weight:1,fillColor:col,fillOpacity:1}}).bindPopup(popup(p)).addTo(layer); }} }}); try{{ const g=L.featureGroup(layer.getLayers()); if(g.getLayers().length) map.fitBounds(g.getBounds().pad(.18),{{maxZoom:{8 if portal else 13}}}); }}catch(e){{}} }}
const hours = Array.from(new Set(features.map(f => (f.properties||{{}}).hour).filter(Boolean))).sort();
const tb=document.getElementById('timebar'); hours.forEach((h,i)=>{{ const b=document.createElement('button'); b.textContent=h.replace(':','.'); b.className=i===0?'active':''; b.onclick=()=>{{ document.querySelectorAll('#timebar button').forEach(x=>x.classList.remove('active')); b.classList.add('active'); activeHour=h; draw(); }}; tb.appendChild(b); if(i===0) activeHour=h; }});
document.querySelectorAll('.layerbar button').forEach(b=>{{ b.onclick=()=>{{ document.querySelectorAll('.layerbar button').forEach(x=>x.classList.remove('active')); b.classList.add('active'); activeLayer=b.dataset.layer; draw(); }} }});
draw();
'''
    return f'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>{MAP_CSS}</style></head><body><div id="map"></div><section class="panel"><h1>{esc(title)}</h1><p>Risk surface lokal. Pilih jam dan layer untuk melihat perubahan.</p><a class="btn" href="{esc(back_href)}">Kembali</a></section><div class="layerbar"><button class="active" data-layer="risk">Risk</button><button data-layer="rain">Hujan</button><button data-layer="heat">Panas</button><button data-layer="humidity">Lembap</button><button data-layer="confidence">Conf.</button></div><div class="legend"><div><span class="dot" style="--c:#188a50"></span>Aman</div><div><span class="dot" style="--c:#c88410"></span>Dipantau</div><div><span class="dot" style="--c:#d45a18"></span>Waspada</div><div><span class="dot" style="--c:#c82842"></span>Tinggi</div></div><div id="timebar" class="timebar"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>{js}</script></body></html>'''

# ---------------------------------------------------------------------------
# Portal and ops
# ---------------------------------------------------------------------------

def portal_page(locations: List[Dict[str, Any]], run: str) -> str:
    safest = min(locations, key=lambda l: clamp(l["today"].get("risk_total"), default=99)) if locations else None
    watch = max(locations, key=lambda l: clamp(l["today"].get("risk_total"), default=0)) if locations else None
    regional_window = []
    for l in locations:
        if l["today"].get("peak_rain_hour"):
            regional_window.append(hint(l["today"].get("peak_rain_hour")))
    common_hour = f"{round(sum(regional_window)/len(regional_window)):02d}.00" if regional_window else "—"
    rows = ''
    for loc in sorted(locations, key=lambda l: clamp(l["today"].get("risk_total"), default=0), reverse=True):
        d = loc["today"]
        rows += f'<tr><td><a href="{esc(loc["location_slug"])}/anemos_app.html"><b>{esc(loc["location_name"])}</b></a></td><td>{mini_status(d.get("risk_class"), d.get("risk_label"))}</td><td>{pct(d.get("peak_rain_probability"))}</td><td>{esc(hlabel(d.get("peak_rain_hour")))}</td><td>{round(clamp(d.get("risk_total"))):.0f}</td></tr>'
    body = f'''
<section class="console">
  <div class="panel mapbox"><iframe src="langit_portal_map.html" loading="lazy"></iframe></div>
  <aside class="panel decision">
    <p class="kicker">{esc(fmt_date(now_local().date()))} · run {esc(run)}</p>
    <h1>Regional situation board</h1>
    <p>Ringkasan cuaca lokal untuk seluruh lokasi aktif.</p>
    <div class="metricgrid">
      <div class="metric"><small>Paling stabil</small><b>{esc(safest['location_name'] if safest else '—')}</b></div>
      <div class="metric"><small>Perlu dipantau</small><b>{esc(watch['location_name'] if watch else '—')}</b></div>
      <div class="metric"><small>Jam umum</small><b>{esc(common_hour)}</b></div>
      <div class="metric"><small>Lokasi</small><b>{len(locations)}</b></div>
    </div>
    <a class="btn primary" href="ops.html">Operations</a>
  </aside>
</section>
<section class="panel section"><div class="head"><h2>Risk ranking</h2><p>Semua lokasi</p></div><div class="drawer"><table class="matrix"><thead><tr><th>Lokasi</th><th>Status</th><th>Hujan</th><th>Jam</th><th>Risk</th></tr></thead><tbody>{rows}</tbody></table></div></section>
'''
    return html_doc("LANGIT — Regional Board", "overview", body, root_rel="")


def ops_page(root: Path, locations: List[Dict[str, Any]], run: str) -> str:
    html_files = list(root.glob("*.html")) + list(root.glob("*/*.html"))
    json_files = list(root.glob("*.json")) + list(root.glob("*/*.json"))
    csv_files = list(root.glob("*.csv")) + list(root.glob("*/*.csv"))
    total_sources = sum((l.get("source_health", {}).get("total") or 0) for l in locations)
    active_sources = sum((l.get("source_health", {}).get("active") or 0) for l in locations)
    body = f'''
<section class="panel"><div class="head"><h2>Operations monitor</h2><p>Run {esc(run)}</p></div><div class="grid ops">
<div class="metric"><small>Lokasi</small><b>{len(locations)}</b></div>
<div class="metric"><small>HTML</small><b>{len(html_files)}</b></div>
<div class="metric"><small>JSON</small><b>{len(json_files)}</b></div>
<div class="metric"><small>CSV</small><b>{len(csv_files)}</b></div>
<div class="metric"><small>Sumber aktif</small><b>{active_sources}/{total_sources or '—'}</b></div>
<div class="metric"><small>Archive</small><b>SQLite</b></div>
<div class="metric"><small>Risk field</small><b>GeoJSON</b></div>
<div class="metric"><small>Status</small><b>OK</b></div>
</div></section>
<section class="panel section"><div class="head"><h2>Output penting</h2><p>Generated public files</p></div><div class="drawer"><table class="matrix"><tbody>{''.join(f'<tr><td>{esc(str(p.relative_to(root)))}</td><td>{p.stat().st_size} bytes</td></tr>' for p in sorted(html_files)[:80])}</tbody></table></div></section>
'''
    return html_doc("LANGIT Operations", "ops", body, root_rel="")

# ---------------------------------------------------------------------------
# Rebuild + verify
# ---------------------------------------------------------------------------

BANNED = ["ANEMOS ·", "ANEMOS sedang", "AETHER Sentinel", "visualvisual", "[.new Set", "const hours=[.new"]


def sanitize_public_text(content: str) -> str:
    replacements = {
        "ANEMOS sedang": "LANGIT sedang",
        "AETHER Sentinel X": "LANGIT",
        "AETHER Sentinel": "LANGIT",
        "Sentinel X": "LANGIT",
        "visual-first": "",
        "Data confidence": "Keandalan data",
        "Window ": "Jam ",
        "window ": "jam ",
        "ANEMOS ·": "LANGIT ·",
    }
    out = content
    for old, new in replacements.items():
        if old:
            out = out.replace(old, new)
    out = re.sub(r"(?:visual){8,}", "", out, flags=re.I)
    return out


def sanitize_existing(root: Path) -> int:
    changed = 0
    for p in list(root.glob("*.html")) + list(root.glob("*/*.html")) + list(root.glob("*.json")) + list(root.glob("*/*.json")):
        try:
            old = p.read_text(encoding="utf-8", errors="replace")
            new = sanitize_public_text(old)
            if new != old:
                p.write_text(new, encoding="utf-8")
                changed += 1
        except Exception:
            pass
    return changed


def rebuild(root: Path, public_base_url: str) -> int:
    meta = metadata_by_slug(root)
    dirs = location_dirs(root)
    if not dirs:
        print("ERROR: outputs/ belum berisi folder lokasi. Jalankan forecast engine dulu.", file=sys.stderr)
        return 2
    locations = [load_location(d, meta.get(d.name, {"slug": d.name})) for d in dirs]
    run = create_archive(root, locations, public_base_url)
    for loc in locations:
        d = root / loc["location_slug"]
        surf = risk_surface_for_location(loc)
        write_json(d / "langit_core_intelligence.json", loc)
        write_json(d / "langit_risk_surface.geojson", surf)
        write_text(d / "langit_map_room.html", leaflet_map(f"Risk field — {loc['location_name']}", surf, "anemos_app.html"))
        write_text(d / "anemos_map.html", leaflet_map(f"Risk field — {loc['location_name']}", surf, "anemos_app.html"))
        console = console_page(loc)
        write_text(d / "anemos_app.html", console)
        write_text(d / "langit_app.html", console)
        write_text(d / "langit_console.html", console)
        write_text(d / "command_center_sentinel_x.html", console)
        write_text(d / "anemos_3day.html", outlook_page(loc))
        write_text(d / "langit_3day.html", outlook_page(loc))
        write_text(d / "anemos_activity.html", activity_page(loc))
        write_text(d / "langit_activity.html", activity_page(loc))
        write_text(d / "langit_model_court.html", analyst_page(loc))
        write_text(d / "langit_analyst.html", analyst_page(loc))
        write_text(d / "sentinel_x_accuracy_public.html", reliability_page(loc))
        write_text(d / "langit_reliability.html", reliability_page(loc))
        write_csv_rows(d / "langit_core_points.csv", [p for day in loc["days"] for p in day["points"]], ["date_iso","date_label","relative","hour","condition","temp_c","rh_pct","heat_index_c","rain_probability","wind_kmh","rain_risk","heat_risk","humidity_risk","wind_risk","data_risk","risk_total","risk_class","risk_label","risk_driver","valid"])
    regional = regional_surface(locations)
    write_json(root / "langit_all_locations.geojson", regional)
    write_text(root / "langit_portal_map.html", leaflet_map("Regional risk field", regional, "index.html", portal=True))
    write_text(root / "index.html", portal_page(locations, run))
    write_text(root / "ops.html", ops_page(root, locations, run))
    manifest = {"brand": BRAND, "version": VERSION, "run_id": run, "generated_at": now_local().isoformat(), "public_base_url": public_base_url, "locations": [{"slug": l["location_slug"], "name": l["location_name"]} for l in locations], "archive": "core/langit_core.sqlite"}
    write_json(root / "langit_core_manifest.json", manifest)
    write_json(root / "langit_portal_manifest.json", manifest)
    sanitize_existing(root)
    print(f"OK: {VERSION} rebuild selesai. lokasi={len(locations)} run={run}")
    return verify(root)


def verify(root: Path) -> int:
    sanitize_existing(root)
    required = [root / "index.html", root / "ops.html", root / "langit_portal_map.html", root / "core" / "langit_core.sqlite", root / "langit_core_manifest.json"]
    for d in location_dirs(root):
        required += [d / "anemos_app.html", d / "langit_map_room.html", d / "langit_risk_surface.geojson", d / "langit_core_intelligence.json", d / "langit_model_court.html", d / "sentinel_x_accuracy_public.html"]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        print("ERROR: file wajib belum ada:")
        for p in missing[:60]:
            print(" -", p)
        return 2
    hits: List[Tuple[str,str]] = []
    for p in list(root.glob("*.html")) + list(root.glob("*/*.html")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        for token in BANNED:
            if token and token in txt:
                hits.append((str(p), token))
    if hits:
        print("ERROR: token lama/rusak masih muncul:")
        for p, t in hits[:40]:
            print(" -", p, "contains", repr(t))
        return 3
    print("OK: LANGIT CORE public output verified.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build LANGIT CORE from forecast outputs.")
    parser.add_argument("--root", default="outputs")
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.verify_only:
        return verify(root)
    return rebuild(root, args.public_base_url)

if __name__ == "__main__":
    raise SystemExit(main())
