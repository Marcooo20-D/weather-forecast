#!/usr/bin/env python3
"""
LANGIT v64 — Atmospheric Map Engine.

Post-processor untuk output LANGIT v63/v63.1.
Fokus:
1. Mengubah halaman peta dari marker biasa menjadi atmospheric map.
2. Membuat peta lokasi dengan layer: Risiko, Hujan, Panas, Kelembapan, Confidence, Angin.
3. Membuat portal peta regional untuk semua lokasi.
4. Menyediakan verify-only agar GitHub Actions gagal cepat jika output peta rusak.

Pakai di root repo:
  python langit_v64_atmospheric_map_engine.py --root outputs --public-base-url https://marcooo20-d.github.io/weather-forecast

Verify:
  python langit_v64_atmospheric_map_engine.py --root outputs --verify-only
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

VERSION = "v64"
ENGINE_NAME = "LANGIT v64 Atmospheric Map Engine"
JAKARTA = ZoneInfo("Asia/Jakarta")

BAD_PUBLIC_TOKENS = [
    "visual-first",
    "ANEMOS sedang",
    "AETHER Sentinel",
    "[.new Set",
    "const hours=[.new",
    "Data confidence",
]

MONTH_ID = [
    "", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember"
]

DAY_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]


@dataclass
class HourPoint:
    iso: str
    date_label: str
    day_label: str
    hour: str
    temp: float | None
    feels: float | None
    rh: float | None
    rain: float | None
    wind_speed: float | None
    wind_dir: float | None
    condition: str
    risk: int
    status: str
    confidence: int
    note: str


@dataclass
class LocationPack:
    slug: str
    name: str
    short_name: str
    admin: str
    lat: float
    lon: float
    updated_label: str
    generated_label: str
    hours: list[HourPoint]


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    text = str(value).strip()
    if not text or text in {"-", "—", "None", "null", "nan"}:
        return None
    text = text.replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def as_int_percent(value: Any) -> int | None:
    num = as_float(value)
    if num is None:
        return None
    if 0 <= num <= 1:
        num *= 100
    return int(round(clamp(num, 0, 100)))


def normalize_hour(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        hour = int(value)
        if 0 <= hour <= 23:
            return f"{hour:02d}:00"
    text = str(value).strip()
    if not text:
        return None

    # ISO-like datetime.
    iso_match = re.search(r"T?(\d{1,2}):(\d{2})", text)
    if iso_match:
        h = int(iso_match.group(1))
        m = int(iso_match.group(2))
        if 0 <= h <= 23:
            return f"{h:02d}:{m:02d}"

    # Plain HH or HHMM.
    simple = re.fullmatch(r"(\d{1,2})(?:\.00)?", text)
    if simple:
        h = int(simple.group(1))
        if 0 <= h <= 23:
            return f"{h:02d}:00"

    return None


def parse_datetime_any(value: Any, fallback_day: date, fallback_hour: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = "" if value is None else str(value).strip()
        text = text.replace("Z", "+00:00")
        dt = None
        if text:
            for candidate in [
                text,
                text.replace(" ", "T"),
                re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text),
            ]:
                try:
                    dt = datetime.fromisoformat(candidate)
                    break
                except ValueError:
                    pass
        if dt is None:
            h, m = [int(x) for x in fallback_hour.split(":")]
            dt = datetime(fallback_day.year, fallback_day.month, fallback_day.day, h, m)

    if dt.tzinfo is not None:
        dt = dt.astimezone(JAKARTA).replace(tzinfo=None)
    return dt


def date_id(d: date) -> str:
    return f"{DAY_ID[d.weekday()]}, {d.day} {MONTH_ID[d.month]} {d.year}"


def sentence_case(text: Any, fallback: str = "Berawan") -> str:
    s = str(text or "").strip()
    if not s or s in {"-", "—", "None", "null"}:
        s = fallback
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:1].upper() + s[1:]


def pick_value(row: dict[str, Any], names: list[str]) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    compact = {re.sub(r"[^a-z0-9]+", "", str(k).lower()): v for k, v in row.items()}
    for name in names:
        key = name.lower()
        if key in lower:
            return lower[key]
        ckey = re.sub(r"[^a-z0-9]+", "", key)
        if ckey in compact:
            return compact[ckey]
    for key, value in lower.items():
        for name in names:
            if name.lower() in key:
                return value
    return None


def heat_index_simple(temp: float | None, rh: float | None) -> float | None:
    if temp is None:
        return None
    if rh is None or temp < 27:
        return round(temp, 1)

    # NOAA heat index approximation converted to Celsius.
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


def risk_from_fields(rain: int | None, feels: float | None, rh: float | None, confidence: int) -> int:
    rain_part = rain if rain is not None else 0
    heat_part = 0
    if feels is not None:
        if feels >= 40:
            heat_part = 35
        elif feels >= 37:
            heat_part = 25
        elif feels >= 34:
            heat_part = 15
        elif feels >= 32:
            heat_part = 8
    humidity_part = 0
    if rh is not None and rh >= 88:
        humidity_part = 8
    confidence_penalty = max(0, 70 - confidence) * 0.25
    return int(round(clamp(max(rain_part, heat_part) + humidity_part + confidence_penalty, 0, 100)))


def status_from_risk(risk: int) -> str:
    if risk >= 76:
        return "Tinggi"
    if risk >= 56:
        return "Waspada"
    if risk >= 31:
        return "Pantau"
    return "Aman"


def note_from_fields(status: str, rain: int | None, feels: float | None, condition: str) -> str:
    rain = rain or 0
    if status == "Tinggi":
        return "Tunda aktivitas luar ruang jika tidak perlu."
    if status == "Waspada":
        return "Siapkan pelindung hujan dan pantau perubahan lokal."
    if rain >= 30:
        return "Awan/hujan perlu dipantau pada jam ini."
    if feels is not None and feels >= 34:
        return "Panas terasa meningkat; pilih aktivitas ringan."
    if "cerah" in condition.lower():
        return "Kondisi cukup baik untuk aktivitas luar ruang."
    return "Kondisi masih relatif aman, tetap pantau lokal."


def confidence_from_row(row: dict[str, Any]) -> int:
    explicit = as_int_percent(pick_value(row, ["confidence", "confidence_pct", "kepercayaan", "data_confidence"]))
    if explicit is not None:
        return explicit
    active = pick_value(row, ["active_sources", "sources_active", "source_count", "model_count"])
    total = pick_value(row, ["total_sources", "sources_total", "model_total"])
    a = as_float(active)
    t = as_float(total)
    if a is not None and t is not None and t > 0:
        return int(round(clamp(a / t * 100, 0, 100)))
    return 72


def flatten_dicts(obj: Any, path: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            for x in obj:
                x2 = dict(x)
                x2["_path"] = path
                rows.append(x2)
        for i, item in enumerate(obj):
            rows.extend(flatten_dicts(item, f"{path}[{i}]"))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            rows.extend(flatten_dicts(value, f"{path}.{key}" if path else str(key)))
    return rows


def row_score(row: dict[str, Any]) -> int:
    keys = " ".join(str(k).lower() for k in row.keys())
    score = 0
    if any(k in keys for k in ["time", "hour", "jam", "datetime", "valid"]):
        score += 3
    if any(k in keys for k in ["temp", "suhu", "temperature"]):
        score += 3
    if any(k in keys for k in ["rain", "precip", "hujan", "pop"]):
        score += 3
    if any(k in keys for k in ["humidity", "rh", "kelembapan"]):
        score += 2
    if any(k in keys for k in ["weather", "condition", "summary", "cuaca"]):
        score += 1
    return score


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_csv_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row["_path"] = str(path.name)
                rows.append(dict(row))
    except Exception:
        pass
    return rows


def extract_geojson(loc_dir: Path) -> tuple[float | None, float | None, str | None]:
    for name in ["langit_location.geojson", "location.geojson", "geojson.json"]:
        p = loc_dir / name
        if not p.exists():
            continue
        data = load_json(p)
        if not isinstance(data, dict):
            continue
        features = data.get("features")
        if isinstance(features, list) and features:
            feat = features[0]
            props = feat.get("properties", {}) if isinstance(feat, dict) else {}
            geom = feat.get("geometry", {}) if isinstance(feat, dict) else {}
            coords = geom.get("coordinates") if isinstance(geom, dict) else None
            if isinstance(coords, list) and len(coords) >= 2:
                return as_float(coords[1]), as_float(coords[0]), str(props.get("name") or props.get("title") or "") or None
        coords = data.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            return as_float(coords[1]), as_float(coords[0]), None
    return None, None, None


def title_from_slug(slug: str) -> tuple[str, str, str]:
    known = {
        "arjawinangun": ("Arjawinangun, Cirebon", "Arjawinangun", "Cirebon"),
        "dago": ("Dago, Bandung", "Dago", "Bandung"),
        "jatinangor": ("Jatinangor, Sumedang", "Jatinangor", "Sumedang"),
    }
    if slug in known:
        return known[slug]
    clean = slug.replace("-", " ").replace("_", " ").title()
    return clean, clean, ""


def extract_location_name(api_data: Any, slug: str, geo_name: str | None) -> tuple[str, str, str]:
    full, short, admin = title_from_slug(slug)
    candidates: list[str] = []
    if geo_name:
        candidates.append(geo_name)

    if isinstance(api_data, dict):
        for key in ["location_name", "name", "title", "label", "display_name"]:
            v = api_data.get(key)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
        loc = api_data.get("location")
        if isinstance(loc, dict):
            parts = []
            for key in ["name", "adm4", "city", "regency", "admin", "province"]:
                v = loc.get(key)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
            if parts:
                candidates.append(", ".join(dict.fromkeys(parts)))

    if candidates:
        full = candidates[0]
        bits = [b.strip() for b in full.split(",")]
        short = bits[0] if bits else full
        admin = bits[1] if len(bits) > 1 else admin
    return full, short, admin


def extract_lat_lon(api_data: Any, loc_dir: Path) -> tuple[float, float]:
    lat, lon, _ = extract_geojson(loc_dir)
    if lat is not None and lon is not None:
        return lat, lon

    def walk(obj: Any) -> tuple[float | None, float | None]:
        if isinstance(obj, dict):
            lat_value = pick_value(obj, ["latitude", "lat"])
            lon_value = pick_value(obj, ["longitude", "lon", "lng"])
            la = as_float(lat_value)
            lo = as_float(lon_value)
            if la is not None and lo is not None and -90 <= la <= 90 and -180 <= lo <= 180:
                return la, lo
            for value in obj.values():
                found = walk(value)
                if found[0] is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = walk(value)
                if found[0] is not None:
                    return found
        return None, None

    found_lat, found_lon = walk(api_data)
    if found_lat is not None and found_lon is not None:
        return found_lat, found_lon

    # Safe fallback around West Java.
    if loc_dir.name == "dago":
        return -6.883, 107.613
    if loc_dir.name == "jatinangor":
        return -6.933, 107.771
    if loc_dir.name == "arjawinangun":
        return -6.646, 108.408
    return -6.9, 107.6


def collect_candidate_rows(loc_dir: Path, api_data: Any) -> list[dict[str, Any]]:
    rows = []
    if api_data is not None:
        rows.extend(flatten_dicts(api_data))
    for csv_path in sorted(loc_dir.glob("*.csv")):
        if any(skip in csv_path.name.lower() for skip in ["source", "model", "accuracy"]):
            continue
        rows.extend(load_csv_rows(csv_path))
    rows = [r for r in rows if row_score(r) >= 5]
    # Avoid duplicates by iso/time/temp/rain-ish signature.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        sig = "|".join(str(pick_value(row, keys)) for keys in [
            ["time", "datetime", "valid_time", "date_time", "timestamp", "jam", "hour"],
            ["temperature_2m", "temp", "suhu", "temperature"],
            ["precipitation_probability", "rain_probability", "pop", "hujan", "rain"],
        ])
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(row)
    return unique


def build_hours(rows: list[dict[str, Any]]) -> list[HourPoint]:
    today = datetime.now(JAKARTA).date()
    points: list[HourPoint] = []

    for row in rows:
        time_value = pick_value(row, [
            "time", "datetime", "valid_time", "date_time", "timestamp", "target_time",
            "jam", "hour", "local_time"
        ])
        hour = normalize_hour(time_value) or normalize_hour(pick_value(row, ["jam", "hour"])) or None
        if hour is None:
            continue

        dt = parse_datetime_any(time_value, today, hour)
        temp = as_float(pick_value(row, [
            "temperature_2m", "temp", "temperature", "suhu", "t2m", "air_temperature",
            "current_temp"
        ]))
        rh = as_float(pick_value(row, [
            "relative_humidity_2m", "relative_humidity", "humidity", "rh", "kelembapan"
        ]))
        rain = as_int_percent(pick_value(row, [
            "precipitation_probability", "rain_probability", "rain_prob", "pop",
            "probability_of_precipitation", "hujan", "peluang_hujan", "rain_chance",
            "precip_prob"
        ]))
        feels = as_float(pick_value(row, [
            "apparent_temperature", "feels_like", "heat_index", "terasa", "suhu_terasa"
        ]))
        if feels is None:
            feels = heat_index_simple(temp, rh)

        wind_speed = as_float(pick_value(row, [
            "wind_speed_10m", "wind_speed", "windspeed", "angin", "kecepatan_angin"
        ]))
        wind_dir = as_float(pick_value(row, [
            "wind_direction_10m", "winddirection_10m", "wind_dir", "wind_direction",
            "arah_angin"
        ]))
        condition = sentence_case(pick_value(row, [
            "condition", "weather", "summary", "cuaca", "weather_desc", "description"
        ]), "Berawan")
        confidence = confidence_from_row(row)
        risk = risk_from_fields(rain, feels, rh, confidence)
        status = status_from_risk(risk)
        note = note_from_fields(status, rain, feels, condition)

        points.append(HourPoint(
            iso=dt.isoformat(timespec="minutes"),
            date_label=date_id(dt.date()),
            day_label=("Hari ini" if dt.date() == today else "Besok" if dt.date() == today.replace(day=today.day) else DAY_ID[dt.weekday()]),
            hour=f"{dt.hour:02d}:{dt.minute:02d}",
            temp=None if temp is None else round(temp, 1),
            feels=None if feels is None else round(feels, 1),
            rh=None if rh is None else round(rh),
            rain=rain,
            wind_speed=None if wind_speed is None else round(wind_speed, 1),
            wind_dir=None if wind_dir is None else round(wind_dir % 360),
            condition=condition,
            risk=risk,
            status=status,
            confidence=confidence,
            note=note,
        ))

    if not points:
        default_hours = ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]
        for h in default_hours:
            dt = parse_datetime_any(None, today, h)
            points.append(HourPoint(
                iso=dt.isoformat(timespec="minutes"),
                date_label=date_id(dt.date()),
                day_label="Hari ini",
                hour=h,
                temp=None,
                feels=None,
                rh=None,
                rain=0,
                wind_speed=2.0,
                wind_dir=115,
                condition="Data terbatas",
                risk=20,
                status="Aman",
                confidence=45,
                note="Data terbatas; pantau sumber resmi untuk keputusan penting.",
            ))

    # Keep first 72h-ish and sort.
    points.sort(key=lambda p: p.iso)
    compact: list[HourPoint] = []
    seen: set[tuple[str, str]] = set()
    for p in points:
        key = (p.date_label, p.hour)
        if key in seen:
            continue
        seen.add(key)
        compact.append(p)
        if len(compact) >= 72:
            break
    return compact


def read_location(loc_dir: Path) -> LocationPack | None:
    if not loc_dir.is_dir():
        return None
    api_data = None
    for name in ["langit_api_v1.json", "anemos_api_v1.json", "forecast.json", "public_api.json"]:
        p = loc_dir / name
        if p.exists():
            api_data = load_json(p)
            if api_data is not None:
                break

    lat, lon, geo_name = extract_geojson(loc_dir)
    full, short, admin = extract_location_name(api_data, loc_dir.name, geo_name)
    final_lat, final_lon = extract_lat_lon(api_data, loc_dir)
    if lat is not None and lon is not None:
        final_lat, final_lon = lat, lon

    rows = collect_candidate_rows(loc_dir, api_data)
    hours = build_hours(rows)
    now = datetime.now(JAKARTA)
    generated = date_id(now.date())
    updated = f"{generated}, {now:%H:%M} WIB"
    return LocationPack(
        slug=loc_dir.name,
        name=full,
        short_name=short,
        admin=admin,
        lat=final_lat,
        lon=final_lon,
        updated_label=updated,
        generated_label=generated,
        hours=hours,
    )


def best_hour(hours: list[HourPoint]) -> HourPoint:
    return max(hours, key=lambda h: (h.rain or 0, h.risk, h.feels or 0))


def safest_hour(hours: list[HourPoint]) -> HourPoint:
    return min(hours, key=lambda h: (h.risk, h.rain or 0, h.feels or 99))


def esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def data_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def map_color_expr() -> str:
    return r"""
function colorForValue(value, layer){
  if(layer === 'confidence'){
    if(value < 45) return '#8b5cf6';
    if(value < 65) return '#f59e0b';
    return '#2dd4bf';
  }
  if(layer === 'humidity'){
    if(value >= 90) return '#8b5cf6';
    if(value >= 80) return '#38bdf8';
    return '#2dd4bf';
  }
  if(layer === 'heat'){
    if(value >= 38) return '#ef4444';
    if(value >= 34) return '#f97316';
    if(value >= 31) return '#facc15';
    return '#2dd4bf';
  }
  if(value >= 76) return '#ef4444';
  if(value >= 56) return '#f97316';
  if(value >= 31) return '#facc15';
  return '#2dd4bf';
}
function valueFor(point, layer){
  if(layer === 'rain') return Number(point.rain ?? 0);
  if(layer === 'heat') return Number(point.feels ?? point.temp ?? 0);
  if(layer === 'humidity') return Number(point.rh ?? 0);
  if(layer === 'confidence') return Number(point.confidence ?? 0);
  if(layer === 'wind') return Number(point.wind_speed ?? 2);
  return Number(point.risk ?? 0);
}
"""


def build_map_html(pack: LocationPack, *, portal: bool = False, all_locations: list[LocationPack] | None = None) -> str:
    locs = all_locations or [pack]
    payload = {
        "version": VERSION,
        "engine": ENGINE_NAME,
        "label": "LANGIT v64 Atmospheric Map Engine",
        "location": asdict(pack),
        "locations": [asdict(x) for x in locs],
        "portal": portal,
    }
    title = "LANGIT Portal Map" if portal else f"LANGIT Map — {pack.name}"
    subtitle = "Regional risk field" if portal else pack.name
    now_label = pack.updated_label
    first = pack.hours[0]
    peak = best_hour(pack.hours)

    leaflet_css = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    leaflet_js = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"

    return f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="generator" content="LANGIT v64 Atmospheric Map Engine">
<link rel="stylesheet" href="{leaflet_css}">
<style>
:root {{
  --bg:#06111f;
  --panel:rgba(7,20,35,.86);
  --panel2:rgba(11,30,50,.90);
  --line:rgba(148,163,184,.26);
  --text:#f8fbff;
  --muted:#b8c7d9;
  --blue:#25a9ff;
  --green:#2dd4bf;
  --yellow:#facc15;
  --orange:#fb923c;
  --red:#ef4444;
}}
* {{ box-sizing:border-box; }}
html,body,#map {{ height:100%; margin:0; }}
body {{
  font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background:var(--bg);
  color:var(--text);
  overflow:hidden;
}}
#map {{ z-index:1; background:#07111d; }}
.map-shell::after {{
  content:"";
  position:absolute;
  inset:0;
  z-index:420;
  pointer-events:none;
  background:
    radial-gradient(circle at 50% 45%, rgba(37,169,255,.12), transparent 24%),
    linear-gradient(90deg, rgba(3,12,24,.34), transparent 24%, transparent 72%, rgba(3,12,24,.28));
  mix-blend-mode:screen;
}}
.wind-canvas {{
  position:absolute;
  inset:0;
  width:100%;
  height:100%;
  z-index:430;
  pointer-events:none;
  opacity:.54;
}}
.top-card {{
  position:absolute;
  z-index:700;
  left:20px;
  top:20px;
  width:min(360px, calc(100vw - 40px));
  padding:18px 18px 16px;
  border:1px solid rgba(96,165,250,.42);
  border-radius:18px;
  background:linear-gradient(180deg, rgba(5,15,30,.92), rgba(5,17,31,.78));
  box-shadow:0 18px 40px rgba(0,0,0,.34);
  backdrop-filter:blur(16px);
}}
.brand {{
  display:flex;
  gap:10px;
  align-items:center;
  font-weight:900;
  letter-spacing:-.02em;
  margin-bottom:8px;
}}
.logo {{
  width:26px;height:26px;border-radius:9px;
  background:linear-gradient(135deg,#31e6c3,#20a4ff 55%,#1d4ed8);
  box-shadow:0 0 24px rgba(37,169,255,.55);
}}
.top-card h1 {{
  margin:0 0 6px;
  font-size:24px;
  line-height:1.05;
  letter-spacing:-.04em;
}}
.top-card p {{
  margin:0;
  color:var(--muted);
  font-size:13px;
  line-height:1.45;
}}
.stat-grid {{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:8px;
  margin-top:14px;
}}
.stat {{
  border:1px solid var(--line);
  background:rgba(15,38,61,.70);
  border-radius:12px;
  padding:10px;
}}
.stat b {{ display:block; font-size:18px; line-height:1.1; }}
.stat span {{ color:var(--muted); font-size:11px; }}
.controls {{
  position:absolute;
  z-index:710;
  right:20px;
  top:20px;
  display:flex;
  flex-wrap:wrap;
  gap:8px;
  justify-content:flex-end;
  max-width:min(520px, calc(100vw - 420px));
}}
.pill, .map-btn {{
  border:1px solid rgba(148,163,184,.34);
  color:var(--text);
  background:rgba(6,18,32,.82);
  border-radius:999px;
  padding:9px 12px;
  font-size:12px;
  font-weight:800;
  cursor:pointer;
  box-shadow:0 10px 24px rgba(0,0,0,.16);
  backdrop-filter:blur(14px);
}}
.pill.active {{
  background:linear-gradient(135deg,#1d4ed8,#25a9ff);
  border-color:rgba(125,211,252,.75);
}}
.timeline {{
  position:absolute;
  z-index:720;
  left:50%;
  bottom:24px;
  transform:translateX(-50%);
  width:min(760px, calc(100vw - 40px));
  border:1px solid rgba(148,163,184,.30);
  background:rgba(4,15,27,.86);
  border-radius:22px;
  padding:10px;
  box-shadow:0 20px 50px rgba(0,0,0,.42);
  backdrop-filter:blur(16px);
}}
.timeline-head {{
  display:flex;
  align-items:center;
  justify-content:space-between;
  color:var(--muted);
  font-size:12px;
  margin:2px 6px 8px;
}}
.time-row {{
  display:flex;
  gap:8px;
  overflow-x:auto;
  padding-bottom:2px;
}}
.time {{
  flex:0 0 auto;
  border:1px solid rgba(148,163,184,.30);
  background:rgba(15,38,61,.88);
  color:var(--text);
  border-radius:13px;
  padding:9px 11px;
  min-width:64px;
  text-align:center;
  font-weight:900;
  cursor:pointer;
}}
.time small {{
  display:block;
  margin-top:2px;
  color:var(--muted);
  font-weight:700;
  font-size:10px;
}}
.time.active {{
  background:linear-gradient(135deg,#1d4ed8,#24a8ff);
  border-color:rgba(125,211,252,.8);
}}
.legend {{
  position:absolute;
  z-index:705;
  right:20px;
  bottom:112px;
  padding:12px;
  border-radius:14px;
  border:1px solid var(--line);
  background:rgba(4,15,27,.84);
  backdrop-filter:blur(14px);
  font-size:12px;
}}
.legend div {{ display:flex; align-items:center; gap:8px; margin:5px 0; }}
.dot {{ width:9px; height:9px; border-radius:50%; display:inline-block; }}
.info-card {{
  position:absolute;
  z-index:704;
  left:20px;
  bottom:112px;
  width:min(360px, calc(100vw - 40px));
  border:1px solid var(--line);
  background:rgba(4,15,27,.84);
  border-radius:18px;
  padding:14px;
  backdrop-filter:blur(14px);
}}
.info-card h2 {{
  margin:0 0 8px;
  font-size:18px;
  letter-spacing:-.03em;
}}
.info-card p {{
  margin:0;
  color:var(--muted);
  font-size:13px;
  line-height:1.45;
}}
.popup-title {{ font-weight:900; font-size:15px; margin-bottom:6px; }}
.popup-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:6px; margin-top:8px; }}
.popup-grid div {{ background:#eef6ff; padding:6px 8px; border-radius:8px; color:#06111f; }}
.popup-grid b {{ display:block; }}
.leaflet-container {{ font-family:inherit; }}
.leaflet-control-attribution {{ font-size:10px; opacity:.65; }}
.leaflet-popup-content-wrapper {{
  border-radius:16px;
  box-shadow:0 20px 45px rgba(0,0,0,.30);
}}
.leaflet-popup-content {{ margin:14px; }}
@media(max-width:760px) {{
  .controls {{ top:auto; right:12px; bottom:190px; max-width:calc(100vw - 24px); }}
  .top-card {{ left:12px; top:12px; }}
  .info-card {{ display:none; }}
  .legend {{ right:12px; bottom:104px; }}
  .timeline {{ bottom:14px; }}
}}
</style>
</head>
<body>
<!-- LANGIT v64 Atmospheric Map Engine -->
<div id="map" class="map-shell"></div>
<canvas id="wind" class="wind-canvas"></canvas>

<section class="top-card" aria-label="Ringkasan peta">
  <div class="brand"><span class="logo"></span><span>LANGIT</span></div>
  <h1>{esc(title)}</h1>
  <p>{esc(subtitle)}. Layer warna mengikuti jam yang dipilih. Tanggal dan peluang hujan tersedia di popup.</p>
  <div class="stat-grid">
    <div class="stat"><b id="statStatus">{esc(first.status)}</b><span>Status</span></div>
    <div class="stat"><b id="statRain">{esc(first.rain if first.rain is not None else 0)}%</b><span>Peluang hujan</span></div>
    <div class="stat"><b id="statRisk">{esc(first.risk)}/100</b><span>Risiko</span></div>
    <div class="stat"><b id="statHour">{esc(first.hour)}</b><span>Jam aktif</span></div>
  </div>
</section>

<nav class="controls" aria-label="Layer peta">
  <button class="pill active" data-layer="risk">Risiko</button>
  <button class="pill" data-layer="rain">Hujan</button>
  <button class="pill" data-layer="heat">Panas</button>
  <button class="pill" data-layer="humidity">Lembap</button>
  <button class="pill" data-layer="confidence">Confidence</button>
  <button class="pill" data-layer="wind">Angin</button>
  <button class="map-btn" id="toggleBase">Mode peta</button>
</nav>

<aside class="legend" aria-label="Legenda">
  <div><span class="dot" style="background:#2dd4bf"></span>Aman</div>
  <div><span class="dot" style="background:#facc15"></span>Pantau</div>
  <div><span class="dot" style="background:#fb923c"></span>Waspada</div>
  <div><span class="dot" style="background:#ef4444"></span>Tinggi</div>
  <div><span class="dot" style="background:#8b5cf6"></span>Confidence rendah</div>
</aside>

<section class="info-card">
  <h2 id="panelTitle">{esc(pack.name)}</h2>
  <p id="panelText">{esc(now_label)}. Puncak hujan sekitar {esc(peak.hour)} dengan peluang {esc(peak.rain if peak.rain is not None else 0)}%.</p>
</section>

<section class="timeline" aria-label="Timeline prakiraan">
  <div class="timeline-head">
    <button class="map-btn" id="playBtn">Play</button>
    <span id="dateLabel">{esc(first.date_label)}</span>
  </div>
  <div class="time-row" id="timeRow"></div>
</section>

<script src="{leaflet_js}"></script>
<script>
const LANGIT = {data_json(payload)};
{map_color_expr()}

let currentLayer = 'risk';
let currentIndex = 0;
let playing = false;
let playTimer = null;
let baseIndex = 0;
let fieldLayers = [];
let markers = [];

const baseLayers = [
  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{maxZoom:19, attribution:'&copy; OpenStreetMap & CARTO'}}),
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:19, attribution:'&copy; OpenStreetMap'}}),
  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{maxZoom:19, attribution:'&copy; OpenStreetMap & CARTO'}})
];

const center = [LANGIT.location.lat, LANGIT.location.lon];
const map = L.map('map', {{zoomControl:true, attributionControl:true}}).setView(center, LANGIT.portal ? 8 : 10);
baseLayers[baseIndex].addTo(map);

function activePoint(loc){{
  return loc.hours[Math.min(currentIndex, loc.hours.length - 1)] || loc.hours[0];
}}

function fieldOffsets(){{
  return [
    [0,0,1.0], [0.035,0,0.82], [-0.035,0,0.78], [0,0.035,0.76], [0,-0.035,0.74],
    [0.055,0.03,0.55], [-0.05,-0.025,0.50], [0.026,-0.055,0.46], [-0.026,0.052,0.44]
  ];
}}

function clearFields(){{
  fieldLayers.forEach(x => map.removeLayer(x));
  fieldLayers = [];
  markers.forEach(x => map.removeLayer(x));
  markers = [];
}}

function popupHtml(loc, point){{
  const value = valueFor(point, currentLayer);
  return `<div class="popup-title">${{loc.name}}</div>
  <div>${{point.date_label}} — ${{point.hour}} WIB</div>
  <div class="popup-grid">
    <div><b>${{point.status}}</b>Status</div>
    <div><b>${{point.rain ?? 0}}%</b>Hujan</div>
    <div><b>${{point.temp ?? '—'}}°C</b>Suhu</div>
    <div><b>${{point.feels ?? '—'}}°C</b>Terasa</div>
    <div><b>${{point.rh ?? '—'}}%</b>RH</div>
    <div><b>${{point.risk}}/100</b>Risiko</div>
  </div>
  <p style="margin:8px 0 0;color:#334155">${{point.note}}</p>`;
}}

function draw(){{
  clearFields();
  const locs = LANGIT.portal ? LANGIT.locations : [LANGIT.location];

  locs.forEach((loc) => {{
    const point = activePoint(loc);
    const raw = valueFor(point, currentLayer);
    const color = colorForValue(raw, currentLayer);
    const normalized = currentLayer === 'heat' ? Math.max(0, raw - 24) * 4 : raw;
    const baseRadius = 1200 + Math.min(18000, Math.max(0, normalized) * (LANGIT.portal ? 180 : 110));

    fieldOffsets().forEach(([dlat, dlon, factor], idx) => {{
      const circle = L.circle([loc.lat + dlat, loc.lon + dlon], {{
        radius: baseRadius * factor,
        color: idx === 0 ? color : 'transparent',
        weight: idx === 0 ? 2 : 0,
        fillColor: color,
        fillOpacity: idx === 0 ? 0.24 : 0.09 * factor,
        interactive: idx === 0
      }});
      if(idx === 0) circle.bindPopup(popupHtml(loc, point));
      circle.addTo(map);
      fieldLayers.push(circle);
    }});

    const marker = L.circleMarker([loc.lat, loc.lon], {{
      radius: LANGIT.portal ? 9 : 11,
      color:'#eaffff',
      weight:2,
      fillColor:color,
      fillOpacity:.95
    }}).bindPopup(popupHtml(loc, point)).addTo(map);
    markers.push(marker);
  }});

  const main = activePoint(LANGIT.location);
  document.getElementById('statStatus').textContent = main.status;
  document.getElementById('statRain').textContent = `${{main.rain ?? 0}}%`;
  document.getElementById('statRisk').textContent = `${{main.risk}}/100`;
  document.getElementById('statHour').textContent = main.hour;
  document.getElementById('dateLabel').textContent = main.date_label;
  document.getElementById('panelTitle').textContent = LANGIT.portal ? 'Regional risk field' : LANGIT.location.name;
  document.getElementById('panelText').textContent =
    `${{main.date_label}} pukul ${{main.hour}} WIB. ${{main.note}} Layer aktif: ${{currentLayer}}.`;
}}

function buildTimeline(){{
  const row = document.getElementById('timeRow');
  row.innerHTML = '';
  LANGIT.location.hours.forEach((p, i) => {{
    const btn = document.createElement('button');
    btn.className = 'time' + (i === currentIndex ? ' active' : '');
    btn.innerHTML = `${{p.hour}}<small>${{p.rain ?? 0}}%</small>`;
    btn.onclick = () => {{
      currentIndex = i;
      document.querySelectorAll('.time').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
      draw();
    }};
    row.appendChild(btn);
  }});
}}

document.querySelectorAll('[data-layer]').forEach(btn => {{
  btn.addEventListener('click', () => {{
    currentLayer = btn.dataset.layer;
    document.querySelectorAll('[data-layer]').forEach(x => x.classList.remove('active'));
    btn.classList.add('active');
    draw();
  }});
}});

document.getElementById('toggleBase').onclick = () => {{
  map.removeLayer(baseLayers[baseIndex]);
  baseIndex = (baseIndex + 1) % baseLayers.length;
  baseLayers[baseIndex].addTo(map);
}};

document.getElementById('playBtn').onclick = () => {{
  playing = !playing;
  document.getElementById('playBtn').textContent = playing ? 'Pause' : 'Play';
  if(playing){{
    playTimer = setInterval(() => {{
      currentIndex = (currentIndex + 1) % LANGIT.location.hours.length;
      buildTimeline();
      draw();
    }}, 1100);
  }} else {{
    clearInterval(playTimer);
  }}
}};

map.on('click', (e) => {{
  const loc = LANGIT.location;
  const p = activePoint(loc);
  const dist = map.distance(e.latlng, L.latLng(loc.lat, loc.lon)) / 1000;
  const base = valueFor(p, currentLayer);
  const estimate = Math.round(Math.max(0, base * Math.exp(-dist / 18)));
  L.popup()
    .setLatLng(e.latlng)
    .setContent(`<div class="popup-title">Titik dipilih</div>
      <div>Jarak dari ${{loc.short_name}}: ${{dist.toFixed(1)}} km</div>
      <div class="popup-grid"><div><b>${{estimate}}</b>Estimasi layer</div><div><b>${{p.hour}}</b>Jam</div></div>
      <p style="margin:8px 0 0;color:#334155">Estimasi lokal, bukan observasi titik.</p>`)
    .openOn(map);
}});

document.addEventListener('keydown', (e) => {{
  if(e.key === 'ArrowRight') currentIndex = Math.min(LANGIT.location.hours.length - 1, currentIndex + 1);
  if(e.key === 'ArrowLeft') currentIndex = Math.max(0, currentIndex - 1);
  buildTimeline();
  draw();
}});

// Wind canvas.
const canvas = document.getElementById('wind');
const ctx = canvas.getContext('2d');
let particles = [];
function resizeCanvas(){{
  canvas.width = window.innerWidth * devicePixelRatio;
  canvas.height = window.innerHeight * devicePixelRatio;
  canvas.style.width = window.innerWidth + 'px';
  canvas.style.height = window.innerHeight + 'px';
  ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
  particles = Array.from({{length: Math.min(520, Math.floor(window.innerWidth * window.innerHeight / 2600))}}, () => ({{
    x: Math.random()*window.innerWidth,
    y: Math.random()*window.innerHeight,
    age: Math.random()*80
  }}));
}}
function animateWind(){{
  const p = activePoint(LANGIT.location);
  const dir = Number(p.wind_dir ?? 115) * Math.PI / 180;
  const speed = Math.max(0.8, Math.min(4.8, Number(p.wind_speed ?? 2.5)));
  ctx.clearRect(0,0,window.innerWidth,window.innerHeight);
  ctx.strokeStyle = 'rgba(174,232,255,.32)';
  ctx.lineWidth = 1;
  particles.forEach(pt => {{
    const wobble = Math.sin((pt.x + pt.y + pt.age) * 0.006) * 0.55;
    const vx = Math.sin(dir + wobble) * speed;
    const vy = -Math.cos(dir + wobble) * speed;
    ctx.beginPath();
    ctx.moveTo(pt.x, pt.y);
    pt.x += vx;
    pt.y += vy;
    pt.age += 1;
    ctx.lineTo(pt.x, pt.y);
    ctx.stroke();
    if(pt.x < -40 || pt.x > window.innerWidth + 40 || pt.y < -40 || pt.y > window.innerHeight + 40 || pt.age > 150){{
      pt.x = Math.random()*window.innerWidth;
      pt.y = Math.random()*window.innerHeight;
      pt.age = 0;
    }}
  }});
  requestAnimationFrame(animateWind);
}}
window.addEventListener('resize', resizeCanvas);
resizeCanvas();
buildTimeline();
draw();
animateWind();
</script>
</body>
</html>
"""


def build_portal_html(locations: list[LocationPack]) -> str:
    primary = max(locations, key=lambda p: best_hour(p.hours).risk) if locations else None
    if primary is None:
        raise RuntimeError("Tidak ada lokasi untuk portal.")
    return build_map_html(primary, portal=True, all_locations=locations)


def build_redirect_html(target: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0; url={esc(target)}">
<title>{esc(title)}</title>
<style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#06111f;color:#f8fbff;display:grid;place-items:center;height:100vh;margin:0}}
a{{color:#38bdf8}}
</style>
</head>
<body>
<p>Membuka <a href="{esc(target)}">{esc(title)}</a>...</p>
</body>
</html>
"""


def write_outputs(root: Path, base_url: str, debug: bool = False) -> list[LocationPack]:
    if not root.exists():
        raise FileNotFoundError(f"Root output tidak ditemukan: {root}")

    locations: list[LocationPack] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith(".") or d.name in {"assets", "logs", "raw_payloads"}:
            continue
        pack = read_location(d)
        if not pack:
            continue
        locations.append(pack)
        map_html = build_map_html(pack)
        (d / "langit_map_room.html").write_text(map_html, encoding="utf-8")
        (d / "langit_map.html").write_text(map_html, encoding="utf-8")
        (d / "anemos_map.html").write_text(map_html, encoding="utf-8")

        # Lightweight manifest per location.
        manifest = {
            "version": VERSION,
            "engine": ENGINE_NAME,
            "location": pack.name,
            "slug": pack.slug,
            "updated": pack.updated_label,
            "map": f"{base_url.rstrip('/')}/{pack.slug}/langit_map_room.html" if base_url else f"{pack.slug}/langit_map_room.html",
            "hours": len(pack.hours),
            "peak": asdict(best_hour(pack.hours)),
        }
        (d / "langit_v64_map_manifest.json").write_text(data_json(manifest), encoding="utf-8")

        if debug:
            print(f"OK map: {d / 'langit_map_room.html'} ({len(pack.hours)} jam)")

    if not locations:
        raise RuntimeError("Tidak ada folder lokasi yang bisa diproses di outputs/.")

    portal_html = build_portal_html(locations)
    (root / "langit_portal_map.html").write_text(portal_html, encoding="utf-8")
    (root / "map.html").write_text(build_redirect_html("langit_portal_map.html", "LANGIT Portal Map"), encoding="utf-8")

    root_manifest = {
        "version": VERSION,
        "engine": ENGINE_NAME,
        "generated": datetime.now(JAKARTA).isoformat(timespec="seconds"),
        "locations": [
            {
                "slug": p.slug,
                "name": p.name,
                "lat": p.lat,
                "lon": p.lon,
                "peak_rain": best_hour(p.hours).rain,
                "peak_hour": best_hour(p.hours).hour,
                "risk": best_hour(p.hours).risk,
                "status": best_hour(p.hours).status,
            }
            for p in locations
        ],
    }
    (root / "langit_v64_manifest.json").write_text(json.dumps(root_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return locations


def verify(root: Path) -> None:
    errors: list[str] = []
    if not root.exists():
        errors.append(f"Root tidak ditemukan: {root}")
    if not (root / "langit_portal_map.html").exists():
        errors.append("outputs/langit_portal_map.html belum ada.")
    if not (root / "langit_v64_manifest.json").exists():
        errors.append("outputs/langit_v64_manifest.json belum ada.")

    loc_dirs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")] if root.exists() else []
    usable_dirs = []
    for d in loc_dirs:
        if d.name in {"assets", "logs", "raw_payloads"}:
            continue
        if (d / "langit_api_v1.json").exists() or (d / "langit_location.geojson").exists() or list(d.glob("*.csv")):
            usable_dirs.append(d)

    for d in usable_dirs:
        for name in ["langit_map_room.html", "langit_map.html", "anemos_map.html", "langit_v64_map_manifest.json"]:
            if not (d / name).exists():
                errors.append(f"{d / name} belum ada.")

    html_files = [root / "langit_portal_map.html"]
    for d in usable_dirs:
        html_files.extend([d / "langit_map_room.html", d / "langit_map.html", d / "anemos_map.html"])

    for p in html_files:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for token in BAD_PUBLIC_TOKENS:
            if token in text:
                errors.append(f"{p} masih mengandung token lama: {token}")
        for required in ["LANGIT v64", "Atmospheric Map Engine", "Risiko", "Hujan", "Panas", "Confidence"]:
            if required not in text:
                errors.append(f"{p} tidak mengandung elemen wajib: {required}")

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        raise SystemExit(3)

    print(f"OK: {ENGINE_NAME} verified. lokasi={len(usable_dirs)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=ENGINE_NAME)
    parser.add_argument("--root", default="outputs", help="Folder output publik.")
    parser.add_argument("--public-base-url", default="", help="Base URL GitHub Pages.")
    parser.add_argument("--verify-only", action="store_true", help="Hanya verifikasi output v64.")
    parser.add_argument("--debug", action="store_true", help="Log lebih detail.")
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
