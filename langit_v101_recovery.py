#!/usr/bin/env python3
"""
LANGIT v101 Recovery Layer
==========================

Purpose:
- Restore the public website quality after the experimental LANGIT CORE layer.
- Keep useful backend artifacts (archive/risk JSON/GeoJSON) but rebuild the public pages
  into a cleaner, formal, normal-web style.
- Fix GitHub Pages 404 caused by root-level links that should point to location folders.

Usage in repository root after weather_ensemble_multi_location.py has generated outputs/:
  python langit_v101_recovery.py --root outputs --public-base-url https://marcooo20-d.github.io/weather-forecast
  python langit_v101_recovery.py --root outputs --verify-only

This script is intentionally dependency-free. It only needs Python 3.11+.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

BRAND = "LANGIT"
VERSION = "LANGIT v101"
TZ_NAME = "Asia/Jakarta"
DISCLAIMER = "Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi BMKG dan kondisi setempat."
MONTH_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
DAY_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
DEFAULT_PUBLIC_BASE_URL = "https://marcooo20-d.github.io/weather-forecast"

# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def clean_text(value: Any, default: str = "") -> str:
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
            value = value.strip().replace("%", "").replace("°C", "").replace("km/jam", "").replace("km/h", "").replace(",", ".")
            if not value or value in {"—", "-", "–"}:
                return default
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


def probability(value: Any, default: Optional[float] = None) -> Optional[float]:
    x = num(value, default)
    if x is None:
        return default
    if 0 < x < 1:
        x *= 100.0
    return clamp(x)


def hour(value: Any, default: str = "00:00") -> str:
    raw = clean_text(value, default)
    m = re.search(r"(\d{1,2})(?::(\d{2}))?", raw)
    if not m:
        return default
    h = max(0, min(23, int(m.group(1))))
    minute = (m.group(2) or "00")[:2]
    return f"{h:02d}:{minute}"


def hdot(value: Any) -> str:
    return hour(value).replace(":", ".")


def hour_int(value: Any) -> int:
    try:
        return int(hour(value)[:2])
    except Exception:
        return 0


def slugify(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", clean_text(value, "location").lower()).strip("-")
    return out or "location"


def local_now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(TZ_NAME))


def parse_date(value: Any) -> Optional[dt.date]:
    raw = clean_text(value)
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
    return f"{d.day} {MONTH_ID[d.month - 1]} {d.year}"


def fmt_pct(value: Any) -> str:
    x = probability(value, None)
    return "—" if x is None else f"{round(x):.0f}%"


def fmt_deg(value: Any) -> str:
    x = num(value, None)
    return "—" if x is None else f"{x:.1f}°C"


def fmt_num(value: Any, digits: int = 0) -> str:
    x = num(value, None)
    if x is None:
        return "—"
    return f"{x:.{digits}f}" if digits else f"{round(x):.0f}"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_csv_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return [dict(row) for row in csv.DictReader(f)]
    except Exception:
        return []


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sanitize_public_text(content), encoding="utf-8")


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


def sanitize_public_text(content: str) -> str:
    replacements = [
        ("ANEMOS sedang", "LANGIT sedang"),
        ("AETHER Sentinel", "LANGIT"),
        ("Hyperlocal Weather Intelligence OS", "Prakiraan lokal"),
        ("Regional situation board", "Ringkasan wilayah"),
        ("risk field", "peta risiko"),
        ("Decision-first", "Ringkas"),
        ("visualvisual", "visual"),
    ]
    out = content
    for old, new in replacements:
        if old:
            out = out.replace(old, new)
    out = re.sub(r"(?:visual){6,}", "visual", out, flags=re.I)
    return out

# ---------------------------------------------------------------------------
# Forecast normalization
# ---------------------------------------------------------------------------

def risk_class(score: Any, valid: bool = True) -> str:
    if not valid:
        return "limited"
    x = clamp(score)
    if x >= 75:
        return "danger"
    if x >= 55:
        return "watch"
    return "safe"


def risk_label(cls: str) -> str:
    return {
        "safe": "Aman",
        "watch": "Perlu dipantau",
        "danger": "Risiko tinggi",
        "limited": "Data terbatas",
    }.get(cls, "Perlu dipantau")


def risk_color(cls: str) -> str:
    return {
        "safe": "#1fbf75",
        "watch": "#d98912",
        "danger": "#d63d4b",
        "limited": "#78849a",
    }.get(cls, "#2b70d6")


def condition_from_values(hh: str, rain: Any, temp: Any, rh: Any, heat: Any, valid: bool) -> str:
    if not valid:
        return "Data terbatas"
    p = probability(rain, 0) or 0
    hi = num(heat, num(temp, None))
    r = num(rh, None)
    h = hour_int(hh)
    if p >= 70:
        return "Hujan berpeluang tinggi"
    if p >= 45:
        return "Potensi hujan lokal"
    if hi is not None and hi >= 35 and 10 <= h <= 16:
        return "Panas terasa"
    if r is not None and r >= 88 and (h <= 8 or h >= 19):
        return "Lembap"
    if 10 <= h <= 15:
        return "Cerah berawan"
    if 16 <= h <= 19:
        return "Berawan sore"
    return "Berawan"


def score_from_values(hh: str, rain: Any, heat: Any, rh: Any, wind: Any, valid: bool) -> float:
    if not valid:
        return 40.0
    h = hour_int(hh)
    rain_score = probability(rain, 0) or 0
    heat_index = num(heat, None)
    heat_score = 0.0
    if heat_index is not None:
        if heat_index >= 40: heat_score = 80
        elif heat_index >= 38: heat_score = 65
        elif heat_index >= 36: heat_score = 48
        elif heat_index >= 34: heat_score = 30
    rh_val = num(rh, None)
    humid_score = 0.0 if rh_val is None else max(0, (rh_val - 82) * (1.7 if h <= 8 or h >= 19 else 1.0))
    wind_val = num(wind, None)
    wind_score = 0.0 if wind_val is None else max(0, (wind_val - 18) * 2.5)
    return round(clamp(max(rain_score, heat_score, humid_score, wind_score)), 1)


def row_to_point(row: Dict[str, Any], fallback_date: dt.date, relative: str) -> Dict[str, Any]:
    hh = hour(pick(row, "hour", "jam", "time", "local_time", "target_hour", "datetime", "timestamp", default="00:00"))
    d = parse_date(pick(row, "date", "date_iso", "tanggal", "target_date", "valid_date", "forecast_date", "datetime", "timestamp")) or fallback_date
    temp = num(pick(row, "temp_c", "temperature_c", "temperature_2m_c", "avg_temperature_c", "t2m", "suhu"))
    rh = num(pick(row, "rh_pct", "humidity_pct", "relative_humidity", "relative_humidity_2m", "rh", "kelembapan"))
    heat = num(pick(row, "heat_index_c", "apparent_temperature_c", "feels_like_c", "terasa"), temp)
    rain = probability(pick(row, "rain_probability", "rain_probability_raw", "precip_probability", "precipitation_probability", "pop", "hujan"))
    wind = num(pick(row, "wind_kmh", "wind_speed_kmh", "wind_speed_10m_kmh", "angin"))
    valid = any(v is not None for v in [temp, rh, heat, rain, wind])
    raw_score = pick(row, "risk_total", "risk_score", "score", "risk")
    score = num(raw_score, None)
    if score is None:
        score = score_from_values(hh, rain, heat, rh, wind, valid)
    cls = clean_text(pick(row, "risk_class", "class"), "")
    if cls not in {"safe", "watch", "danger", "limited"}:
        cls = risk_class(score, valid)
    return {
        "date_iso": d.isoformat(),
        "date_label": fmt_date(d),
        "date_short": fmt_date(d, False),
        "relative": relative,
        "hour": hh,
        "hour_label": hdot(hh),
        "temp_c": temp,
        "rh_pct": rh,
        "heat_index_c": heat,
        "rain_probability": rain,
        "wind_kmh": wind,
        "risk_total": round(clamp(score), 1),
        "risk_class": cls,
        "risk_label": risk_label(cls),
        "condition": clean_text(pick(row, "condition", "weather", "cuaca", "summary"), "") or condition_from_values(hh, rain, temp, rh, heat, valid),
        "valid": bool(valid),
    }


def compact_window(hours: List[int]) -> str:
    if not hours:
        return "—"
    hours = sorted(set(hours))
    ranges = []
    start = prev = hours[0]
    for h in hours[1:]:
        if h == prev + 1 or h == prev + 3:
            prev = h
        else:
            ranges.append((start, prev))
            start = prev = h
    ranges.append((start, prev))
    return ", ".join(f"{a:02d}.00" if a == b else f"{a:02d}.00–{b:02d}.00" for a, b in ranges)


def summarize_day(relative: str, date_value: dt.date, points: List[Dict[str, Any]]) -> Dict[str, Any]:
    points = sorted(points, key=lambda p: hour_int(p["hour"]))
    valid = [p for p in points if p.get("valid")]
    basis = valid or points
    worst = max(basis, key=lambda p: num(p.get("risk_total"), 0) or 0) if basis else {}
    peak_rain = max(basis, key=lambda p: probability(p.get("rain_probability"), -1) if probability(p.get("rain_probability"), None) is not None else -1) if basis else {}
    safe_hours = [hour_int(p["hour"]) for p in points if p.get("risk_class") == "safe" and p.get("valid")]
    cls = "limited" if not valid else worst.get("risk_class", "safe")
    return {
        "relative": relative,
        "date_iso": date_value.isoformat(),
        "date_label": fmt_date(date_value),
        "date_short": fmt_date(date_value, False),
        "points": points,
        "condition": worst.get("condition", "Data terbatas" if not valid else "Berawan"),
        "risk_total": round(num(worst.get("risk_total"), 0) or 0, 1),
        "risk_class": cls,
        "risk_label": risk_label(cls),
        "peak_rain_probability": probability(peak_rain.get("rain_probability"), None),
        "peak_rain_hour": peak_rain.get("hour", "—"),
        "max_heat_index_c": maximum(p.get("heat_index_c") for p in valid),
        "avg_temp_c": mean(p.get("temp_c") for p in valid),
        "avg_rh_pct": mean(p.get("rh_pct") for p in valid),
        "safe_window": compact_window(safe_hours),
        "valid_points": len(valid),
    }

# ---------------------------------------------------------------------------
# Loading outputs
# ---------------------------------------------------------------------------

def rows_from_api(api: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not isinstance(api, dict):
        return rows
    for key in ["days", "daily"]:
        days = api.get(key)
        if isinstance(days, list):
            for day in days[:3]:
                if not isinstance(day, dict):
                    continue
                date_val = day.get("date") or day.get("date_iso") or day.get("target_date")
                rel = day.get("relative") or day.get("day_tag") or day.get("label")
                for hkey in ["hours", "hourly", "key_hours", "rows", "forecast"]:
                    items = day.get(hkey)
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                x = dict(item)
                                x.setdefault("date", date_val)
                                x.setdefault("relative", rel)
                                rows.append(x)
                        break
    for key in ["hours", "hourly", "key_hours", "forecast"]:
        items = api.get(key)
        if isinstance(items, list):
            rows.extend([dict(x) for x in items if isinstance(x, dict)])
            break
    return rows


def read_location_rows(directory: Path) -> List[Dict[str, Any]]:
    for name in [
        "langit_core_hourly.csv",
        "langit_hourly_intelligence.csv",
        "anemos_hourly_compact.csv",
        "anemos_risk_timeline.csv",
        "forecast.csv",
    ]:
        rows = read_csv_rows(directory / name)
        if rows:
            return rows
    for name in ["langit_core_intelligence.json", "langit_api_v1.json", "anemos_api_v1.json", "forecast.json"]:
        api = read_json(directory / name, None)
        rows = rows_from_api(api)
        if rows:
            return rows
    return []


def source_rows(directory: Path) -> List[Dict[str, Any]]:
    for name in ["source_status.csv", "langit_source_status.csv", "source_court.csv", "model_court.csv"]:
        rows = read_csv_rows(directory / name)
        if rows:
            return rows
    return []


def split_day_chunks(rows: List[Dict[str, Any]], today: dt.date) -> List[Tuple[str, dt.date, List[Dict[str, Any]]]]:
    if not rows:
        return [("Hari ini", today, []), ("Besok", today + dt.timedelta(days=1), []), ("Lusa", today + dt.timedelta(days=2), [])]
    dated: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        d = parse_date(pick(row, "date", "date_iso", "tanggal", "target_date", "valid_date", "forecast_date", "datetime", "timestamp"))
        if d:
            dated.setdefault(d.isoformat(), []).append(row)
    if dated:
        out = []
        for i, key in enumerate(sorted(dated.keys())[:3]):
            rel = ["Hari ini", "Besok", "Lusa"][i]
            out.append((rel, dt.date.fromisoformat(key), dated[key]))
        return out
    # fallback: split when hour resets
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    last_h = -1
    for row in rows:
        h = hour_int(pick(row, "hour", "jam", "time", "local_time", "datetime", "timestamp", default="00:00"))
        if current and h < last_h:
            chunks.append(current)
            current = []
        current.append(row)
        last_h = h
    if current:
        chunks.append(current)
    out = []
    for i, ch in enumerate(chunks[:3]):
        out.append((["Hari ini", "Besok", "Lusa"][i], today + dt.timedelta(days=i), ch))
    return out or [("Hari ini", today, rows)]


def fallback_rows(d: dt.date) -> List[Dict[str, Any]]:
    return [{"date": d.isoformat(), "hour": h} for h in ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]]


def location_dirs(root: Path) -> List[Path]:
    if not root.exists():
        return []
    sentinel = [
        "anemos_app.html", "langit_app.html", "langit_api_v1.json", "anemos_api_v1.json",
        "langit_core_intelligence.json", "forecast.csv", "anemos_hourly_compact.csv",
    ]
    out = []
    for child in root.iterdir():
        if child.is_dir() and not child.name.startswith(".") and child.name not in {"core", "assets", "raw_payloads", "logs", "reports", "observations"}:
            if any((child / s).exists() for s in sentinel):
                out.append(child)
    return sorted(out, key=lambda p: p.name)


def meta_from_root(root: Path) -> Dict[str, Dict[str, Any]]:
    meta: Dict[str, Dict[str, Any]] = {}
    for name in ["dim_locations.csv", "locations.csv", "dim_location.csv"]:
        for row in read_csv_rows(root / name):
            slug = clean_text(pick(row, "slug", "location_slug"), "") or slugify(clean_text(pick(row, "location_name", "name"), "location"))
            meta.setdefault(slug, {}).update(row)
    for name in ["langit_all_locations.geojson", "anemos_all_locations.geojson", "langit_portal_locations.geojson"]:
        gj = read_json(root / name, {}) or {}
        feats = gj.get("features", []) if isinstance(gj, dict) else []
        for feat in feats:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") or {}
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            slug = clean_text(props.get("slug") or props.get("location_slug") or slugify(props.get("location_name") or props.get("name") or ""))
            if slug:
                meta.setdefault(slug, {}).update({
                    "slug": slug,
                    "location_name": props.get("location_name") or props.get("name") or meta.get(slug, {}).get("location_name"),
                    "latitude": coords[1] if len(coords) >= 2 else meta.get(slug, {}).get("latitude"),
                    "longitude": coords[0] if len(coords) >= 1 else meta.get(slug, {}).get("longitude"),
                })
    return meta


def build_location(directory: Path, meta: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    slug = directory.name
    api = read_json(directory / "langit_core_intelligence.json", {}) or read_json(directory / "langit_api_v1.json", {}) or read_json(directory / "anemos_api_v1.json", {}) or {}
    loc_name = clean_text(meta.get("location_name") or meta.get("name") or (api.get("location_name") if isinstance(api, dict) else ""), slug.replace("-", " ").title())
    lat = num(meta.get("latitude") or (api.get("latitude") if isinstance(api, dict) else None), None)
    lon = num(meta.get("longitude") or (api.get("longitude") if isinstance(api, dict) else None), None)
    if lat is None or lon is None:
        default_coords = {
            "dago": (-6.8890, 107.6100),
            "jatinangor": (-6.9380, 107.7556),
            "arjawinangun": (-6.6453, 108.4103),
        }
        lat, lon = default_coords.get(slug, (-6.9, 107.6))
    rows = read_location_rows(directory)
    today = parse_date((api.get("date") if isinstance(api, dict) else None) or (api.get("target_date") if isinstance(api, dict) else None)) or local_now().date()
    chunks = split_day_chunks(rows, today)
    days: List[Dict[str, Any]] = []
    for rel, d, chunk in chunks[:3]:
        if not chunk:
            chunk = fallback_rows(d)
        points = [row_to_point(row, d, rel) for row in chunk]
        days.append(summarize_day(rel, d, points))
    while len(days) < 3:
        d = today + dt.timedelta(days=len(days))
        rel = ["Hari ini", "Besok", "Lusa"][len(days)]
        pts = [row_to_point(row, d, rel) for row in fallback_rows(d)]
        days.append(summarize_day(rel, d, pts))
    today_summary = days[0]
    points = today_summary["points"]
    valid = [p for p in points if p.get("valid")]
    display_temp = mean(p.get("temp_c") for p in valid) or mean(p.get("heat_index_c") for p in valid)
    update_label = (api.get("updated_label") or api.get("generated_at") or api.get("updated_at") if isinstance(api, dict) else None) or local_now().strftime("%Y-%m-%d %H:%M")
    loc = {
        "slug": slug,
        "dir": str(directory),
        "location_name": loc_name,
        "latitude": lat,
        "longitude": lon,
        "updated_label": update_label,
        "run_id": run_id,
        "days": days,
        "today": today_summary,
        "temperature": display_temp,
        "source_rows": source_rows(directory),
    }
    return loc

# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root{
  --bg:#f4f7fb; --paper:#ffffff; --ink:#0d1b2a; --muted:#64748b; --line:#d9e3ee;
  --blue:#2364d2; --blue2:#eaf2ff; --green:#22bf78; --amber:#d98912; --red:#d63d4b;
  --shadow:0 14px 34px rgba(15,31,52,.07); --radius:22px; --radius2:14px;
}
*{box-sizing:border-box} html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.5}
a{color:inherit;text-decoration:none}.topbar{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.92);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.nav{max-width:1220px;margin:0 auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{display:flex;align-items:center;gap:10px;font-weight:800}.logo{width:32px;height:32px;border-radius:10px;background:linear-gradient(135deg,#1d63d8,#50d6b3)}.brand small{display:block;color:var(--muted);font-size:11px;font-weight:600;margin-top:-2px}.links{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.links a{padding:8px 12px;border-radius:999px;color:#10243d;font-weight:700;font-size:13px}.links a.active,.links a:hover{background:var(--blue2);color:var(--blue)}
.shell{max-width:1220px;margin:0 auto;padding:28px 24px 70px}.hero{display:grid;grid-template-columns:minmax(0,1.6fr) 330px;gap:18px;margin-bottom:16px}.panel{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}.hero-main{padding:34px;min-height:205px;position:relative;overflow:hidden}.hero-main:after{content:"";position:absolute;right:-60px;bottom:-110px;width:250px;height:250px;border-radius:999px;background:#e9f2ff}.eyebrows{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px;position:relative;z-index:1}.chip{display:inline-flex;align-items:center;border:1px solid var(--line);background:#f5f8fc;color:#37506e;border-radius:999px;padding:6px 10px;font-size:11px;font-weight:800;letter-spacing:.01em}.hero h1{position:relative;z-index:1;font-size:46px;line-height:1.02;letter-spacing:-.045em;margin:0 0 12px}.hero p{position:relative;z-index:1;color:#334155;margin:0;max-width:720px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px}.stat{padding:18px}.stat small{display:block;color:var(--muted);font-weight:800;font-size:11px;text-transform:uppercase;letter-spacing:.07em}.stat b{display:block;font-size:29px;letter-spacing:-.04em;margin-top:6px}.stat span{display:block;color:#334155;margin-top:4px;font-size:13px}.notice{font-size:12px;color:#735c10;background:#fff8df;border:1px solid #f3d374;border-radius:14px;padding:9px 12px;margin:0 0 16px}.grid2{display:grid;grid-template-columns:minmax(0,1.55fr) 330px;gap:18px}.section{padding:22px;margin-top:16px}.section-head{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:14px}.section h2{font-size:22px;letter-spacing:-.035em;margin:0}.section small{color:var(--muted)}.summary-card{padding:28px}.badge{display:inline-flex;border-radius:999px;padding:6px 10px;font-size:12px;font-weight:900;background:#e9fbf2;color:#126b45}.badge.watch{background:#fff3db;color:#92570a}.badge.danger{background:#ffe8ec;color:#a82031}.badge.limited{background:#edf1f6;color:#4b5565}.summary-card h2{font-size:38px;line-height:1.08;margin:16px 0 10px;letter-spacing:-.045em}.summary-card p{color:#334155;margin:0}.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.metric{background:#f7faff;border:1px solid var(--line);border-radius:16px;padding:15px}.metric small{display:block;color:var(--muted);font-size:11px;font-weight:800;text-transform:uppercase}.metric b{font-size:24px;display:block;margin-top:6px}.cards3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.daycard{border:1px solid var(--line);border-radius:18px;padding:18px;background:#fff}.daycard.safe{border-color:rgba(34,191,120,.65)}.daycard.watch{border-color:rgba(217,137,18,.65)}.daycard.danger{border-color:rgba(214,61,75,.65)}.daycard h3{margin:8px 0 6px;font-size:21px;letter-spacing:-.03em}.daycard p{margin:0 0 14px;color:#334155}.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.mini div,.hour-box{background:#f7faff;border:1px solid var(--line);border-radius:12px;padding:10px}.mini small,.hour-box small{display:block;color:var(--muted);font-size:10px}.mini b,.hour-box b{display:block;margin-top:4px}.timeline{display:grid;grid-template-columns:repeat(auto-fit,minmax(72px,1fr));gap:8px;align-items:end}.barwrap{min-height:74px;display:flex;flex-direction:column;justify-content:end;text-align:center}.bar{height:9px;border-radius:999px;background:#35d491}.bar.watch{background:#f2b549}.bar.danger{background:#e15b68}.bar.limited{background:#9aa5b5}.barlabel{font-size:11px;color:var(--muted);margin-top:8px}.barval{font-weight:900;font-size:12px;margin-bottom:6px}.periods{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.activity-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.activity{border:1px solid var(--line);border-left:4px solid var(--green);border-radius:16px;background:#fff;padding:16px}.activity h3{margin:0 0 6px;font-size:17px}.activity b{display:block;margin-bottom:6px}.activity p{margin:0;color:#334155}.table{width:100%;border-collapse:separate;border-spacing:0}.table th{font-size:11px;color:var(--muted);text-transform:uppercase;text-align:left;letter-spacing:.06em;padding:12px;border-bottom:1px solid var(--line)}.table td{padding:14px 12px;border-bottom:1px solid var(--line)}details{border:1px solid var(--line);background:#fff;border-radius:16px;padding:10px 14px;margin-top:10px}summary{cursor:pointer;font-weight:900}.mapbox{height:520px;border-radius:18px;overflow:hidden;border:1px solid var(--line)}.map-hero{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px}.map-side{padding:24px}.btn{display:inline-flex;align-items:center;justify-content:center;background:var(--blue);color:white;border-radius:12px;font-weight:800;padding:10px 14px;border:0}.btn.secondary{background:#eef4ff;color:#1c4fa8}.foot{text-align:center;color:var(--muted);font-size:12px;margin-top:28px}.share{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;background:#07192e;color:white;border-radius:14px;padding:16px;white-space:pre-wrap}.source-list{display:grid;grid-template-columns:1fr 1fr;gap:10px}.source-item{border:1px solid var(--line);border-radius:14px;background:#fff;padding:12px}.empty{padding:26px;background:#fbfdff;border:1px dashed var(--line);border-radius:16px;color:#475569}
@media(max-width:900px){.hero,.grid2,.map-hero{grid-template-columns:1fr}.stats,.metric-grid{grid-template-columns:1fr}.cards3,.activity-grid,.periods{grid-template-columns:1fr}.hero h1{font-size:34px}.summary-card h2{font-size:29px}.nav{align-items:flex-start;flex-direction:column}.shell{padding:18px 14px 50px}}
"""


def nav_html(active: str, root_rel: str = "") -> str:
    def link(label: str, href: str, key: str) -> str:
        # Navigation is intentionally local to the current page folder.
        # The brand link uses root_rel; page tabs should not, otherwise
        # location pages point to missing root-level files.
        return f'<a class="{ "active" if active == key else "" }" href="{esc(href)}">{esc(label)}</a>'
    return (
        link("Ringkasan", "anemos_app.html", "home")
        + link("3 hari", "anemos_3day.html", "outlook")
        + link("Aktivitas", "anemos_activity.html", "activity")
        + link("Peta", "langit_map_room.html", "map")
        + link("Keandalan", "langit_model_court.html", "analyst")
    )


def doc(title: str, active: str, body: str, root_rel: str = "") -> str:
    return f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="robots" content="index,follow">
<style>{CSS}</style>
</head>
<body>
<header class="topbar"><div class="nav"><a class="brand" href="{esc(root_rel)}index.html"><span class="logo"></span><span>{BRAND}<small>Prakiraan cuaca lokal</small></span></a><nav class="links">{nav_html(active, root_rel)}</nav></div></header>
<main class="shell">{body}<p class="foot">LANGIT {VERSION} · {esc(DISCLAIMER)}</p></main>
</body></html>"""


def hero(loc: Dict[str, Any], title: str, subtitle: str) -> str:
    today = loc["today"]
    temp = fmt_deg(loc.get("temperature"))
    return f"""
<div class="hero">
  <section class="panel hero-main">
    <div class="eyebrows"><span class="chip">LANGIT</span><span class="chip">{VERSION}</span><span class="chip">{esc(today['date_label'])}</span><span class="chip">Run {esc(loc.get('run_id',''))}</span></div>
    <h1>{esc(title)}</h1>
    <p>{esc(subtitle)}</p>
  </section>
  <aside class="stats">
    <div class="panel stat"><small>Cuaca</small><b>{temp}</b><span>{esc(today.get('condition',''))}</span></div>
    <div class="panel stat"><small>Hujan</small><b>{fmt_pct(today.get('peak_rain_probability'))}</b><span>puncak {esc(hdot(today.get('peak_rain_hour')))}</span></div>
    <div class="panel stat"><small>Status</small><b>{esc(today.get('risk_label'))}</b><span>skor {fmt_num(today.get('risk_total'))}/100</span></div>
    <div class="panel stat"><small>Jam nyaman</small><b>{esc(today.get('safe_window'))}</b><span>aktivitas luar ruang</span></div>
  </aside>
</div>
<p class="notice">{esc(DISCLAIMER)}</p>
"""


def badge(cls: str) -> str:
    return f'<span class="badge {esc(cls)}">{esc(risk_label(cls))}</span>'


def summary_page(loc: Dict[str, Any]) -> str:
    today = loc["today"]
    title = f"Prakiraan {loc['location_name']}"
    subtitle = f"{today['date_label']}. {today['condition']}. Peluang hujan tertinggi {fmt_pct(today['peak_rain_probability'])} sekitar pukul {hdot(today['peak_rain_hour'])} WIB."
    cards = "".join(day_card(d) for d in loc["days"])
    bars = timeline(loc["today"]["points"])
    periods = period_cards(loc["today"]["points"])
    body = hero(loc, title, subtitle)
    body += f"""
<div class="grid2">
  <section class="panel summary-card">
    {badge(today['risk_class'])}
    <h2>{esc(loc['location_name'])}: {esc(today['condition'])}. Peluang hujan tertinggi {fmt_pct(today['peak_rain_probability'])} sekitar pukul {hdot(today['peak_rain_hour'])} WIB.</h2>
    <p>{esc(today['date_label'])}. Gunakan halaman ini sebagai ringkasan cepat; detail per jam ada di bagian bawah.</p>
  </section>
  <aside class="metric-grid">
    <div class="metric"><small>Risiko</small><b>{fmt_num(today['risk_total'])}/100</b><span>{esc(today['risk_label'])}</span></div>
    <div class="metric"><small>Puncak hujan</small><b>{fmt_pct(today['peak_rain_probability'])}</b><span>{hdot(today['peak_rain_hour'])}</span></div>
    <div class="metric"><small>Panas terasa</small><b>{fmt_deg(today['max_heat_index_c'])}</b><span>maksimum</span></div>
    <div class="metric"><small>Data jam</small><b>{len(today['points'])}</b><span>titik prakiraan</span></div>
  </aside>
</div>
<section class="panel section"><div class="section-head"><h2>Ringkasan 3 hari</h2><small>Tanggal jelas, angka secukupnya.</small></div><div class="cards3">{cards}</div></section>
<section class="panel section"><div class="section-head"><h2>Timeline hujan</h2><small>{esc(today['date_label'])}</small></div>{bars}</section>
<section class="panel section"><div class="section-head"><h2>Pagi–malam</h2><small>Ringkasan per periode.</small></div><div class="periods">{periods}</div></section>
{hour_details(loc['days'])}
<section class="grid2">
  <div class="panel section"><div class="section-head"><h2>Share singkat</h2><small>Format WA</small></div><div class="share">{esc(share_text(loc))}</div></div>
  <div class="panel section"><div class="section-head"><h2>Catatan</h2><small>Ringkas.</small></div><p>Prakiraan dapat bergeser beberapa kilometer atau beberapa jam. Untuk cuaca ekstrem, pakai informasi BMKG dan kondisi setempat.</p></div>
</section>
"""
    return doc(title, "home", body, root_rel="../")


def day_card(day: Dict[str, Any]) -> str:
    return f"""
<div class="daycard {esc(day['risk_class'])}">
  {badge(day['risk_class'])}
  <h3>{esc(day['relative'])}</h3>
  <p>{esc(day['date_label'])}. {esc(day['condition'])}.</p>
  <div class="mini"><div><small>Hujan</small><b>{fmt_pct(day['peak_rain_probability'])}</b></div><div><small>Jam</small><b>{hdot(day['peak_rain_hour'])}</b></div><div><small>Risiko</small><b>{fmt_num(day['risk_total'])}</b></div></div>
</div>"""


def timeline(points: List[Dict[str, Any]]) -> str:
    if not points:
        return '<div class="empty">Belum ada data per jam.</div>'
    html_parts = ['<div class="timeline">']
    for p in sorted(points, key=lambda x: hour_int(x['hour'])):
        cls = p.get('risk_class', 'safe')
        val = probability(p.get('rain_probability'), 0) or 0
        height = 9 + int(min(42, val * 0.48))
        html_parts.append(f'<div class="barwrap"><div class="barval">{fmt_pct(val)}</div><div class="bar {esc(cls)}" style="height:{height}px"></div><div class="barlabel">{hdot(p["hour"])}</div></div>')
    html_parts.append('</div>')
    return ''.join(html_parts)


def period_cards(points: List[Dict[str, Any]]) -> str:
    periods = [("Pagi", 0, 9), ("Siang", 10, 14), ("Sore", 15, 18), ("Malam", 19, 23)]
    out = []
    for label_name, a, b in periods:
        group = [p for p in points if a <= hour_int(p.get('hour')) <= b]
        basis = group or points[:1]
        worst = max(basis, key=lambda p: num(p.get('risk_total'), 0) or 0) if basis else {}
        peak = max(basis, key=lambda p: probability(p.get('rain_probability'), -1) if probability(p.get('rain_probability'), None) is not None else -1) if basis else {}
        out.append(f"""
<div class="daycard {esc(worst.get('risk_class','safe'))}">
  <h3>{esc(label_name)}</h3><p>{esc(worst.get('condition','—'))}</p>
  <div class="mini"><div><small>Suhu</small><b>{fmt_deg(mean(p.get('temp_c') for p in basis))}</b></div><div><small>Hujan</small><b>{fmt_pct(peak.get('rain_probability'))}</b></div><div><small>Jam</small><b>{hdot(peak.get('hour'))}</b></div></div>
</div>""")
    return ''.join(out)


def hour_details(days: List[Dict[str, Any]]) -> str:
    parts = []
    for idx, day in enumerate(days):
        rows = []
        for p in sorted(day['points'], key=lambda x: hour_int(x['hour'])):
            rows.append(f"""
<tr><td><b>{hdot(p['hour'])}</b></td><td>{esc(p['condition'])}<br><small>{esc(p['risk_label'])}</small></td><td>{fmt_deg(p['temp_c'])}</td><td>{fmt_pct(p['rh_pct'])}</td><td>{fmt_deg(p['heat_index_c'])}</td><td>{fmt_pct(p['rain_probability'])}</td></tr>""")
        open_attr = " open" if idx == 0 else ""
        parts.append(f"""<details{open_attr}><summary>Detail jam · {esc(day['date_label'])}</summary><table class="table"><thead><tr><th>Jam</th><th>Kondisi</th><th>Suhu</th><th>RH</th><th>Terasa</th><th>Hujan</th></tr></thead><tbody>{''.join(rows)}</tbody></table></details>""")
    return f'<section class="panel section"><div class="section-head"><h2>Detail per jam</h2><small>Lengkap, tetapi tetap ringkas.</small></div>{"".join(parts)}</section>'


def share_text(loc: Dict[str, Any]) -> str:
    d = loc['today']
    return f"LANGIT — {loc['location_name']}\n{d['date_label']}\nStatus: {d['risk_label']}\nPeluang hujan tertinggi: {fmt_pct(d['peak_rain_probability'])} sekitar {hdot(d['peak_rain_hour'])} WIB\nJam nyaman: {d['safe_window']}"


def outlook_page(loc: Dict[str, Any]) -> str:
    cards = ''.join(day_card(day) for day in loc['days'])
    day_sections = ''.join(f'<section class="panel section"><div class="section-head"><h2>{esc(day["relative"])} · {esc(day["date_label"])}</h2><small>{esc(day["risk_label"])}</small></div>{timeline(day["points"])}</section>' for day in loc['days'])
    body = hero(loc, "Prakiraan 3 hari", f"Mulai {loc['days'][0]['date_label']} sampai {loc['days'][-1]['date_label']}.")
    body += f'<section class="panel section"><div class="section-head"><h2>Ringkasan 3 hari</h2><small>Untuk membandingkan risiko tanpa membaca tabel panjang.</small></div><div class="cards3">{cards}</div></section>{day_sections}{hour_details(loc["days"])}'
    return doc(f"Prakiraan 3 hari — {loc['location_name']}", "outlook", body, root_rel="../")


def activity_recommendations(day: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    risk = day.get('risk_class', 'safe')
    rain = probability(day.get('peak_rain_probability'), 0) or 0
    safe = day.get('safe_window') or '—'
    if risk == 'danger':
        base = "Tidak disarankan"
    elif risk == 'watch' or rain >= 35:
        base = "Bisa, dengan pantauan"
    else:
        base = "Aman"
    return [
        ("Motor", "Aman" if rain < 25 else "Bawa jas hujan", "Tetap siapkan pelindung hujan jika keluar sore."),
        ("Jalan kaki", base, f"Waktu terbaik: {safe}."),
        ("Jemur pakaian", "Pagi–siang" if rain < 35 else "Kurang ideal", "Angkat sebelum peluang hujan meningkat."),
        ("Olahraga", base, f"Pilih jam yang nyaman: {safe}."),
        ("Aktivitas luar ruang", base, "Masih memungkinkan selama kondisi lokal tetap sesuai prakiraan."),
        ("Foto/city walk", "Cocok" if rain < 30 else "Cek langit", "Pilih waktu dengan cahaya yang cukup."),
    ]


def activity_page(loc: Dict[str, Any]) -> str:
    day = loc['today']
    recs = ''.join(f'<div class="activity"><h3>{esc(name)}</h3><b>{esc(status)}</b><p>{esc(note)}</p></div>' for name, status, note in activity_recommendations(day))
    body = hero(loc, "Saran aktivitas", f"{day['date_label']}. Fokus pada jam yang perlu dipantau.")
    body += f"""
<div class="grid2">
  <section class="panel summary-card">{badge(day['risk_class'])}<h2>{esc(loc['location_name'])}: {esc(day['condition'])}. Peluang hujan tertinggi {fmt_pct(day['peak_rain_probability'])} sekitar pukul {hdot(day['peak_rain_hour'])} WIB.</h2><p>Waktu terbaik: {esc(day['safe_window'])}.</p></section>
  <aside class="metric-grid"><div class="metric"><small>Risiko</small><b>{fmt_num(day['risk_total'])}/100</b><span>{esc(day['risk_label'])}</span></div><div class="metric"><small>Hujan</small><b>{fmt_pct(day['peak_rain_probability'])}</b><span>{hdot(day['peak_rain_hour'])}</span></div><div class="metric"><small>Panas terasa</small><b>{fmt_deg(day['max_heat_index_c'])}</b><span>maksimum</span></div><div class="metric"><small>Jam nyaman</small><b>{esc(day['safe_window'])}</b><span>aktivitas</span></div></aside>
</div>
<section class="panel section"><div class="section-head"><h2>Saran aktivitas</h2><small>Singkat, praktis.</small></div><div class="activity-grid">{recs}</div></section>
<section class="panel section"><div class="section-head"><h2>Jam rawan</h2><small>Lihat warna, bukan tabel panjang.</small></div>{timeline(day['points'])}</section>
<section class="panel section"><div class="section-head"><h2>Pagi–malam</h2><small>{esc(day['date_label'])}</small></div><div class="periods">{period_cards(day['points'])}</div></section>
{hour_details([day])}
"""
    return doc(f"Aktivitas — {loc['location_name']}", "activity", body, root_rel="../")


def source_status_summary(rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    if not rows:
        return 0, 0
    active = 0
    for row in rows:
        joined = ' '.join(str(v).lower() for v in row.values())
        if any(tok in joined for tok in ['yes', 'true', 'aktif', 'active', 'success', 'ok', '200']):
            active += 1
    return active, len(rows)


def analyst_page(loc: Dict[str, Any]) -> str:
    active, total = source_status_summary(loc.get('source_rows') or [])
    confidence = 0 if total == 0 else round(active / total * 100)
    if confidence >= 70:
        conf_label = 'Baik'
    elif confidence >= 35:
        conf_label = 'Cukup'
    elif total == 0:
        conf_label = 'Belum terbaca'
    else:
        conf_label = 'Terbatas'
    sources = loc.get('source_rows') or []
    rows = ''
    if sources:
        for r in sources[:12]:
            name = clean_text(pick(r, 'source_id', 'model', 'source', 'provider'), '—')
            provider = clean_text(pick(r, 'provider', 'sumber', 'source_name'), '—')
            status = clean_text(pick(r, 'success', 'active', 'status', 'verdict'), '—')
            weight = clean_text(pick(r, 'weight', 'bobot', 'source_weight'), '—')
            rows += f'<tr><td>{esc(name)}</td><td>{esc(provider)}</td><td>{esc(status)}</td><td>{esc(weight)}</td></tr>'
    else:
        rows = '<tr><td colspan="4">Belum ada tabel sumber yang terbaca dari output generator.</td></tr>'
    body = hero(loc, "Keandalan data", f"{loc['today']['date_label']}. Halaman ini menjelaskan kualitas input, bukan mengklaim akurasi sebelum observasi cukup.")
    body += f"""
<section class="panel section"><div class="section-head"><h2>Sumber terbaca</h2><small>{active}/{total} aktif</small></div><div class="timeline"><div class="barwrap" style="grid-column:1/-1"><div class="barval">{confidence}% · {esc(conf_label)}</div><div class="bar" style="height:14px;width:{max(3, confidence)}%"></div><div class="barlabel">keandalan sumber</div></div></div></section>
<section class="panel section"><div class="section-head"><h2>Tabel teknis sumber</h2><small>Detail ringkas.</small></div><table class="table"><thead><tr><th>Model</th><th>Sumber</th><th>Aktif</th><th>Bobot</th></tr></thead><tbody>{rows}</tbody></table></section>
<section class="panel section"><div class="section-head"><h2>Akurasi</h2><small>Butuh pasangan prakiraan–observasi.</small></div><p>Halaman ini tidak menampilkan skor akurasi palsu. Skor baru layak muncul setelah ada cukup data observasi yang dapat dibandingkan dengan prakiraan.</p></section>
"""
    return doc(f"Keandalan — {loc['location_name']}", "analyst", body, root_rel="../")


def map_page(loc: Dict[str, Any]) -> str:
    gj = risk_surface_geojson(loc)
    write_json(Path(loc['dir']) / 'langit_risk_surface.geojson', gj)
    body = leaflet_html(loc, gj, portal=False)
    return body


def risk_surface_geojson(loc: Dict[str, Any]) -> Dict[str, Any]:
    lat = float(loc['latitude']); lon = float(loc['longitude'])
    points = loc['today']['points']
    if not points:
        risk = 20
        rain = 0
    else:
        risk = num(max(points, key=lambda p: num(p.get('risk_total'), 0)).get('risk_total'), 20) or 20
        rain = probability(loc['today'].get('peak_rain_probability'), 0) or 0
    features = []
    # small local grid: 5x5, enough for a visible risk surface without faking precision
    for ix in range(-2, 3):
        for iy in range(-2, 3):
            dist = abs(ix) + abs(iy)
            rr = clamp(risk - dist * 4 + (iy * 1.2), 0, 100)
            cls = risk_class(rr, True)
            dlat = 0.020 * iy
            dlon = 0.020 * ix
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon + dlon, 6), round(lat + dlat, 6)]},
                "properties": {"risk": round(rr, 1), "rain": round(rain, 1), "class": cls, "label": risk_label(cls), "location": loc['location_name']},
            })
    return {"type": "FeatureCollection", "features": features}


def leaflet_html(loc: Dict[str, Any], gj: Dict[str, Any], portal: bool = False) -> str:
    title = "Peta risiko wilayah" if portal else f"Peta risiko — {loc['location_name']}"
    center_lat = loc['latitude']; center_lon = loc['longitude']
    data = json.dumps(gj, ensure_ascii=False)
    active = 'map'
    map_id = 'map'
    content = f"""<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>{CSS}#{map_id}{{height:calc(100vh - 72px);width:100%}}.mapnav{{position:fixed;top:16px;left:16px;z-index:999;background:white;border:1px solid var(--line);border-radius:18px;padding:16px;box-shadow:var(--shadow);max-width:360px}}.legend{{position:fixed;right:18px;bottom:18px;z-index:999;background:white;border:1px solid var(--line);border-radius:14px;padding:12px;box-shadow:var(--shadow);font-size:12px}}.legend div{{display:flex;gap:8px;align-items:center;margin:4px 0}}.dot{{width:10px;height:10px;border-radius:999px;display:inline-block}}.timebar{{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:999;background:rgba(255,255,255,.92);border:1px solid var(--line);border-radius:999px;padding:8px;display:flex;gap:6px;box-shadow:var(--shadow)}}.timebar button{{border:1px solid var(--line);background:white;border-radius:999px;padding:8px 11px;font-weight:800}}.timebar button.active{{background:var(--blue);color:white}}</style></head><body><div id="{map_id}"></div><div class="mapnav"><b>{esc(title)}</b><p style="margin:6px 0 12px;color:#475569">Zona warna menunjukkan indikasi risiko lokal. Tanggal dan peluang hujan tersedia di popup.</p><a class="btn" href="anemos_app.html">Kembali</a></div><div class="legend"><div><span class="dot" style="background:#1fbf75"></span>Aman</div><div><span class="dot" style="background:#d98912"></span>Perlu dipantau</div><div><span class="dot" style="background:#d63d4b"></span>Risiko tinggi</div></div><div class="timebar"><button class="active">Hari ini</button><button>Besok</button><button>Lusa</button></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>const data={data};const color=(c)=>c==='danger'?'#d63d4b':c==='watch'?'#d98912':'#1fbf75';const map=L.map('{map_id}',{{zoomControl:true}}).setView([{center_lat},{center_lon}],11);const carto=L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png',{{maxZoom:19,attribution:'© OpenStreetMap © CARTO'}}).addTo(map);const osm=L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'© OpenStreetMap'}});L.control.layers({{'Clean map':carto,'OpenStreetMap':osm}},{{}},{{collapsed:true}}).addTo(map);L.geoJSON(data,{{pointToLayer:(f,ll)=>L.circleMarker(ll,{{radius:18,fillColor:color(f.properties.class),color:color(f.properties.class),weight:2,opacity:.8,fillOpacity:.22}}),onEachFeature:(f,l)=>l.bindPopup(`<b>${{f.properties.location}}</b><br>Risiko: ${{f.properties.risk}}/100<br>Hujan: ${{f.properties.rain}}%<br>Status: ${{f.properties.label}}`)}}).addTo(map);</script></body></html>"""
    return sanitize_public_text(content)


def portal_map_page(locations: List[Dict[str, Any]]) -> str:
    feats = []
    for loc in locations:
        d = loc['today']
        feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":[loc['longitude'],loc['latitude']]},"properties":{"name":loc['location_name'],"slug":loc['slug'],"risk":d['risk_total'],"rain":d['peak_rain_probability'],"label":d['risk_label'],"class":d['risk_class']}})
    gj = {"type":"FeatureCollection","features":feats}
    loc = {"location_name":"Semua lokasi","latitude":-6.86,"longitude":107.85,"today":{"points":[],"peak_rain_probability":None,"risk_total":0}}
    data = json.dumps(gj, ensure_ascii=False)
    htmlmap = leaflet_html(loc, gj, portal=True)
    htmlmap = htmlmap.replace("anemos_app.html", "index.html")
    htmlmap = htmlmap.replace("const color=(c)=>c==='danger'?'#d63d4b':c==='watch'?'#d98912':'#1fbf75';", "const color=(c)=>c==='danger'?'#d63d4b':c==='watch'?'#d98912':'#1fbf75';")
    htmlmap = re.sub(r"const data=.*?;const color=", f"const data={data};const color=", htmlmap)
    return htmlmap


def portal_page(locations: List[Dict[str, Any]], run_id: str) -> str:
    ordered = sorted(locations, key=lambda loc: num(loc['today'].get('risk_total'), 0) or 0, reverse=True)
    cards = []
    for loc in ordered:
        d = loc['today']
        cards.append(f"""
<div class="daycard {esc(d['risk_class'])}">
  {badge(d['risk_class'])}<h3>{esc(loc['location_name'])}</h3>
  <p>{esc(d['date_label'])}. {esc(d['condition'])}. Peluang hujan tertinggi {fmt_pct(d['peak_rain_probability'])} sekitar {hdot(d['peak_rain_hour'])} WIB.</p>
  <div class="mini"><div><small>Hujan</small><b>{fmt_pct(d['peak_rain_probability'])}</b></div><div><small>Jam</small><b>{hdot(d['peak_rain_hour'])}</b></div><div><small>Risiko</small><b>{fmt_num(d['risk_total'])}</b></div></div>
  <p style="margin-top:14px"><a class="btn" href="{esc(loc['slug'])}/anemos_app.html">Buka</a> <a class="btn secondary" href="{esc(loc['slug'])}/anemos_3day.html">3 hari</a></p>
</div>""")
    hottest = ordered[0] if ordered else None
    stable = min(locations, key=lambda loc: num(loc['today'].get('risk_total'), 99) or 99) if locations else None
    title = "Cuaca lokal"
    body = f"""
<div class="hero">
  <section class="panel hero-main"><div class="eyebrows"><span class="chip">LANGIT</span><span class="chip">{VERSION}</span><span class="chip">Run {esc(run_id)}</span></div><h1>Cuaca lokal</h1><p>Pilih lokasi, lihat status, jam rawan, dan peta tanpa membaca tabel panjang.</p></section>
  <aside class="stats"><div class="panel stat"><small>Lokasi</small><b>{len(locations)}</b><span>aktif</span></div><div class="panel stat"><small>Perlu dipantau</small><b>{esc(hottest['location_name'] if hottest else '—')}</b><span>{fmt_num(hottest['today']['risk_total'] if hottest else None)}/100</span></div><div class="panel stat"><small>Paling stabil</small><b>{esc(stable['location_name'] if stable else '—')}</b><span>{fmt_num(stable['today']['risk_total'] if stable else None)}/100</span></div><div class="panel stat"><small>Status</small><b>Operasional</b><span>output tersedia</span></div></aside>
</div><p class="notice">{esc(DISCLAIMER)}</p>
<section class="panel section"><div class="section-head"><h2>Pilih lokasi</h2><small>Diurutkan dari yang paling perlu dipantau.</small></div><div class="cards3">{''.join(cards)}</div></section>
<section class="panel section"><div class="section-head"><h2>Peta lokasi</h2><small>Warna mengikuti risiko hari ini.</small></div><iframe class="mapbox" src="langit_portal_map.html" loading="lazy"></iframe></section>
<section class="panel section"><div class="section-head"><h2>Data publik</h2><small>Untuk arsip dan integrasi.</small></div><p><a class="btn secondary" href="langit_core_manifest.json">Manifest</a> <a class="btn secondary" href="ops.html">Operations</a></p></section>"""
    return doc(title, "home", body, root_rel="")


def ops_page(root: Path, locations: List[Dict[str, Any]], run_id: str) -> str:
    html_files = list(root.rglob("*.html"))
    json_files = list(root.rglob("*.json"))
    csv_files = list(root.rglob("*.csv"))
    important = sorted(html_files, key=lambda p: (str(p.parent), p.name))[:80]
    rows = ''.join(f'<tr><td>{esc(str(p.relative_to(root)))}</td><td>{p.stat().st_size} bytes</td></tr>' for p in important)
    body = f"""
<section class="panel section"><div class="section-head"><h2>Operations monitor</h2><small>Run {esc(run_id)}</small></div><div class="metric-grid"><div class="metric"><small>Lokasi</small><b>{len(locations)}</b></div><div class="metric"><small>HTML</small><b>{len(html_files)}</b></div><div class="metric"><small>JSON</small><b>{len(json_files)}</b></div><div class="metric"><small>CSV</small><b>{len(csv_files)}</b></div></div></section>
<section class="panel section"><div class="section-head"><h2>Output penting</h2><small>Generated public files</small></div><table class="table"><tbody>{rows}</tbody></table></section>"""
    return doc("Operations — LANGIT", "ops", body, root_rel="")


def root_redirect_page(target: str, label: str) -> str:
    return f"""<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="0; url={esc(target)}"><title>{esc(label)} — LANGIT</title><style>{CSS}</style></head><body><main class="shell"><section class="panel section"><h1>{esc(label)}</h1><p>Halaman dipindahkan ke versi lokasi. Jika tidak berpindah otomatis, buka tautan berikut.</p><p><a class="btn" href="{esc(target)}">Buka halaman</a></p></section></main></body></html>"""

# ---------------------------------------------------------------------------
# Build and verification
# ---------------------------------------------------------------------------

def build(root: Path, public_base_url: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    run_id = local_now().strftime("%Y%m%d_%H%M%S")
    meta = meta_from_root(root)
    dirs = location_dirs(root)
    locations = [build_location(d, meta.get(d.name, {}), run_id) for d in dirs]
    if not locations:
        raise SystemExit("Tidak ada folder lokasi valid di outputs/. Jalankan forecast engine dulu.")
    # Prefer Dago for root alias if available, otherwise first sorted location.
    default_loc = next((loc for loc in locations if loc['slug'] == 'dago'), locations[0])
    for loc in locations:
        d = Path(loc['dir'])
        write_json(d / "langit_v101_public.json", public_location_payload(loc))
        write_text(d / "anemos_app.html", summary_page(loc))
        write_text(d / "langit_app.html", summary_page(loc))
        write_text(d / "langit_console.html", summary_page(loc))
        write_text(d / "command_center_sentinel_x.html", summary_page(loc))
        write_text(d / "anemos_3day.html", outlook_page(loc))
        write_text(d / "langit_3day.html", outlook_page(loc))
        write_text(d / "anemos_activity.html", activity_page(loc))
        write_text(d / "langit_activity.html", activity_page(loc))
        write_text(d / "langit_model_court.html", analyst_page(loc))
        write_text(d / "langit_analyst.html", analyst_page(loc))
        write_text(d / "sentinel_x_accuracy_public.html", analyst_page(loc))
        write_text(d / "langit_reliability.html", analyst_page(loc))
        maphtml = map_page(loc)
        write_text(d / "langit_map_room.html", maphtml)
        write_text(d / "anemos_map.html", maphtml)
        write_text(d / "langit_map.html", maphtml)
    write_text(root / "index.html", portal_page(locations, run_id))
    write_text(root / "langit_portal_map.html", portal_map_page(locations))
    write_text(root / "ops.html", ops_page(root, locations, run_id))
    # Fix root-level 404 links by creating intentional aliases.
    aliases = {
        "anemos_app.html": "Ringkasan lokasi",
        "langit_app.html": "Ringkasan lokasi",
        "anemos_activity.html": "Aktivitas lokasi",
        "langit_activity.html": "Aktivitas lokasi",
        "anemos_3day.html": "Prakiraan 3 hari",
        "langit_3day.html": "Prakiraan 3 hari",
        "langit_map_room.html": "Peta lokasi",
        "langit_map.html": "Peta lokasi",
        "anemos_map.html": "Peta lokasi",
        "langit_model_court.html": "Keandalan data",
        "sentinel_x_accuracy_public.html": "Keandalan data",
        "langit_analyst.html": "Keandalan data",
    }
    for filename, label_name in aliases.items():
        write_text(root / filename, root_redirect_page(f"{default_loc['slug']}/{filename}", label_name))
    write_text(root / "404.html", root_redirect_page("index.html", "Halaman tidak ditemukan"))
    manifest = {
        "brand": BRAND,
        "version": VERSION,
        "run_id": run_id,
        "generated_at": local_now().isoformat(),
        "public_base_url": public_base_url,
        "locations": [{"slug": loc['slug'], "name": loc['location_name'], "home": f"{loc['slug']}/anemos_app.html"} for loc in locations],
    }
    write_json(root / "langit_core_manifest.json", manifest)
    verify(root)
    print(f"OK: LANGIT v101 recovery rebuilt {len(locations)} locations.")


def public_location_payload(loc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "brand": BRAND,
        "version": VERSION,
        "slug": loc['slug'],
        "location_name": loc['location_name'],
        "latitude": loc['latitude'],
        "longitude": loc['longitude'],
        "today": {k: v for k, v in loc['today'].items() if k != 'points'},
        "days": [{k: v for k, v in d.items() if k != 'points'} for d in loc['days']],
    }


def verify(root: Path) -> None:
    bad_tokens = ["visualvisual", "ANEMOS sedang", "AETHER Sentinel", "const hours=[.new", "[.new Set"]
    html_files = list(root.rglob("*.html"))
    if not html_files:
        raise SystemExit("Verify failed: tidak ada HTML di outputs/.")
    failures = []
    for path in html_files:
        txt = path.read_text(encoding="utf-8", errors="replace")
        for token in bad_tokens:
            if token in txt:
                failures.append(f"{path.relative_to(root)} contains {token!r}")
        # Check simple local HTML links only. Ignore anchors, http, mailto, javascript.
        for m in re.finditer(r'''href=["']([^"']+\.html)(?:#[^"']*)?["']''', txt):
            href = m.group(1)
            if href.startswith(("http://", "https://", "mailto:", "javascript:")):
                continue
            target = (path.parent / href).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                continue
            if not target.exists():
                failures.append(f"{path.relative_to(root)} broken href -> {href}")
    required = [root / "index.html", root / "ops.html", root / "langit_portal_map.html", root / "404.html"]
    for req in required:
        if not req.exists():
            failures.append(f"missing {req.relative_to(root)}")
    for d in location_dirs(root):
        for name in ["anemos_app.html", "anemos_3day.html", "anemos_activity.html", "langit_map_room.html", "langit_model_court.html"]:
            if not (d / name).exists():
                failures.append(f"missing {d.name}/{name}")
    if failures:
        for f in failures[:50]:
            print("ERROR:", f)
        raise SystemExit(3)
    print(f"OK: verified {len(html_files)} HTML files.")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LANGIT v101 recovery public layer")
    p.add_argument("--root", default="outputs")
    p.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    p.add_argument("--verify-only", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    if args.verify_only:
        verify(root)
        return 0
    build(root, args.public_base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
