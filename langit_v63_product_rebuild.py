#!/usr/bin/env python3
"""
LANGIT v63 Product Rebuild
==========================

Public visual layer replacement for the existing weather forecast generator.
It does not change the forecast engine. It only rebuilds public HTML/JSON/map
outputs inside outputs/ after weather_ensemble_multi_location.py finishes.

Usage in repository root:
  python langit_v63_product_rebuild.py --root outputs --public-base-url https://marcooo20-d.github.io/weather-forecast
  python langit_v63_product_rebuild.py --root outputs --verify-only
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
VERSION = "LANGIT v63"
TZ_NAME = "Asia/Jakarta"
DISCLAIMER = "Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti BMKG dan kondisi setempat."
ID_BOUNDS = [[-11.25, 94.0], [6.45, 141.25]]
MONTH_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
DAY_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

# -----------------------------------------------------------------------------
# Safe helpers
# -----------------------------------------------------------------------------

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
            value = value.strip().replace("%", "").replace("°C", "").replace("km/jam", "").replace(",", ".")
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


def prob(value: Any, default: Optional[float] = None) -> Optional[float]:
    x = num(value, default)
    if x is None:
        return default
    # Most public files use 0-100. Some APIs use 0-1. Keep 1 as 1%, but 0.49 as 49%.
    if 0 < x < 1:
        x *= 100.0
    return clamp(x)


def hour(value: Any, default: str = "00:00") -> str:
    raw = text(value, default)
    m = re.search(r"(\d{1,2})(?::(\d{2}))?", raw)
    if not m:
        return default
    h = max(0, min(23, int(m.group(1))))
    minute = (m.group(2) or "00")[:2]
    return f"{h:02d}:{minute}"


def hour_int(value: Any) -> int:
    try:
        return int(hour(value)[:2])
    except Exception:
        return 0


def slugify(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text(value, "location").lower()).strip("-")
    return out or "location"


def local_now() -> dt.datetime:
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
        return f"{DAY_ID[d.weekday()]}, {d.day} {MONTH_ID[d.month-1]} {d.year}"
    return f"{d.day} {MONTH_ID[d.month-1]}"


def fmt_update(value: Any = None) -> str:
    raw = text(value)
    d = parse_date(raw)
    if d:
        h = hour(raw, "00:00")
        return f"Diperbarui {fmt_date(d, False)}, {h} WIB"
    return local_now().strftime("Diperbarui %d/%m/%Y, %H:%M WIB")


def pct(value: Any) -> str:
    x = prob(value, None)
    return "—" if x is None else f"{round(x):.0f}%"


def deg(value: Any) -> str:
    x = num(value, None)
    return "—" if x is None else f"{x:.1f}°C"


def kmh(value: Any) -> str:
    x = num(value, None)
    return "—" if x is None else f"{x:.1f} km/jam"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return [dict(row) for row in csv.DictReader(f)]
    except Exception:
        return []


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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

# -----------------------------------------------------------------------------
# Forecast logic + copywriting
# -----------------------------------------------------------------------------

def heat_risk(heat: Any, temp: Any = None, rh: Any = None) -> float:
    h = num(heat, num(temp, None))
    r = num(rh, None)
    if h is None:
        return 0
    score = 0.0
    if h >= 40: score = 78
    elif h >= 38: score = 65
    elif h >= 36: score = 52
    elif h >= 34: score = 38
    elif h >= 32: score = 24
    if r is not None and r >= 82 and h >= 32:
        score += 6
    return clamp(score)


def risk_class(score: Any, valid: bool = True) -> str:
    if not valid:
        return "limited"
    x = clamp(score)
    if x >= 78:
        return "danger"
    if x >= 55:
        return "rain"
    if x >= 25:
        return "watch"
    return "safe"


def risk_label(cls: str) -> str:
    return {
        "safe": "Aman",
        "watch": "Pantau",
        "rain": "Waspada",
        "danger": "Tinggi",
        "limited": "Terbatas",
    }.get(cls, "Pantau")


def risk_color(cls: str) -> str:
    return {
        "safe": "#35e8a4",
        "watch": "#ffd052",
        "rain": "#ff9346",
        "danger": "#ff4778",
        "limited": "#9ba8ff",
    }.get(cls, "#38bdf8")


def condition_label(hh: str, rain: Any, temp: Any, rh: Any, heat: Any, valid: bool) -> str:
    if not valid:
        return "Data terbatas"
    p = prob(rain, 0) or 0
    t = num(temp, None)
    hi_val = num(heat, t)
    r = num(rh, None)
    h = hour_int(hh)
    if p >= 78:
        return "Hujan kuat"
    if p >= 55:
        return "Hujan lokal"
    if p >= 35:
        return "Potensi hujan"
    if p >= 20:
        return "Awan menebal"
    if hi_val is not None and hi_val >= 36 and 10 <= h <= 16:
        return "Panas menyengat"
    if hi_val is not None and hi_val >= 34 and 9 <= h <= 16:
        return "Panas lembap"
    if r is not None and r >= 88 and (h <= 8 or h >= 19):
        return "Lembap"
    if 10 <= h <= 15:
        return "Cerah berawan"
    if 16 <= h <= 18:
        return "Berawan sore"
    return "Berawan"


def row_to_hour(row: Dict[str, Any], fallback_date: Optional[dt.date] = None, fallback_relative: str = "Hari ini") -> Dict[str, Any]:
    hh = hour(pick(row, "hour", "jam", "time", "local_time", "target_hour", "target_time", "datetime", "timestamp", default="00:00"))
    d = (
        parse_date(pick(row, "date", "tanggal", "target_date", "valid_date", "forecast_date", "datetime", "timestamp"))
        or fallback_date
    )
    temp = num(pick(row, "temp_c", "temperature_c", "temperature_2m_c", "avg_temperature_c", "t2m", "suhu"))
    rh = num(pick(row, "humidity_pct", "relative_humidity", "relative_humidity_2m", "rh", "kelembapan"))
    heat = num(pick(row, "heat_index_c", "apparent_temperature_c", "feels_like_c", "terasa"), temp)
    rain = prob(pick(row, "rain_probability", "rain_probability_raw", "precip_probability", "precipitation_probability", "pop", "hujan"))
    wind = num(pick(row, "wind_kmh", "wind_speed_kmh", "wind_speed_10m_kmh", "angin"))
    base_score = clamp(pick(row, "risk_score", "score", "risk", default=0), default=0)
    score = max(base_score, rain or 0, heat_risk(heat, temp, rh))
    valid = any(x is not None for x in [temp, rh, heat, rain, wind])
    cls = text(pick(row, "risk_class", "risk_level", default="")).lower()
    cls = cls if cls in {"safe", "watch", "rain", "danger", "limited"} else risk_class(score, valid)
    if not valid:
        cls = "limited"
    cond = text(pick(row, "condition", "weather", "cuaca", "summary", default=""))
    if not cond or cond.lower() in {"aman", "dipantau", "safe", "watch"}:
        cond = condition_label(hh, rain, temp, rh, heat, valid)
    return {
        "date_iso": d.isoformat() if d else "",
        "date_label": fmt_date(d) if d else "Tanggal belum terbaca",
        "date_short": fmt_date(d, False) if d else "—",
        "relative": text(pick(row, "relative_day", "day_tag", "hari", "day", default=fallback_relative), fallback_relative),
        "hour": hh,
        "temp_c": temp,
        "humidity_pct": rh,
        "heat_index_c": heat,
        "rain_probability": rain,
        "wind_kmh": wind,
        "risk_score": round(score),
        "risk_class": cls,
        "risk_label": risk_label(cls),
        "condition": cond,
        "valid": valid,
    }


def default_hours(base_date: dt.date, relative: str) -> List[Dict[str, Any]]:
    return [row_to_hour({"hour": h}, base_date, relative) for h in ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]]


def best_windows(hours: List[Dict[str, Any]]) -> List[str]:
    good = sorted({hour_int(x["hour"]) for x in hours if x.get("valid") and x.get("risk_class") == "safe"})
    if not good:
        good = sorted({hour_int(x["hour"]) for x in hours if x.get("risk_class") in {"safe", "watch"}})
    if not good:
        return []
    blocks: List[Tuple[int, int]] = []
    a = b = good[0]
    for x in good[1:]:
        if x <= b + 3:
            b = x
        else:
            blocks.append((a, b))
            a = b = x
    blocks.append((a, b))
    return [f"{a:02d}:00" if a == b else f"{a:02d}:00–{b:02d}:00" for a, b in blocks[:3]]


def period_name(hour_value: str) -> str:
    h = hour_int(hour_value)
    if 5 <= h <= 10:
        return "Pagi"
    if 11 <= h <= 14:
        return "Siang"
    if 15 <= h <= 18:
        return "Sore"
    return "Malam"


def period_summaries(hours: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for name in ["Pagi", "Siang", "Sore", "Malam"]:
        sub = [x for x in hours if period_name(x["hour"]) == name]
        if not sub:
            sub = []
        valid = [x for x in sub if x.get("valid")]
        basis = valid or sub
        if not basis:
            out.append({"name": name, "hour": "—", "condition": "—", "temp_c": None, "rain_probability": None, "risk_class": "limited", "risk_label": "Terbatas"})
            continue
        worst = max(basis, key=lambda z: clamp(z.get("risk_score"), default=0))
        out.append({
            "name": name,
            "hour": worst.get("hour", "—"),
            "condition": worst.get("condition", "—"),
            "temp_c": mean(x.get("temp_c") for x in valid),
            "rain_probability": maximum(x.get("rain_probability") for x in valid),
            "risk_class": worst.get("risk_class", "limited"),
            "risk_label": risk_label(worst.get("risk_class", "limited")),
        })
    return out


def summarize_day(relative: str, date_value: dt.date, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = sorted(rows or default_hours(date_value, relative), key=lambda x: hour_int(x["hour"]))
    valid = [x for x in rows if x.get("valid")]
    basis = valid or rows
    peak = max(basis, key=lambda z: prob(z.get("rain_probability"), -1) if prob(z.get("rain_probability"), None) is not None else -1) if basis else {}
    worst = max(basis, key=lambda z: clamp(z.get("risk_score"), default=0)) if basis else {}
    cls = "limited" if not valid else worst.get("risk_class", "watch")
    score = 35 if cls == "limited" else clamp(worst.get("risk_score"), default=0)
    return {
        "relative": relative,
        "date_iso": date_value.isoformat(),
        "date_label": fmt_date(date_value),
        "date_short": fmt_date(date_value, False),
        "weekday": DAY_ID[date_value.weekday()],
        "hours": rows,
        "periods": period_summaries(rows),
        "peak_rain_probability": prob(peak.get("rain_probability"), None),
        "peak_rain_hour": text(peak.get("hour"), "—"),
        "risk_score": round(score),
        "risk_class": cls,
        "risk_label": risk_label(cls),
        "condition": text(worst.get("condition"), "Data terbatas" if cls == "limited" else "Berawan"),
        "avg_temp_c": mean(x.get("temp_c") for x in valid),
        "avg_rh": mean(x.get("humidity_pct") for x in valid),
        "max_heat_c": maximum(x.get("heat_index_c") for x in valid),
        "max_wind_kmh": maximum(x.get("wind_kmh") for x in valid),
        "safe_windows": best_windows(rows),
        "valid_points": len(valid),
    }


def rain_phrase(day: Dict[str, Any]) -> str:
    p = prob(day.get("peak_rain_probability"), 0) or 0
    h = text(day.get("peak_rain_hour"), "—")
    if p >= 55:
        return f"hujan paling perlu diwaspadai sekitar {h}"
    if p >= 25:
        return f"awan/hujan perlu dipantau sekitar {h}"
    if p > 0:
        return f"peluang hujan kecil, puncaknya sekitar {h}"
    return "peluang hujan rendah"


def decision_sentence(location: str, day: Dict[str, Any], short: bool = False) -> str:
    c = day.get("risk_class", "watch")
    p = pct(day.get("peak_rain_probability"))
    peak = text(day.get("peak_rain_hour"), "—")
    win = ", ".join(day.get("safe_windows") or [])
    if c == "limited":
        return "Data belum cukup. Cek langit lokal sebelum berangkat."
    if c == "danger":
        return f"Tunda aktivitas luar saat jam rawan. Puncak {peak}, peluang {p}."
    if c == "rain":
        return f"Siapkan payung. Jam rawan {peak}, peluang {p}."
    if c == "watch":
        return f"Masih bisa. Pantau sekitar {peak}." if short else f"Masih bisa beraktivitas, tapi pantau awan sekitar {peak}."
    return f"Relatif aman. Jam nyaman: {win or 'pagi hingga siang'}."


def short_activity_advice(day: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    c = day.get("risk_class", "watch")
    peak = text(day.get("peak_rain_hour"), "—")
    win = ", ".join(day.get("safe_windows") or ["cek langit"])
    heat = num(day.get("max_heat_c"), 0) or 0
    if c in {"danger", "rain"}:
        return [
            ("Motor", "Bawa jas", f"Hindari {peak}.", "rain"),
            ("Jalan kaki", "Cari teduh", f"Siapkan tempat berhenti.", "rain"),
            ("Jemur", "Pagi saja", "Jangan ditinggal.", "watch"),
            ("Outdoor", "Plan B", "Siapkan opsi indoor.", "rain"),
            ("Olahraga", "Geser jam", win, "watch"),
            ("Foto", "Aman bersyarat", "Lindungi kamera.", "watch"),
        ]
    if c == "watch":
        return [
            ("Motor", "Masih bisa", f"Waspadai {peak}.", "watch"),
            ("Jalan kaki", "Aman bersyarat", win, "safe"),
            ("Jemur", "Pagi–siang", "Angkat sebelum sore.", "safe" if heat < 36 else "watch"),
            ("Outdoor", "Bisa", "Tetap lihat awan.", "watch"),
            ("Olahraga", "Pilih teduh", win, "watch" if heat >= 34 else "safe"),
            ("Foto", "Cek awan", f"Pantau {peak}.", "watch"),
        ]
    if c == "limited":
        return [
            ("Motor", "Cek manual", "Data belum lengkap.", "limited"),
            ("Jalan kaki", "Hati-hati", "Lihat kondisi lokal.", "limited"),
            ("Jemur", "Jangan ditinggal", "Pantau berkala.", "limited"),
            ("Outdoor", "Fleksibel", "Siapkan teduh.", "limited"),
            ("Olahraga", "Pendek saja", "Cek cuaca langsung.", "limited"),
            ("Foto", "Cek langit", "Tunggu data lebih baik.", "limited"),
        ]
    return [
        ("Motor", "Aman", "Tetap waspada lokal.", "safe"),
        ("Jalan kaki", "Nyaman", win, "safe"),
        ("Jemur", "Aman", "Pagi–siang bagus.", "safe"),
        ("Outdoor", "Aman", "Cocok untuk acara kecil.", "safe"),
        ("Olahraga", "Pilih pagi/sore", win, "safe"),
        ("Foto", "Cocok", "Cahaya lebih enak pagi/sore.", "safe"),
    ]

# -----------------------------------------------------------------------------
# Load existing generator outputs
# -----------------------------------------------------------------------------

def metadata_by_slug(root: Path) -> Dict[str, Dict[str, Any]]:
    meta: Dict[str, Dict[str, Any]] = {}
    for name in ["dim_locations.csv", "locations.csv", "dim_location.csv"]:
        for row in read_csv(root / name):
            slug = text(pick(row, "slug", "location_slug", default="")) or slugify(text(pick(row, "location_name", "name", default="location")))
            meta.setdefault(slug, {}).update(row)
    gj = read_json(root / "langit_all_locations.geojson", {}) or {}
    for feat in gj.get("features", []) if isinstance(gj, dict) else []:
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        slug = text(props.get("slug") or props.get("location_slug") or slugify(props.get("location_name") or props.get("name") or ""))
        if slug:
            meta.setdefault(slug, {}).update({
                "slug": slug,
                "location_name": props.get("location_name") or props.get("name"),
                "longitude": coords[0] if len(coords) >= 1 else None,
                "latitude": coords[1] if len(coords) >= 2 else None,
            })
    return meta


def location_dirs(root: Path) -> List[Path]:
    if not root.exists():
        return []
    out = []
    sentinel_files = [
        "anemos_app.html", "langit_hourly_intelligence.csv", "anemos_hourly_compact.csv",
        "langit_api_v1.json", "anemos_api_v1.json", "forecast.csv", "forecast_all_locations.csv",
    ]
    for p in root.iterdir():
        if p.is_dir() and any((p / s).exists() for s in sentinel_files):
            out.append(p)
    return sorted(out, key=lambda x: x.name)


def rows_from_api(api: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    days = api.get("days")
    if isinstance(days, list):
        for day in days[:3]:
            if not isinstance(day, dict):
                continue
            date_value = day.get("date") or day.get("date_iso") or day.get("target_date")
            relative = day.get("relative") or day.get("day_tag") or day.get("label")
            for key in ["hours", "key_hours", "hourly", "rows", "forecast"]:
                items = day.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            x = dict(item)
                            x.setdefault("date", date_value)
                            x.setdefault("relative_day", relative)
                            rows.append(x)
                    break
    for key in ["hours", "hourly", "key_hours", "forecast"]:
        if isinstance(api.get(key), list):
            for item in api[key]:
                if isinstance(item, dict):
                    rows.append(dict(item))
            break
    return rows


def split_rows_into_days(rows: List[Dict[str, Any]], base_date: dt.date) -> List[List[Dict[str, Any]]]:
    if not rows:
        return []
    # First: exact dates from rows.
    dated: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        d = parse_date(pick(r, "date", "tanggal", "target_date", "valid_date", "forecast_date", "datetime", "timestamp"))
        if d:
            dated.setdefault(d.isoformat(), []).append(r)
    if dated:
        return [dated[k] for k in sorted(dated.keys())[:3]]

    # Second: day tags.
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        tag = text(pick(r, "relative_day", "day_tag", "hari", "day", default=""))
        if tag:
            groups.setdefault(tag.lower(), []).append(r)
    if groups and len(groups) > 1:
        order = ["hari ini", "today", "besok", "tomorrow", "lusa", "day 2"]
        ordered_keys = sorted(groups.keys(), key=lambda k: order.index(k) if k in order else 99)
        return [groups[k] for k in ordered_keys[:3]]

    # Third: keep source order and split when hour decreases.
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    last_h = -1
    for r in rows:
        h = hour_int(pick(r, "hour", "jam", "time", "local_time", "datetime", "timestamp", default="00:00"))
        if current and h < last_h:
            chunks.append(current)
            current = []
        current.append(r)
        last_h = h
    if current:
        chunks.append(current)
    return chunks[:3]


def load_location_api(directory: Path, meta: Dict[str, Any]) -> Dict[str, Any]:
    raw_api: Dict[str, Any] = {}
    for name in ["langit_api_v1.json", "anemos_api_v1.json", "api.json"]:
        if (directory / name).exists():
            raw_api = read_json(directory / name, {}) or {}
            if isinstance(raw_api, dict):
                break
    loc_name = text(raw_api.get("location_name"), text(meta.get("location_name"), directory.name.replace("-", " ").title()))
    slug = text(raw_api.get("location_slug"), text(meta.get("slug"), directory.name))
    lat = num(raw_api.get("latitude"), num(meta.get("latitude"), num(meta.get("lat"))))
    lon = num(raw_api.get("longitude"), num(meta.get("longitude"), num(meta.get("lon"))))
    if lat is None or lon is None:
        gj = read_json(directory / "langit_location.geojson", {}) or {}
        feats = gj.get("features") if isinstance(gj, dict) else []
        if feats:
            coords = (feats[0].get("geometry") or {}).get("coordinates") or []
            if len(coords) >= 2:
                lon = num(coords[0], lon)
                lat = num(coords[1], lat)

    rows: List[Dict[str, Any]] = []
    for fname in [
        "langit_hourly_intelligence.csv", "anemos_hourly_compact.csv", "anemos_risk_timeline.csv",
        "forecast.csv", "forecast_all_locations.csv",
    ]:
        rows = read_csv(directory / fname)
        if rows:
            break
    if not rows and raw_api:
        rows = rows_from_api(raw_api)

    base_date = local_now().date()
    d0 = parse_date(raw_api.get("target_date") or raw_api.get("date") or raw_api.get("generated_at") or raw_api.get("updated_at"))
    if d0:
        base_date = d0

    chunks = split_rows_into_days(rows, base_date)
    relatives = ["Hari ini", "Besok", "Lusa"]
    days = []
    for i in range(3):
        date_value = base_date + dt.timedelta(days=i)
        chunk = chunks[i] if i < len(chunks) else []
        parsed = [row_to_hour(r, date_value, relatives[i]) for r in chunk] if chunk else default_hours(date_value, relatives[i])
        days.append(summarize_day(relatives[i], date_value, parsed))

    sources: List[Dict[str, Any]] = []
    for fname in ["source_status.csv", "source_status_all_locations.csv", "langit_source_status.csv"]:
        sources = read_csv(directory / fname)
        if sources:
            break

    api = {
        "brand": BRAND,
        "version": VERSION,
        "generated_at": fmt_update(raw_api.get("generated_at") or raw_api.get("updated_at")),
        "location_name": loc_name,
        "location_slug": slug,
        "latitude": lat,
        "longitude": lon,
        "today": days[0],
        "days": days,
        "sources": sources,
        "raw_version": raw_api.get("version"),
    }
    return api

# -----------------------------------------------------------------------------
# HTML components
# -----------------------------------------------------------------------------

CSS = r'''
:root{--bg:#07111f;--panel:#101f32;--panel2:#142840;--line:#284763;--text:#f7fbff;--muted:#9fb4c9;--blue:#32b7ff;--green:#35e8a4;--amber:#ffd052;--orange:#ff9346;--red:#ff4778;--limited:#9ba8ff;--shadow:0 20px 70px rgba(0,0,0,.35)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 73% 8%,rgba(50,183,255,.28),transparent 30%),radial-gradient(circle at 15% 0,rgba(53,232,164,.13),transparent 24%),linear-gradient(180deg,#07111f,#0a1b2e 48%,#07111f);color:var(--text);font-family:Inter,"Plus Jakarta Sans",Manrope,system-ui,-apple-system,Segoe UI,sans-serif;letter-spacing:-.025em}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.12;background-image:linear-gradient(rgba(255,255,255,.07) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.07) 1px,transparent 1px);background-size:42px 42px}a{text-decoration:none;color:inherit}.top{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:15px clamp(16px,4vw,70px);background:rgba(7,17,31,.86);backdrop-filter:blur(22px);border-bottom:1px solid rgba(148,190,235,.17)}.brand{display:flex;align-items:center;gap:12px}.logo{width:38px;height:38px;border-radius:15px;background:radial-gradient(circle at 30% 22%,#95f7ff,transparent 23%),linear-gradient(135deg,#176bff,#21c7ff 48%,#38e7a2);box-shadow:0 0 34px rgba(50,183,255,.42)}.brand b{display:block;font-size:18px}.brand small{display:block;color:var(--muted);font-size:11px;margin-top:1px}.navs{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.nav,.btn{border:1px solid rgba(148,190,235,.27);background:rgba(255,255,255,.045);border-radius:999px;padding:9px 13px;font-size:12px;font-weight:850;color:#dcecff}.nav.active,.btn.primary{background:linear-gradient(135deg,#0a91ff,#32d0ff);border-color:transparent;box-shadow:0 12px 30px rgba(50,183,255,.25)}.wrap{width:min(1180px,calc(100% - 32px));margin:0 auto;padding:24px 0 64px}.hero-grid{display:grid;grid-template-columns:minmax(0,1.6fr) 300px;gap:16px}.hero{min-height:210px;border-radius:30px;border:1px solid rgba(80,211,255,.38);padding:30px;background:radial-gradient(circle at 88% 68%,rgba(111,219,255,.52),transparent 28%),linear-gradient(135deg,#123e83,#176bff 58%,#26c7ff);box-shadow:var(--shadow);position:relative;overflow:hidden}.hero:after{content:"";position:absolute;right:-92px;bottom:-120px;width:340px;height:340px;border-radius:50%;background:rgba(255,255,255,.16)}.chips{display:flex;gap:7px;flex-wrap:wrap;position:relative;z-index:1}.chip{font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.06em;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);padding:6px 9px;border-radius:999px}.hero h1{position:relative;z-index:1;margin:32px 0 10px;font-size:clamp(38px,5.9vw,76px);line-height:.92}.hero p{position:relative;z-index:1;margin:0;color:#eaf7ff;font-size:clamp(15px,1.8vw,20px);max-width:760px}.side{display:grid;grid-template-columns:1fr 1fr;gap:12px}.tile,.panel,.decision-main,.kpi,.card,.hour,.stat,.map-card{background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.035));border:1px solid rgba(148,190,235,.21);box-shadow:0 15px 45px rgba(0,0,0,.18)}.tile{border-radius:22px;padding:18px;min-height:102px}.tile.main{grid-column:1/-1;min-height:132px}.tile span,.kpi span,.stat span{display:block;color:var(--muted);font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.09em}.tile strong{display:block;font-size:39px;margin:8px 0}.tile b{display:block;font-size:25px;margin:8px 0}.tile p,.kpi small{margin:0;color:#cfe1f5}.notice{margin:14px 0 17px;padding:8px 12px;border-radius:999px;border:1px solid rgba(255,208,82,.45);background:rgba(255,208,82,.07);color:#ffe39b;font-size:11px;font-weight:900}.decision{display:grid;grid-template-columns:minmax(0,1.35fr) 330px;gap:16px;margin-top:16px}.decision-main{border-radius:28px;padding:26px;min-height:210px;display:flex;flex-direction:column;justify-content:space-between}.badge{display:inline-flex;width:max-content;padding:7px 10px;border-radius:999px;border:1px solid currentColor;font-size:11px;font-weight:950}.safe{color:var(--green)}.watch{color:var(--amber)}.rain{color:var(--orange)}.danger{color:var(--red)}.limited{color:var(--limited)}.decision-main h2{font-size:clamp(34px,4.8vw,62px);line-height:.94;margin:18px 0 0}.decision-main p{margin:16px 0 0;color:#cfe1f5}.kpis{display:grid;grid-template-columns:1fr 1fr;gap:11px}.kpi{border-radius:19px;padding:16px}.kpi strong{display:block;font-size:26px;margin:9px 0 3px}.panel{margin-top:20px;padding:22px;border-radius:28px}.head{display:flex;align-items:end;justify-content:space-between;gap:16px;margin-bottom:18px}.head h2{font-size:24px;margin:0}.head p{margin:0;color:var(--muted);font-size:13px}.timeline{display:grid;grid-template-columns:repeat(var(--n),1fr);align-items:end;gap:10px;min-height:155px}.bar{text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:end;gap:8px}.bar b{font-size:13px}.bar small{font-size:10px;color:var(--muted)}.bar .v{width:100%;min-height:8px;border-radius:16px 16px 8px 8px;background:linear-gradient(180deg,var(--c),rgba(255,255,255,.05));box-shadow:0 14px 28px rgba(0,0,0,.18)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card{border-radius:22px;padding:19px}.card h3{margin:0 0 8px;font-size:22px}.card p{margin:0;color:#cce0f4}.card .micro{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px}.stat{border-radius:14px;padding:11px;background:#0d2949}.stat b{display:block;font-size:18px;margin-top:5px}.activities{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.activity{min-height:132px;border-color:var(--accent);border-radius:22px;padding:19px;background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.035))}.activity h3{margin:0 0 8px;font-size:20px}.activity b{display:block;font-size:17px}.activity p{color:#cbdff3}.focus{font-size:11px;color:#8fd7ff;font-weight:950;text-transform:uppercase;margin-top:12px}.hours{display:grid;gap:9px}.hour{display:grid;grid-template-columns:78px minmax(0,1.4fr) repeat(4,minmax(94px,.55fr));gap:9px;align-items:center;border-radius:18px;padding:12px;border-left:5px solid var(--accent)}.time{font-size:20px;font-weight:950}.hour h3{margin:0;font-size:16px}.hour p{margin:2px 0 0;color:var(--muted);font-size:12px}.hbox{border-radius:14px;padding:11px;background:#0c294b;border:1px solid rgba(74,133,196,.38)}.hbox b{display:block;font-size:16px}.hbox span{font-size:10px;color:var(--muted)}.map-frame{width:100%;height:430px;border:0;border-radius:22px;background:#03101f}.map-card{border-radius:26px;padding:16px}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}.subtle{color:var(--muted)}details.clean{margin-top:14px;border:1px solid rgba(148,190,235,.18);border-radius:20px;background:rgba(255,255,255,.035);padding:14px}details.clean summary{cursor:pointer;font-weight:900}table{width:100%;border-collapse:collapse;min-width:620px}th,td{text-align:left;padding:12px;border-bottom:1px solid rgba(148,190,235,.15)}th{font-size:11px;color:#8fd7ff;text-transform:uppercase;letter-spacing:.08em}.tablewrap{overflow:auto}.footer{margin:28px 0 0;text-align:center;color:var(--muted);font-size:12px}.mini-footer{display:flex;justify-content:center;gap:10px;flex-wrap:wrap;margin-top:20px}.mini-footer a{color:#9bd6ff;font-size:12px}.portal .card{border-color:var(--accent)}.visual-note{font-size:12px;color:#9fb4c9}@media(max-width:980px){.hero-grid,.decision,.grid2,.grid3,.grid4{grid-template-columns:1fr}.side,.kpis{grid-template-columns:1fr 1fr}.activities{grid-template-columns:1fr 1fr}.hour{grid-template-columns:70px 1fr 1fr 1fr}.hour .hbox:nth-last-child(-n+2){display:none}.top{align-items:flex-start;flex-direction:column}}@media(max-width:640px){.wrap{width:calc(100% - 22px)}.hero{min-height:190px;padding:22px;border-radius:24px}.hero h1{font-size:42px}.side,.kpis,.activities,.card .micro{grid-template-columns:1fr 1fr}.timeline{display:flex;overflow:auto;min-height:140px}.bar{min-width:54px}.bar .v{width:42px}.hour{grid-template-columns:62px 1fr}.hour .hbox{display:none}.hour .rainbox,.hour .riskbox{display:block}.panel{padding:16px}.map-frame{height:360px}.nav{padding:8px 10px}}
'''


def nav(api: Dict[str, Any], active: str, root: bool = False) -> str:
    if root:
        items = [("Lokasi", "index.html", "locations"), ("Peta", "langit_portal_map.html", "map")]
        subtitle = f"Portal · {VERSION}"
        href = "index.html"
    else:
        items = [("Hari ini", "anemos_app.html", "today"), ("3 hari", "anemos_3day.html", "3day"), ("Aktivitas", "anemos_activity.html", "activity"), ("Peta", "langit_map_room.html", "map")]
        subtitle = f"{api['location_name']} · {VERSION}"
        href = "../index.html"
    links = "".join(f'<a class="nav {"active" if key == active else ""}" href="{esc(url)}">{esc(label)}</a>' for label, url, key in items)
    return f'<header class="top"><a class="brand" href="{href}"><span class="logo"></span><span><b>{BRAND}</b><small>{esc(subtitle)}</small></span></a><nav class="navs">{links}</nav></header>'


def document(api: Dict[str, Any], active: str, title: str, body: str, root: bool = False) -> str:
    footer_links = ""
    if not root:
        footer_links = '<div class="mini-footer"><a href="langit_model_court.html">Keandalan data</a><a href="sentinel_x_accuracy_public.html">Akurasi</a><a href="langit_api_v1.json">JSON</a><a href="langit_location.geojson">GeoJSON</a></div>'
    return f'<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><meta name="theme-color" content="#07111f"><style>{CSS}</style></head><body>{nav(api, active, root=root)}<main class="wrap">{body}{footer_links}<p class="footer">{BRAND} · {VERSION} · {esc(api.get("generated_at", fmt_update()))}</p></main></body></html>'


def hero(api: Dict[str, Any], heading: str, sub: str, day: Dict[str, Any]) -> str:
    return f'''
<section class="hero-grid">
  <article class="hero">
    <div class="chips"><span class="chip">{BRAND}</span><span class="chip">{VERSION}</span><span class="chip">{esc(day.get('date_label'))}</span><span class="chip">{esc(api.get('generated_at'))}</span></div>
    <h1>{esc(heading)}</h1>
    <p>{esc(sub)}</p>
  </article>
  <aside class="side">
    <div class="tile main"><span>Cuaca</span><strong>{deg(day.get('avg_temp_c'))}</strong><p>{esc(day.get('condition'))}</p></div>
    <div class="tile"><span>Hujan</span><b>{pct(day.get('peak_rain_probability'))}</b><p>puncak {esc(day.get('peak_rain_hour'))}</p></div>
    <div class="tile"><span>Status</span><b>{esc(day.get('risk_label'))}</b><p>{round(clamp(day.get('risk_score'))):.0f}/100</p></div>
  </aside>
</section>
<div class="notice">{esc(DISCLAIMER)}</div>
'''


def kpi(label: str, value: str, sub: str = "") -> str:
    return f'<div class="kpi"><span>{esc(label)}</span><strong>{esc(value)}</strong><small>{esc(sub)}</small></div>'


def decision_block(api: Dict[str, Any], day: Dict[str, Any]) -> str:
    cls = day.get("risk_class", "watch")
    loc = api["location_name"]
    windows_text = ", ".join(day.get("safe_windows") or ["cek kondisi lokal"])
    return f'''
<section class="decision">
  <article class="decision-main">
    <div><span class="badge {esc(cls)}">{esc(day.get('risk_label'))}</span><h2>{esc(loc)}: {esc(decision_sentence(loc, day, short=False))}</h2></div>
    <p>{esc(day.get('date_label'))}. {esc(rain_phrase(day))}. Jam nyaman: {esc(windows_text)}.</p>
  </article>
  <aside class="kpis">
    {kpi('Risiko', f"{round(clamp(day.get('risk_score'))):.0f}/100", day.get('risk_label',''))}
    {kpi('Puncak hujan', pct(day.get('peak_rain_probability')), day.get('peak_rain_hour','—'))}
    {kpi('Jam nyaman', windows_text, 'aktivitas')}
    {kpi('Panas terasa', deg(day.get('max_heat_c')), 'maksimum')}
  </aside>
</section>
'''


def timeline(hours: List[Dict[str, Any]], title: str = "Timeline hujan", note: str = "") -> str:
    bars = []
    selected = hours[:12]
    for item in selected:
        cls = item.get("risk_class", "limited")
        p = prob(item.get("rain_probability"), 0) or 0
        height = max(8, round(12 + p * 1.15))
        bars.append(f'<div class="bar"><b>{pct(item.get("rain_probability"))}</b><div class="v" style="height:{height}px;--c:{risk_color(cls)}"></div><small>{esc(item.get("hour"))}</small></div>')
    return f'<section class="panel"><div class="head"><h2>{esc(title)}</h2><p>{esc(note)}</p></div><div class="timeline" style="--n:{max(1,len(selected))}">{"".join(bars)}</div></section>'


def period_cards(day: Dict[str, Any]) -> str:
    cards = []
    for p in day.get("periods", []):
        cls = p.get("risk_class", "limited")
        cards.append(f'''
<article class="card" style="border-color:{risk_color(cls)}">
  <h3>{esc(p.get('name'))}</h3>
  <p>{esc(p.get('condition'))}</p>
  <div class="micro"><div class="stat"><span>Suhu</span><b>{deg(p.get('temp_c'))}</b></div><div class="stat"><span>Hujan</span><b>{pct(p.get('rain_probability'))}</b></div><div class="stat"><span>Jam</span><b>{esc(p.get('hour'))}</b></div></div>
</article>''')
    return f'<section class="panel"><div class="head"><h2>Pagi–malam</h2><p>{esc(day.get("date_label"))}</p></div><div class="grid4">{"".join(cards)}</div></section>'


def activity_cards(day: Dict[str, Any]) -> str:
    cards = []
    for name, status, advice, cls in short_activity_advice(day):
        cards.append(f'<article class="activity" style="--accent:{risk_color(cls)}"><h3>{esc(name)}</h3><b>{esc(status)}</b><p>{esc(advice)}</p></article>')
    return f'<section class="panel"><div class="head"><h2>Saran aktivitas</h2><p>Singkat, praktis.</p></div><div class="activities">{"".join(cards)}</div></section>'


def hour_rows(day: Dict[str, Any], open_details: bool = False) -> str:
    rows = []
    for x in day.get("hours", []):
        cls = x.get("risk_class", "limited")
        rows.append(f'''
<div class="hour" style="--accent:{risk_color(cls)}">
  <div class="time">{esc(x.get('hour'))}</div>
  <div><h3>{esc(x.get('condition'))}</h3><p>{esc(x.get('risk_label'))}</p></div>
  <div class="hbox"><b>{deg(x.get('temp_c'))}</b><span>Suhu</span></div>
  <div class="hbox"><b>{pct(x.get('humidity_pct'))}</b><span>RH</span></div>
  <div class="hbox"><b>{deg(x.get('heat_index_c'))}</b><span>Terasa</span></div>
  <div class="hbox rainbox"><b>{pct(x.get('rain_probability'))}</b><span>Hujan</span></div>
</div>''')
    attr = " open" if open_details else ""
    return f'<details class="clean"{attr}><summary>Detail jam · {esc(day.get("date_label"))}</summary><div class="hours" style="margin-top:12px">{"".join(rows)}</div></details>'


def day_cards(days: List[Dict[str, Any]]) -> str:
    cards = []
    for d in days:
        cls = d.get("risk_class", "limited")
        cards.append(f'''
<article class="card" style="border-color:{risk_color(cls)}">
  <span class="badge {esc(cls)}">{esc(d.get('relative'))}</span>
  <h3>{esc(d.get('risk_label'))}</h3>
  <p>{esc(d.get('date_label'))}. {esc(decision_sentence('', d, short=True))}</p>
  <div class="micro"><div class="stat"><span>Hujan</span><b>{pct(d.get('peak_rain_probability'))}</b></div><div class="stat"><span>Jam</span><b>{esc(d.get('peak_rain_hour'))}</b></div><div class="stat"><span>Risiko</span><b>{round(clamp(d.get('risk_score'))):.0f}</b></div></div>
</article>''')
    return f'<section class="panel"><div class="head"><h2>Ringkasan 3 hari</h2><p>Tanggal jelas, angka secukupnya.</p></div><div class="grid3">{"".join(cards)}</div></section>'


def share_box(api: Dict[str, Any], day: Dict[str, Any]) -> str:
    msg = f"LANGIT — {api['location_name']}\n{day['date_label']}\n{decision_sentence(api['location_name'], day, short=True)}\nPuncak hujan {pct(day.get('peak_rain_probability'))} sekitar {day.get('peak_rain_hour','—')}."
    return f'<section class="grid2"><article class="panel"><div class="head"><h2>Share singkat</h2><p>Format WA.</p></div><textarea readonly style="width:100%;min-height:118px;border:1px solid rgba(148,190,235,.22);border-radius:16px;background:#071525;color:#f7fbff;padding:14px">{esc(msg)}</textarea></article><article class="panel"><div class="head"><h2>Catatan</h2><p>Ringkas.</p></div><p class="subtle">Prakiraan bisa bergeser beberapa kilometer atau beberapa jam. Untuk cuaca ekstrem, pakai informasi BMKG dan kondisi setempat.</p></article></section>'

# -----------------------------------------------------------------------------
# Map pages
# -----------------------------------------------------------------------------

def geo_for_api(api: Dict[str, Any]) -> Dict[str, Any]:
    lat = num(api.get("latitude"), -6.2)
    lon = num(api.get("longitude"), 106.8)
    features = []
    for day in api.get("days", [])[:3]:
        for h in day.get("hours", []):
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "location_name": api.get("location_name"),
                    "slug": api.get("location_slug"),
                    "date": day.get("date_label"),
                    "date_iso": day.get("date_iso"),
                    "relative": day.get("relative"),
                    "hour": h.get("hour"),
                    "rain_probability": h.get("rain_probability"),
                    "risk_score": h.get("risk_score"),
                    "risk_class": h.get("risk_class"),
                    "risk_label": h.get("risk_label"),
                    "condition": h.get("condition"),
                    "temp_c": h.get("temp_c"),
                    "humidity_pct": h.get("humidity_pct"),
                    "heat_index_c": h.get("heat_index_c"),
                },
            })
    return {"type": "FeatureCollection", "features": features}


def map_page(title: str, geojson: Dict[str, Any], back_href: str, full: bool = True) -> str:
    data = json.dumps(geojson, ensure_ascii=False)
    css = r'''
html,body,#map{height:100%;margin:0;background:#050d18;color:#fff;font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif}.hud{position:absolute;z-index:900;left:22px;top:24px;width:min(360px,calc(100% - 44px));padding:18px;border-radius:24px;background:rgba(6,17,31,.86);backdrop-filter:blur(18px);border:1px solid rgba(148,190,235,.28);box-shadow:0 20px 70px rgba(0,0,0,.35)}.hud h1{font-size:24px;line-height:1.05;margin:0 0 8px}.hud p{margin:0;color:#bfd1e6;font-size:13px}.btn{display:inline-flex;margin-top:14px;padding:9px 13px;border-radius:999px;background:#22b5ff;color:#fff;text-decoration:none;font-weight:900;font-size:12px}.timebar{position:absolute;z-index:900;left:50%;bottom:24px;transform:translateX(-50%);display:flex;gap:8px;max-width:calc(100% - 34px);overflow:auto;padding:10px;border-radius:999px;background:rgba(6,17,31,.84);border:1px solid rgba(148,190,235,.28);backdrop-filter:blur(16px)}.tbtn{border:1px solid rgba(148,190,235,.32);background:rgba(255,255,255,.07);color:#eaf6ff;border-radius:999px;padding:8px 12px;font-weight:950;cursor:pointer}.tbtn.active{background:#22b5ff;border-color:#22b5ff}.legend{position:absolute;right:22px;bottom:24px;z-index:901;background:rgba(6,17,31,.84);border:1px solid rgba(148,190,235,.28);border-radius:18px;padding:12px;font-size:12px}.legend div{display:flex;gap:8px;align-items:center;margin:5px 0}.dot{width:10px;height:10px;border-radius:50%;background:var(--c)}.fallback{position:absolute;inset:0;display:grid;place-items:center;color:#bcd0e6}.leaflet-control-attribution{background:rgba(6,17,31,.78)!important;color:#bcd0e6!important}.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#071525;color:#fff}.leaflet-popup-content b{font-size:15px}@media(max-width:700px){.hud{left:12px;top:12px}.legend{right:12px;bottom:92px}.timebar{bottom:18px}}
'''
    js = r'''
const data = __DATA__;
const colors = {safe:'#35e8a4', watch:'#ffd052', rain:'#ff9346', danger:'#ff4778', limited:'#9ba8ff'};
const features = (data && data.features) ? data.features : [];
const hours = [...new Set(features.map(f => (f.properties||{}).hour).filter(Boolean))].sort();
const first = features[0] || {geometry:{coordinates:[106.8,-6.2]}, properties:{location_name:'Lokasi'}};
const center = first.geometry && first.geometry.coordinates ? [first.geometry.coordinates[1], first.geometry.coordinates[0]] : [-6.2,106.8];
function ptxt(p){
  const x = p || {};
  return `<b>${x.location_name || 'Lokasi'}</b><br>${x.date || ''} · ${x.hour || ''}<br>${x.condition || ''}<br>Hujan ${Math.round(Number(x.rain_probability||0))}% · Risiko ${x.risk_label || '-'}`;
}
function draw(activeHour){
  layer.clearLayers();
  const selected = features.filter(f => !activeHour || (f.properties||{}).hour === activeHour);
  const use = selected.length ? selected : features.slice(0,1);
  use.forEach(f => {
    const p = f.properties || {}, coords = (f.geometry||{}).coordinates || center.slice().reverse();
    const latlng = [coords[1], coords[0]];
    const cls = p.risk_class || 'limited', color = colors[cls] || '#38bdf8';
    const risk = Math.max(Number(p.risk_score||0), Number(p.rain_probability||0));
    const radius = 1300 + risk * 34;
    L.circle(latlng, {radius:radius, color:color, weight:2, opacity:.95, fillColor:color, fillOpacity:.18}).bindPopup(ptxt(p)).addTo(layer);
    L.circleMarker(latlng, {radius:7, color:'#fff', weight:1, fillColor:color, fillOpacity:1}).bindPopup(ptxt(p)).addTo(layer);
  });
}
let map;
try{
  map = L.map('map',{scrollWheelZoom:true,worldCopyJump:false,maxBounds:[[-11.25,94],[6.45,141.25]],maxBoundsViscosity:.8,minZoom:5,zoomControl:true}).setView(center, 11);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap & CARTO'}).addTo(map);
  var layer = L.layerGroup().addTo(map);
  const bar = document.getElementById('timebar');
  (hours.length ? hours : ['00:00']).forEach((h,i) => {
    const b = document.createElement('button'); b.className = 'tbtn' + (i===0?' active':''); b.textContent = h;
    b.onclick = () => {document.querySelectorAll('.tbtn').forEach(x=>x.classList.remove('active')); b.classList.add('active'); draw(h);};
    bar.appendChild(b);
  });
  draw(hours[0]);
}catch(e){
  document.body.insertAdjacentHTML('beforeend','<div class="fallback">Peta gagal dimuat. Coba refresh atau buka lagi nanti.</div>');
}
'''.replace("__DATA__", data)
    legend = '<div class="legend"><div><i class="dot" style="--c:#35e8a4"></i>Aman</div><div><i class="dot" style="--c:#ffd052"></i>Pantau</div><div><i class="dot" style="--c:#ff9346"></i>Waspada</div><div><i class="dot" style="--c:#ff4778"></i>Tinggi</div></div>'
    return f'<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>{css}</style></head><body><div id="map"></div><section class="hud"><h1>{esc(title)}</h1><p>Zona warna mengikuti jam yang dipilih. Tanggal dan peluang hujan ada di popup.</p><a class="btn" href="{esc(back_href)}">Kembali</a></section><div id="timebar" class="timebar"></div>{legend}<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>{js}</script></body></html>'


def map_embed(api: Dict[str, Any], href: str = "langit_map_room.html") -> str:
    return f'<section class="panel"><div class="head"><h2>Peta risiko</h2><p>Warna berubah per jam.</p></div><div class="map-card"><iframe class="map-frame" src="{esc(href)}" loading="lazy"></iframe><div class="actions"><a class="btn primary" href="{esc(href)}">Buka peta penuh</a><a class="btn" href="langit_location.geojson">GeoJSON</a></div></div></section>'

# -----------------------------------------------------------------------------
# Page builders
# -----------------------------------------------------------------------------

def today_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    heading = f"Prakiraan {api['location_name']}"
    sub = f"{day['date_label']}. {decision_sentence(api['location_name'], day, short=True)}"
    body = hero(api, heading, sub, day)
    body += decision_block(api, day)
    body += timeline(day["hours"], "Timeline hujan", day["date_label"])
    body += period_cards(day)
    body += activity_cards(day)
    body += map_embed(api)
    body += hour_rows(day, open_details=False)
    body += share_box(api, day)
    return document(api, "today", f"LANGIT — {api['location_name']}", body)


def three_day_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    sub = f"Mulai {api['days'][0]['date_label']} sampai {api['days'][-1]['date_label']}."
    body = hero(api, "Prakiraan 3 hari", sub, day)
    body += day_cards(api["days"])
    for d in api["days"]:
        body += timeline(d["hours"], f"{d['relative']} · {d['date_label']}", d.get("risk_label", ""))
    for d in api["days"]:
        body += hour_rows(d, open_details=False)
    return document(api, "3day", f"LANGIT 3 hari — {api['location_name']}", body)


def activity_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    body = hero(api, "Saran aktivitas", f"{day['date_label']}. Fokus pada jam yang perlu dipantau.", day)
    body += decision_block(api, day)
    body += activity_cards(day)
    body += timeline(day["hours"], "Jam rawan", "Lihat warna, bukan tabel panjang.")
    body += period_cards(day)
    body += hour_rows(day, open_details=False)
    body += share_box(api, day)
    return document(api, "activity", f"LANGIT Aktivitas — {api['location_name']}", body)


def data_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    sources = api.get("sources") or []
    active = 0
    for row in sources:
        val = text(pick(row, "active", "is_active", "used", "ok", default=""), "").lower()
        if val in {"yes", "true", "1", "aktif", "ok"}:
            active += 1
    total = len(sources) or 1
    rows = "".join(
        f"<tr><td>{esc(pick(r,'model','source','name',default='—'))}</td><td>{esc(pick(r,'provider','origin',default='—'))}</td><td>{esc(pick(r,'active','is_active','used','ok',default='—'))}</td><td>{esc(pick(r,'weight','score','quality',default='—'))}</td></tr>"
        for r in sources
    ) or '<tr><td colspan="4">Belum ada tabel sumber.</td></tr>'
    body = hero(api, "Keandalan data", f"{day['date_label']}. Ringkasan sumber dibuat pendek; detail teknis disimpan di bawah.", day)
    body += f'<section class="panel"><div class="head"><h2>Sumber terbaca</h2><p>{active}/{total} aktif</p></div><div class="timeline" style="--n:1;min-height:70px"><div class="bar"><b>{round(active/total*100):.0f}%</b><div class="v" style="height:18px;--c:#35e8a4;width:100%"></div><small>keandalan</small></div></div></section>'
    body += f'<section class="panel"><details class="clean" open><summary>Tabel teknis sumber</summary><div class="tablewrap"><table><thead><tr><th>Model</th><th>Sumber</th><th>Aktif</th><th>Bobot</th></tr></thead><tbody>{rows}</tbody></table></div></details></section>'
    body += map_embed(api)
    return document(api, "data", f"LANGIT Data — {api['location_name']}", body)


def accuracy_page(api: Dict[str, Any], directory: Path) -> str:
    day = api["today"]
    # Try multiple possible verification files. If none exist, show a useful empty state.
    summary: Dict[str, Any] = {}
    for name in ["sentinel_x_accuracy_summary.json", "verification_summary.json", "accuracy_summary.json", "sentinel_verification_summary.json"]:
        obj = read_json(directory / name, {})
        if isinstance(obj, dict) and obj:
            summary = obj
            break
    matched = int(num(pick(summary, "matched_cases", "pairs", "n", default=0), 0) or 0)
    target = int(num(pick(summary, "target_cases", "minimum_cases", default=30), 30) or 30)
    pct_done = clamp(matched / max(1, target) * 100)
    body = hero(api, "Akurasi", f"{day['date_label']}. Skor ditampilkan setelah pasangan prakiraan–observasi cukup.", day)
    body += f'<section class="panel"><div class="head"><h2>{"Data akurasi cukup" if matched >= target else "Belum cukup data"}</h2><p>{matched}/{target} pasangan</p></div><div class="timeline" style="--n:1;min-height:80px"><div class="bar"><b>{pct_done:.0f}%</b><div class="v" style="height:20px;--c:#32b7ff"></div><small>progress</small></div></div></section>'
    if matched >= target:
        body += f'<section class="grid3"><article class="card"><h3>Error suhu</h3><p>{esc(pick(summary,"mae_temp","temperature_mae",default="—"))}</p></article><article class="card"><h3>Skor hujan</h3><p>{esc(pick(summary,"rain_score","brier_score",default="—"))}</p></article><article class="card"><h3>Alarm keliru</h3><p>{esc(pick(summary,"false_alarm_rate",default="—"))}</p></article></section>'
    else:
        body += '<section class="panel"><p class="subtle">Halaman ini sengaja tidak mengklaim akurasi sebelum data cukup. Begitu observasi terkumpul, metrik akan muncul otomatis.</p></section>'
    return document(api, "accuracy", f"LANGIT Akurasi — {api['location_name']}", body)


def portal_page(apis: List[Dict[str, Any]], root: Path) -> str:
    today = apis[0]["today"] if apis else summarize_day("Hari ini", local_now().date(), [])
    dummy = {"location_name": "Portal", "generated_at": fmt_update(), "today": today}
    cards = []
    for api in sorted(apis, key=lambda a: clamp(a["today"].get("risk_score"), default=0), reverse=True):
        d = api["today"]
        cls = d.get("risk_class", "limited")
        cards.append(f'''
<article class="card" style="--accent:{risk_color(cls)};border-color:{risk_color(cls)}">
  <span class="badge {esc(cls)}">{esc(d.get('risk_label'))}</span>
  <h3>{esc(api.get('location_name'))}</h3>
  <p>{esc(d.get('date_label'))}. {esc(decision_sentence(api.get('location_name',''), d, short=True))}</p>
  <div class="micro"><div class="stat"><span>Hujan</span><b>{pct(d.get('peak_rain_probability'))}</b></div><div class="stat"><span>Jam</span><b>{esc(d.get('peak_rain_hour'))}</b></div><div class="stat"><span>Risiko</span><b>{round(clamp(d.get('risk_score'))):.0f}</b></div></div>
  <div class="actions"><a class="btn primary" href="{esc(api['location_slug'])}/anemos_app.html">Buka</a><a class="btn" href="{esc(api['location_slug'])}/anemos_3day.html">3 hari</a><a class="btn" href="{esc(api['location_slug'])}/anemos_activity.html">Aktivitas</a></div>
</article>''')
    body = hero(dummy, "Cuaca lokal visual", f"{fmt_date(local_now().date())}. Pilih lokasi dan lihat jam rawan tanpa tabel panjang.", today)
    body += f'<section class="panel portal"><div class="head"><h2>Pilih lokasi</h2><p>Diurutkan dari yang paling perlu dipantau.</p></div><div class="grid3">{"".join(cards)}</div></section>'
    body += '<section class="panel"><div class="head"><h2>Peta lokasi</h2><p>Warna mengikuti risiko hari ini.</p></div><div class="map-card"><iframe class="map-frame" src="langit_portal_map.html" loading="lazy"></iframe><div class="actions"><a class="btn primary" href="langit_portal_map.html">Buka peta penuh</a><a class="btn" href="langit_all_locations.geojson">GeoJSON</a></div></div></section>'
    body += '<section class="panel"><div class="head"><h2>Data publik</h2><p>Untuk arsip dan integrasi.</p></div><div class="actions"><a class="btn" href="forecast_all_locations.csv">Forecast CSV</a><a class="btn" href="source_status_all_locations.csv">Sumber CSV</a><a class="btn" href="langit_portal_manifest.json">Manifest</a></div></section>'
    return document(dummy, "locations", "LANGIT Portal", body, root=True)


def portal_geo(apis: List[Dict[str, Any]]) -> Dict[str, Any]:
    features = []
    for api in apis:
        lat = num(api.get("latitude"), None)
        lon = num(api.get("longitude"), None)
        if lat is None or lon is None:
            continue
        d = api["today"]
        h = d.get("peak_rain_hour") or (d.get("hours") or [{}])[0].get("hour", "00:00")
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "location_name": api.get("location_name"),
                "slug": api.get("location_slug"),
                "date": d.get("date_label"),
                "hour": h,
                "rain_probability": d.get("peak_rain_probability"),
                "risk_score": d.get("risk_score"),
                "risk_class": d.get("risk_class"),
                "risk_label": d.get("risk_label"),
                "condition": d.get("condition"),
                "temp_c": d.get("avg_temp_c"),
            },
        })
    return {"type": "FeatureCollection", "features": features}

# -----------------------------------------------------------------------------
# Rebuild + verify
# -----------------------------------------------------------------------------

def verify(root: Path) -> int:
    required_root = [root / "index.html", root / "langit_portal_map.html", root / "langit_portal_manifest.json"]
    missing = [str(p) for p in required_root if not p.exists()]
    for d in location_dirs(root):
        for name in ["anemos_app.html", "anemos_3day.html", "anemos_activity.html", "langit_map_room.html", "langit_model_court.html", "sentinel_x_accuracy_public.html", "langit_api_v1.json", "langit_location.geojson"]:
            if not (d / name).exists():
                missing.append(str(d / name))
    if missing:
        print("ERROR: file publik kurang:")
        for p in missing[:30]:
            print(" -", p)
        return 2
    banned = ["visual-first", "Data confidence", "Window ", "data publik</small>", "ANEMOS sedang", "AETHER Sentinel", "[.new Set", "const hours=[.new"]
    bad_hits = []
    for path in list(root.glob("*.html")) + list(root.glob("*/*.html")):
        txt = path.read_text(encoding="utf-8", errors="replace")
        for b in banned:
            if b in txt:
                bad_hits.append((str(path), b))
    if bad_hits:
        print("ERROR: teks/JS lama masih muncul:")
        for path, token in bad_hits[:40]:
            print(" -", path, "contains", repr(token))
        return 3
    print(f"OK: {VERSION} public output verified.")
    return 0


def rebuild(root: Path, public_base_url: str = "") -> int:
    meta = metadata_by_slug(root)
    dirs = location_dirs(root)
    if not dirs:
        print("ERROR: tidak ada folder lokasi di outputs/. Jalankan forecast dulu.", file=sys.stderr)
        return 2
    apis: List[Dict[str, Any]] = []
    for d in dirs:
        api = load_location_api(d, meta.get(d.name, {"slug": d.name}))
        apis.append(api)
        gj = geo_for_api(api)
        write_json(d / "langit_api_v1.json", api)
        write_json(d / "langit_location.geojson", gj)
        write_json(d / "langit_map_layers.json", {"brand": BRAND, "version": VERSION, "geojson": gj})
        write_text(d / "anemos_app.html", today_page(api))
        write_text(d / "langit_app.html", today_page(api))
        write_text(d / "anemos_today.html", today_page(api))
        write_text(d / "anemos_3day.html", three_day_page(api))
        write_text(d / "langit_3day.html", three_day_page(api))
        write_text(d / "anemos_activity.html", activity_page(api))
        write_text(d / "langit_activity.html", activity_page(api))
        write_text(d / "langit_model_court.html", data_page(api))
        write_text(d / "sentinel_x_accuracy_public.html", accuracy_page(api, d))
        write_text(d / "langit_map_room.html", map_page(f"LANGIT Map — {api['location_name']}", gj, "anemos_app.html"))
        write_text(d / "langit_whatsapp_brief.txt", f"LANGIT — {api['location_name']}\n{api['today']['date_label']}\n{decision_sentence(api['location_name'], api['today'], short=True)}\n")

    pgeo = portal_geo(apis)
    write_json(root / "langit_all_locations.geojson", pgeo)
    write_json(root / "langit_portal_manifest.json", {"brand": BRAND, "version": VERSION, "generated_at": fmt_update(), "public_base_url": public_base_url, "locations": [{"slug": a["location_slug"], "name": a["location_name"]} for a in apis]})
    write_text(root / "langit_portal_map.html", map_page("LANGIT Portal Map", pgeo, "index.html"))
    write_text(root / "index.html", portal_page(apis, root))
    print(f"OK: {VERSION} rebuild selesai. lokasi={len(apis)}")
    return verify(root)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild LANGIT public HTML layer.")
    parser.add_argument("--root", default="outputs", help="Output directory from forecast generator.")
    parser.add_argument("--public-base-url", default="", help="GitHub Pages base URL.")
    parser.add_argument("--verify-only", action="store_true", help="Only verify public files.")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.verify_only:
        return verify(root)
    return rebuild(root, args.public_base_url)


if __name__ == "__main__":
    raise SystemExit(main())
