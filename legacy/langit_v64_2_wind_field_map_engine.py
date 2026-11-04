#!/usr/bin/env python3
"""
LANGIT v64.2 — Wind Field Map Engine

Pengganti peta v64 yang sebelumnya masih terlihat seperti marker biasa.
Versi ini membuat peta lebih hidup dengan:
- continuous atmosphere canvas layer, bukan lingkaran marker saja
- animasi angin berbasis canvas
- layer Risiko, Hujan, Panas, Lembap, Confidence, Angin
- portal regional dan peta per lokasi
- fallback aman kalau data forecast tidak lengkap

Pakai di root repo:
  python langit_v64_2_wind_field_map_engine.py --root outputs --public-base-url https://marcooo20-d.github.io/weather-forecast

Verify:
  python langit_v64_2_wind_field_map_engine.py --root outputs --verify-only
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

VERSION = "v64.2"
ENGINE_NAME = "LANGIT v64.2 Wind Field Map Engine"
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


@dataclass
class HourPoint:
    iso: str
    date_label: str
    hour: str
    temp: float | None
    feels: float | None
    rh: float | None
    rain: int
    wind_speed: float
    wind_dir: float
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
    hours: list[HourPoint]


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
    if not text or text in {"-", "—", "None", "null", "nan"}:
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


def pick(row: dict[str, Any], keys: list[str]) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    compact = {re.sub(r"[^a-z0-9]+", "", str(k).lower()): v for k, v in row.items()}
    for key in keys:
        k = key.lower()
        if k in lower:
            return lower[k]
        ck = re.sub(r"[^a-z0-9]+", "", k)
        if ck in compact:
            return compact[ck]
    for lk, v in lower.items():
        for key in keys:
            if key.lower() in lk:
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


def parse_dt(value: Any, fallback_day: date, fallback_hour: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = "" if value is None else str(value).strip().replace("Z", "+00:00")
        dt = None
        if text:
            for candidate in [text, text.replace(" ", "T"), re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)]:
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


def risk_score(rain: int, feels: float | None, rh: float | None, confidence: int) -> int:
    heat = 0
    if feels is not None:
        if feels >= 40:
            heat = 35
        elif feels >= 37:
            heat = 25
        elif feels >= 34:
            heat = 16
        elif feels >= 32:
            heat = 8
    moist = 6 if rh is not None and rh >= 88 else 0
    conf_penalty = max(0, 65 - confidence) * 0.22
    return int(round(clamp(max(rain, heat) + moist + conf_penalty, 0, 100)))


def status_from_risk(risk: int) -> str:
    if risk >= 76:
        return "Tinggi"
    if risk >= 56:
        return "Waspada"
    if risk >= 31:
        return "Pantau"
    return "Aman"


def note_for(status: str, rain: int, feels: float | None, condition: str) -> str:
    if status == "Tinggi":
        return "Risiko tinggi pada jam ini. Hindari aktivitas luar ruang jika tidak mendesak."
    if status == "Waspada":
        return "Perlu persiapan hujan/panas dan pantau perubahan lokal."
    if rain >= 30:
        return "Ada peluang hujan yang perlu dipantau."
    if feels is not None and feels >= 34:
        return "Panas terasa cukup kuat; pilih waktu yang lebih teduh."
    if "cerah" in condition.lower():
        return "Kondisi cukup baik untuk aktivitas luar ruang."
    return "Kondisi relatif aman, tetap pantau lokal."


def confidence_from(row: dict[str, Any]) -> int:
    explicit = as_percent(pick(row, ["confidence", "confidence_pct", "kepercayaan", "data_confidence"]))
    if explicit is not None:
        return explicit
    active = as_float(pick(row, ["active_sources", "sources_active", "source_count", "model_count"]))
    total = as_float(pick(row, ["total_sources", "sources_total", "model_total"]))
    if active is not None and total is not None and total > 0:
        return int(round(clamp(active / total * 100, 0, 100)))
    return 74


def flatten_dicts(obj: Any, path: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            for item in obj:
                row = dict(item)
                row["_path"] = path
                rows.append(row)
        for i, item in enumerate(obj):
            rows.extend(flatten_dicts(item, f"{path}[{i}]"))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            rows.extend(flatten_dicts(v, f"{path}.{k}" if path else str(k)))
    return rows


def row_score(row: dict[str, Any]) -> int:
    keys = " ".join(str(k).lower() for k in row)
    score = 0
    if any(x in keys for x in ["time", "hour", "jam", "datetime", "valid"]):
        score += 3
    if any(x in keys for x in ["temp", "suhu", "temperature"]):
        score += 3
    if any(x in keys for x in ["rain", "precip", "hujan", "pop"]):
        score += 3
    if any(x in keys for x in ["humidity", "rh", "kelembapan"]):
        score += 2
    if any(x in keys for x in ["wind", "angin"]):
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


def candidate_rows(loc_dir: Path, api: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if api is not None:
        rows.extend(flatten_dicts(api))
    for path in sorted(loc_dir.glob("*.csv")):
        if any(skip in path.name.lower() for skip in ["source", "model", "accuracy"]):
            continue
        rows.extend(csv_rows(path))
    rows = [r for r in rows if row_score(r) >= 5]
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        sig = "|".join(str(pick(r, keys)) for keys in [
            ["time", "datetime", "valid_time", "date_time", "timestamp", "jam", "hour"],
            ["temperature_2m", "temp", "temperature", "suhu"],
            ["precipitation_probability", "rain_probability", "pop", "hujan", "rain"],
        ])
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(r)
    return unique


def build_hours(rows: list[dict[str, Any]]) -> list[HourPoint]:
    today = datetime.now(JAKARTA).date()
    points: list[HourPoint] = []
    for row in rows:
        time_value = pick(row, [
            "time", "datetime", "valid_time", "date_time", "timestamp", "target_time",
            "jam", "hour", "local_time"
        ])
        hour = normalize_hour(time_value) or normalize_hour(pick(row, ["jam", "hour"]))
        if hour is None:
            continue
        dt = parse_dt(time_value, today, hour)
        temp = as_float(pick(row, ["temperature_2m", "temp", "temperature", "suhu", "t2m", "air_temperature"]))
        rh = as_float(pick(row, ["relative_humidity_2m", "relative_humidity", "humidity", "rh", "kelembapan"]))
        rain = as_percent(pick(row, [
            "precipitation_probability", "rain_probability", "rain_prob", "pop",
            "probability_of_precipitation", "hujan", "peluang_hujan", "rain_chance", "precip_prob"
        ]))
        if rain is None:
            rain = 0
        feels = as_float(pick(row, ["apparent_temperature", "feels_like", "heat_index", "terasa", "suhu_terasa"]))
        if feels is None:
            feels = heat_index(temp, rh)
        wind_speed = as_float(pick(row, ["wind_speed_10m", "wind_speed", "windspeed", "kecepatan_angin", "angin"]))
        wind_dir = as_float(pick(row, ["wind_direction_10m", "winddirection_10m", "wind_dir", "wind_direction", "arah_angin"]))
        condition = sentence(pick(row, ["condition", "weather", "summary", "cuaca", "description"]), "Berawan")
        confidence = confidence_from(row)
        risk = risk_score(rain, feels, rh, confidence)
        status = status_from_risk(risk)
        note = note_for(status, rain, feels, condition)
        points.append(HourPoint(
            iso=dt.isoformat(timespec="minutes"),
            date_label=date_id(dt.date()),
            hour=f"{dt.hour:02d}:{dt.minute:02d}",
            temp=None if temp is None else round(temp, 1),
            feels=None if feels is None else round(feels, 1),
            rh=None if rh is None else int(round(rh)),
            rain=rain,
            wind_speed=round(wind_speed if wind_speed is not None else 2.2, 1),
            wind_dir=round((wind_dir if wind_dir is not None else 115) % 360, 0),
            condition=condition,
            risk=risk,
            status=status,
            confidence=confidence,
            note=note,
        ))

    if not points:
        for i, hour in enumerate(["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]):
            dt = parse_dt(None, today, hour)
            temp = 24 + max(0, math.sin(i / 7 * math.pi) * 6)
            rh = 82 - max(0, math.sin(i / 7 * math.pi) * 22)
            rain = 0
            feels = heat_index(temp, rh)
            risk = risk_score(rain, feels, rh, 52)
            points.append(HourPoint(
                iso=dt.isoformat(timespec="minutes"),
                date_label=date_id(dt.date()),
                hour=hour,
                temp=round(temp, 1),
                feels=feels,
                rh=int(round(rh)),
                rain=rain,
                wind_speed=2.0,
                wind_dir=115,
                condition="Data terbatas",
                risk=risk,
                status=status_from_risk(risk),
                confidence=52,
                note="Data terbatas. Gunakan hanya sebagai visualisasi sementara.",
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
        if len(out) >= 72:
            break
    return out


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
    return max(pack.hours, key=lambda h: (h.rain, h.risk, h.feels or 0))


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="generator" content="LANGIT v64.2 Wind Field Map Engine">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{
  --bg:#020814;
  --panel:rgba(3,13,25,.76);
  --panel2:rgba(8,25,44,.82);
  --line:rgba(148,163,184,.22);
  --line-strong:rgba(125,211,252,.38);
  --text:#f8fbff;
  --muted:#b8c7d9;
  --blue:#22a7ff;
  --cyan:#2dd4bf;
  --yellow:#facc15;
  --orange:#fb923c;
  --red:#ef4444;
  --violet:#8b5cf6;
}
*{box-sizing:border-box}
html,body,#map{height:100%;margin:0}
body{
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:var(--bg);
  color:var(--text);
  overflow:hidden;
}
#map{z-index:1;background:#08111d}
.leaflet-container{font-family:inherit;background:#08111d}
.leaflet-control-attribution{font-size:10px;opacity:.55}
.leaflet-control-zoom a{
  background:rgba(4,15,27,.86)!important;
  color:#eaf6ff!important;
  border-color:rgba(148,163,184,.20)!important;
}
.atmos-canvas,.wind-canvas{
  position:absolute;
  inset:0;
  width:100%;
  height:100%;
  pointer-events:none;
}
.atmos-canvas{z-index:405;mix-blend-mode:screen;opacity:.92}
.wind-canvas{z-index:430;opacity:.70}
.map-vignette{
  position:absolute;inset:0;z-index:440;pointer-events:none;
  background:
    radial-gradient(circle at 48% 48%,transparent 0,transparent 45%,rgba(0,0,0,.22) 78%,rgba(0,0,0,.44) 100%),
    linear-gradient(90deg,rgba(2,8,20,.26),transparent 18%,transparent 78%,rgba(2,8,20,.22));
}
.topbar{
  position:absolute;left:0;right:0;top:0;z-index:720;
  display:flex;align-items:center;justify-content:space-between;
  padding:14px 18px;
  background:linear-gradient(180deg,rgba(2,8,20,.82),rgba(2,8,20,.18));
  pointer-events:none;
}
.brand{
  pointer-events:auto;
  display:flex;align-items:center;gap:10px;
  border:1px solid var(--line);
  background:rgba(3,13,25,.62);
  padding:8px 12px;border-radius:999px;
  box-shadow:0 18px 44px rgba(0,0,0,.28);
  backdrop-filter:blur(16px);
}
.logo{
  width:24px;height:24px;border-radius:9px;
  background:linear-gradient(135deg,#31e6c3,#20a4ff 54%,#1d4ed8);
  box-shadow:0 0 22px rgba(37,169,255,.58);
}
.brand b{font-size:13px;letter-spacing:-.03em}
.brand small{display:block;color:var(--muted);font-size:10px;line-height:1}
.actions{pointer-events:auto;display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.chip{
  border:1px solid var(--line);
  background:rgba(3,13,25,.70);
  color:var(--text);
  border-radius:999px;
  padding:8px 10px;
  font-weight:850;
  font-size:12px;
  cursor:pointer;
  backdrop-filter:blur(14px);
  box-shadow:0 14px 32px rgba(0,0,0,.22);
}
.chip.active{
  border-color:rgba(125,211,252,.80);
  background:linear-gradient(135deg,#1d4ed8,#22a7ff);
}
.side-panel{
  position:absolute;left:18px;top:78px;z-index:710;
  width:min(360px,calc(100vw - 36px));
  border:1px solid rgba(96,165,250,.38);
  background:linear-gradient(180deg,rgba(3,13,25,.88),rgba(6,21,38,.76));
  border-radius:22px;
  overflow:hidden;
  box-shadow:0 24px 64px rgba(0,0,0,.42);
  backdrop-filter:blur(20px);
}
.panel-head{padding:18px 18px 14px;border-bottom:1px solid rgba(148,163,184,.15)}
.panel-kicker{
  display:flex;align-items:center;gap:8px;
  color:#aee9ff;font-size:11px;font-weight:900;letter-spacing:.06em;text-transform:uppercase;
  margin-bottom:8px;
}
.pulse{
  width:8px;height:8px;border-radius:50%;background:var(--cyan);
  box-shadow:0 0 0 7px rgba(45,212,191,.12),0 0 22px rgba(45,212,191,.70);
}
.panel-head h1{
  margin:0 0 8px;font-size:25px;line-height:1.02;letter-spacing:-.05em;
}
.panel-head p{margin:0;color:var(--muted);font-size:12.5px;line-height:1.45}
.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:12px}
.metric{
  min-height:76px;
  border:1px solid rgba(148,163,184,.18);
  background:rgba(15,42,70,.62);
  border-radius:15px;
  padding:11px;
}
.metric span{display:block;color:var(--muted);font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.05em}
.metric b{display:block;margin-top:5px;font-size:22px;line-height:1}
.panel-foot{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:12px 14px;border-top:1px solid rgba(148,163,184,.14);
  color:var(--muted);font-size:11px;
}
.legend{
  position:absolute;right:18px;bottom:118px;z-index:710;
  width:210px;
  border:1px solid var(--line);
  background:rgba(3,13,25,.74);
  border-radius:18px;
  padding:13px;
  backdrop-filter:blur(18px);
  box-shadow:0 22px 48px rgba(0,0,0,.32);
}
.legend-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:12px;font-weight:900}
.scale{
  height:9px;border-radius:999px;
  background:linear-gradient(90deg,#2dd4bf 0%,#facc15 36%,#fb923c 62%,#ef4444 100%);
  box-shadow:0 0 20px rgba(250,204,21,.20);
}
.scale-labels{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;margin-top:5px}
.legend-note{margin-top:10px;color:var(--muted);font-size:11px;line-height:1.35}
.timeline{
  position:absolute;left:50%;bottom:22px;transform:translateX(-50%);z-index:730;
  width:min(780px,calc(100vw - 40px));
  border:1px solid rgba(125,211,252,.25);
  background:rgba(3,13,25,.80);
  border-radius:22px;
  padding:12px;
  box-shadow:0 24px 70px rgba(0,0,0,.45);
  backdrop-filter:blur(22px);
}
.timeline-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 4px 10px}
.timeline-title{font-size:12px;color:var(--muted);font-weight:800}
.play{
  border:1px solid rgba(125,211,252,.35);background:rgba(15,42,70,.70);color:var(--text);
  border-radius:999px;padding:8px 12px;font-weight:900;cursor:pointer;
}
.time-track{position:relative;display:flex;gap:7px;overflow-x:auto;padding-bottom:2px;scrollbar-color:#8aa4bd transparent}
.time{
  flex:0 0 auto;min-width:62px;
  border:1px solid rgba(148,163,184,.22);
  background:rgba(15,42,70,.64);
  color:var(--text);
  border-radius:13px;
  padding:9px 8px;
  cursor:pointer;
  text-align:center;
}
.time b{display:block;font-size:13px}
.time span{display:block;margin-top:2px;color:var(--muted);font-size:10px;font-weight:800}
.time.active{
  border-color:rgba(125,211,252,.9);
  background:linear-gradient(135deg,#1d4ed8,#22a7ff);
  box-shadow:0 0 0 4px rgba(34,167,255,.12);
}
.inspect{
  position:absolute;left:18px;bottom:118px;z-index:710;
  width:min(330px,calc(100vw - 36px));
  border:1px solid var(--line);
  background:rgba(3,13,25,.74);
  border-radius:18px;
  padding:14px;
  backdrop-filter:blur(18px);
  box-shadow:0 22px 48px rgba(0,0,0,.32);
}
.inspect h2{margin:0 0 6px;font-size:16px;letter-spacing:-.03em}
.inspect p{margin:0;color:var(--muted);font-size:12px;line-height:1.42}
.location-label{
  border:1px solid rgba(125,211,252,.45);
  background:rgba(3,13,25,.80);
  color:#f8fbff;
  border-radius:999px;
  padding:6px 9px;
  font-size:11px;
  font-weight:900;
  box-shadow:0 10px 24px rgba(0,0,0,.28);
}
.leaflet-marker-icon{filter:drop-shadow(0 10px 18px rgba(0,0,0,.42))}
.leaflet-popup-content-wrapper{
  border-radius:18px;
  background:#f8fbff;
  box-shadow:0 24px 60px rgba(0,0,0,.35);
}
.leaflet-popup-content{margin:14px;min-width:220px}
.popup-title{font-weight:950;font-size:16px;letter-spacing:-.03em;color:#07111f}
.popup-sub{color:#475569;font-size:12px;margin-top:3px}
.popup-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:10px}
.popup-cell{border:1px solid #dbe7f3;background:#eef6ff;border-radius:10px;padding:8px;color:#06111f}
.popup-cell b{display:block;font-size:15px}
.popup-cell span{font-size:10px;color:#64748b;text-transform:uppercase;font-weight:800}
@media(max-width:820px){
  .side-panel{top:68px;left:12px;width:calc(100vw - 24px);max-height:42vh;overflow:auto}
  .actions{max-width:calc(100vw - 128px);overflow-x:auto;flex-wrap:nowrap}
  .chip{white-space:nowrap}
  .legend{display:none}
  .inspect{display:none}
  .timeline{bottom:12px;width:calc(100vw - 24px)}
}
</style>
</head>
<body>
<!-- LANGIT v64.2 Wind Field Map Engine -->
<div id="map"></div>
<canvas id="atmos" class="atmos-canvas"></canvas>
<canvas id="wind" class="wind-canvas"></canvas>
<div class="map-vignette"></div>

<header class="topbar">
  <div class="brand">
    <span class="logo"></span>
    <div><b>LANGIT</b><small>Wind Field Map</small></div>
  </div>
  <div class="actions">
    <button class="chip active" data-layer="risk">Risiko</button>
    <button class="chip" data-layer="rain">Hujan</button>
    <button class="chip" data-layer="heat">Panas</button>
    <button class="chip" data-layer="humidity">Lembap</button>
    <button class="chip" data-layer="confidence">Confidence</button>
    <button class="chip" data-layer="wind">Angin</button>
    <button class="chip" id="baseBtn">Mode peta</button>
  </div>
</header>

<section class="side-panel">
  <div class="panel-head">
    <div class="panel-kicker"><span class="pulse"></span><span id="panelMode">Live forecast field</span></div>
    <h1 id="panelTitle">__PANEL_TITLE__</h1>
    <p id="panelDesc">Layer peta dibuat sebagai permukaan atmosfer halus dari titik prakiraan. Ini bukan radar resmi.</p>
  </div>
  <div class="metric-grid">
    <div class="metric"><span>Status</span><b id="mStatus">—</b></div>
    <div class="metric"><span>Peluang hujan</span><b id="mRain">—</b></div>
    <div class="metric"><span>Risiko</span><b id="mRisk">—</b></div>
    <div class="metric"><span>Jam aktif</span><b id="mHour">—</b></div>
  </div>
  <div class="panel-foot">
    <span id="mDate">—</span>
    <span>LANGIT __VERSION__</span>
  </div>
</section>

<aside class="inspect">
  <h2 id="inspectTitle">Field atmosfer</h2>
  <p id="inspectText">Klik titik mana pun di peta untuk membaca estimasi lokal dari layer aktif.</p>
</aside>

<aside class="legend">
  <div class="legend-title"><span id="legendTitle">Skala risiko</span><span id="legendUnit">0–100</span></div>
  <div class="scale" id="legendScale"></div>
  <div class="scale-labels"><span>rendah</span><span>sedang</span><span>tinggi</span></div>
  <div class="legend-note" id="legendNote">Warna adalah interpolasi visual dari data lokasi, bukan radar observasi.</div>
</aside>

<section class="timeline">
  <div class="timeline-top">
    <button class="play" id="playBtn">Play</button>
    <div class="timeline-title" id="timeTitle">—</div>
  </div>
  <div class="time-track" id="timeTrack"></div>
</section>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const LANGIT = __PAYLOAD__;
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
  const bounds = L.latLngBounds(locs.map(l => [l.lat,l.lon]));
  map.fitBounds(bounds, {padding:[160,160], maxZoom:9});
}else{
  map.setView([LANGIT.location.lat, LANGIT.location.lon], 10);
}

const atmos = document.getElementById('atmos');
const actx = atmos.getContext('2d', {alpha:true});
const wind = document.getElementById('wind');
const wctx = wind.getContext('2d', {alpha:true});
let particles = [];

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

function pointFor(loc, index=currentIndex){
  return loc.hours[Math.min(index, loc.hours.length-1)] || loc.hours[0];
}
function val(p, layer=currentLayer){
  if(layer === 'rain') return Number(p.rain || 0);
  if(layer === 'heat') return Number(p.feels ?? p.temp ?? 0);
  if(layer === 'humidity') return Number(p.rh || 0);
  if(layer === 'confidence') return Number(p.confidence || 0);
  if(layer === 'wind') return Number(p.wind_speed || 0);
  return Number(p.risk || 0);
}
function statusColor(status){
  if(status === 'Tinggi') return '#ef4444';
  if(status === 'Waspada') return '#fb923c';
  if(status === 'Pantau') return '#facc15';
  return '#2dd4bf';
}
function colorRamp(n, layer=currentLayer, alpha=0.55){
  n = Math.max(0, Math.min(1, n));
  let stops;
  if(layer === 'confidence'){
    stops = [[0,'139,92,246'],[.45,'245,158,11'],[.72,'56,189,248'],[1,'45,212,191']];
  }else if(layer === 'humidity'){
    stops = [[0,'45,212,191'],[.45,'56,189,248'],[.78,'139,92,246'],[1,'236,72,153']];
  }else if(layer === 'heat'){
    stops = [[0,'45,212,191'],[.35,'250,204,21'],[.68,'249,115,22'],[1,'239,68,68']];
  }else if(layer === 'wind'){
    stops = [[0,'56,189,248'],[.5,'45,212,191'],[.78,'250,204,21'],[1,'249,115,22']];
  }else{
    stops = [[0,'45,212,191'],[.36,'250,204,21'],[.62,'249,115,22'],[1,'239,68,68']];
  }
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
function normalizeValue(v, layer=currentLayer){
  if(layer === 'heat') return Math.max(0, Math.min(1, (v - 24) / 18));
  if(layer === 'wind') return Math.max(0, Math.min(1, v / 12));
  return Math.max(0, Math.min(1, v / 100));
}
function noise(lat, lon, t=0){
  const x = Math.sin(lat*18.9898 + lon*78.233 + t*0.013) * 43758.5453;
  return x - Math.floor(x);
}
function fieldAt(latlng){
  const layer = currentLayer;
  let num = 0, den = 0;
  const scaleKm = LANGIT.portal ? 60 : 26;
  for(const loc of locs){
    const p = pointFor(loc);
    const dKm = Math.max(0.3, map.distance(latlng, L.latLng(loc.lat,loc.lon)) / 1000);
    const w = Math.exp(-dKm/scaleKm) + 1 / Math.pow(dKm + 2, 1.42);
    const base = val(p, layer);
    num += base * w;
    den += w;
  }
  let v = den ? num / den : 0;
  const n = noise(latlng.lat, latlng.lng, currentIndex);
  if(layer === 'rain' || layer === 'risk') v += (n - .5) * 6;
  if(layer === 'heat') v += (n - .5) * 1.2;
  if(layer === 'humidity') v += (n - .5) * 3.5;
  if(layer === 'confidence') v += (n - .5) * 4;
  if(layer === 'wind') v += (n - .5) * .8;
  return Math.max(0, v);
}
function drawAtmosphere(){
  if(!map || !actx) return;
  const w = window.innerWidth, h = window.innerHeight;
  actx.clearRect(0,0,w,h);
  const step = LANGIT.portal ? 8 : 7;
  for(let y=0; y<h; y+=step){
    for(let x=0; x<w; x+=step){
      const ll = map.containerPointToLatLng([x+step/2,y+step/2]);
      const v = fieldAt(ll);
      const n = normalizeValue(v);
      let alpha = 0.05 + n * 0.48;
      if(currentLayer === 'confidence') alpha = 0.10 + n * 0.34;
      if(currentLayer === 'wind') alpha = 0.08 + n * 0.30;
      actx.fillStyle = colorRamp(n, currentLayer, alpha);
      actx.fillRect(x,y,step+1,step+1);
    }
  }
}
function popupHtml(loc){
  const p = pointFor(loc);
  return `<div class="popup-title">${loc.name}</div>
  <div class="popup-sub">${p.date_label} · ${p.hour} WIB · ${p.condition}</div>
  <div class="popup-grid">
    <div class="popup-cell"><b>${p.status}</b><span>Status</span></div>
    <div class="popup-cell"><b>${p.rain}%</b><span>Hujan</span></div>
    <div class="popup-cell"><b>${p.temp ?? '—'}°C</b><span>Suhu</span></div>
    <div class="popup-cell"><b>${p.feels ?? '—'}°C</b><span>Terasa</span></div>
    <div class="popup-cell"><b>${p.rh ?? '—'}%</b><span>RH</span></div>
    <div class="popup-cell"><b>${p.risk}/100</b><span>Risiko</span></div>
  </div>
  <p style="color:#475569;font-size:12px;line-height:1.45;margin:10px 0 0">${p.note}</p>`;
}
function drawMarkers(){
  markers.forEach(m => map.removeLayer(m));
  labels.forEach(l => map.removeLayer(l));
  markers = []; labels = [];
  for(const loc of locs){
    const p = pointFor(loc);
    const c = statusColor(p.status);
    const marker = L.circleMarker([loc.lat, loc.lon], {
      radius: LANGIT.portal ? 7 : 9,
      color: '#eaffff',
      weight: 2,
      fillColor: c,
      fillOpacity: .96
    }).bindPopup(popupHtml(loc)).addTo(map);
    markers.push(marker);
    const label = L.marker([loc.lat, loc.lon], {
      icon: L.divIcon({
        className: '',
        html: `<div class="location-label">${loc.short_name}</div>`,
        iconSize: [100,24],
        iconAnchor: [-14,8]
      }),
      interactive:false
    }).addTo(map);
    labels.push(label);
  }
}
function activeMain(){
  if(LANGIT.portal){
    let best = locs[0];
    for(const loc of locs){
      if(pointFor(loc).risk > pointFor(best).risk) best = loc;
    }
    return {loc:best, p:pointFor(best)};
  }
  return {loc:LANGIT.location, p:pointFor(LANGIT.location)};
}
function refreshPanel(){
  const {loc,p} = activeMain();
  document.getElementById('panelTitle').textContent = LANGIT.portal ? 'Regional field' : loc.name;
  document.getElementById('mStatus').textContent = p.status;
  document.getElementById('mRain').textContent = `${p.rain}%`;
  document.getElementById('mRisk').textContent = `${p.risk}/100`;
  document.getElementById('mHour').textContent = p.hour;
  document.getElementById('mDate').textContent = p.date_label;
  document.getElementById('timeTitle').textContent = `${p.date_label} · layer ${currentLayer}`;
  document.getElementById('inspectTitle').textContent = LANGIT.portal ? 'Regional risk field' : loc.name;
  document.getElementById('inspectText').textContent = `${p.date_label} pukul ${p.hour} WIB. ${p.note}`;
  const legend = {
    risk:['Skala risiko','0–100','Warna menunjukkan tingkat gangguan aktivitas.'],
    rain:['Peluang hujan','0–100%','Semakin hangat warnanya, semakin tinggi peluang hujan.'],
    heat:['Panas terasa','24–42°C','Warna menyorot area dengan panas terasa lebih tinggi.'],
    humidity:['Kelembapan','0–100%','Biru–ungu menunjukkan udara makin lembap.'],
    confidence:['Confidence','0–100%','Ungu/oranye berarti confidence lebih rendah.'],
    wind:['Kecepatan angin','m/s','Partikel menunjukkan arah gerak angin.']
  }[currentLayer];
  document.getElementById('legendTitle').textContent = legend[0];
  document.getElementById('legendUnit').textContent = legend[1];
  document.getElementById('legendNote').textContent = legend[2];
}
function buildTimeline(){
  const track = document.getElementById('timeTrack');
  track.innerHTML = '';
  const hours = LANGIT.location.hours;
  hours.forEach((p,i) => {
    const b = document.createElement('button');
    b.className = 'time' + (i === currentIndex ? ' active' : '');
    b.innerHTML = `<b>${p.hour}</b><span>${p.rain}%</span>`;
    b.onclick = () => {
      currentIndex = i;
      buildTimeline();
      refreshAll();
    };
    track.appendChild(b);
  });
}
function refreshAll(){
  refreshPanel();
  drawAtmosphere();
  drawMarkers();
}
document.querySelectorAll('[data-layer]').forEach(btn => {
  btn.onclick = () => {
    currentLayer = btn.dataset.layer;
    document.querySelectorAll('[data-layer]').forEach(x => x.classList.remove('active'));
    btn.classList.add('active');
    refreshAll();
  };
});
document.getElementById('baseBtn').onclick = () => {
  map.removeLayer(baseLayers[baseIndex]);
  baseIndex = (baseIndex + 1) % baseLayers.length;
  baseLayers[baseIndex].addTo(map);
};
document.getElementById('playBtn').onclick = () => {
  playing = !playing;
  document.getElementById('playBtn').textContent = playing ? 'Pause' : 'Play';
  if(playing){
    playTimer = setInterval(() => {
      currentIndex = (currentIndex + 1) % LANGIT.location.hours.length;
      buildTimeline();
      refreshAll();
    }, 950);
  }else{
    clearInterval(playTimer);
  }
};
map.on('click', e => {
  const estimate = fieldAt(e.latlng);
  const n = normalizeValue(estimate);
  L.popup()
    .setLatLng(e.latlng)
    .setContent(`<div class="popup-title">Estimasi titik</div>
      <div class="popup-sub">Layer ${currentLayer}</div>
      <div class="popup-grid">
        <div class="popup-cell"><b>${Math.round(estimate)}</b><span>Nilai</span></div>
        <div class="popup-cell"><b>${Math.round(n*100)}%</b><span>Intensitas</span></div>
      </div>
      <p style="color:#475569;font-size:12px;line-height:1.45;margin:10px 0 0">Estimasi visual dari interpolasi lokasi, bukan observasi titik.</p>`)
    .openOn(map);
});

function windVectorAt(x,y){
  const ll = map.containerPointToLatLng([x,y]);
  let sx = 0, sy = 0, den = 0;
  for(const loc of locs){
    const p = pointFor(loc);
    const dKm = Math.max(0.3, map.distance(ll, L.latLng(loc.lat,loc.lon)) / 1000);
    const w = Math.exp(-dKm/(LANGIT.portal ? 70 : 32)) + 1 / Math.pow(dKm + 2, 1.25);
    const dir = Number(p.wind_dir || 115) * Math.PI / 180;
    const sp = Math.max(.7, Number(p.wind_speed || 2.2));
    sx += Math.sin(dir) * sp * w;
    sy += -Math.cos(dir) * sp * w;
    den += w;
  }
  if(!den) return {x:1,y:0,s:1};
  const nx = sx / den, ny = sy / den;
  const s = Math.sqrt(nx*nx+ny*ny);
  return {x:nx,y:ny,s:s};
}
function seedParticles(){
  const count = Math.min(1150, Math.max(300, Math.floor(window.innerWidth * window.innerHeight / 1600)));
  particles = Array.from({length:count}, () => ({
    x: Math.random()*window.innerWidth,
    y: Math.random()*window.innerHeight,
    age: Math.random()*120,
    life: 80 + Math.random()*100
  }));
}
function animateWind(){
  wctx.clearRect(0,0,window.innerWidth,window.innerHeight);
  wctx.lineWidth = 1;
  for(const pt of particles){
    const v = windVectorAt(pt.x, pt.y);
    const speed = Math.max(.45, Math.min(3.6, v.s * .72));
    const wobble = Math.sin((pt.x + pt.y + pt.age) * .008) * .45;
    const vx = v.x * speed + Math.cos(wobble) * .05;
    const vy = v.y * speed + Math.sin(wobble) * .05;
    const alpha = Math.max(.08, Math.min(.38, .12 + v.s * .045));
    wctx.strokeStyle = `rgba(190,235,255,${alpha})`;
    wctx.beginPath();
    wctx.moveTo(pt.x, pt.y);
    pt.x += vx;
    pt.y += vy;
    pt.age += 1;
    wctx.lineTo(pt.x, pt.y);
    wctx.stroke();
    if(pt.x < -40 || pt.x > window.innerWidth+40 || pt.y < -40 || pt.y > window.innerHeight+40 || pt.age > pt.life){
      pt.x = Math.random()*window.innerWidth;
      pt.y = Math.random()*window.innerHeight;
      pt.age = 0;
      pt.life = 80 + Math.random()*100;
    }
  }
  requestAnimationFrame(animateWind);
}
document.addEventListener('keydown', e => {
  if(e.key === 'ArrowRight') currentIndex = Math.min(LANGIT.location.hours.length-1, currentIndex+1);
  if(e.key === 'ArrowLeft') currentIndex = Math.max(0, currentIndex-1);
  buildTimeline(); refreshAll();
});
resizeCanvas();
buildTimeline();
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
        if not ((d / "langit_api_v1.json").exists() or (d / "langit_location.geojson").exists() or list(d.glob("*.csv"))):
            continue
        pack = read_location(d)
        if pack is None:
            continue
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
        }
        write_text(d / "langit_v64_2_map_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
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
        "locations": [
            {
                "slug": p.slug,
                "name": p.name,
                "lat": p.lat,
                "lon": p.lon,
                "peak_rain": peak_hour(p).rain,
                "peak_hour": peak_hour(p).hour,
                "risk": peak_hour(p).risk,
                "status": peak_hour(p).status,
            }
            for p in locations
        ],
    }
    write_text(root / "langit_v64_2_manifest.json", json.dumps(root_manifest, ensure_ascii=False, indent=2))
    return locations


def verify(root: Path) -> None:
    errors: list[str] = []
    if not root.exists():
        errors.append(f"Root tidak ditemukan: {root}")
    if not (root / "langit_portal_map.html").exists():
        errors.append("outputs/langit_portal_map.html belum ada.")
    if not (root / "langit_v64_2_manifest.json").exists():
        errors.append("outputs/langit_v64_2_manifest.json belum ada.")

    loc_dirs = []
    if root.exists():
        for d in root.iterdir():
            if d.is_dir() and not d.name.startswith(".") and d.name not in {"assets", "logs", "raw_payloads"}:
                if (d / "langit_api_v1.json").exists() or (d / "langit_location.geojson").exists() or list(d.glob("*.csv")):
                    loc_dirs.append(d)

    for d in loc_dirs:
        for name in ["langit_map_room.html", "langit_map.html", "anemos_map.html", "langit_v64_2_map_manifest.json"]:
            if not (d / name).exists():
                errors.append(f"{d / name} belum ada.")

    html_files = [root / "langit_portal_map.html"]
    for d in loc_dirs:
        html_files += [d / "langit_map_room.html", d / "langit_map.html", d / "anemos_map.html"]

    required = ["LANGIT v64.2", "Wind Field Map Engine", "atmos-canvas", "wind-canvas", "fieldAt", "Risiko", "Hujan", "Panas", "Confidence"]
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
