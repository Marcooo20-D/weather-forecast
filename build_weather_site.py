#!/usr/bin/env python3
"""
LANGIT public site builder
=============================

Public presentation layer for the forecast engine's existing output.
Scientific quantities keep their source units, dates and missing values.

Usage:
  python build_weather_site.py --root outputs --public-base-url https://marcooo20-d.github.io/weather-forecast
  python build_weather_site.py --root outputs --verify-only
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo
from site_tools import campus_components as campus_ui
from site_tools import weather_field, weather_news

BRAND = "LANGIT"
VERSION = "LANGIT v67.0"
TZ_NAME = "Asia/Jakarta"
DISCLAIMER = "Untuk cuaca ekstrem, ikuti peringatan resmi BMKG."
ID_BOUNDS = [[-11.25, 94.0], [6.45, 141.25]]
MONTH_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
DAY_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

# ---------------------------------------------------------------------------
# Safe helpers (ported from v63 — preserving exact data parsing logic)
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
        if value is None or isinstance(value, bool):
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
    """Public schemas use percent (0..100), including values below one percent."""
    x = num(value, default)
    if x is None:
        return default
    return x if 0 <= x <= 100 else default


def hour(value: Any, default: str = "00:00") -> str:
    raw = text(value, default)
    if parse_date(raw):
        stamp = parse_timestamp(raw)
        if stamp and re.search(r"[T\s]\d{1,2}:\d{2}", raw):
            return stamp.strftime("%H:%M")
    # An ISO date starts with a year; never interpret its first digits as hours.
    m = re.search(r"(?:T|\s)(\d{1,2}):(\d{2})(?::\d{2})?", raw) if parse_date(raw) else None
    if not m:
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?(?:\s*WIB)?", raw)
    if not m:
        return default
    h, minute = int(m.group(1)), int(m.group(2) or 0)
    return f"{h:02d}:{minute:02d}" if 0 <= h <= 23 and 0 <= minute <= 59 else default


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


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    raw = text(value)
    if not re.search(r"[T\s]\d{1,2}:\d{2}", raw):
        return None
    try:
        stamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo(TZ_NAME))
        return stamp.astimezone(ZoneInfo(TZ_NAME))
    except ValueError:
        return None


def fmt_update(value: Any = None) -> str:
    raw = text(value)
    stamp = parse_timestamp(raw)
    if stamp:
        return f"Diperbarui {fmt_date(stamp.date(), False)}, {stamp:%H:%M} WIB"
    return raw if raw.startswith("Diperbarui ") else "Waktu pembaruan belum tersedia"


def pct(value: Any) -> str:
    x = prob(value, None)
    return "—" if x is None else "<1%" if 0 < x < 1 else f"{round(x):.0f}%"


def deg(value: Any) -> str:
    x = num(value, None)
    return "—" if x is None else f"{x:.1f}°C"


def kmh(value: Any) -> str:
    x = num(value, None)
    return "—" if x is None else f"{x:.1f} km/jam"


def score_text(value: Any) -> str:
    value = num(value)
    return "—" if value is None else str(round(value))


def read_json(path: Path, default: Any = None, *, strict: bool = False) -> Any:
    if not path.exists():
        return default
    try:
        def reject_constant(value):
            raise ValueError("Non-finite JSON constant: " + value)
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        if strict:
            raise ValueError(f"Invalid forecast input {path}: {exc}") from exc
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return [dict(row) for row in csv.DictReader(f)]
    except Exception:
        return []


SANITIZE_REPLACEMENTS = [
    ("Window aman", "Jam aman"), ("window aman", "jam aman"),
    ("Window nyaman", "Jam nyaman"), ("window nyaman", "jam nyaman"),
    ("Window aktivitas", "Jam aktivitas"), ("window aktivitas", "jam aktivitas"),
    ("Window hujan", "Jam hujan"), ("window hujan", "jam hujan"),
    ("Data confidence", "Keyakinan data"), ("data confidence", "keyakinan data"),
    ("visual-first", "visual"),
    ("ANEMOS sedang", "LANGIT sedang"), ("AETHER Sentinel", "LANGIT Sentinel"),
    ("data publik</small>", "data</small>"),
]


def sanitize_public_text(content: str) -> str:
    out = content
    for old, new in SANITIZE_REPLACEMENTS:
        out = out.replace(old, new)
    out = re.sub(r"\bAI[- ]generated\b", "otomatis", out, flags=re.I)
    out = re.sub(r"\bDecision[- ]first\b", "ringkas", out, flags=re.I)
    out = re.sub(r"\bHyperlocal Weather Intelligence OS\b", "Prakiraan lokal", out, flags=re.I)
    return out


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {".html", ".htm", ".txt"}:
        content = sanitize_public_text(content)
    path.write_text(content, encoding="utf-8")


def sanitize_existing_public_files(root: Path) -> int:
    changed = 0
    if not root.exists():
        return changed
    for path in list(root.glob("*.html")) + list(root.glob("*/*.html")):
        try:
            old = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        new = sanitize_public_text(old)
        new = new.replace("[.new Set", "[...new Set")
        new = new.replace("const hours=[.new", "const hours=[...new")
        if new != old:
            path.write_text(new, encoding="utf-8")
            changed += 1
    if changed:
        print(f"[SANITIZE] cleaned legacy public HTML files: {changed}")
    return changed


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
# Forecast logic + copywriting (ported from v63)
# ---------------------------------------------------------------------------

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


def wind_risk(wind: Any) -> float:
    w = num(wind, 0)
    if w is None:
        return 0
    if w >= 50: return 80
    if w >= 40: return 60
    if w >= 30: return 30
    return 0


def risk_class(score: Any, valid: bool = True) -> str:
    if not valid:
        return "limited"
    x = clamp(score)
    if x >= 78: return "danger"
    if x >= 55: return "rain"
    if x >= 25: return "watch"
    return "safe"


def risk_label(cls: str) -> str:
    return {"safe": "Potensi gangguan rendah", "watch": "Perlu dipantau", "rain": "Gangguan aktivitas mungkin", "danger": "Potensi gangguan tinggi", "limited": "Data terbatas"}.get(cls, "Perlu dipantau")


def risk_color(cls: str) -> str:
    return {"safe": "#35e8a4", "watch": "#ffd052", "rain": "#ff9346", "danger": "#ff4778", "limited": "#9ba8ff"}.get(cls, "#32b7ff")


def condition_label(hh: str, rain: Any, temp: Any, rh: Any, heat: Any, valid: bool) -> str:
    if not valid:
        return "Data terbatas"
    p = prob(rain, 0) or 0
    t = num(temp, None)
    hi_val = num(heat, t)
    r = num(rh, None)
    h = hour_int(hh)
    if p >= 55: return "Peluang hujan tinggi"
    if p >= 20: return "Ada peluang hujan"
    if hi_val is not None and hi_val >= 36 and 10 <= h <= 16: return "Panas menyengat"
    if hi_val is not None and hi_val >= 34 and 9 <= h <= 16: return "Panas lembap"
    if r is not None and r >= 88 and (h <= 8 or h >= 19): return "Lembap"
    return "Peluang hujan rendah"


def row_to_hour(row: Dict[str, Any], fallback_date: Optional[dt.date] = None, fallback_relative: str = "Hari ini") -> Dict[str, Any]:
    flags = []
    raw_moment = text(pick(row, "valid_time", "target_datetime", "datetime", "timestamp", "time", "local_time"))
    moment = parse_timestamp(raw_moment) if re.search(r"[T\s]\d{1,2}:\d{2}", raw_moment) else None
    hh = moment.strftime("%H:%M") if moment else hour(pick(row, "hour", "jam", "time", "local_time", "target_hour", "target_time"), "")
    d = moment.date() if moment else parse_date(pick(row, "date", "date_iso", "tanggal", "target_date", "valid_date", "forecast_date", "time")) or fallback_date
    if not hh:
        flags.append("missing_or_invalid_valid_hour")
    def bounded(value, lower, upper, field):
        number = num(value)
        if number is not None and not lower <= number <= upper:
            flags.append(field + "_out_of_range")
            return None
        return number
    temp = num(pick(row, "temp_c", "temp_p50", "temperature_c", "temperature_2m_c", "avg_temperature_c", "t2m", "suhu"))
    rh = num(pick(row, "humidity_pct", "rh_p50", "rh_pct", "relative_humidity", "relative_humidity_2m", "rh", "kelembapan"))
    heat = num(pick(row, "heat_index_c", "heat_index_p50", "apparent_temperature_c", "feels_like_c", "terasa"), temp)
    rain = prob(pick(row, "rain_probability", "prob_rain", "rain_probability_raw", "precip_probability", "precipitation_probability", "pop", "hujan"))
    wind = num(pick(row, "wind_kmh", "wind_p50", "wind_speed_kmh", "wind_speed_10m_kmh", "angin"))
    temp = bounded(temp, -80, 60, "temp_c")
    rh = bounded(rh, 0, 100, "humidity_pct")
    wind = bounded(wind, 0, 500, "wind_kmh")
    raw_rain = pick(row, "rain_probability", "prob_rain", "rain_probability_raw", "precip_probability", "precipitation_probability", "pop", "hujan")
    if num(raw_rain) is not None and rain is None:
        flags.append("rain_probability_out_of_range")
    heat = bounded(pick(row, "heat_index_c", "heat_index_p50", "apparent_temperature_c", "feels_like_c", "terasa"), -100, 100, "heat_index_c")
    direction = bounded(pick(row, "wind_direction_deg", "wind_direction", "wind_dir", "arah_angin"), 0, 360, "wind_dir")
    cloud = bounded(pick(row, "cloud_cover_pct", "cloud_cover", "cloud_p50", "cloud_pct", "awan"), 0, 100, "cloud_pct")
    if wind is None or wind == 0:
        direction = None
    elif direction is not None:
        direction %= 360
    base_score = bounded(pick(row, "risk_score", "rain_threat_score", "score", "risk", default=0), 0, 100, "risk_score") or 0
    score = max(base_score, rain or 0, heat_risk(heat, temp, rh), wind_risk(wind))
    valid = bool(hh) and all(x is not None for x in [temp, rain, wind]) and text(row.get("data_quality")).lower() not in {"unavailable", "invalid", "stale", "limited"}
    cls = text(pick(row, "risk_class", "risk_level", default="")).lower()
    cls = cls if cls in {"safe", "watch", "rain", "danger", "limited"} else risk_class(score, valid)
    if not valid or row.get("valid") is False:
        valid = False
        cls = "limited"
    cond = text(pick(row, "condition", "dominant_category", "weather", "cuaca", "summary", default=""))
    if not valid:
        cond = "Data terbatas"
    if not cond or cond.lower() in {"aman", "dipantau", "safe", "watch"}:
        cond = condition_label(hh, rain, temp, rh, heat, valid)
        w_val = num(wind, 0)
        if w_val is not None and w_val >= 50:
            cond = "Angin Kencang"
    return {
        "date_iso": d.isoformat() if d else "",
        "date_label": fmt_date(d) if d else "Tanggal belum terbaca",
        "date_short": fmt_date(d, False) if d else "—",
        "relative": text(pick(row, "relative_day", "day_tag", "hari", "day", default=fallback_relative), fallback_relative),
        "hour": hh, "temp_c": temp, "humidity_pct": rh, "heat_index_c": heat,
        "rain_probability": rain, "wind_kmh": wind, "risk_score": round(score) if valid else None,
        "risk_class": cls, "risk_label": risk_label(cls), "condition": cond, "valid": valid,
        "wind_dir": direction, "cloud_pct": cloud,
        "validation_flags": flags,
        "risk_score_basis": "activity_disruption_heuristic_not_hazard_probability",
        "rain_interval_start": row.get("rain_interval_start"),
        "rain_interval_end": row.get("rain_interval_end"),
        "rain_interval_minutes": num(row.get("rain_interval_minutes")),
        "rain_amount_p50_mm": num(pick(row, "rain_amount_p50_mm", "rain_p50")),
        "data_quality": row.get("data_quality"),
        "probability_basis": row.get("probability_basis"),
        "calibration_status": row.get("calibration_status"),
    }


def default_hours(base_date: dt.date, relative: str) -> List[Dict[str, Any]]:
    return [row_to_hour({"hour": h}, base_date, relative) for h in ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]]


def best_windows(hours: List[Dict[str, Any]]) -> List[str]:
    now = local_now()
    hours = [row for row in hours if not (
        row.get("date_iso") == now.date().isoformat() and hour(row.get("hour"), "") < now.strftime("%H:%M"))]
    good = sorted({hour_int(x["hour"]) for x in hours if x.get("valid") and x.get("risk_class") == "safe"})
    if not good:
        good = sorted({hour_int(x["hour"]) for x in hours if x.get("valid") and x.get("risk_class") in {"safe", "watch"}})
    if not good:
        return []
    blocks: List[Tuple[int, int]] = []
    a = b = good[0]
    for x in good[1:]:
        if x == b + 1:
            b = x
        else:
            blocks.append((a, b))
            a = b = x
    blocks.append((a, b))
    return [f"{a:02d}:00" if a == b else f"{a:02d}:00–{b:02d}:00" for a, b in blocks[:3]]


def period_name(hour_value: str) -> str:
    h = hour_int(hour_value)
    if 5 <= h <= 10: return "Pagi"
    if 11 <= h <= 14: return "Siang"
    if 15 <= h <= 18: return "Sore"
    return "Malam"


def period_summaries(hours: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for name in ["Pagi", "Siang", "Sore", "Malam"]:
        sub = [x for x in hours if period_name(x["hour"]) == name]
        valid = [x for x in sub if x.get("valid")]
        basis = valid or sub
        if not basis:
            out.append({"name": name, "hour": "—", "condition": "—", "temp_c": None, "rain_probability": None, "risk_class": "limited", "risk_label": "Terbatas"})
            continue
        worst = max(basis, key=lambda z: clamp(z.get("risk_score"), default=0))
        out.append({
            "name": name, "hour": worst.get("hour", "—"), "condition": worst.get("condition", "—"),
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
    score = None if cls == "limited" else clamp(worst.get("risk_score"), default=0)
    return {
        "relative": relative, "date_iso": date_value.isoformat(), "date_label": fmt_date(date_value),
        "date_short": fmt_date(date_value, False), "weekday": DAY_ID[date_value.weekday()],
        "hours": rows, "periods": period_summaries(rows),
        "peak_rain_probability": prob(peak.get("rain_probability"), None),
        "peak_rain_hour": text(peak.get("hour"), "—"),
        "risk_score": round(score) if score is not None else None, "risk_class": cls, "risk_label": risk_label(cls),
        "condition": text(worst.get("condition"), "Data terbatas" if cls == "limited" else "Berawan"),
        "avg_temp_c": mean(x.get("temp_c") for x in valid),
        "avg_rh": mean(x.get("humidity_pct") for x in valid),
        "max_heat_c": maximum(x.get("heat_index_c") for x in valid),
        "max_wind_kmh": maximum(x.get("wind_kmh") for x in valid),
        "safe_windows": best_windows(rows), "valid_points": len(valid),
    }


def rain_phrase(day: Dict[str, Any]) -> str:
    p = prob(day.get("peak_rain_probability"))
    h = text(day.get("peak_rain_hour"), "—")
    if p is None: return "Peluang hujan belum tersedia"
    return f"Peluang hujan tertinggi {pct(p)} sekitar {h} WIB"


def decision_sentence(location: str, day: Dict[str, Any], short: bool = False) -> str:
    c = day.get("risk_class", "watch")
    p = pct(day.get("peak_rain_probability"))
    peak = text(day.get("peak_rain_hour"), "—")
    win = ", ".join(day.get("safe_windows") or [])
    if c == "limited":
        return "Data belum cukup. Periksa kondisi setempat."
    if c == "danger":
        return "Potensi gangguan tinggi. Pertimbangkan aktivitas dalam ruang."
    if c == "rain":
        return "Siapkan rencana cadangan untuk aktivitas luar ruang."
    if c == "watch":
        return f"Pantau peluang hujan sekitar {peak} WIB."
    return "Cuaca mendukung aktivitas luar ruang." + (f" Pilihan waktu: {win} WIB." if win else "")


def short_activity_advice(day: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    c = day.get("risk_class", "watch")
    peak = text(day.get("peak_rain_hour"), "—")
    win = ", ".join(day.get("safe_windows") or ["cek langit"])
    heat = num(day.get("max_heat_c"), 0) or 0
    if c in {"danger", "rain"}:
        return [
            ("Berkendara", "Bawa jas hujan", f"Hindari berkendara sekitar pukul {peak}.", "rain"),
            ("Jalan kaki", "Bawa payung", f"Antisipasi tempat berteduh di sekitar pukul {peak}.", "rain"),
            ("Menjemur", "Periksa jam", "Pilih periode peluang hujan lebih rendah jika tersedia.", "watch"),
            ("Luar ruang", "Siapkan cadangan", "Gunakan opsi ruangan tertutup.", "rain"),
            ("Olahraga", "Sesuaikan waktu", f"Pilih jam alternatif: {win}.", "watch"),
            ("Fotografi", "Lindungi peralatan", "Lindungi peralatan elektronik dari kelembapan.", "watch"),
        ]
    if c == "watch":
        return [
            ("Berkendara", "Pantau kondisi", f"Tetap antisipasi potensi hujan sekitar pukul {peak}.", "watch"),
            ("Jalan kaki", "Periksa kondisi", f"Periode nyaman: {win}.", "safe"),
            ("Menjemur", "Pantau cuaca", "Pilih waktu dengan data lengkap dan peluang hujan lebih rendah.", "watch"),
            ("Luar ruang", "Kondusif", "Tetap pantau perkembangan awan.", "watch"),
            ("Olahraga", "Periksa waktu", f"Periode nyaman: {win}.", "watch" if heat >= 34 else "safe"),
            ("Fotografi", "Pantau Awan", f"Perhatikan perubahan intensitas cahaya sekitar pukul {peak}.", "watch"),
        ]
    if c == "limited":
        return [
            ("Berkendara", "Pantau Mandiri", "Data prakiraan belum lengkap.", "limited"),
            ("Jalan kaki", "Perhatikan Cuaca", "Lihat kondisi langit setempat secara berkala.", "limited"),
            ("Menjemur", "Pantau Berkala", "Sebaiknya tidak ditinggalkan dalam waktu lama.", "limited"),
            ("Luar ruang", "Fleksibel", "Siapkan opsi berteduh yang memadai.", "limited"),
            ("Olahraga", "Durasi Singkat", "Periksa kondisi cuaca langsung di lokasi.", "limited"),
            ("Fotografi", "Cek Kondisi", "Tunggu hingga data prakiraan diperbarui.", "limited"),
        ]
    return [
        ("Berkendara", "Mendukung", "Kondisi cuaca mendukung perjalanan luar ruang.", "safe"),
        ("Jalan kaki", "Mendukung", f"Pilihan waktu: {win}.", "safe"),
        ("Menjemur", "Periksa prakiraan", "Pilih jam dengan peluang hujan lebih rendah.", "safe"),
        ("Luar ruang", "Relatif mendukung", "Tetap pantau kondisi setempat.", "safe"),
        ("Olahraga", "Pagi / Sore", f"Periode nyaman: {win}.", "safe"),
        ("Fotografi", "Periksa cahaya", "Peluang hujan rendah belum memastikan langit cerah.", "safe"),
    ]


# ---------------------------------------------------------------------------
# Load existing generator outputs (ported from v63)
# ---------------------------------------------------------------------------

def safe_find_file(directory: Path, filename: str) -> Path:
    if not directory.exists():
        return directory / filename
    target = filename.lower()
    for p in directory.iterdir():
        if p.is_file() and p.name.lower() == target:
            return p
    return directory / filename


def metadata_by_slug(root: Path) -> Dict[str, Dict[str, Any]]:
    meta: Dict[str, Dict[str, Any]] = {}
    for name in ["dim_locations.csv", "locations.csv", "dim_location.csv"]:
        for row in read_csv(safe_find_file(root, name)):
            slug = text(pick(row, "slug", "location_slug", default="")) or slugify(text(pick(row, "location_name", "name", default="location")))
            meta.setdefault(slug, {}).update(row)
    gj = read_json(safe_find_file(root, "langit_all_locations.geojson"), {}) or {}
    for feat in gj.get("features", []) if isinstance(gj, dict) else []:
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        slug = text(props.get("slug") or props.get("location_slug") or slugify(props.get("location_name") or props.get("name") or ""))
        if slug:
            meta.setdefault(slug, {}).update({"slug": slug, "location_name": props.get("location_name") or props.get("name"), "longitude": coords[0] if len(coords) >= 1 else None, "latitude": coords[1] if len(coords) >= 2 else None})
    return meta


def location_dirs(root: Path) -> List[Path]:
    if not root.exists():
        return []
    out = []
    sentinel_files = ["anemos_app.html", "langit_hourly_intelligence.csv", "anemos_hourly_compact.csv", "langit_api_v1.json", "anemos_api_v1.json", "forecast.csv", "forecast_all_locations.csv"]
    for p in root.iterdir():
        if p.is_dir() and any(safe_find_file(p, s).exists() for s in sentinel_files):
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
    dated: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        d = parse_date(pick(r, "date", "tanggal", "target_date", "valid_date", "forecast_date", "datetime", "timestamp"))
        if d:
            dated.setdefault(d.isoformat(), []).append(r)
    if dated:
        return [dated[k] for k in sorted(dated.keys())[:3]]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        tag = text(pick(r, "relative_day", "day_tag", "hari", "day", default=""))
        if tag:
            groups.setdefault(tag.lower(), []).append(r)
    if groups and len(groups) > 1:
        order = ["hari ini", "today", "besok", "tomorrow", "lusa", "day 2"]
        ordered_keys = sorted(groups.keys(), key=lambda k: order.index(k) if k in order else 99)
        return [groups[k] for k in ordered_keys[:3]]
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
        candidate = read_json(safe_find_file(directory, name), {}, strict=True)
        if isinstance(candidate, dict) and candidate:
            raw_api = candidate
            break
    loc_name = text(raw_api.get("location_name"), text(meta.get("location_name"), directory.name.title()))
    slug = text(raw_api.get("location_slug"), text(meta.get("slug"), directory.name))
    lat = num(raw_api.get("latitude"), num(meta.get("latitude"), num(meta.get("lat"))))
    lon = num(raw_api.get("longitude"), num(meta.get("longitude"), num(meta.get("lon"))))
    if lat is None or lon is None:
        gj = read_json(safe_find_file(directory, "langit_location.geojson"), {}) or {}
        for feat in gj.get("features", []):
            if (feat.get("geometry") or {}).get("type") == "Point":
                coords = feat["geometry"].get("coordinates") or []
                if len(coords) == 2:
                    lon, lat = num(coords[0], lon), num(coords[1], lat)
                    break
    api_rows = rows_from_api(raw_api)
    api_dates = [parse_date(pick(r, "date", "date_iso", "target_date")) for r in api_rows]
    base_date = (parse_date(raw_api.get("target_date")) or
                 min((d for d in api_dates if d), default=None) or local_now().date())
    fallback_rows = []
    # Prefer structured API over compact CSV, which may omit missing values.
    if not api_rows:
        for name in ["langit_hourly_intelligence.csv", "anemos_hourly_compact.csv", "forecast.csv"]:
            fallback_rows = read_csv(safe_find_file(directory, name))
            if fallback_rows:
                break
    generated = raw_api.get("generated_at_iso") or raw_api.get("issued_at") or raw_api.get("generated_at") or raw_api.get("updated_at")
    days = []
    sources_by_date = {}
    previous_sources = raw_api.get("sources_by_date") or {}
    for i in range(3):
        date_value = base_date + dt.timedelta(days=i)
        stamp = date_value.strftime("%Y%m%d")
        payload = read_json(safe_find_file(directory, f"sentinel_x_{stamp}.json"), {}, strict=True) or {}
        rows = payload.get("hourly") or []
        issued = payload.get("issued_at") or (payload.get("daily") or {}).get("issued_at") or (payload.get("daily") or {}).get("generated_at")
        if not rows:
            rows = read_csv(safe_find_file(directory, f"sentinel_x_{stamp}.csv"))
        if not rows:
            rows = [r for r in api_rows or fallback_rows
                    if parse_date(pick(r, "date", "date_iso", "target_date", "datetime", "timestamp")) == date_value]
        # Index by date, never position: a missing middle day must stay missing.
        delta = (date_value - local_now().date()).days
        relative = {0: "Hari ini", 1: "Besok", 2: "Lusa", -1: "Kemarin"}.get(delta, fmt_date(date_value, False))
        parsed = {}
        for row in rows:
            item = row_to_hour(row, date_value, relative)
            if not item["hour"]:
                continue
            if item["date_iso"] == date_value.isoformat():
                item["relative"] = relative
                if item["hour"] in parsed:
                    raise ValueError(f"Duplicate forecast hour: {directory.name} {date_value} {item['hour']}")
                parsed[item["hour"]] = item
        day = summarize_day(relative, date_value, list(parsed.values()))
        day["rejected_time_records"] = len(rows) - len(parsed)
        day["issued_at"] = issued or generated
        day["time_basis"] = "forecast_issue_not_provider_model_initialization"
        sources_by_date[date_value.isoformat()] = (
            read_csv(directory / f"source_status_{stamp}.csv") or
            payload.get("source_states") or previous_sources.get(date_value.isoformat()) or [])
        ensemble = payload.get("ensemble_status") or {}
        day["ensemble_status"] = {key: ensemble.get(key) for key in
                                  ("status", "requested_model_count", "successful_model_count", "calibration_status")}
        days.append(day)
        if parse_timestamp(issued) and (not parse_timestamp(generated) or parse_timestamp(issued) > parse_timestamp(generated)):
            generated = issued
    sources = []
    for name in ["source_status.csv", "source_status_all_locations.csv", "langit_source_status.csv"]:
        sources = read_csv(safe_find_file(directory, name))
        if sources:
            break
    today = next((d for d in days if d["date_iso"] == local_now().date().isoformat()), days[0])
    selected_sources = sources_by_date.get(today["date_iso"]) or [
        row for row in sources
        if parse_date(pick(row, "tanggal", "target_date", "date")) == parse_date(today["date_iso"])]
    if not selected_sources and sources and not any(pick(row, "tanggal", "target_date", "date") for row in sources):
        selected_sources = sources  # Legacy fixtures without a date are explicitly unclassified.
    return {
        "brand": BRAND, "version": VERSION, "generated_at": fmt_update(generated),
        "generated_at_iso": generated if parse_timestamp(generated) else None,
        "location_name": loc_name, "location_slug": slug, "latitude": lat, "longitude": lon,
        "today": today, "days": days, "sources": selected_sources,
        "sources_by_date": sources_by_date, "source_status_date": today["date_iso"],
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scientific_status": "Probabilitas belum terkalibrasi; akurasi lokal harus dibuktikan dengan observasi.",
        "raw_version": raw_api.get("raw_version") or raw_api.get("version"),
    }


def write_public_tables(directory: Path, api: Dict[str, Any]) -> None:
    """The final public API is also the source of all three public UI tables."""
    def save(name, fields, rows):
        with (directory / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    hourly, daily, activities = [], [], []
    for day in api["days"]:
        common = {"date": day["date_iso"], "day_tag": day["relative"], "issued_at": day["issued_at"]}
        for row in day["hours"]:
            hourly.append({**row, **common})
        daily.append({**day, **common, "best_activity_window": ", ".join(day["safe_windows"]),
                      "summary": decision_sentence(api["location_name"], day)})
        for activity, status, advice, cls in short_activity_advice(day):
            activities.append({**common, "activity": activity, "status": status,
                               "advice": advice, "priority_hour": day["peak_rain_hour"], "risk_class": cls})
    save("langit_hourly_intelligence.csv",
         ["date", "day_tag", "hour", "condition", "temp_c", "humidity_pct", "heat_index_c",
          "rain_probability", "wind_kmh", "risk_score", "risk_label", "risk_class",
          "valid", "data_quality", "probability_basis", "calibration_status", "risk_score_basis",
          "issued_at", "rain_interval_start", "rain_interval_end", "rain_interval_minutes", "rain_amount_p50_mm"], hourly)
    save("langit_daily_outlook.csv", ["date", "day_tag", "risk_score", "risk_label",
         "peak_rain_probability", "peak_rain_hour", "best_activity_window", "summary", "issued_at"], daily)
    save("langit_activity_matrix.csv", ["date", "day_tag", "activity", "status", "advice",
         "priority_hour", "risk_class", "issued_at"], activities)


# ---------------------------------------------------------------------------
# Campus styles
# ---------------------------------------------------------------------------

CSS_V65 = (Path(__file__).parent / "site_tools" / "campus_ui.css").read_text(encoding="utf-8") + (Path(__file__).parent / "site_tools" / "editorial.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Browser interactions
# ---------------------------------------------------------------------------

DATA_JS = (Path(__file__).parent / 'site_tools' / 'public_data.js').read_text(encoding='utf-8')

JS_V65 = (Path(__file__).parent / "site_tools" / "campus_ui.js").read_text(encoding="utf-8")



# ---------------------------------------------------------------------------
# v65 HTML Components
# ---------------------------------------------------------------------------

FIELD_JS = (Path(__file__).parent / "site_tools" / "weather_field.js").read_text()

def asset_name(kind: str) -> str:
    source = {"css": CSS_V65, "js": JS_V65, "data.js": DATA_JS, "field.js": FIELD_JS}[kind]
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    return f"langit-{digest}.{kind}"


def inline_json(data: Any) -> str:
    """Keep provider text from terminating a script element."""
    return json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def v65_nav(api, active, root=False):
    return campus_ui.nav(sys.modules[__name__], api, active, root)


def v65_document(api, active, title, body, root=False):
    return campus_ui.document(sys.modules[__name__], api, active, title, body, root)


def v65_hero(api, heading, subtitle, day, show_metrics=True):
    return campus_ui.hero(sys.modules[__name__], api, heading, subtitle, day, show_metrics)


def v65_notice() -> str:
    return f'<div class="container"><div class="notice reveal">{esc(DISCLAIMER)} Indeks aktivitas bukan peringatan bahaya. Peluang hujan bukan intensitas.</div></div>'


def _accent_glow(cls: str) -> str:
    colors = {"safe": "rgba(16,185,129,0.08)", "watch": "rgba(245,158,11,0.08)", "rain": "rgba(249,115,22,0.08)", "danger": "rgba(239,68,68,0.08)", "limited": "rgba(99,102,241,0.08)"}
    return colors.get(cls, "rgba(6,182,212,0.06)")


def v65_decision(api: Dict[str, Any], day: Dict[str, Any]) -> str:
    cls = day.get("risk_class", "watch")
    loc = api["location_name"]
    windows = ", ".join(day.get("safe_windows") or ["cek kondisi lokal"])
    return f'''<section class="section-compact"><div class="container">
    <div class="decision reveal">
      <article class="decision-main" style="--accent-glow:{_accent_glow(cls)}">
        <div>
          <span class="status-badge status-{esc(cls)}">{esc(day.get("risk_label"))}</span>
          <h2 class="decision-title">{esc(decision_sentence(loc, day, short=False))}</h2>
        </div>
        <p class="decision-desc">{esc(day.get("date_label"))}. {esc(rain_phrase(day))}. Pilihan waktu: {esc(windows)}.</p>
      </article>
      <aside class="kpi-grid">
        <div class="kpi-card"><div class="kpi-label">Indeks aktivitas</div><div class="kpi-value" data-animate-value="{score_text(day.get("risk_score"))}" data-animate-suffix="/100">{score_text(day.get("risk_score"))}<span style="font-size:16px;color:var(--mist)">/100</span></div><div class="kpi-sub">{esc(day.get("risk_label"))}</div></div>
        <div class="kpi-card"><div class="kpi-label">Peluang hujan tertinggi</div><div class="kpi-value" data-animate-value="{prob(day.get("peak_rain_probability"), 0)}" data-animate-suffix="%">{pct(day.get("peak_rain_probability"))}</div><div class="kpi-sub">{esc(day.get("peak_rain_hour","—"))}</div></div>
        <div class="kpi-card"><div class="kpi-label">Pilihan waktu</div><div class="kpi-value" style="font-size:16px">{esc(windows)}</div><div class="kpi-sub">aktivitas</div></div>
        <div class="kpi-card"><div class="kpi-label">Terasa seperti</div><div class="kpi-value" data-animate-value="{num(day.get("max_heat_c"), 0)}" data-animate-suffix="°C" data-animate-decimals="1">{deg(day.get("max_heat_c"))}</div><div class="kpi-sub">maksimum</div></div>
      </aside>
    </div>
  </div></section>'''


def v65_timeline(hours: List[Dict[str, Any]], title: str = "Timeline prakiraan", note: str = "") -> str:
    import json
    fields = ("hour", "temp_c", "humidity_pct", "rain_probability", "wind_kmh", "condition")
    data_str = esc(json.dumps([{key: row.get(key) for key in fields} for row in hours],
                             ensure_ascii=False, separators=(",", ":")))
    return f'''<section class="section-compact"><div class="container">
    <div class="chart-wrapper reveal">
      <div class="section-header">
        <div class="section-overline">Timeline</div>
        <h2 class="section-title">{esc(title)}</h2>
        <p class="section-desc">{esc(note)}</p>
      </div>

      <!-- Multi-Variable Switches -->
      <div class="chart-selector">
        <button class="chart-chip active" aria-pressed="true" data-var="rain">Peluang hujan</button>
        <button class="chart-chip" aria-pressed="false" data-var="temp">Suhu</button>
        <button class="chart-chip" aria-pressed="false" data-var="humidity">Kelembapan</button>
        <button class="chart-chip" aria-pressed="false" data-var="wind">Angin</button>
      </div>

      <p class="chart-status" role="status"></p>
      <p class="section-desc">Garis terputus: data tidak tersedia. Angka lengkap ada di rincian per jam.</p>
      <div class="chart-svg-container" data-points="{data_str}">
        <svg class="rain-curve" viewBox="0 0 900 200" role="img" aria-label="Grafik prakiraan; detail angka tersedia pada tabel per jam" preserveAspectRatio="none"></svg>
        <div class="chart-tooltip">
          <div class="chart-tooltip-time"></div>
          <div class="chart-tooltip-value"></div>
        </div>
      </div>
    </div>
  </div></section>'''


def v65_periods(day: Dict[str, Any]) -> str:
    cards = []
    period_gradients = {"Pagi": "rgba(249,115,22,0.06)", "Siang": "rgba(6,182,212,0.06)", "Sore": "rgba(139,92,246,0.06)", "Malam": "rgba(99,102,241,0.04)"}
    for i, p in enumerate(day.get("periods", [])):
        cls = p.get("risk_class", "limited")
        bg = period_gradients.get(p.get("name", ""), "transparent")
        cards.append(f'''<div class="period-card reveal reveal-delay-{i+1}" style="--accent-color:{risk_color(cls)};background:linear-gradient(180deg,{bg},var(--glass))">
        <div class="period-name">{esc(p.get("name"))}</div>
        <div class="period-condition">{esc(p.get("condition"))}</div>
        <div class="period-stats">
          <div><div class="period-stat-value" data-animate-value="{num(p.get("temp_c"), 0)}" data-animate-suffix="°C" data-animate-decimals="1">{deg(p.get("temp_c"))}</div><div class="period-stat-label">Suhu</div></div>
          <div><div class="period-stat-value" data-animate-value="{prob(p.get("rain_probability"), 0)}" data-animate-suffix="%">{pct(p.get("rain_probability"))}</div><div class="period-stat-label">Hujan</div></div>
        </div>
      </div>''')
    return f'''<section class="section-compact"><div class="container">
    <div class="section-header reveal">
      <div class="section-overline">Periode</div>
      <h2 class="section-title">Pagi hingga malam</h2>
      <p class="section-desc">{esc(day.get("date_label"))}</p>
    </div>
    <div class="period-grid">{"".join(cards)}</div>
  </div></section>'''


def v65_activities(day: Dict[str, Any]) -> str:
    cards = []
    for i, (name, status, advice, cls) in enumerate(short_activity_advice(day)):
        cards.append(f'''<div class="activity-card reveal reveal-delay-{(i % 3) + 1}" style="--accent-color:{risk_color(cls)}">
        <div class="activity-name">{esc(name)}</div>
        <div class="activity-status">{esc(status)}</div>
        <div class="activity-advice">{esc(advice)}</div>
      </div>''')
    return f'''<section class="section-compact"><div class="container">
    <div class="section-header reveal">
      <div class="section-overline">Aktivitas</div>
      <h2 class="section-title">Saran aktivitas</h2>

    </div>
    <div class="activity-grid">{"".join(cards)}</div>
  </div></section>'''


def v65_hours(day: Dict[str, Any]) -> str:
    rows = []
    for x in day.get("hours", []):
        cls = x.get("risk_class", "limited")
        rows.append(f'''<div class="hour-row" style="--accent-color:{risk_color(cls)}">
        <div class="hour-time">{esc(x.get("hour"))}</div>
        <div><div class="hour-condition">{esc(x.get("condition"))}</div><div class="hour-status">{esc(x.get("risk_label"))}</div></div>
        <div class="hour-box"><div class="hour-box-value">{deg(x.get("temp_c"))}</div><div class="hour-box-label">Suhu</div></div>
        <div class="hour-box"><div class="hour-box-value">{pct(x.get("humidity_pct"))}</div><div class="hour-box-label">Kelembapan</div></div>
        <div class="hour-box"><div class="hour-box-value">{deg(x.get("heat_index_c"))}</div><div class="hour-box-label">Indeks panas</div></div>
        <div class="hour-box hour-box-rain"><div class="hour-box-value">{pct(x.get("rain_probability"))}</div><div class="hour-box-label">Hujan</div></div>
      </div>''')

    rows_html = "".join(rows)
    return f'''<section class="section-compact"><div class="container">
    <details class="hourly-section reveal">
      <summary class="hourly-toggle">
        <span>Detail per jam · {esc(day.get("date_label"))}</span>
        <svg class="hourly-chevron" width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M5 8l5 5 5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
      </summary>
      <div class="hourly-list">{rows_html}</div>
    </details>

    <!-- Mobile Bottom Sheet (Sliding Panel fallback) -->
    <dialog class="mobile-bottom-sheet" aria-label="Detail prakiraan per jam">
      <button type="button" class="sheet-close">Tutup detail per jam</button>
      <h3 style="font-family:var(--font-display);font-weight:800;font-size:18px;margin-bottom:16px;text-align:center;color:var(--white)">Prakiraan per jam</h3>
      <div class="hourly-list">{rows_html}</div>
    </dialog>
  </div></section>'''


def v65_command_center(api: Dict[str, Any]) -> str:
    return f'''<section class="section-compact"><div class="container">
    <div class="section-header reveal">
      <div class="section-overline">Peta Cuaca</div>
      <h2 class="section-title">Peta cuaca</h2>
      <p class="section-desc">Lapisan GFS untuk wilayah Bandung–Cirebon.</p>
    </div>

    <div class="command-center reveal">
      <!-- Left: Map Frame wrapper -->
      <div class="map-wrapper">
        <iframe class="map-frame" title="Peta titik prakiraan cuaca LANGIT" src="langit_map_room.html" loading="lazy"></iframe>
      </div>

      <!-- Right: Controls & Diagnostics -->
      <div class="command-sidebar">
        <div class="glass glass-static">
          <div class="section-overline">Lapisan peta</div>
          <h3 style="font-size:18px;font-weight:800;margin:8px 0 16px;font-family:var(--font-display);">Lapisan peta</h3>

          <div class="layer-controls">
            <button class="layer-btn active" data-layer="rain" style="--active-bg:rgba(6,182,212,0.12); --active-color:var(--noon); --active-border:rgba(6,182,212,0.3);">Hujan</button>
            <button class="layer-btn" data-layer="temp" style="--active-bg:rgba(249,115,22,0.12); --active-color:var(--dawn); --active-border:rgba(249,115,22,0.3);">Suhu</button>
            <button class="layer-btn" data-layer="wind" style="--active-bg:rgba(16,185,129,0.12); --active-color:var(--safe); --active-border:rgba(16,185,129,0.3);">Angin</button>
            <button class="layer-btn" data-layer="cloud" style="--active-bg:rgba(148,190,235,0.12); --active-color:var(--cloud); --active-border:rgba(148,190,235,0.3);">Awan</button>
            <button class="layer-btn" data-layer="humidity" style="--active-bg:rgba(99,102,241,0.12); --active-color:var(--limited); --active-border:rgba(99,102,241,0.3);">Kelembapan</button>
          </div>
          <p style="font-size:12px;color:var(--mist);line-height:1.6;margin-top:12px;">Lapisan memakai GFS. Angka lokasi memakai prakiraan gabungan.</p>
        </div>

        <div class="glass glass-static">
          <div class="section-overline">Data</div>
          <h3 style="font-size:18px;font-weight:800;margin:8px 0 12px;font-family:var(--font-display);">Status sumber</h3>
          <p style="font-size:13px;color:var(--cloud);line-height:1.6;margin-bottom:16px;">Periksa sumber dan hasil evaluasi.</p>
          <div style="display:flex;gap:12px;">
            <a class="btn btn-primary" href="keandalan_data.html">Status sumber</a>
            <a class="btn" href="akurasi_data.html">Akurasi</a>
          </div>
        </div>
      </div>
    </div>
  </div></section>'''


def v65_map_embed(href: str = "langit_map_room.html") -> str:
    return f'''<section class="section-compact"><div class="container">
    <div class="section-header reveal">
      <div class="section-overline">Peta</div>
      <h2 class="section-title">Peta cuaca</h2>
      <p class="section-desc">Pilih lapisan dan waktu prakiraan.</p>
    </div>
    <div class="map-wrapper reveal">
      <iframe class="map-frame" title="Peta titik prakiraan cuaca LANGIT" src="{esc(href)}" loading="lazy"></iframe>
      <div class="map-actions">
        <a class="btn btn-primary" href="{esc(href)}">Buka peta penuh</a>
        <a class="btn" href="langit_location.geojson">GeoJSON</a>
      </div>
    </div>
  </div></section>'''


def v65_share(api, day):
    return campus_ui.share(sys.modules[__name__], api, day)


def v65_day_cards(days: List[Dict[str, Any]]) -> str:
    cards = []
    for i, d in enumerate(days):
        cls = d.get("risk_class", "limited")
        cards.append(f'''<div class="day-overview-card reveal reveal-delay-{i+1}">
        <span class="status-badge status-{esc(cls)}" style="margin-bottom:12px">{esc(d.get("relative"))}</span>
        <h3 style="font-size:24px;font-weight:800;margin:12px 0 8px">{esc(d.get("risk_label"))}</h3>
        <p style="color:var(--cloud);font-size:14px;line-height:1.5;margin-bottom:16px">{esc(d.get("date_label"))}. {esc(decision_sentence("", d, short=True))}</p>
        <div class="location-stats">
          <div class="location-stat"><div class="location-stat-value">{pct(d.get("peak_rain_probability"))}</div><div class="location-stat-label">Peluang hujan</div></div>
          <div class="location-stat"><div class="location-stat-value">{esc(d.get("peak_rain_hour"))}</div><div class="location-stat-label">Waktu</div></div>
          <div class="location-stat"><div class="location-stat-value">{score_text(d.get("risk_score"))}</div><div class="location-stat-label">Indeks aktivitas</div></div>
        </div>
      </div>''')
    return f'''<section class="section"><div class="container">
    <div class="section-header reveal">
      <div class="section-overline">Prakiraan</div>
      <h2 class="section-title">Prakiraan 3 hari ke depan</h2>
    </div>
    <div class="day-card-grid">{"".join(cards)}</div>
  </div></section>'''


# ---------------------------------------------------------------------------
# v65 Map pages (enhanced)
# ---------------------------------------------------------------------------

def v65_geo_for_api(api: Dict[str, Any]) -> Dict[str, Any]:
    lat = num(api.get("latitude"))
    lon = num(api.get("longitude"))
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return {"type": "FeatureCollection", "features": []}
    features = []
    for day in api.get("days", [])[:3]:
        for h in day.get("hours", []):
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "location_name": api.get("location_name"), "slug": api.get("location_slug"),
                    "date": day.get("date_label"), "date_iso": day.get("date_iso"),
                    "relative": day.get("relative"), "hour": h.get("hour"),
                    "rain_probability": h.get("rain_probability"), "risk_score": h.get("risk_score"),
                    "risk_class": h.get("risk_class"), "risk_label": h.get("risk_label"),
                    "condition": h.get("condition"), "temp_c": h.get("temp_c"),
                    "humidity_pct": h.get("humidity_pct"), "heat_index_c": h.get("heat_index_c"),
                    "wind_kmh": num(h.get("wind_kmh")),
                    "wind_dir": num(h.get("wind_dir")),
                    "cloud_pct": num(h.get("cloud_pct")),
                    "issued_at": day.get("issued_at"),
                    "data_quality": h.get("data_quality"),
                    "probability_basis": h.get("probability_basis"),
                    "calibration_status": h.get("calibration_status"),
                    "risk_score_basis": h.get("risk_score_basis"),
                },
            })
    return {"type": "FeatureCollection", "features": features}
def v65_map_page(title: str, geojson: Dict[str, Any], back_href: str) -> str:
    fields = {"location_name", "slug", "date", "date_iso", "hour", "rain_probability",
              "risk_score", "risk_class", "risk_label", "condition", "temp_c",
              "humidity_pct", "wind_kmh", "wind_dir", "cloud_pct", "issued_at"}
    map_data = {**geojson, "features": [
        {**feature, "properties": {key: value for key, value in feature["properties"].items() if key in fields}}
        for feature in geojson.get("features", [])]}
    data = inline_json(map_data)
    asset_prefix = "assets/" if back_href == "index.html" else "../assets/"
    css = r'''*,*::before,*::after{box-sizing:border-box}
html,body,#map{height:100%;margin:0;background:#030712;color:#f8fbff;font-family:'Inter',system-ui,-apple-system,sans-serif;overflow:hidden}
a:focus-visible,button:focus-visible,select:focus-visible{outline:2px solid #67e8f9;outline-offset:3px}
#map-location{width:100%;padding:8px;background:#0b1932;color:#fff;border:1px solid #94a3b8;border-radius:8px}
.map-fallback{position:absolute;left:12px;bottom:96px;z-index:1100;max-width:calc(100% - 48px);padding:12px;background:#15294a;color:#fff;border-radius:12px;font-size:12px}
.map-fallback[hidden]{display:none}
.map-fallback a{text-decoration:underline}
.leaflet-bottom.leaflet-left{bottom:96px}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
#particle-canvas{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:800}
.hud{max-height:calc(100% - 152px);overflow-y:auto;position:absolute;z-index:1000;left:24px;top:24px;width:min(340px,calc(100% - 48px));padding:24px;border-radius:24px;background:rgba(7,14,30,0.75);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,0.08);box-shadow:0 24px 80px rgba(0,0,0,0.5),inset 0 1px 1px rgba(255,255,255,0.1);animation:slideIn 0.6s cubic-bezier(0.16,1,0.3,1) both}
.hud-brand{font-family:'Outfit',sans-serif;font-weight:800;font-size:10px;letter-spacing:0.1em;color:#32b7ff;text-transform:uppercase}
.hud-version{font-family:'Outfit',sans-serif;font-weight:600;font-size:10px;color:rgba(255,255,255,0.3)}
.hud h1{font-family:'Outfit',sans-serif;font-size:24px;font-weight:800;letter-spacing:-0.02em;line-height:1.2;margin:12px 0 4px;color:#fff}
.hud-meta{color:#94a3b8;font-size:13px;font-weight:500}
.hud-divider{height:1px;background:linear-gradient(90deg,rgba(255,255,255,0.08) 0%,rgba(255,255,255,0) 100%);margin:16px 0}
.hud-stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.hud-stat-box{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);padding:10px 14px;border-radius:14px;display:flex;flex-direction:column;gap:2px}
.hud-stat-val{font-family:'Outfit',sans-serif;font-size:18px;font-weight:800;color:#fff}
.hud-stat-lbl{font-size:10px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em}
.parameter-tabs{display:flex;gap:4px;margin-top:16px;background:rgba(3,7,18,0.6);padding:3px;border-radius:12px;border:1px solid rgba(255,255,255,0.05)}
.param-tab{flex:1;border:none;background:none;color:#94a3b8;font-family:inherit;font-size:10px;font-weight:700;padding:8px 0;border-radius:9px;cursor:pointer;transition:all 0.2s ease;text-align:center}
.param-tab:hover{color:#fff;background:rgba(255,255,255,0.04)}
.param-tab.active{background:rgba(255,255,255,0.08);color:#fff;box-shadow:0 2px 8px rgba(0,0,0,0.2)}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:12px 24px;border-radius:999px;background:rgba(50,183,255,0.12);border:1px solid rgba(50,183,255,0.25);color:#32b7ff;text-decoration:none;font-weight:700;font-size:13px;transition:all 0.3s cubic-bezier(0.4,0,0.2,1);cursor:pointer;font-family:inherit}
.btn:hover{background:rgba(50,183,255,0.2);border-color:rgba(50,183,255,0.4);transform:translateY(-1px);box-shadow:0 4px 12px rgba(50,183,255,0.15)}
.timeline-container{position:absolute;z-index:1000;left:50%;bottom:24px;transform:translateX(-50%);width:min(800px,calc(100% - 48px));padding:12px 20px;border-radius:24px;background:rgba(7,14,30,0.75);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,0.08);box-shadow:0 24px 80px rgba(0,0,0,0.5);display:flex;align-items:center;gap:16px;animation:slideUp 0.6s cubic-bezier(0.16,1,0.3,1) both}
.play-btn{background:rgba(50,183,255,0.12);border:1px solid rgba(50,183,255,0.25);color:#32b7ff;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all 0.3s ease;flex-shrink:0;font-size:14px}
.play-btn:hover{background:rgba(50,183,255,0.2);transform:scale(1.05)}
.time-scrubber{flex:1;display:flex;overflow-x:auto;gap:8px;padding:4px 0;scrollbar-width:none}
.time-scrubber::-webkit-scrollbar{display:none}
.time-pill{flex-shrink:0;padding:8px 16px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);border-radius:14px;color:#8fa0dd;font-family:inherit;font-size:13px;font-weight:700;cursor:pointer;transition:all 0.2s ease;display:flex;flex-direction:column;align-items:center;gap:2px}
.time-pill:hover{background:rgba(255,255,255,0.06)}
.time-pill span.day-label{font-size:9px;color:#94a3b8;text-transform:uppercase;font-weight:600}
.time-pill.active{background:linear-gradient(135deg,rgba(50,183,255,0.2) 0%,rgba(50,183,255,0.05) 100%);border-color:rgba(50,183,255,0.4);color:#32b7ff;box-shadow:0 4px 12px rgba(50,183,255,0.15)}
.legend{max-height:28vh;overflow-y:auto;position:absolute;right:24px;bottom:110px;z-index:1000;background:rgba(7,14,30,0.75);border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:16px;font-size:11px;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);box-shadow:0 12px 40px rgba(0,0,0,0.4);width:150px;animation:slideIn 0.6s cubic-bezier(0.16,1,0.3,1) both}
.legend-title{font-family:'Outfit',sans-serif;font-weight:800;color:#fff;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.05em;font-size:9px}
.legend div{display:flex;gap:10px;align-items:center;margin:8px 0;color:#a8c4e0;font-weight:500}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.leaflet-control-attribution{background:rgba(3,7,12,0.85)!important;color:#94a3b8!important;font-size:10px!important}
.leaflet-popup-content-wrapper{background:rgba(7,14,30,0.92)!important;color:#f8fbff!important;border-radius:20px!important;border:1px solid rgba(255,255,255,0.08)!important;backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);box-shadow:0 20px 50px rgba(0,0,0,0.6)!important}
.leaflet-popup-tip{background:rgba(7,14,30,0.92)!important;border-left:1px solid rgba(255,255,255,0.08)!important;border-bottom:1px solid rgba(255,255,255,0.08)!important}
.leaflet-popup-content{margin:16px 20px!important;width:200px!important}
.popup-badge{padding:3px 8px;border-radius:999px;font-family:'Outfit',sans-serif;font-size:10px;font-weight:700;text-transform:uppercase}
.custom-leaflet-marker{background:none;border:none;display:flex;align-items:center;justify-content:center}
.pulse-marker{position:relative;display:flex;align-items:center;justify-content:center;width:var(--pulse-size);height:var(--pulse-size)}
.pulse-dot{background:var(--marker-color);border-radius:50%;box-shadow:0 0 12px var(--marker-color);border:1.5px solid #fff;z-index:2}
.pulse-ring{position:absolute;width:100%;height:100%;border:2px solid var(--marker-color);border-radius:50%;animation:pulse-ring-animation 2s cubic-bezier(0.215,0.61,0.355,1) infinite;opacity:0;z-index:1}
@keyframes pulse-ring-animation{0%{transform:scale(0.3);opacity:0}50%{opacity:0.5}100%{transform:scale(1);opacity:0}}
.temp-badge{color:#fff;font-family:'Outfit',sans-serif;font-size:11px;font-weight:800;padding:4px 8px;border-radius:12px;border:1px solid rgba(255,255,255,0.2);display:flex;align-items:center;justify-content:center;white-space:nowrap}
.radar-marker{position:relative;width:60px;height:60px;display:flex;align-items:center;justify-content:center}
.radar-core{width:8px;height:8px;background:var(--radar-color);border-radius:50%;box-shadow:0 0 10px var(--radar-color);z-index:2;border:1px solid #fff}
.radar-ping{position:absolute;width:100%;height:100%;border:1.5px solid var(--radar-color);border-radius:50%;animation:radar-ping-animation 2s linear infinite;opacity:0}
@keyframes radar-ping-animation{0%{transform:scale(0.1);opacity:0.8}100%{transform:scale(1);opacity:0}}
.humidity-halo{border-radius:50%;position:relative;display:flex;align-items:center;justify-content:center}
.cloud-glow{border-radius:50%;position:relative;display:flex;align-items:center;justify-content:center}
@keyframes slideIn{from{transform:translateX(-20px);opacity:0}to{transform:translateX(0);opacity:1}}
@keyframes slideUp{from{transform:translate(-50%,20px);opacity:0}to{transform:translate(-50%,0);opacity:1}}
@media(max-width:768px){
  .hud{left:12px;top:12px;width:calc(100% - 24px);padding:16px;border-radius:20px}
  .timeline-container{bottom:16px;width:calc(100% - 24px);padding:10px;border-radius:20px}
  .legend{right:12px;bottom:96px;width:120px;padding:12px;border-radius:16px}
}
'''
    css += (Path(__file__).parent / 'site_tools' / 'campus_map.css').read_text(encoding='utf-8')
    js = (Path(__file__).parent / "site_tools" / "forecast_map.js").read_text().replace("__DATA__", data)

    hud_html = f'''  <section class="hud">
    <div style="display:flex; justify-content:space-between; align-items:center">
      <span class="hud-brand">{BRAND} Peta</span>
      <button type="button" id="map-panel-toggle" class="map-panel-toggle" aria-expanded="true" aria-controls="map-controls">Panel</button>
      <a class="hud-back-link" id="hud-back-btn" href="{esc(back_href)}">← Kembali</a>
    </div>
    <div id="map-controls">
    <label for="map-location" class="hud-meta">Lokasi prakiraan</label>
    <select id="map-location" aria-label="Lokasi prakiraan"></select>
    <h1 id="hud-location-name">Memuat...</h1>
    <div class="hud-meta" id="hud-time-label">Memuat data prakiraan cuaca.</div>
    <div class="hud-meta" id="hud-issue"></div>
    <p class="hud-meta">Prakiraan lokasi: gabungan sumber.</p>

    <div class="hud-divider"></div>

    <details class="point-details" open><summary>Cuaca di lokasi</summary><div class="hud-stats-grid">
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-temp">—</span>
        <span class="hud-stat-lbl">Suhu</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-rain">—</span>
        <span class="hud-stat-lbl">Peluang hujan</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-wind">—</span>
        <span class="hud-stat-lbl">Angin</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-humidity">—</span>
        <span class="hud-stat-lbl">Kelembapan</span>
      </div>
    </div></details>

    <div class="hud-divider"></div>

    <div class="parameter-tabs">
      <button class="param-tab" aria-pressed="false" data-layer="risiko" onclick="switchLayer('risiko')">Aktivitas</button>
      <button class="param-tab" aria-pressed="false" data-layer="temp" onclick="switchLayer('temp')">Suhu</button>
      <button class="param-tab" aria-pressed="false" data-layer="rain" onclick="switchLayer('rain')">Hujan</button>
      <button class="param-tab active" aria-pressed="true" data-layer="wind" onclick="switchLayer('wind')">Angin</button>
      <button class="param-tab" aria-pressed="false" data-layer="humidity" onclick="switchLayer('humidity')">Lembap</button>
      <button class="param-tab" aria-pressed="false" data-layer="cloud" onclick="switchLayer('cloud')">Awan</button>
    </div>

    <div class="field-tools"><p id="field-status" class="hud-meta" data-state="loading">Memuat grid GFS…</p><button type="button" id="wind-animation" class="wind-animation" aria-pressed="false" disabled>Animasi angin</button><p class="field-hint">Arah dan kecepatan relatif angin 10 m.</p></div>
    <details class="map-legend-details">
      <summary>Arti warna peta</summary>
      <div class="legend" id="map-legend"></div>
    </details>

    <p class="field-credit">NOAA GFS via <a href="https://open-meteo.com/">Open-Meteo</a> · <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a> · <a href="{'weather_field_v1.json' if back_href == 'index.html' else '../weather_field_v1.json'}">Data grid</a></p>
    </div>
  </section>'''

    timeline_html = '''  <div class="timeline-container">
    <button class="play-btn" id="play-btn" aria-label="Putar timeline" aria-pressed="false" onclick="togglePlay()">&#9654;</button>
    <div class="timeline-track"><input id="forecast-range" type="range" min="0" max="0" value="0" aria-label="Waktu prakiraan"><div class="time-scrubber" id="time-scrubber"></div></div>
  </div>'''

    legend_html = ''

    return f'''<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{esc(title)}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>{css}</style>
</head>
<body>
  <div id="map"></div>

  {hud_html}
  {timeline_html}
  {legend_html}
  <p class="field-inspector" id="field-inspector" role="status">Klik peta untuk nilai grid GFS.</p>
  <small class="map-author">Gias Sabik</small>
  <div id="map-fallback" class="map-fallback" role="alert" hidden><span>Peta interaktif belum tersedia. Data prakiraan tetap dapat dibaca.</span> <a href="{esc(back_href)}">Buka prakiraan</a> · <a href="{'langit_all_locations.geojson' if back_href == 'index.html' else 'langit_location.geojson'}">Data GeoJSON</a></div>
  <noscript><p class="map-fallback">Aktifkan JavaScript untuk peta, atau <a href="{esc(back_href)}">buka prakiraan tertulis</a>.</p></noscript>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="{asset_prefix}{asset_name('data.js')}"></script>
  <script src="{asset_prefix}{asset_name('field.js')}"></script>
  <script>window.LANGIT_FIELD_URL={'"weather_field_v1.json"' if back_href == 'index.html' else '"../weather_field_v1.json"'};</script>
  <script>{js}</script>
</body>
</html>'''
def v65_today_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    heading = f"Prakiraan\n{api['location_name']}"
    sub = f"{day['date_label']}. {decision_sentence(api['location_name'], day, short=True)}"
    body = v65_hero(api, heading, sub, day)
    body += v65_notice()

    active_index = next((i for i, item in enumerate(api["days"]) if item["date_iso"] == day["date_iso"]), 0)
    tabs = "".join(
        f'<button class="timeline-node {"active" if i == active_index else ""}" aria-pressed="{str(i == active_index).lower()}" data-target="day-{i}">'
        f'<div class="timeline-node-dot"></div><span class="timeline-node-label">{esc(item["relative"])}</span>'
        f'<small>{esc(item["date_short"])}</small></button>' for i, item in enumerate(api["days"]))
    body += f'''<section class="section-compact"><div class="container"><div class="day-tabs-container reveal">
      <div class="timeline-progress-container"><div class="timeline-progress-bar"></div><div class="timeline-progress-fill"></div>{tabs}</div>
    </div></div></section>'''

    # Panels containing details for each day
    body += '<div class="day-panels">'
    for i, d in enumerate(api["days"]):
        active_class = "active" if i == active_index else ""
        panel_body = v65_decision(api, d)
        panel_body += v65_timeline(d["hours"], f"Timeline {d['relative'].lower()}", d["date_label"])
        panel_body += v65_periods(d)
        panel_body += v65_activities(d)
        panel_body += v65_hours(d)
        body += f'<div id="day-{i}" class="day-panel {active_class}">{panel_body}</div>'
    body += '</div>'

    # Tactic Weather Command Center Console
    body += v65_command_center(api)

    body += '<div class="divider"></div>'
    body += v65_share(api, day)
    return v65_document(api, "today", f"LANGIT — {api['location_name']}", body)


def v65_three_day_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    sub = f"Mulai {api['days'][0]['date_label']} sampai {api['days'][-1]['date_label']}."
    body = v65_hero(api, "Prakiraan\n3 hari", sub, day, show_metrics=False)
    body += v65_notice()
    body += v65_day_cards(api["days"])
    for d in api["days"]:
        body += v65_timeline(d["hours"], f'{d["relative"]} · {d["date_label"]}', d.get("risk_label", ""))
    for d in api["days"]:
        body += v65_hours(d)
    return v65_document(api, "3day", f"LANGIT 3 hari — {api['location_name']}", body)


def v65_activity_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    body = v65_hero(api, "Saran\naktivitas", f"{day['date_label']}. Panduan berdasarkan prakiraan hari ini.", day)
    body += v65_notice()
    body += v65_decision(api, day)
    body += v65_activities(day)
    body += v65_timeline(day["hours"], "Jam yang perlu dipantau", "Lihat rincian per jam untuk angka lengkap.")
    body += v65_periods(day)
    body += v65_hours(day)
    body += '<div class="divider"></div>'
    body += v65_share(api, day)
    return v65_document(api, "activity", f"LANGIT Aktivitas — {api['location_name']}", body)


def v65_planner_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    rows = day.get("hours") or []
    options = "".join(f'<option value="{esc(x.get("hour"))}">{esc(x.get("hour"))} · {esc(x.get("condition"))} · hujan {pct(x.get("rain_probability"))}</option>' for x in rows)
    data = inline_json(day)
    body = v65_hero(api, "Rencana\naktivitas", f"{day['date_label']}. Periksa prakiraan sebelum beraktivitas.", day, show_metrics=False)
    body += v65_notice()
    body += f'''<section class="section-compact"><div class="container">
    <div class="glass glass-static reveal">
      <div class="section-header">
        <div class="section-overline">Planner</div>
        <h2 style="font-size:24px;font-weight:800">Cek jam terbaik</h2>
      </div>
      <div class="planner-controls">
        <select id="act" aria-label="Aktivitas" class="btn" style="background:var(--glass);text-align:left;min-height:48px">
          <option>Motor</option><option>Jalan kaki</option><option>Jemur</option><option>Outdoor</option><option>Olahraga</option><option>Foto</option>
        </select>
        <select id="hh" aria-label="Jam prakiraan dalam WIB" class="btn" style="background:var(--glass);text-align:left;min-height:48px">{options}</select>
        <button class="btn btn-primary" type="button" onclick="decidePlanner()" style="min-height:48px">Cek</button>
      </div>
      <div class="decision-main" style="min-height:140px;margin:0;--accent-glow:rgba(50,183,255,0.06)">
        <div>
          <span id="plannerBadge" class="status-badge status-watch">Pilih jam</span>
          <h2 id="plannerOut" role="status" aria-live="polite" class="decision-title" style="font-size:clamp(24px,4vw,40px)">Cek sebelum berangkat.</h2>
        </div>
        <p id="plannerWhy" class="decision-desc">Pilih aktivitas dan jam. Sistem akan pakai peluang hujan, risiko, dan panas terasa.</p>
      </div>
    </div>
  </div></section>
<script>
const plannerDay={data};
function plannerRiskClass(x){{return x==='danger'?'Tinggi':x==='rain'?'Waspada':x==='watch'?'Pantau':x==='safe'?'Gangguan lebih rendah':'Terbatas';}}
function decidePlanner(){{
  const act=document.getElementById('act').value;
  const val=document.getElementById('hh').value;
  const h=(plannerDay.hours||[]).find(x=>x.hour===val)||{{}};
  if (!h.valid || h.rain_probability == null || h.risk_class === 'limited'){{
    document.getElementById('plannerOut').textContent='Data belum cukup untuk rekomendasi.';
    document.getElementById('plannerWhy').textContent='Periksa pembaruan prakiraan dan kondisi setempat.';
    document.getElementById('plannerBadge').className='status-badge status-limited';
    document.getElementById('plannerBadge').textContent='Data terbatas';
    return;
  }}
  const p=Math.round(Number(h.rain_probability));
  const r=Math.round(Number(h.risk_score||0));
  const hi=Number(h.heat_index_c||h.temp_c||0);
  let msg='Bisa', cls='safe';
  let why='Tetap lihat kondisi sekitar.';
  if(r>=78 || p>=70){{msg='Tunda'; cls='danger'; why='Potensi gangguan aktivitas tinggi pada jam ini; bukan peringatan cuaca ekstrem.';}}
  else if(r>=55 || p>=45){{msg='Siapkan plan B'; cls='rain'; why='Awan/hujan perlu diwaspadai.';}}
  else if(r>=25 || p>=25){{msg='Masih bisa, pantau'; cls='watch'; why='Masih aman bersyarat.';}}
  if((act==='Jemur'||act==='Foto') && p>=25){{msg='Pilih jam lain'; cls='watch'; why='Hujan/awan bisa mengganggu.';}}
  if((act==='Olahraga'||act==='Jalan kaki') && hi>=36){{msg='Cari jam lebih teduh'; cls='watch'; why='Panas terasa tinggi.';}}
  const badge=document.getElementById('plannerBadge');
  badge.className='status-badge status-'+cls;
  badge.textContent=plannerRiskClass(cls);
  document.getElementById('plannerOut').textContent=act+' '+val+': '+msg;
  document.getElementById('plannerWhy').textContent=why+' Hujan '+p+'%, indeks aktivitas '+r+'/100, kondisi '+(h.condition||'-')+'.';
}}
</script>'''
    body += v65_timeline(day["hours"], "Timeline hari ini", day["date_label"])
    body += v65_activities(day)
    return v65_document(api, "activity", f"LANGIT Planner — {api['location_name']}", body)


def v65_data_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    sources = api.get("sources") or []
    active = sum(1 for row in sources if text(pick(row, "success", "active", "is_active", "used", "ok", default=""), "").lower() in {"yes", "true", "1", "aktif", "ok"})
    total = len(sources)
    pct_val = round(active / total * 100) if total else 0
    rows_html = "".join(
        f'<tr><td>{esc(pick(r,"source_id","model","source","name",default="—"))}</td><td>{esc(pick(r,"provider","origin",default="—"))}</td><td>{esc(pick(r,"success","active","is_active","used","ok",default="—"))}</td><td>{esc(pick(r,"effective_base_weight","weight","score","quality",default="—"))}</td></tr>'
        for r in sources
    ) or '<tr><td colspan="4" style="color:var(--mist)">Belum ada tabel sumber.</td></tr>'

    body = v65_hero(api, "Keandalan\ndata", f"{day['date_label']}. Ketersediaan sumber pada tanggal terpilih.", day, show_metrics=False)
    body += v65_notice()
    body += f'''<section class="section-compact"><div class="container">
    <div class="glass glass-static reveal">
      <div class="section-header">
        <div class="section-overline">Sumber</div>
        <h2 style="font-size:24px;font-weight:800">{active}/{total} sumber aktif</h2>
      </div>
      <div class="trust-bar"><div class="trust-fill" data-width="{pct_val}%" style="width:0"></div></div>
    </div>
  </div></section>'''
    body += f'''<section class="section-compact"><div class="container">
    <div class="glass glass-static reveal" style="overflow:auto">
      <table class="source-table"><thead><tr><th>Model</th><th>Sumber</th><th>Aktif</th><th>Bobot</th></tr></thead><tbody>{rows_html}</tbody></table>
    </div>
  </div></section>'''
    body += v65_map_embed()
    return v65_document(api, "data", f"LANGIT Data — {api['location_name']}", body)


def v65_accuracy_page(api: Dict[str, Any], directory: Path) -> str:
    day = api["today"]
    summary: Dict[str, Any] = {}
    for name in ["sentinel_x_verification_summary.json", "sentinel_x_accuracy_summary.json", "verification_summary.json", "accuracy_summary.json", "sentinel_verification_summary.json"]:
        obj = read_json(directory / name, {})
        if isinstance(obj, dict) and obj:
            summary = obj
            break
    matched = int(num(pick(summary, "matched_cases", "pairs", "n", default=0), 0) or 0)
    target = int(num(pick(summary, "verification_min_cases", "target_cases", "minimum_cases", default=30), 30) or 30)
    pct_done = clamp(matched / max(1, target) * 100)
    enough = summary.get("sample_status") == "ENOUGH_TO_EVALUATE" and matched >= target

    body = v65_hero(api, "Akurasi", f"{day['date_label']}. Evaluasi ditampilkan jika data pembanding cukup.", day, show_metrics=False)
    body += v65_notice()
    body += f'''<section class="section-compact"><div class="container">
    <div class="glass glass-static reveal">
      <div class="section-header">
        <div class="section-overline">Verifikasi</div>
        <h2 style="font-size:24px;font-weight:800">{"Data cukup untuk evaluasi" if enough else "Belum cukup bukti verifikasi"}</h2>
        <p style="font-size:14px;color:var(--mist);margin-top:4px">{matched}/{target} pasangan</p>
      </div>
      <div class="trust-bar"><div class="trust-fill" data-width="{pct_done:.0f}%" style="width:0"></div></div>
    </div>
  </div></section>'''

    if enough:
        # Format the values nicely
        mae_val = num(pick(summary, "temperature_mae_c", "mae_temp", "temperature_mae"))
        mae_str = f"{mae_val:.1f}°C" if mae_val is not None else "—"

        brier_val = num(pick(summary, "rain_brier_score", "brier_score", "rain_score"))
        brier_str = f"{brier_val:.3f}" if brier_val is not None else "—"

        pod_val = num(pick(summary, "rain_pod", "pod"))
        pod_str = f"{pod_val * 100.0:.1f}%" if pod_val is not None else "—"

        far_val = num(pick(summary, "rain_far", "far"))
        far_str = f"{far_val * 100.0:.1f}%" if far_val is not None else "—"

        csi_val = num(pick(summary, "rain_csi", "csi"))
        csi_str = f"{csi_val * 100.0:.1f}%" if csi_val is not None else "—"

        cat_val = num(pick(summary, "category_accuracy", "accuracy_cat"))
        cat_str = f"{cat_val * 100.0:.1f}%" if cat_val is not None else "—"

        body += f'''<section class="section-compact"><div class="container">
      <div class="day-card-grid reveal">
        <div class="glass glass-static"><div class="kpi-label">Error Suhu (MAE)</div><div class="kpi-value" style="margin-top:8px;font-size:28px">{esc(mae_str)}</div><div class="kpi-label" style="font-size:10px;text-transform:none;color:var(--mist);margin-top:4px">Lebih kecil lebih baik</div></div>
        <div class="glass glass-static"><div class="kpi-label">Brier Peluang Hujan</div><div class="kpi-value" style="margin-top:8px;font-size:28px">{esc(brier_str)}</div><div class="kpi-label" style="font-size:10px;text-transform:none;color:var(--mist);margin-top:4px">Lebih kecil lebih baik</div></div>
        <div class="glass glass-static"><div class="kpi-label">Hujan Terdeteksi (POD)</div><div class="kpi-value" style="margin-top:8px;font-size:28px">{esc(pod_str)}</div><div class="kpi-label" style="font-size:10px;text-transform:none;color:var(--mist);margin-top:4px">Kerap hujan tertangkap</div></div>
        <div class="glass glass-static"><div class="kpi-label">Alarm Keliru (FAR)</div><div class="kpi-value" style="margin-top:8px;font-size:28px">{esc(far_str)}</div><div class="kpi-label" style="font-size:10px;text-transform:none;color:var(--mist);margin-top:4px">Alarm palsu hujan</div></div>
        <div class="glass glass-static"><div class="kpi-label">Skor Deteksi Hujan (CSI)</div><div class="kpi-value" style="margin-top:8px;font-size:28px">{esc(csi_str)}</div><div class="kpi-label" style="font-size:10px;text-transform:none;color:var(--mist);margin-top:4px">Critical success index</div></div>
        <div class="glass glass-static"><div class="kpi-label">Kecocokan Kategori</div><div class="kpi-value" style="margin-top:8px;font-size:28px">{esc(cat_str)}</div><div class="kpi-label" style="font-size:10px;text-transform:none;color:var(--mist);margin-top:4px">Akurasi kategori cuaca</div></div>
      </div>
    </div></section>'''

        # Parse reliability bins
        rel_rows = ""
        reliability = summary.get("reliability_bins") or []
        for r in reliability:
            bin_label = r.get("probability_bin") or r.get("bin") or ""
            n_cases = r.get("n", 0)
            mean_forecast = pick(r, "mean_forecast_probability", "mean_forecast_pct")
            obs_freq = pick(r, "observed_rain_frequency", "observed_frequency_pct")

            mean_str = f"{float(mean_forecast):.1f}%" if mean_forecast not in (None, "", "—") else "—"
            obs_str = f"{float(obs_freq):.1f}%" if obs_freq not in (None, "", "—") else "—"

            rel_rows += f'''<tr>
              <td><span style="font-weight:700">{esc(bin_label)}</span></td>
              <td>{esc(n_cases)}</td>
              <td>{esc(mean_str)}</td>
              <td>{esc(obs_str)}</td>
            </tr>'''

        if not rel_rows:
            rel_rows = "<tr><td colspan='4' style='text-align:center;color:var(--mist)'>Belum ada data observasi yang cocok.</td></tr>"

        body += f'''<section class="section"><div class="container">
      <div class="glass glass-static reveal">
        <div class="section-header">
          <div class="section-overline">Kalibrasi</div>
          <h2 class="section-title">Bukti Peluang Hujan (Reliability)</h2>
          <p class="section-desc">Bandingkan peluang prakiraan dengan frekuensi hujan teramati.</p>
        </div>
        <div style="overflow-x:auto;margin-top:20px">
          <table class="source-table">
            <thead>
              <tr>
                <th>Kelompok Peluang</th>
                <th>Jumlah Kasus</th>
                <th>Rata-rata Prediksi</th>
                <th>Hujan yang Terjadi</th>
              </tr>
            </thead>
            <tbody>
              {rel_rows}
            </tbody>
          </table>
        </div>
      </div>
    </div></section>'''
    else:
        body += '''<section class="section-compact"><div class="container"><div class="glass glass-static reveal"><p style="color:var(--mist);font-size:14px;line-height:1.7">Halaman ini sengaja tidak mengklaim akurasi sebelum data cukup. Begitu observasi terkumpul, metrik akan muncul otomatis.</p></div></div></section>'''

    return v65_document(api, "accuracy", f"LANGIT Akurasi — {api['location_name']}", body)


def latest_issue(apis: List[Dict[str, Any]]) -> Optional[str]:
    stamps = [stamp for api in apis if (stamp := parse_timestamp(api.get("generated_at_iso"))) is not None]
    return max(stamps).isoformat() if stamps else None


def v65_portal_page(apis, root):
    return campus_ui.portal(sys.modules[__name__], apis, root)


def v65_portal_geo(apis: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Use the same null and coordinate contract as individual location maps.
    features = [feature for api in apis for feature in v65_geo_for_api(api)["features"]]
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Rebuild + Verify
# ---------------------------------------------------------------------------

def verify(root: Path) -> int:
    required_root = [root / "index.html", root / "langit_portal_map.html", root / "langit_portal_manifest.json"]
    missing = [str(p) for p in required_root if not p.is_file() or p.stat().st_size == 0]
    dirs = location_dirs(root)
    if not dirs:
        missing.append("No location outputs")
    for d in dirs:
        for name in ["langit_app.html", "langit_3day.html", "langit_activity.html", "langit_map_room.html", "keandalan_data.html", "akurasi_data.html", "langit_api_v1.json", "langit_location.geojson"]:
            if not (d / name).is_file() or (d / name).stat().st_size == 0:
                missing.append(str(d / name))
    if missing:
        print("ERROR: file publik kurang:")
        for p in missing[:30]:
            print(" -", p)
        return 2
    banned = ["visual-first", "Data confidence", "Window aman", "data publik</small>", "ANEMOS sedang", "AETHER Sentinel", "[.new Set", "const hours=[.new"]
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
    weather_field.ensure(root)
    weather_news.ensure(root)
    meta = metadata_by_slug(root)
    dirs = location_dirs(root)
    if not dirs:
        print("ERROR: tidak ada folder lokasi di outputs/. Jalankan forecast dulu.", file=sys.stderr)
        return 2
    write_text(root / "assets" / asset_name("css"), CSS_V65)
    write_text(root / "assets" / asset_name("js"), JS_V65)
    write_text(root / "assets" / asset_name("data.js"), DATA_JS)
    write_text(root / "assets" / asset_name("field.js"), FIELD_JS)
    # Photography is a presentation asset; source forecast data is untouched.
    for asset in campus_ui.ASSET_ROOT.iterdir():
        if asset.suffix == ".webp":
            (root / "assets" / asset.name).write_bytes(asset.read_bytes())
    write_json(root / "assets" / "campus-credits.json", campus_ui.THEME)
    apis: List[Dict[str, Any]] = []
    for d in dirs:
        api = load_location_api(d, meta.get(d.name, {"slug": d.name}))
        apis.append(api)
        gj = v65_geo_for_api(api)
        write_json(d / "langit_api_v1.json", api)
        write_public_tables(d, api)
        write_json(d / "langit_location.geojson", gj)
        write_json(d / "langit_map_layers.json", {"brand": BRAND, "version": VERSION, "geojson": gj})
        # Available destinations are presentation context, never public API fields.
        api = {**api, "_campus_available_slugs": [directory.name for directory in dirs]}
        # Main pages
        write_text(d / "langit_app.html", v65_today_page(api))
        write_text(d / "langit_3day.html", v65_three_day_page(api))
        write_text(d / "langit_activity.html", v65_activity_page(api))
        write_text(d / "offline.html", '<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LANGIT — koneksi tidak tersedia</title></head><body><h1>LANGIT</h1><p>Periksa koneksi untuk mendapatkan prakiraan terbaru. Perhatikan waktu terbit dan waktu berlaku data yang tersimpan.</p><a href="langit_app.html">Buka prakiraan lokasi</a></body></html>')
        write_text(d / "keandalan_data.html", v65_data_page(api))
        acc_html = v65_accuracy_page(api, d)
        write_text(d / "akurasi_data.html", acc_html)
        write_text(d / "sentinel_x_accuracy_public.html", acc_html)
        map_html = v65_map_page(f"LANGIT Map — {api['location_name']}", gj, "langit_app.html")
        write_text(d / "langit_map_room.html", map_html)
        # Preserve bookmarked URLs while retiring stale duplicate dashboards.
        write_text(d / "langit_planner.html", v65_planner_page(api))
        aliases = {
            "langit_map.html": "langit_map_room.html", "anemos_map.html": "langit_map_room.html",
            "anemos_3day.html": "langit_3day.html", "anemos_activity.html": "langit_activity.html",
            "anemos_commute_advice.html": "langit_activity.html", "anemos_laundry_advice.html": "langit_activity.html",
            "anemos_app.html": "langit_app.html", "anemos_today.html": "langit_app.html",
            "anemos_public_landing.html": "langit_app.html", "langit_public_landing.html": "langit_app.html",
            "command_center_sentinel_x.html": "langit_app.html", "sentinel_x_report.html": "langit_app.html",
            "langit_planer.html": "langit_planner.html", "langit_model_court.html": "keandalan_data.html",
        }
        for old_name, target in aliases.items():
            write_text(d / old_name, f"<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta http-equiv='refresh' content='0;url={target}'><link rel='canonical' href='{target}'><title>LANGIT</title></head><body><a href='{target}'>Buka halaman LANGIT terbaru</a></body></html>")
        write_text(d / "langit_whatsapp_brief.txt", f"LANGIT — {api['location_name']}\n{api['today']['date_label']}\n{decision_sentence(api['location_name'], api['today'], short=True)}\n")

    pgeo = v65_portal_geo(apis)
    write_json(root / "langit_all_locations.geojson", pgeo)
    source_revision = os.environ.get("LANGIT_SOURCE_SHA") or None
    if source_revision and not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("LANGIT_SOURCE_SHA must be a full Git commit SHA")
    write_json(root / "langit_portal_manifest.json", {
        "brand": BRAND, "version": VERSION, "generated_at": latest_issue(apis),
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(), "source_revision": source_revision,
        "public_base_url": public_base_url,
        "locations": [{"slug": a["location_slug"], "name": a["location_name"],
                       "forecast_issued_at": a["today"].get("issued_at")} for a in apis]})
    write_text(root / "langit_portal_map.html", v65_map_page("LANGIT Portal Map", pgeo, "index.html"))
    write_text(root / "index.html", v65_portal_page(apis, root))
    print(f"OK: {VERSION} rebuild selesai. lokasi={len(apis)}")
    return verify(root)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the LANGIT public website.")
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


