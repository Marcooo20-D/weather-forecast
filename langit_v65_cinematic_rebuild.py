#!/usr/bin/env python3
"""
LANGIT v65 Cinematic Rebuild
=============================

Premium atmospheric experience layer for LANGIT weather intelligence.
Replaces langit_v63_product_rebuild.py with a cinematic, Apple-grade
visual experience while preserving 100% of the data pipeline.

Usage:
  python langit_v65_cinematic_rebuild.py --root outputs --public-base-url https://marcooo20-d.github.io/weather-forecast
  python langit_v65_cinematic_rebuild.py --root outputs --verify-only
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
VERSION = "LANGIT v65.1"
TZ_NAME = "Asia/Jakarta"
DISCLAIMER = "Bukan informasi resmi BMKG. Untuk cuaca ekstrem, pantau peringatan dini BMKG dan kondisi setempat."
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
    return {"safe": "Aman", "watch": "Perlu diperhatikan", "rain": "Waspada", "danger": "Berpotensi signifikan", "limited": "Data terbatas"}.get(cls, "Perlu diperhatikan")


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
    if p >= 78: return "Hujan kuat"
    if p >= 55: return "Hujan lokal"
    if p >= 35: return "Potensi hujan"
    if p >= 20: return "Awan menebal"
    if hi_val is not None and hi_val >= 36 and 10 <= h <= 16: return "Panas menyengat"
    if hi_val is not None and hi_val >= 34 and 9 <= h <= 16: return "Panas lembap"
    if r is not None and r >= 88 and (h <= 8 or h >= 19): return "Lembap"
    if 10 <= h <= 15: return "Cerah berawan"
    if 16 <= h <= 18: return "Berawan sore"
    return "Berawan"


def row_to_hour(row: Dict[str, Any], fallback_date: Optional[dt.date] = None, fallback_relative: str = "Hari ini") -> Dict[str, Any]:
    hh = hour(pick(row, "hour", "jam", "time", "local_time", "target_hour", "target_time", "datetime", "timestamp", default="00:00"))
    d = parse_date(pick(row, "date", "tanggal", "target_date", "valid_date", "forecast_date", "datetime", "timestamp")) or fallback_date
    temp = num(pick(row, "temp_c", "temperature_c", "temperature_2m_c", "avg_temperature_c", "t2m", "suhu"))
    rh = num(pick(row, "humidity_pct", "relative_humidity", "relative_humidity_2m", "rh", "kelembapan"))
    heat = num(pick(row, "heat_index_c", "apparent_temperature_c", "feels_like_c", "terasa"), temp)
    rain = prob(pick(row, "rain_probability", "rain_probability_raw", "precip_probability", "precipitation_probability", "pop", "hujan"))
    wind = num(pick(row, "wind_kmh", "wind_speed_kmh", "wind_speed_10m_kmh", "angin"))
    base_score = clamp(pick(row, "risk_score", "score", "risk", default=0), default=0)
    score = max(base_score, rain or 0, heat_risk(heat, temp, rh), wind_risk(wind))
    valid = any(x is not None for x in [temp, rh, heat, rain, wind])
    cls = text(pick(row, "risk_class", "risk_level", default="")).lower()
    cls = cls if cls in {"safe", "watch", "rain", "danger", "limited"} else risk_class(score, valid)
    if not valid:
        cls = "limited"
    cond = text(pick(row, "condition", "weather", "cuaca", "summary", default=""))
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
        "rain_probability": rain, "wind_kmh": wind, "risk_score": round(score),
        "risk_class": cls, "risk_label": risk_label(cls), "condition": cond, "valid": valid,
        "wind_dir": num(pick(row, "wind_direction_deg", "wind_direction", "wind_dir", "arah_angin")),
        "cloud_pct": num(pick(row, "cloud_cover_pct", "cloud_cover", "cloud_p50", "cloud_pct", "awan")),
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
    score = 35 if cls == "limited" else clamp(worst.get("risk_score"), default=0)
    return {
        "relative": relative, "date_iso": date_value.isoformat(), "date_label": fmt_date(date_value),
        "date_short": fmt_date(date_value, False), "weekday": DAY_ID[date_value.weekday()],
        "hours": rows, "periods": period_summaries(rows),
        "peak_rain_probability": prob(peak.get("rain_probability"), None),
        "peak_rain_hour": text(peak.get("hour"), "—"),
        "risk_score": round(score), "risk_class": cls, "risk_label": risk_label(cls),
        "condition": text(worst.get("condition"), "Data terbatas" if cls == "limited" else "Berawan"),
        "avg_temp_c": mean(x.get("temp_c") for x in valid),
        "avg_rh": mean(x.get("humidity_pct") for x in valid),
        "max_heat_c": maximum(x.get("heat_index_c") for x in valid),
        "max_wind_kmh": maximum(x.get("wind_kmh") for x in valid),
        "safe_windows": best_windows(rows), "valid_points": len(valid),
    }


def rain_phrase(day: Dict[str, Any]) -> str:
    p = prob(day.get("peak_rain_probability"), 0) or 0
    h = text(day.get("peak_rain_hour"), "—")
    if p >= 55: return f"hujan paling perlu diwaspadai sekitar {h}"
    if p >= 25: return f"awan/hujan perlu dipantau sekitar {h}"
    if p > 0: return f"peluang hujan kecil, puncaknya sekitar {h}"
    return "peluang hujan rendah"


def decision_sentence(location: str, day: Dict[str, Any], short: bool = False) -> str:
    c = day.get("risk_class", "watch")
    p = pct(day.get("peak_rain_probability"))
    peak = text(day.get("peak_rain_hour"), "—")
    win = ", ".join(day.get("safe_windows") or [])
    if c == "limited":
        return "Data prakiraan belum lengkap. Pantau kondisi langit secara mandiri."
    if c == "danger":
        return f"Disarankan untuk membatasi aktivitas luar ruang pada jam rawan sekitar pukul {peak} WIB (peluang {p})."
    if c == "rain":
        return f"Potensi hujan terpantau cukup tinggi. Siapkan perlengkapan hujan jika beraktivitas luar ruang di sekitar pukul {peak} WIB (peluang {p})."
    if c == "watch":
        return f"Kondisi cuaca mendukung, tetap pantau potensi hujan sekitar pukul {peak} WIB." if short else f"Kondisi cuaca secara umum kondusif untuk beraktivitas, namun tetap pantau potensi hujan lokal di sekitar pukul {peak} WIB."
    return f"Kondisi cuaca mendukung aktivitas luar ruang. Periode nyaman terpantau pada: {win or 'pagi hingga siang hari'}."


def short_activity_advice(day: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    c = day.get("risk_class", "watch")
    peak = text(day.get("peak_rain_hour"), "—")
    win = ", ".join(day.get("safe_windows") or ["cek langit"])
    heat = num(day.get("max_heat_c"), 0) or 0
    if c in {"danger", "rain"}:
        return [
            ("Perjalanan / Motor", "Bawa Jas Hujan", f"Hindari berkendara sekitar pukul {peak}.", "rain"),
            ("Jalan Kaki", "Siapkan Payung", f"Antisipasi tempat berteduh di sekitar pukul {peak}.", "rain"),
            ("Jemur Pakaian", "Pagi Hari", "Hindari meninggalkan jemuran terlalu lama.", "watch"),
            ("Aktivitas Outdoor", "Siapkan Rencana Cadangan", "Gunakan opsi ruangan tertutup.", "rain"),
            ("Olahraga", "Sesuaikan Jadwal", f"Pilih jam alternatif: {win}.", "watch"),
            ("Fotografi", "Gunakan Pelindung", "Lindungi peralatan elektronik dari kelembapan.", "watch"),
        ]
    if c == "watch":
        return [
            ("Perjalanan / Motor", "Cukup Kondusif", f"Tetap antisipasi potensi hujan sekitar pukul {peak}.", "watch"),
            ("Jalan Kaki", "Aman Bersyarat", f"Periode nyaman: {win}.", "safe"),
            ("Jemur Pakaian", "Pagi–Siang", "Angkat pakaian sebelum memasuki sore hari.", "safe" if heat < 36 else "watch"),
            ("Aktivitas Outdoor", "Kondusif", "Tetap pantau perkembangan awan.", "watch"),
            ("Olahraga", "Hindari Terik", f"Periode nyaman: {win}.", "watch" if heat >= 34 else "safe"),
            ("Fotografi", "Pantau Awan", f"Perhatikan perubahan intensitas cahaya sekitar pukul {peak}.", "watch"),
        ]
    if c == "limited":
        return [
            ("Perjalanan / Motor", "Pantau Mandiri", "Data prakiraan belum lengkap.", "limited"),
            ("Jalan Kaki", "Perhatikan Cuaca", "Lihat kondisi langit setempat secara berkala.", "limited"),
            ("Jemur Pakaian", "Pantau Berkala", "Sebaiknya tidak ditinggalkan dalam waktu lama.", "limited"),
            ("Aktivitas Outdoor", "Fleksibel", "Siapkan opsi berteduh yang memadai.", "limited"),
            ("Olahraga", "Durasi Singkat", "Periksa kondisi cuaca langsung di lokasi.", "limited"),
            ("Fotografi", "Cek Kondisi", "Tunggu hingga data prakiraan diperbarui.", "limited"),
        ]
    return [
        ("Perjalanan / Motor", "Aman", "Kondisi cuaca mendukung perjalanan luar ruang.", "safe"),
        ("Jalan Kaki", "Sangat Nyaman", f"Periode terbaik: {win}.", "safe"),
        ("Jemur Pakaian", "Sangat Baik", "Pagi hingga siang hari sangat mendukung.", "safe"),
        ("Aktivitas Outdoor", "Sangat Aman", "Sangat mendukung untuk kegiatan luar ruang.", "safe"),
        ("Olahraga", "Pagi / Sore", f"Periode nyaman: {win}.", "safe"),
        ("Fotografi", "Sangat Baik", "Kondisi cahaya pagi dan sore terpantau optimal.", "safe"),
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
        file_path = safe_find_file(directory, name)
        if file_path.exists():
            raw_api = read_json(file_path, {}) or {}
            if isinstance(raw_api, dict):
                break
    loc_name = text(raw_api.get("location_name"), text(meta.get("location_name"), directory.name.replace("-", " ").title()))
    slug = text(raw_api.get("location_slug"), text(meta.get("slug"), directory.name))
    lat = num(raw_api.get("latitude"), num(meta.get("latitude"), num(meta.get("lat"))))
    lon = num(raw_api.get("longitude"), num(meta.get("longitude"), num(meta.get("lon"))))
    if lat is None or lon is None:
        gj_path = safe_find_file(directory, "langit_location.geojson")
        gj = read_json(gj_path, {}) or {}
        feats = gj.get("features") if isinstance(gj, dict) else []
        if feats:
            coords = (feats[0].get("geometry") or {}).get("coordinates") or []
            if len(coords) >= 2:
                lon = num(coords[0], lon)
                lat = num(coords[1], lat)
    rows: List[Dict[str, Any]] = []
    for fname in ["langit_hourly_intelligence.csv", "anemos_hourly_compact.csv", "anemos_risk_timeline.csv", "forecast.csv", "forecast_all_locations.csv"]:
        rows = read_csv(safe_find_file(directory, fname))
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
    
    # Enrich days with cloud cover and wind direction from sentinel_x.csv
    sentinel_map = {}
    for fname in ["sentinel_x.csv", "sentinel_x_all_locations.csv"]:
        spath = safe_find_file(directory, fname)
        if spath.exists():
            for sr in read_csv(spath):
                t_date = text(sr.get("target_date"))
                t_jam = hour(sr.get("jam"))
                if t_date and t_jam:
                    sentinel_map[(t_date, t_jam)] = (
                        num(sr.get("cloud_p50")),
                        num(sr.get("wind_direction_deg"))
                    )
            break
            
    for d in days:
        date_iso = text(d.get("date_iso"))
        for h in d.get("hours", []):
            h_time = h.get("hour")
            sentinel_data = sentinel_map.get((date_iso, h_time))
            
            # Cloud cover enrichment
            cloud = sentinel_data[0] if sentinel_data else None
            if cloud is None:
                cloud = h.get("cloud_pct")
            if cloud is None:
                cond = text(h.get("condition"), "").lower()
                if any(k in cond for k in ["hujan lebat", "danger", "ekstrem"]):
                     cloud = 95.0
                elif any(k in cond for k in ["hujan", "rain"]):
                     cloud = 85.0
                elif any(k in cond for k in ["berawan", "cloudy", "overcast", "mendung"]):
                     cloud = 70.0
                elif any(k in cond for k in ["cerah berawan", "partly cloudy", "partlycloudy"]):
                     cloud = 40.0
                else:
                     cloud = 15.0
            h["cloud_pct"] = cloud

            # Wind direction enrichment
            wind_dir = sentinel_data[1] if sentinel_data else None
            if wind_dir is None:
                wind_dir = h.get("wind_dir")
            if wind_dir is None:
                wind_dir = 0.0
            h["wind_dir"] = wind_dir

    sources: List[Dict[str, Any]] = []
    for fname in ["source_status.csv", "source_status_all_locations.csv", "langit_source_status.csv"]:
        sources = read_csv(safe_find_file(directory, fname))
        if sources:
            break
    return {
        "brand": BRAND, "version": VERSION,
        "generated_at": fmt_update(raw_api.get("generated_at") or raw_api.get("updated_at")),
        "location_name": loc_name, "location_slug": slug, "latitude": lat, "longitude": lon,
        "today": days[0], "days": days, "sources": sources, "raw_version": raw_api.get("version"),
    }


# ---------------------------------------------------------------------------
# v65 CINEMATIC DESIGN SYSTEM — CSS
# ---------------------------------------------------------------------------

CSS_V65 = r'''
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=Outfit:wght@300;400;500;600;700;800;900&display=swap');

:root {
  --void: #030712;
  --abyss: #070e1e;
  --ocean: #0b1932;
  --steel: #15294a;
  --mist: #94a3b8;
  --cloud: #94a3b8;
  --snow: #f1f5f9;
  --white: #ffffff;
  --dawn: #f97316;
  --noon: #06b6d4;
  --dusk: #8b5cf6;
  --safe: #10b981;
  --watch: #f59e0b;
  --alert: #f97316;
  --danger: #ef4444;
  --limited: #6366f1;

  --glow-noon: rgba(6, 182, 212, 0.15);
  --glow-dawn: rgba(249, 115, 22, 0.1);
  --glow-dusk: rgba(139, 92, 246, 0.12);
  --glow-void: rgba(99, 102, 241, 0.08);

  --glass: rgba(10, 20, 40, 0.4);
  --glass-border: rgba(255, 255, 255, 0.08);
  --glass-glow: rgba(50, 183, 255, 0.05);
  --glass-hover: rgba(15, 30, 60, 0.55);
  
  --radius-sm: 12px;
  --radius-md: 18px;
  --radius-lg: 24px;
  --radius-xl: 32px;
  
  --font-display: 'Outfit', 'Inter', system-ui, sans-serif;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);

  /* Responsive Design Tokens */
  --container-max: 1200px;
  --container-pad: clamp(16px, 4vw, 80px);
  --section-pad: clamp(32px, 6vw, 80px);
  --card-pad: clamp(16px, 3vw, 32px);
  --font-body: clamp(14px, 0.25vw + 13px, 16px);
  --font-label: clamp(10px, 0.15vw + 9px, 12px);
  --nav-height: 72px;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; min-width: 0; }

html {
  scroll-behavior: smooth;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  overflow-x: hidden;
}

body {
  background: var(--void);
  color: var(--snow);
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  font-size: var(--font-body);
  letter-spacing: -0.01em;
  line-height: 1.5;
  overflow-x: hidden;
  transition: background 1.2s var(--ease-in-out);
}

img, svg, canvas, video, iframe { max-width: 100%; }

/* Adaptive visibility utilities */
.desktop-only { display: none !important; }
.mobile-only { display: initial !important; }
@media (min-width: 768px) {
  .desktop-only { display: initial !important; }
  .mobile-only { display: none !important; }
}

/* Word-break safety for long text */
.hero-title, .section-title, .decision-title, .location-name,
.period-condition, .activity-name, h1, h2, h3 {
  overflow-wrap: break-word;
  word-break: break-word;
}

body.theme-dawn { background: radial-gradient(circle at 50% -100px, rgba(249,115,22,0.18), var(--void) 70%), var(--void); }
body.theme-noon { background: radial-gradient(circle at 50% -100px, rgba(6,182,212,0.18), var(--void) 70%), var(--void); }
body.theme-dusk { background: radial-gradient(circle at 50% -100px, rgba(139,92,246,0.18), var(--void) 70%), var(--void); }
body.theme-void { background: radial-gradient(circle at 50% -100px, rgba(99,102,241,0.15), var(--void) 70%), var(--void); }

a { color: inherit; text-decoration: none; }

/* --- ATMOSPHERIC CANVAS --- */
.atmo {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
}
#atmo-canvas {
  width: 100%;
  height: 100%;
  opacity: 0.8;
  mix-blend-mode: screen;
}

/* --- SPATIAL ZOOMS / SCANNER OVERLAY --- */
#spatial-overlay {
  position: fixed;
  inset: 0;
  z-index: 99999;
  background: var(--void);
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  transition: opacity 0.8s var(--ease-out), transform 0.8s var(--ease-out);
  overflow: hidden;
}
#spatial-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.spatial-hud {
  position: relative;
  z-index: 10;
  text-align: center;
  font-family: var(--font-display);
  color: var(--snow);
  max-width: 90vw;
}
.spatial-title {
  font-size: 13px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.3em;
  color: var(--noon);
  margin-bottom: 12px;
  animation: tracking-pulse 2s infinite ease-in-out;
}
.spatial-status {
  font-size: 26px;
  font-weight: 500;
  letter-spacing: -0.03em;
  margin-bottom: 24px;
}
.spatial-skip {
  position: absolute;
  bottom: 40px;
  z-index: 11;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.15em;
  padding: 10px 24px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: var(--mist);
  cursor: pointer;
  backdrop-filter: blur(12px);
  transition: all 0.3s var(--ease-out);
}
.spatial-skip:hover {
  color: var(--white);
  border-color: rgba(6, 182, 212, 0.4);
  background: rgba(6, 182, 212, 0.1);
  box-shadow: 0 0 20px rgba(6, 182, 212, 0.2);
}

@keyframes tracking-pulse {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

/* --- NAVIGATION --- */
.nav-bar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--container-pad);
  height: var(--nav-height);
  background: rgba(3, 7, 18, 0.6);
  backdrop-filter: blur(28px) saturate(1.6);
  -webkit-backdrop-filter: blur(28px) saturate(1.6);
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  transition: background 0.4s var(--ease-out), border-color 0.4s var(--ease-out);
}
.nav-brand {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}
.nav-logo {
  width: 36px;
  height: 36px;
  border-radius: 12px;
  background: linear-gradient(135deg, #06b6d4, #8b5cf6 50%, #10b981);
  box-shadow: 0 0 24px rgba(6, 182, 212, 0.35);
  position: relative;
  overflow: hidden;
  transition: transform 0.5s var(--ease-spring);
  flex-shrink: 0;
}
.nav-brand:hover .nav-logo {
  transform: rotate(180deg) scale(1.1);
}
.nav-logo::after {
  content: '';
  position: absolute;
  top: 15%;
  left: 15%;
  width: 40%;
  height: 40%;
  border-radius: 50%;
  background: rgba(255,255,255,0.4);
  filter: blur(2px);
}
.nav-title {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 20px;
  letter-spacing: -0.04em;
  background: linear-gradient(to right, var(--white), var(--cloud));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.nav-sub {
  font-size: 11px;
  color: var(--mist);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
/* Hamburger toggle (hidden on desktop) */
.nav-toggle {
  display: none;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  width: 44px;
  height: 44px;
  padding: 10px;
  background: none;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all 0.3s var(--ease-out);
  flex-shrink: 0;
}
.nav-toggle span {
  display: block;
  width: 100%;
  height: 2px;
  background: var(--cloud);
  border-radius: 2px;
  transition: all 0.3s var(--ease-out);
}
.nav-toggle:hover {
  border-color: rgba(6, 182, 212, 0.3);
  background: rgba(255, 255, 255, 0.03);
}
.nav-toggle[aria-expanded="true"] span:nth-child(1) {
  transform: rotate(45deg) translate(5px, 5px);
}
.nav-toggle[aria-expanded="true"] span:nth-child(2) { opacity: 0; }
.nav-toggle[aria-expanded="true"] span:nth-child(3) {
  transform: rotate(-45deg) translate(5px, -5px);
}
.nav-links {
  display: flex;
  gap: 8px;
}
.nav-link {
  padding: 8px 16px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 600;
  color: var(--cloud);
  border: 1px solid transparent;
  transition: all 0.3s var(--ease-out);
  cursor: pointer;
  white-space: nowrap;
}
.nav-link:hover {
  background: rgba(255, 255, 255, 0.05);
  color: var(--white);
}
.nav-link.active {
  background: rgba(6, 182, 212, 0.12);
  color: var(--noon);
  border-color: rgba(6, 182, 212, 0.25);
}

/* --- SCROLL REVEAL --- */
.reveal {
  opacity: 0;
  transform: translateY(24px);
  transition: opacity 0.8s var(--ease-out), transform 0.8s var(--ease-out);
}
.reveal.visible {
  opacity: 1;
  transform: translateY(0);
}
.reveal-delay-1 { transition-delay: 0.1s; }
.reveal-delay-2 { transition-delay: 0.2s; }
.reveal-delay-3 { transition-delay: 0.3s; }
.reveal-delay-4 { transition-delay: 0.4s; }


/* --- LAYOUT --- */
.page { position: relative; z-index: 1; padding-top: var(--nav-height); }
.container { width: min(var(--container-max), calc(100% - var(--container-pad) * 2)); margin: 0 auto; }
.section { padding: var(--section-pad) 0; }
.section-compact { padding: 32px 0; }

/* --- ULTRA CINEMATIC HERO --- */
.hero {
  min-height: calc(100vh - 72px);
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  text-align: center;
  padding: 80px 24px 100px;
  position: relative;
  overflow: hidden;
}
.hero-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(140px);
  opacity: 0.25;
  transition: all 1.5s var(--ease-in-out);
}
.hero-glow-1 { width: 50vw; height: 50vw; background: var(--noon); top: -20%; left: 10%; }
.hero-glow-2 { width: 40vw; height: 40vw; background: var(--dusk); bottom: -10%; right: 10%; }
.hero-glow-3 { width: 30vw; height: 30vw; background: var(--safe); top: 30%; left: 35%; opacity: 0.1; }

.hero-label {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 8px 18px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--glass-border);
  font-family: var(--font-display);
  font-size: 13px;
  font-weight: 600;
  color: var(--cloud);
  margin-bottom: 24px;
  backdrop-filter: blur(16px);
}
.hero-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--safe);
  box-shadow: 0 0 12px var(--safe);
  animation: heartbeat-pulse 2s infinite var(--ease-in-out);
}
@keyframes heartbeat-pulse {
  0%, 100% { transform: scale(1); opacity: 1; box-shadow: 0 0 8px var(--safe); }
  50% { transform: scale(1.3); opacity: 0.6; box-shadow: 0 0 16px var(--safe), 0 0 24px var(--safe); }
}
.hero-title {
  font-family: var(--font-display);
  font-size: clamp(40px, 6.5vw, 80px);
  font-weight: 900;
  letter-spacing: -0.04em;
  line-height: 0.95;
  margin-bottom: 24px;
  background: linear-gradient(135deg, var(--white) 0%, var(--cloud) 50%, var(--noon) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.hero-subtitle {
  font-size: clamp(15px, 1.8vw, 20px);
  color: var(--mist);
  font-weight: 400;
  max-width: 620px;
  line-height: 1.6;
  margin-bottom: 48px;
}
.hero-metrics {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  justify-content: center;
  margin-top: 24px;
}
.hero-metric {
  padding: var(--card-pad);
  border-radius: var(--radius-lg);
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(32px);
  text-align: center;
  min-width: 140px;
  flex: 1 1 140px;
  transition: all 0.4s var(--ease-out);
}
.hero-metric:hover {
  background: rgba(255, 255, 255, 0.06);
  transform: translateY(-6px);
  border-color: rgba(6, 182, 212, 0.3);
  box-shadow: 0 15px 40px rgba(0, 0, 0, 0.3), 0 0 20px rgba(6, 182, 212, 0.08);
}
.hero-metric-value {
  font-family: var(--font-display);
  font-size: 38px;
  font-weight: 800;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.hero-metric-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--mist);
  margin-top: 6px;
}
.hero-scroll {
  position: absolute;
  bottom: 30px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  color: var(--mist);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.15em;
  text-transform: uppercase;
}
.hero-scroll-line {
  width: 1px;
  height: 50px;
  background: linear-gradient(to bottom, var(--mist), transparent);
  position: relative;
  overflow: hidden;
}
.hero-scroll-line::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 20px;
  background: var(--noon);
  animation: scroll-hint-anim 2s infinite ease-in-out;
}
@keyframes scroll-hint-anim {
  0% { transform: translateY(-20px); }
  80%, 100% { transform: translateY(50px); }
}

/* --- STATUS BADGES --- */
.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 16px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.02em;
  border: 1px solid currentColor;
  backdrop-filter: blur(16px);
  text-transform: uppercase;
}
.status-badge::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: badge-pulse 1.8s infinite ease-in-out;
}
.status-safe { color: var(--safe); background: rgba(16, 185, 129, 0.08); }
.status-watch { color: var(--watch); background: rgba(245, 158, 11, 0.08); }
.status-rain { color: var(--alert); background: rgba(249, 115, 22, 0.08); }
.status-danger { color: var(--danger); background: rgba(239, 68, 68, 0.08); }
.status-limited { color: var(--limited); background: rgba(99, 102, 241, 0.08); }

@keyframes badge-pulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50% { transform: scale(1.4); opacity: 0.4; }
}

/* --- GLASS CARD --- */
.glass {
  background: rgba(10, 20, 38, 0.45);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: var(--radius-lg);
  padding: 32px;
  backdrop-filter: blur(28px);
  -webkit-backdrop-filter: blur(28px);
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2), inset 0 1px 0 rgba(255, 255, 255, 0.05);
  transition: transform 0.4s var(--ease-out), border-color 0.4s var(--ease-out), box-shadow 0.4s var(--ease-out), background 0.4s var(--ease-out);
}
.glass:hover {
  background: rgba(12, 25, 48, 0.55);
  border-color: rgba(6, 182, 212, 0.25);
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.35), 0 0 30px rgba(6, 182, 212, 0.08);
  transform: translateY(-4px);
}
.glass-static:hover { transform: none; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2); border-color: rgba(255, 255, 255, 0.06); background: rgba(10, 20, 38, 0.45); }

/* --- SECTION HEADERS --- */
.section-header {
  margin-bottom: 48px;
}
.section-overline {
  font-family: var(--font-display);
  font-size: 11px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.2em;
  color: var(--noon);
  margin-bottom: 12px;
}
.section-title {
  font-family: var(--font-display);
  font-size: clamp(28px, 4vw, 44px);
  font-weight: 800;
  letter-spacing: -0.03em;
  line-height: 1.1;
  margin-bottom: 14px;
}
.section-desc {
  font-size: 16px;
  color: var(--mist);
  max-width: 580px;
  line-height: 1.6;
}

/* --- SCROLL-DRIVEN WEATHER TIMELINE MORPH --- */
.day-tabs-container {
  max-width: 720px;
  margin: 0 auto 56px;
}
.timeline-progress-container {
  width: 100%;
  position: relative;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.timeline-progress-bar {
  position: absolute;
  top: 50%;
  left: 0;
  height: 2px;
  background: rgba(255, 255, 255, 0.08);
  width: 100%;
  transform: translateY(-50%);
  z-index: 1;
}
.timeline-progress-fill {
  position: absolute;
  top: 50%;
  left: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--noon), var(--dusk));
  transform: translateY(-50%);
  z-index: 2;
  transition: width 0.6s var(--ease-out);
  width: 0%;
}
.timeline-node {
  position: relative;
  z-index: 3;
  display: flex;
  flex-direction: column;
  align-items: center;
  cursor: pointer;
  background: none;
  border: none;
  font-family: inherit;
}
.timeline-node-dot {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--void);
  border: 2px solid var(--mist);
  transition: all 0.4s var(--ease-out);
}
.timeline-node:hover .timeline-node-dot {
  border-color: var(--snow);
  transform: scale(1.2);
}
.timeline-node.active .timeline-node-dot {
  background: var(--noon);
  border-color: var(--noon);
  box-shadow: 0 0 16px var(--noon), 0 0 24px rgba(6,182,212,0.5);
  transform: scale(1.3);
}
.timeline-node-label {
  margin-top: 10px;
  font-size: 13px;
  font-weight: 700;
  color: var(--mist);
  transition: color 0.3s;
}
.timeline-node.active .timeline-node-label {
  color: var(--white);
}

.day-panel { display: none; }
.day-panel.active { display: block; animation: panel-in 0.8s var(--ease-out); }
@keyframes panel-in {
  from { opacity: 0; transform: translateY(20px); }
  to { opacity: 1; transform: translateY(0); }
}

/* --- DECISION BLOCK --- */
.decision {
  display: grid;
  grid-template-columns: 1.2fr 0.8fr;
  gap: 24px;
  margin-top: 32px;
}
.decision-main {
  border-radius: var(--radius-xl);
  padding: 44px;
  background: rgba(10, 20, 38, 0.45);
  border: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(32px);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 280px;
  position: relative;
  overflow: hidden;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.25);
  transition: all 0.4s var(--ease-out);
}
.decision-main::before {
  content: '';
  position: absolute;
  top: -120px;
  right: -120px;
  width: 340px;
  height: 340px;
  border-radius: 50%;
  background: var(--accent-glow, rgba(6,182,212,0.08));
  filter: blur(80px);
  transition: background 1s var(--ease-in-out);
}
.decision-main:hover {
  border-color: rgba(255, 255, 255, 0.12);
  box-shadow: 0 20px 50px rgba(0,0,0,0.35);
}
.decision-title {
  font-family: var(--font-display);
  font-size: clamp(24px, 3.2vw, 42px);
  font-weight: 800;
  letter-spacing: -0.03em;
  line-height: 1.1;
  margin-top: 20px;
  position: relative;
  overflow-wrap: break-word;
  hyphens: auto;
}
.decision-desc {
  color: var(--cloud);
  font-size: 15px;
  line-height: 1.6;
  margin-top: 20px;
  position: relative;
}
.kpi-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.kpi-card {
  border-radius: var(--radius-lg);
  padding: 24px;
  background: rgba(10, 20, 38, 0.45);
  border: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(24px);
  transition: all 0.3s;
}
.kpi-card:hover {
  border-color: rgba(255, 255, 255, 0.1);
  background: rgba(12, 25, 48, 0.55);
}
.kpi-label {
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--mist);
}
.kpi-value {
  font-family: var(--font-display);
  font-size: 26px;
  font-weight: 800;
  letter-spacing: -0.02em;
  margin-top: 8px;
  font-variant-numeric: tabular-nums;
}
.kpi-sub {
  font-size: 12px;
  color: var(--cloud);
  margin-top: 4px;
}

/* --- CURVED NEON DATA VISUALIZATION --- */
.chart-wrapper {
  position: relative;
  background: rgba(7, 14, 30, 0.45);
  border-radius: var(--radius-xl);
  padding: 32px;
  border: 1px solid rgba(255, 255, 255, 0.05);
  backdrop-filter: blur(32px);
}
.chart-selector {
  display: flex;
  gap: 8px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}
.chart-chip {
  padding: 8px 18px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  color: var(--mist);
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  cursor: pointer;
  transition: all 0.3s var(--ease-out);
  font-family: inherit;
}
.chart-chip:hover {
  color: var(--snow);
  background: rgba(255, 255, 255, 0.06);
}
.chart-chip.active {
  background: rgba(6, 182, 212, 0.12);
  color: var(--noon);
  border-color: rgba(6, 182, 212, 0.35);
  box-shadow: 0 4px 16px rgba(6, 182, 212, 0.15);
}
.chart-svg-container {
  width: 100%;
  position: relative;
  height: clamp(150px, 20vw, 200px);
}
.chart-tooltip {
  position: absolute;
  pointer-events: none;
  z-index: 100;
  background: rgba(6, 14, 28, 0.9);
  backdrop-filter: blur(16px);
  border: 1px solid rgba(6, 182, 212, 0.35);
  border-radius: var(--radius-sm);
  padding: 12px 16px;
  font-size: 12px;
  color: var(--snow);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4), 0 0 20px rgba(6, 182, 212, 0.1);
  opacity: 0;
  transition: opacity 0.2s, transform 0.1s;
  min-width: 140px;
}
.chart-tooltip-time {
  font-weight: 800;
  margin-bottom: 4px;
  color: var(--cloud);
}
.chart-tooltip-value {
  font-size: 15px;
  font-weight: 800;
  color: var(--white);
}

.rain-curve {
  width: 100%;
  height: 100%;
  overflow: visible;
}
.rain-curve path.curve-fill {
  transition: d 0.6s var(--ease-out);
  opacity: 0.15;
}
.rain-curve path.curve-line {
  fill: none;
  stroke-width: 3;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: d 0.6s var(--ease-out);
  filter: drop-shadow(0 4px 8px rgba(6, 182, 212, 0.3));
}
.rain-curve circle.curve-dot {
  stroke: var(--void);
  stroke-width: 2.5;
  cursor: pointer;
  transition: r 0.2s, transform 0.2s;
}
.rain-curve circle.curve-dot:hover { r: 8; }
.rain-curve text.curve-label {
  fill: var(--mist);
  font-size: 10px;
  font-weight: 700;
  font-family: var(--font-display);
  text-anchor: middle;
}
.rain-curve text.curve-value {
  fill: var(--snow);
  font-size: 11px;
  font-weight: 800;
  font-family: var(--font-display);
  text-anchor: middle;
}

/* --- PERIOD CARDS --- */
.period-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 20px;
}
.period-card {
  border-radius: var(--radius-xl);
  padding: var(--card-pad);
  background: rgba(10, 20, 38, 0.45);
  border: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(24px);
  position: relative;
  overflow: hidden;
  transition: all 0.4s var(--ease-out);
}
.period-card:hover {
  transform: translateY(-6px);
  box-shadow: 0 15px 40px rgba(0, 0, 0, 0.3), 0 0 20px rgba(6, 182, 212, 0.08);
}
.period-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
  background: var(--accent-color, var(--noon));
  opacity: 0.8;
  transition: background 0.4s;
}
.period-name {
  font-family: var(--font-display);
  font-size: 13px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--mist);
  margin-bottom: 14px;
}
.period-condition {
  font-size: 20px;
  font-weight: 800;
  margin-bottom: 24px;
  line-height: 1.2;
}
.period-stats {
  display: flex;
  gap: 20px;
}
.period-stat-value {
  font-family: var(--font-display);
  font-size: 20px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.period-stat-label {
  font-size: 10px;
  color: var(--mist);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-top: 2px;
}

/* --- ACTIVITY CARDS --- */
.activity-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 20px;
}
.activity-card {
  border-radius: var(--radius-xl);
  padding: var(--card-pad);
  background: rgba(10, 20, 38, 0.45);
  border: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(24px);
  border-left: 4px solid var(--accent-color, var(--noon));
  transition: all 0.4s var(--ease-out);
}
.activity-card:hover {
  transform: translateY(-4px);
  background: rgba(12, 25, 48, 0.55);
  border-color: rgba(255, 255, 255, 0.1);
  box-shadow: 0 12px 32px rgba(0,0,0,0.25);
}
.activity-name {
  font-family: var(--font-display);
  font-size: 17px;
  font-weight: 800;
  margin-bottom: 8px;
}
.activity-status {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 800;
  margin-bottom: 8px;
}
.activity-advice {
  font-size: 13px;
  color: var(--mist);
  line-height: 1.5;
}

/* --- HOURLY SECTION --- */
.hourly-section {
  border-radius: var(--radius-xl);
  background: rgba(10, 20, 38, 0.45);
  border: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(24px);
  overflow: hidden;
}
.hourly-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 24px 32px;
  cursor: pointer;
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 16px;
  color: var(--white);
  user-select: none;
  transition: background 0.3s ease;
  border: none;
  background: transparent;
  width: 100%;
  text-align: left;
}
.hourly-toggle:hover { background: rgba(255, 255, 255, 0.04); }
.hourly-chevron {
  transition: transform 0.4s var(--ease-out);
  color: var(--mist);
}
.hourly-section[open] .hourly-chevron { transform: rotate(180deg); }
.hourly-list { padding: 0 24px 24px; display: grid; gap: 10px; }
.hour-row {
  display: grid;
  grid-template-columns: 70px 1.2fr repeat(4, minmax(70px, 0.5fr));
  gap: 12px;
  align-items: center;
  padding: 14px 20px;
  border-radius: var(--radius-md);
  border-left: 3px solid var(--accent-color, var(--noon));
  background: rgba(255, 255, 255, 0.02);
  transition: background 0.3s;
}
.hour-row:hover { background: rgba(255, 255, 255, 0.04); }
.hour-time {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.hour-condition {
  font-size: 14px;
  font-weight: 700;
}
.hour-status {
  font-size: 12px;
  color: var(--mist);
  margin-top: 2px;
}
.hour-box {
  text-align: center;
  padding: 10px;
  border-radius: var(--radius-sm);
  background: rgba(11, 25, 50, 0.5);
  border: 1px solid rgba(6, 182, 212, 0.15);
}
.hour-box-value {
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.hour-box-label {
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--mist);
  margin-top: 2px;
}

/* --- DYNAMIC WEATHER LAYERS --- */
.layer-controls {
  display: flex;
  gap: 10px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}
.layer-btn {
  padding: 12px 20px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  color: var(--cloud);
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  cursor: pointer;
  transition: all 0.3s var(--ease-out);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-family: inherit;
  min-height: 44px;
}
.layer-btn:hover {
  color: var(--white);
  background: rgba(255, 255, 255, 0.06);
}
.layer-btn.active {
  background: var(--active-bg, rgba(6, 182, 212, 0.15));
  color: var(--active-color, var(--noon));
  border-color: var(--active-border, rgba(6, 182, 212, 0.4));
  box-shadow: 0 4px 16px var(--active-shadow, rgba(6, 182, 212, 0.15));
}

/* --- FUTURISTIC MAP & GEOGRAPHICAL SCAN --- */
@keyframes mapSkeleton {
  0% { background-color: var(--ocean); }
  50% { background-color: var(--steel); }
  100% { background-color: var(--ocean); }
}

.map-wrapper {
  border-radius: var(--radius-xl);
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.06);
  background: var(--ocean);
  box-shadow: 0 10px 40px rgba(0,0,0,0.3);
  position: relative;
  animation: mapSkeleton 2.5s infinite ease-in-out;
}
.map-frame {
  width: 100%;
  height: clamp(320px, 55vh, 720px);
  border: 0;
  display: block;
}
.map-actions {
  display: flex;
  gap: 10px;
  padding: 20px 24px;
  background: rgba(7, 14, 30, 0.7);
  backdrop-filter: blur(20px);
  border-top: 1px solid rgba(255, 255, 255, 0.05);
}

/* --- DESKTOP WEATHER COMMAND CENTER --- */
.command-center {
  display: grid;
  grid-template-columns: 7fr 3fr;
  gap: 32px;
  margin-top: 40px;
}
.command-sidebar {
  display: grid;
  grid-template-rows: auto 1fr;
  gap: 24px;
}

/* --- LOCATION PORTAL CARDS --- */
.location-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 24px;
}
.location-card {
  border-radius: var(--radius-xl);
  padding: var(--card-pad);
  background: rgba(10, 20, 38, 0.45);
  border: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(28px);
  position: relative;
  overflow: hidden;
  transition: all 0.5s var(--ease-out);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 280px;
}
.location-card:hover {
  transform: translateY(-8px);
  box-shadow: 0 25px 60px rgba(0,0,0,0.35), 0 0 30px rgba(6, 182, 212, 0.1);
  border-color: var(--accent-color, rgba(6, 182, 212, 0.3));
}
.location-card::before {
  content: '';
  position: absolute;
  top: -80px;
  right: -80px;
  width: 240px;
  height: 240px;
  border-radius: 50%;
  background: var(--accent-glow, rgba(16,185,129,0.06));
  filter: blur(50px);
  transition: opacity 0.5s ease;
}
.location-card:hover::before { opacity: 1.5; }

.location-name {
  font-family: var(--font-display);
  font-size: 24px;
  font-weight: 800;
  letter-spacing: -0.03em;
  margin: 16px 0 10px;
  position: relative;
}
.location-desc {
  font-size: 14px;
  color: var(--cloud);
  line-height: 1.5;
  margin-bottom: 24px;
  position: relative;
}
.location-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-bottom: 24px;
  position: relative;
}
.location-stat {
  padding: 12px;
  border-radius: var(--radius-md);
  background: rgba(11, 25, 50, 0.5);
  border: 1px solid rgba(255, 255, 255, 0.04);
  text-align: center;
}
.location-stat-value {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.location-stat-label {
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--mist);
  margin-top: 4px;
}
.location-actions {
  display: flex;
  gap: 10px;
  position: relative;
}

/* --- BUTTONS --- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 12px 24px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
  font-family: inherit;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: var(--snow);
  cursor: pointer;
  transition: all 0.3s var(--ease-out);
  backdrop-filter: blur(12px);
}
.btn:hover {
  background: rgba(255, 255, 255, 0.08);
  transform: translateY(-2px);
}
.btn-primary {
  background: rgba(6, 182, 212, 0.12);
  border-color: rgba(6, 182, 212, 0.3);
  color: var(--noon);
}
.btn-primary:hover {
  background: rgba(6, 182, 212, 0.22);
  box-shadow: 0 8px 24px rgba(6, 182, 212, 0.2);
}

/* --- WARNING NOTICE --- */
.notice {
  padding: 16px 28px;
  border-radius: 999px;
  border: 1px solid rgba(245, 158, 11, 0.15);
  background: rgba(245, 158, 11, 0.03);
  color: var(--watch);
  font-size: 13px;
  font-weight: 600;
  text-align: center;
  margin: 32px 0;
  backdrop-filter: blur(12px);
}

/* --- SHARE PANEL --- */
.share-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
}
.share-text {
  width: 100%;
  min-height: 130px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: var(--radius-md);
  background: rgba(3, 7, 18, 0.5);
  color: var(--snow);
  padding: 16px;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.6;
  resize: none;
}

/* --- TRUST ACCURACY BAR --- */
.trust-bar {
  height: 6px;
  border-radius: 999px;
  background: rgba(255,255,255,0.06);
  overflow: hidden;
  margin-top: 16px;
}
.trust-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--noon), var(--safe));
  transition: width 1.5s var(--ease-out);
}

/* --- FOOTER --- */
.footer {
  padding: 80px 0 48px;
  text-align: center;
  border-top: 1px solid rgba(255, 255, 255, 0.03);
}
.footer-brand {
  font-family: var(--font-display);
  font-size: 32px;
  font-weight: 900;
  letter-spacing: -0.04em;
  background: linear-gradient(135deg, var(--white), var(--mist));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin-bottom: 14px;
}
.footer-version {
  font-size: 12px;
  color: var(--mist);
}
.footer-links {
  display: flex;
  gap: 20px;
  justify-content: center;
  flex-wrap: wrap;
  margin-top: 24px;
}
.footer-link {
  font-size: 13px;
  color: var(--noon);
  font-weight: 600;
  opacity: 0.7;
  transition: opacity 0.3s ease;
}
.footer-link:hover { opacity: 1; }

/* --- 3-DAY AND OTHER LAYOUTS --- */
.day-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 24px;
}
.day-overview-card {
  border-radius: var(--radius-xl);
  padding: var(--card-pad);
  background: rgba(10, 20, 38, 0.45);
  border: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(28px);
  transition: all 0.4s var(--ease-out);
}
.day-overview-card:hover {
  transform: translateY(-6px);
  box-shadow: 0 15px 40px rgba(0,0,0,0.3);
}

/* --- DATA SOURCES TABLE --- */
.source-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0 8px;
}
.source-table th {
  text-align: left;
  padding: 10px 16px;
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--noon);
}
.source-table td {
  padding: 16px;
  font-size: 14px;
  background: rgba(255, 255, 255, 0.01);
  border-top: 1px solid rgba(255,255,255,0.03);
  border-bottom: 1px solid rgba(255,255,255,0.03);
}
.source-table tr td:first-child { border-radius: var(--radius-sm) 0 0 var(--radius-sm); border-left: 1px solid rgba(255,255,255,0.03); }
.source-table tr td:last-child { border-radius: 0 var(--radius-sm) var(--radius-sm) 0; border-right: 1px solid rgba(255,255,255,0.03); }
.source-table tr:hover td { background: rgba(255, 255, 255, 0.03); }

/* --- SEPARATOR DIVIDER --- */
.divider {
  width: 80px;
  height: 2px;
  background: linear-gradient(90deg, var(--noon), transparent);
  margin: 64px auto;
  opacity: 0.4;
}

/* --- MOBILE BOTTOM SHEET --- */
.mobile-bottom-sheet { display: none; }

/* ============================================================
   ADAPTIVE RESPONSIVE BREAKPOINT SYSTEM (6-Tier)
   ============================================================ */

/* --- WIDE DESKTOP (≥1536px) — cinematic spacing, max-width cap --- */
@media (min-width: 1536px) {
  :root { --container-max: 1440px; --section-pad: 100px; }
  .hero { padding: 100px 40px 120px; }
  .command-center { gap: 40px; }
  .section-header { margin-bottom: 56px; }
}

/* --- DESKTOP (1280–1535px) — default layout, unchanged --- */
/* --- DESKTOP (1280–1535px) — default layout --- */

/* --- LAPTOP (1024–1279px) — 2-col dominant map --- */
@media (min-width: 1024px) and (max-width: 1279px) {
  .command-center { grid-template-columns: 1fr; gap: 24px; }
  .hero-metric-value { font-size: 32px; }
  .decision-main { padding: 32px; min-height: 240px; }
}

/* --- TABLET (768–1023px) — hybrid layout --- */
@media (min-width: 768px) and (max-width: 1023px) {
  :root { --nav-height: 64px; }
  .command-center { grid-template-columns: 1fr; }
  .decision { grid-template-columns: 1fr; }
  .kpi-grid { grid-template-columns: 1fr 1fr; }
  .share-grid { grid-template-columns: 1fr; }
  .hero { min-height: auto; padding: 48px 24px 64px; }
  .hero-metric-value { font-size: 30px; }
  .hero-scroll { display: none; }
  .section-header { margin-bottom: 36px; }
  .decision-main { padding: 28px; }
  .location-card { min-height: 240px; }
  .divider { margin: 40px auto; }
  .footer { padding: 48px 0 32px; }
  .footer-brand { font-size: 26px; }
}

/* --- MOBILE LARGE (480–767px) — single column premium --- */
@media (max-width: 767px) {
  :root { --nav-height: 60px; --container-pad: 16px; --section-pad: 32px; --card-pad: 20px; }

  /* Navbar: hamburger menu */
  .nav-toggle { display: flex; }
  .nav-links {
    position: fixed;
    top: var(--nav-height);
    left: 0;
    right: 0;
    background: rgba(3, 7, 18, 0.95);
    backdrop-filter: blur(28px);
    -webkit-backdrop-filter: blur(28px);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    flex-direction: column;
    padding: 16px;
    gap: 8px;
    transform: translateY(-110%);
    transition: transform 0.35s var(--ease-out);
    z-index: 999;
  }
  .nav-links.open {
    transform: translateY(0);
  }
  .nav-link {
    padding: 14px 20px;
    font-size: 15px;
    border-radius: var(--radius-md);
    text-align: center;
    min-height: 44px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .nav-sub { display: none; }
  .nav-title { font-size: 18px; }

  /* Hero: compact */
  .hero { padding: 24px 16px 40px; min-height: auto; }
  .hero-title { font-size: clamp(32px, 8vw, 42px); }
  .hero-subtitle { font-size: 15px; margin-bottom: 24px; }
  .hero-metrics { gap: 12px; }
  .hero-metric { min-width: 0; flex: 1 1 100%; }
  .hero-metric-value { font-size: 28px; }
  .hero-metric-label { font-size: var(--font-label); }
  .hero-scroll { display: none; }
  .hero-glow-1, .hero-glow-2, .hero-glow-3 { opacity: 0.12; }

  /* Decision */
  .decision { grid-template-columns: 1fr; gap: 16px; }
  .decision-main { padding: 24px; min-height: auto; }
  .decision-title { font-size: clamp(22px, 5vw, 32px); }
  .decision-desc { font-size: 14px; }

  /* KPI */
  .kpi-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .kpi-card { padding: 16px; }
  .kpi-value { font-size: 22px; }
  .kpi-label { font-size: var(--font-label); }

  /* Map */
  .command-center { grid-template-columns: 1fr; gap: 20px; }

  /* Hourly rows — mobile card format */
  .hour-row {
    grid-template-columns: 60px 1fr;
    gap: 6px;
    padding: 12px 14px;
  }
  .hour-box { display: none; }
  .hour-box.hour-box-main {
    display: flex;
    grid-column: 1 / -1;
    gap: 8px;
    flex-wrap: wrap;
  }
  .hour-box.hour-box-rain { display: block; grid-column: 1 / -1; }
  .hourly-list { padding: 0 16px 16px; }

  /* Layer controls — horizontal scroll chips */
  .layer-controls {
    flex-wrap: nowrap;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    padding-bottom: 4px;
  }
  .layer-controls::-webkit-scrollbar { display: none; }
  .layer-btn { flex-shrink: 0; padding: 10px 16px; font-size: 11px; }

  /* Timeline — scrollable */
  .day-tabs-container { margin: 0 auto 32px; }
  .timeline-progress-container {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    padding: 8px 0;
  }
  .timeline-progress-container::-webkit-scrollbar { display: none; }
  .timeline-node {
    min-width: 44px;
    min-height: 44px;
    padding: 8px;
  }
  .timeline-progress-bar, .timeline-progress-fill { display: none; }

  /* Charts */
  .chart-wrapper { padding: 20px; }
  .chart-chip { padding: 8px 14px; font-size: 11px; min-height: 36px; }

  /* Cards */
  .location-card { min-height: auto; }
  .location-stats { grid-template-columns: repeat(3, 1fr); gap: 6px; }
  .location-stat { padding: 8px; }
  .location-stat-value { font-size: 15px; }

  /* Section headers */
  .section-header { margin-bottom: 24px; }
  .section-overline { font-size: 10px; letter-spacing: 0.15em; }

  /* Share */
  .share-grid { grid-template-columns: 1fr; gap: 16px; }

  /* Glass card */
  .glass { padding: var(--card-pad); }
  .glass:hover { transform: none; }

  /* Notice */
  .notice { padding: 14px 20px; border-radius: var(--radius-lg); font-size: 12px; }

  /* Footer */
  .footer { padding: 32px 0 24px; }
  .footer-brand { font-size: 22px; }
  .footer-links { gap: 12px; }

  /* Divider */
  .divider { margin: 32px auto; }

  /* Mobile Bottom Sheet */
  .mobile-bottom-sheet {
    display: block;
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    z-index: 2000;
    background: rgba(7, 10, 22, 0.95);
    backdrop-filter: blur(32px);
    -webkit-backdrop-filter: blur(32px);
    border-top: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 28px 28px 0 0;
    transform: translateY(100%);
    transition: transform 0.4s var(--ease-out);
    padding: 24px;
    max-height: 75vh;
    overflow-y: auto;
    box-shadow: 0 -10px 40px rgba(0,0,0,0.5);
    -webkit-overflow-scrolling: touch;
  }
  .mobile-bottom-sheet.open { transform: translateY(0); }
  .mobile-bottom-sheet-handle {
    width: 44px;
    height: 4px;
    border-radius: 2px;
    background: rgba(255, 255, 255, 0.2);
    margin: -10px auto 20px;
    cursor: pointer;
    min-height: 20px;
    padding: 8px 0;
  }

  /* Disable hover lift effects on touch */
  .period-card:hover,
  .activity-card:hover,
  .day-overview-card:hover,
  .location-card:hover,
  .hero-metric:hover {
    transform: none;
  }
}

/* --- MOBILE SMALL (≤479px) — compact everything --- */
@media (max-width: 479px) {
  :root { --nav-height: 56px; --card-pad: 16px; }
  .nav-title { font-size: 16px; }
  .nav-logo { width: 30px; height: 30px; border-radius: 10px; }
  .hero-title { font-size: clamp(28px, 7vw, 36px); line-height: 1.05; }
  .hero-subtitle { font-size: 14px; }
  .hero-metric-value { font-size: 24px; }
  .hero-label { font-size: 11px; padding: 6px 14px; }
  .kpi-grid { grid-template-columns: 1fr 1fr; }
  .kpi-value { font-size: 20px; }
  .period-stat-value { font-size: 16px; }
  .period-condition { font-size: 17px; }
  .activity-status { font-size: 18px; }
  .hour-time { font-size: 18px; }
  .location-name { font-size: 20px; }
  .location-stat-value { font-size: 14px; }
  .chart-chip { padding: 6px 10px; font-size: 10px; }
  .source-table { font-size: 12px; }
  .source-table td { padding: 10px; }
  .btn { padding: 10px 18px; font-size: 12px; min-height: 44px; }
}

/* --- MOTION ADAPTATION --- */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
  .reveal { opacity: 1; transform: none; }
  .hero-glow, .hero-glow-1, .hero-glow-2, .hero-glow-3 { display: none; }
  .atmo, #atmo-canvas { display: none; }
  .hero-scroll { display: none; }
  .pulse-ring { display: none; }
}

/* --- PRINT STYLES --- */
@media print {
  .nav-bar, .atmo, .hero-scroll, .mobile-bottom-sheet,
  .layer-controls, .map-wrapper, .footer { display: none; }
  body { background: #fff; color: #000; }
  .glass { background: #f5f5f5; border: 1px solid #ddd; backdrop-filter: none; }
}
'''


# ---------------------------------------------------------------------------
# v65 CINEMATIC JAVASCRIPT
# ---------------------------------------------------------------------------

JS_V65 = r'''
(function(){
  'use strict';

  // Retrieve dynamic configuration injected from backend engine
  const config = window.LANGIT_CONFIG || {};

  // Safe SessionStorage wrapper to prevent incognito/private mode crashes
  const safeStorage = {
    getItem(key) {
      try { return sessionStorage.getItem(key); } catch (e) { return null; }
    },
    setItem(key, value) {
      try { sessionStorage.setItem(key, value); } catch (e) {}
    }
  };

  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let initialLayer = 'rain';
  const cond = (config.condition || '').toLowerCase();
  if (cond.includes('hujan') || cond.includes('gerimis') || cond.includes('rain') || cond.includes('drizzle')) {
    initialLayer = 'rain';
  } else if (cond.includes('panas') || cond.includes('cerah') || cond.includes('clear') || cond.includes('hot') || cond.includes('sunny')) {
    initialLayer = 'temp';
  } else if (cond.includes('angin') || cond.includes('wind') || cond.includes('storm')) {
    initialLayer = 'wind';
  } else if (cond.includes('awan') || cond.includes('mendung') || cond.includes('cloudy') || cond.includes('overcast')) {
    initialLayer = 'cloud';
  } else if (cond.includes('lembap') || cond.includes('kabut') || cond.includes('fog') || cond.includes('mist') || cond.includes('humidity')) {
    initialLayer = 'humidity';
  }

  // State Management
  const state = {
    activeDayIndex: 0,
    activeLayer: initialLayer,
    hourlyData: null
  };

  // --- Scroll & Reveal System ---
  const revealEls = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    const obs = new IntersectionObserver(function(entries) {
      entries.forEach(function(e) {
        if (e.isIntersecting) {
          e.target.classList.add('visible');
          obs.unobserve(e.target);
        }
      });
    }, { threshold: 0.05, rootMargin: '0px 0px -20px 0px' });
    revealEls.forEach(function(el) { obs.observe(el); });
  } else {
    revealEls.forEach(function(el) { el.classList.add('visible'); });
  }

  // --- Smooth Numerical Counting Transition ---
  function animateValue(obj, start, end, duration, suffix = '', decimals = 0) {
    if (!obj) return;
    if (obj._animId) {
      window.cancelAnimationFrame(obj._animId);
    }
    let startTimestamp = null;
    const step = (timestamp) => {
      if (!startTimestamp) startTimestamp = timestamp;
      const progress = Math.min((timestamp - startTimestamp) / duration, 1);
      const val = progress * (end - start) + start;
      obj.innerHTML = val.toFixed(decimals) + suffix;
      if (progress < 1) {
        obj._animId = window.requestAnimationFrame(step);
      } else {
        obj._animId = null;
      }
    };
    obj._animId = window.requestAnimationFrame(step);
  }

  // Parses value from element content to animate
  function triggerCounters(panel) {
    if (!panel) return;
    panel.querySelectorAll('[data-animate-value]').forEach(el => {
      const targetVal = parseFloat(el.dataset.animateValue);
      const suffix = el.dataset.animateSuffix || '';
      const decimals = parseInt(el.dataset.animateDecimals || '0');
      if (isNaN(targetVal)) return;
      
      const currentVal = parseFloat(el.textContent) || 0;
      animateValue(el, currentVal, targetVal, 800, suffix, decimals);
    });
  }

  // --- Volumetric Canvas Atmospheric Particle System ---
  const atmoCanvas = document.getElementById('atmo-canvas');
  let atmoCtx = null;
  let particles = [];
  let atmoWidth = 0, atmoHeight = 0;
  let animId = null;

  let isAtmoVisible = true;
  if (atmoCanvas && !prefersReducedMotion) {
    atmoCtx = atmoCanvas.getContext('2d');
    resizeAtmo();
    initParticles();
    
    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
          isAtmoVisible = entry.isIntersecting;
          if (isAtmoVisible && !animId) {
            animLoop();
          } else if (!isAtmoVisible && animId) {
            cancelAnimationFrame(animId);
            animId = null;
          }
        });
      }, { threshold: 0.01 });
      observer.observe(atmoCanvas);
    } else {
      animLoop();
    }
  }

  function resizeAtmo() {
    atmoWidth = atmoCanvas.width = window.innerWidth;
    atmoHeight = atmoCanvas.height = window.innerHeight;
  }

  // Viewport-aware particle count for performance
  function getParticleCount(layer) {
    const baseCounts = { rain: 80, cloud: 15, wind: 40, temp: 40, humidity: 30 };
    const base = baseCounts[layer] || 40;
    const w = window.innerWidth;
    if (w <= 479) return Math.round(base * 0.2);
    if (w <= 767) return Math.round(base * 0.35);
    if (w <= 1023) return Math.round(base * 0.6);
    return base;
  }

  function initParticles() {
    particles = [];
    const count = getParticleCount(state.activeLayer);

    for (let i = 0; i < count; i++) {
      particles.push(createParticle());
    }
  }

  function createParticle(initBottom = false) {
    const p = {
      x: Math.random() * atmoWidth,
      y: initBottom ? (atmoHeight + 50) : (Math.random() * (atmoHeight + 100) - 50),
      size: 0,
      vx: 0,
      vy: 0,
      alpha: Math.random() * 0.4 + 0.1,
      color: '#ffffff'
    };

    if (state.activeLayer === 'rain') {
      p.size = Math.random() * 1.5 + 0.5;
      p.vy = Math.random() * 12 + 10;
      p.vx = -1.5 - Math.random() * 1;
      p.color = '100, 182, 212';
    } else if (state.activeLayer === 'wind') {
      p.size = Math.random() * 1.2 + 0.5;
      p.vx = Math.random() * 4 + 2;
      p.vy = Math.sin(p.x * 0.005) * 0.5;
      p.color = '168, 196, 224';
      p.trail = [];
    } else if (state.activeLayer === 'cloud') {
      p.size = Math.random() * 80 + 50;
      p.vx = Math.random() * 0.2 + 0.05;
      p.vy = 0;
      p.color = '148, 190, 235';
      p.alpha = Math.random() * 0.08 + 0.02;
    } else if (state.activeLayer === 'temp') {
      p.size = Math.random() * 15 + 5;
      p.vy = -(Math.random() * 1.5 + 0.5);
      p.vx = (Math.random() - 0.5) * 0.5;
      p.color = '249, 115, 22';
      p.alpha = Math.random() * 0.15 + 0.05;
    } else if (state.activeLayer === 'humidity') {
      p.size = Math.random() * 40 + 20;
      p.vy = -(Math.random() * 0.8 + 0.2);
      p.vx = (Math.random() - 0.5) * 0.2;
      p.color = '99, 102, 241';
      p.alpha = Math.random() * 0.1 + 0.02;
    }
    return p;
  }

  function animLoop() {
    if (!atmoCtx || !isAtmoVisible) {
      animId = null;
      return;
    }
    atmoCtx.clearRect(0, 0, atmoWidth, atmoHeight);

    particles.forEach(p => {
      // Move particle
      p.x += p.vx;
      p.y += p.vy;

      if (state.activeLayer === 'wind') {
        p.vy = Math.sin(p.x * 0.005) * 0.4;
        p.trail.push({ x: p.x, y: p.y });
        if (p.trail.length > 15) p.trail.shift();
      }

      // Draw particle
      atmoCtx.beginPath();
      if (state.activeLayer === 'rain') {
        atmoCtx.strokeStyle = 'rgba(' + p.color + ',' + p.alpha + ')';
        atmoCtx.lineWidth = p.size;
        atmoCtx.moveTo(p.x, p.y);
        atmoCtx.lineTo(p.x + p.vx * 1.5, p.y + p.vy * 1.5);
        atmoCtx.stroke();
      } else if (state.activeLayer === 'wind') {
        if (p.trail.length > 1) {
          atmoCtx.beginPath();
          atmoCtx.moveTo(p.trail[0].x, p.trail[0].y);
          for (let k = 1; k < p.trail.length; k++) {
            atmoCtx.lineTo(p.trail[k].x, p.trail[k].y);
          }
          atmoCtx.strokeStyle = 'rgba(' + p.color + ',' + p.alpha + ')';
          atmoCtx.lineWidth = p.size;
          atmoCtx.stroke();
        }
      } else if (state.activeLayer === 'cloud' || state.activeLayer === 'temp' || state.activeLayer === 'humidity') {
        const rad = atmoCtx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.size);
        rad.addColorStop(0, 'rgba(' + p.color + ',' + p.alpha + ')');
        rad.addColorStop(1, 'rgba(' + p.color + ',0)');
        atmoCtx.fillStyle = rad;
        atmoCtx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        atmoCtx.fill();
      }

      // Boundary wraps
      if (state.activeLayer === 'rain') {
        if (p.y > atmoHeight + 20 || p.x < -20) {
          Object.assign(p, createParticle());
          p.y = -20;
        }
      } else if (state.activeLayer === 'wind' || state.activeLayer === 'cloud') {
        if (p.x > atmoWidth + p.size) {
          Object.assign(p, createParticle());
          p.x = -p.size;
        }
      } else if (state.activeLayer === 'temp' || state.activeLayer === 'humidity') {
        if (p.y < -p.size || p.x < -p.size || p.x > atmoWidth + p.size) {
          Object.assign(p, createParticle(true));
        }
      }
    });

    animId = window.requestAnimationFrame(animLoop);
  }

  // --- Dynamic Color Shifting Body Themes ---
  function shiftAtmosphereTheme(relativeDay) {
    document.body.className = '';
    const day = relativeDay.toLowerCase();
    
    // Choose theme based on time of day context or weather risk
    if (day.includes('hari') || day.includes('today')) {
      document.body.classList.add('theme-noon'); // Noon cyan for today
    } else if (day.includes('besok') || day.includes('tomorrow')) {
      document.body.classList.add('theme-dusk'); // Violet dusk for tomorrow
    } else {
      document.body.classList.add('theme-dawn'); // Orange dawn for lusa
    }
  }

  // --- Interactive Curved SVG Bezier Chart Switcher ---
  const chartContainers = document.querySelectorAll('.chart-wrapper');
  chartContainers.forEach(container => {
    const svgCont = container.querySelector('.chart-svg-container');
    if (!svgCont) return;
    const rawData = svgCont.dataset.points;
    if (!rawData) return;
    let hours = [];
    try {
      hours = JSON.parse(rawData);
    } catch(err) {
      return;
    }
    
    let activeVar = 'rain'; // rain, temp, humidity, wind
    const chips = container.querySelectorAll('.chart-chip');
    const svg = container.querySelector('.rain-curve');
    const tooltip = container.querySelector('.chart-tooltip');
    
    function drawChart() {
      if (!svg || !hours.length) return;
      const w = svg.clientWidth || 900;
      const h = svg.clientHeight || 200;
      const padX = w < 400 ? 25 : w < 700 ? 35 : 50;
      const padTop = w < 400 ? 20 : 30;
      const padBot = w < 400 ? 30 : 40;
      const innerW = w - 2 * padX;
      const innerH = h - padTop - padBot;
      const n = hours.length;

      // Determine Variable Metadata
      let getter = x => 0;
      let unit = '';
      let strokeColor = '#06b6d4';
      let fillGradientId = 'noonChartGrad';
      let title = '';

      if (activeVar === 'rain') {
        getter = x => parseFloat(x.rain_probability || 0);
        unit = '%';
        strokeColor = '#06b6d4'; // Cyan
        fillGradientId = 'rainChartGrad';
      } else if (activeVar === 'temp') {
        getter = x => parseFloat(x.temp_c || 0);
        unit = '°C';
        strokeColor = '#f97316'; // Orange
        fillGradientId = 'tempChartGrad';
      } else if (activeVar === 'humidity') {
        getter = x => parseFloat(x.humidity_pct || 0);
        unit = '%';
        strokeColor = '#6366f1'; // Indigo
        fillGradientId = 'humidityChartGrad';
      } else if (activeVar === 'wind') {
        getter = x => parseFloat(x.wind_kmh || 0);
        unit = ' km/j';
        strokeColor = '#10b981'; // Green
        fillGradientId = 'windChartGrad';
      }

      const vals = hours.map(getter);
      const minVal = activeVar === 'temp' ? Math.min(...vals, 15) - 2 : 0;
      const maxVal = Math.max(...vals, activeVar === 'temp' ? 30 : 100) || 1;
      const valRange = maxVal - minVal;

      const points = hours.map((hr, i) => {
        const x = padX + (i / Math.max(1, n - 1)) * innerW;
        const y = padTop + innerH - ((getter(hr) - minVal) / valRange) * innerH;
        return { x, y, hr, val: getter(hr) };
      });

      // SVG path helpers
      function bezierPath(pts) {
        if (pts.length < 2) return '';
        let d = 'M ' + pts[0].x.toFixed(1) + ',' + pts[0].y.toFixed(1);
        if (pts.length === 2) {
          d += ' L ' + pts[1].x.toFixed(1) + ',' + pts[1].y.toFixed(1);
          return d;
        }
        for (let i = 0; i < pts.length - 1; i++) {
          const p0 = pts[Math.max(0, i - 1)];
          const p1 = pts[i];
          const p2 = pts[Math.min(pts.length - 1, i + 1)];
          const p3 = pts[Math.min(pts.length - 1, i + 2)];
          
          const cp1x = p1.x + (p2.x - p0.x) / 6;
          const cp1y = p1.y + (p2.y - p0.y) / 6;
          const cp2x = p2.x - (p3.x - p1.x) / 6;
          const cp2y = p2.y - (p3.y - p1.y) / 6;
          
          d += ' C ' + cp1x.toFixed(1) + ',' + cp1y.toFixed(1) + ' ' +
               cp2x.toFixed(1) + ',' + cp2y.toFixed(1) + ' ' +
               p2.x.toFixed(1) + ',' + p2.y.toFixed(1);
        }
        return d;
      }

      const linePathD = bezierPath(points);
      const fillPathD = linePathD + ' L ' + points[points.length-1].x.toFixed(1) + ',' + (h - padBot).toFixed(1) + ' L ' + points[0].x.toFixed(1) + ',' + (h - padBot).toFixed(1) + ' Z';

      // Draw SVG Elements
      svg.innerHTML = `
        <defs>
          <linearGradient id="rainChartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#06b6d4" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#06b6d4" stop-opacity="0"/>
          </linearGradient>
          <linearGradient id="tempChartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#f97316" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#f97316" stop-opacity="0"/>
          </linearGradient>
          <linearGradient id="humidityChartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#6366f1" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#6366f1" stop-opacity="0"/>
          </linearGradient>
          <linearGradient id="windChartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#10b981" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#10b981" stop-opacity="0"/>
          </linearGradient>
        </defs>
        
        <!-- Y Grid Lines -->
        <line x1="${padX}" y1="${padTop}" x2="${w - padX}" y2="${padTop}" stroke="rgba(255,255,255,0.03)" stroke-width="1"/>
        <line x1="${padX}" y1="${padTop + innerH/2}" x2="${w - padX}" y2="${padTop + innerH/2}" stroke="rgba(255,255,255,0.03)" stroke-width="1"/>
        <line x1="${padX}" y1="${h - padBot}" x2="${w - padX}" y2="${h - padBot}" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>
        
        <!-- Main Area Grads & Line -->
        <path class="curve-fill" d="${fillPathD}" fill="url(#${fillGradientId})"/>
        <path class="curve-line" d="${linePathD}" stroke="${strokeColor}"/>
      `;

      // Draw interactive dots and text labels
      points.forEach(p => {
        const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        dot.setAttribute('class', 'curve-dot');
        dot.setAttribute('cx', p.x.toFixed(1));
        dot.setAttribute('cy', p.y.toFixed(1));
        dot.setAttribute('r', '5');
        dot.setAttribute('fill', strokeColor);
        dot.addEventListener('mouseenter', (ev) => showTooltip(ev, p, unit));
        dot.addEventListener('mouseleave', hideTooltip);
        svg.appendChild(dot);

        const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        label.setAttribute('class', 'curve-label');
        label.setAttribute('x', p.x.toFixed(1));
        label.setAttribute('y', (h - 8).toString());
        label.textContent = p.hr.hour;
        svg.appendChild(label);

        // Conditional numeric displays
        if (p.val > 0 || activeVar === 'temp') {
          const valTxt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          valTxt.setAttribute('class', 'curve-value');
          valTxt.setAttribute('x', p.x.toFixed(1));
          valTxt.setAttribute('y', Math.max(15, p.y - 12).toFixed(1));
          valTxt.textContent = Math.round(p.val) + unit;
          svg.appendChild(valTxt);
        }
      });
    }

    function showTooltip(ev, p, unit) {
      if (!tooltip) return;
      const x = p.x;
      const y = p.y;
      
      tooltip.querySelector('.chart-tooltip-time').textContent = 'Pukul ' + p.hr.hour + ' WIB';
      tooltip.querySelector('.chart-tooltip-value').textContent = p.val.toFixed(decimalsForVar()) + unit + ' · ' + (p.hr.condition || '-');
      
      tooltip.style.opacity = '1';
      tooltip.style.transform = 'translate(' + (x - 70) + 'px, ' + (y - 75) + 'px)';
    }

    function hideTooltip() {
      if (tooltip) tooltip.style.opacity = '0';
    }

    function decimalsForVar() {
      return activeVar === 'temp' ? 1 : 0;
    }

    chips.forEach(chip => {
      chip.addEventListener('click', () => {
        chips.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        activeVar = chip.dataset.var;
        
        // Sync particle engine state to the matching visualization
        if (activeVar === 'rain') state.activeLayer = 'rain';
        else if (activeVar === 'temp') state.activeLayer = 'temp';
        else if (activeVar === 'humidity') state.activeLayer = 'humidity';
        else if (activeVar === 'wind') state.activeLayer = 'wind';
        initParticles();

        // Sync active class on layer control buttons
        document.querySelectorAll('.layer-btn').forEach(b => {
          if (b.dataset.layer === state.activeLayer) {
            b.classList.add('active');
          } else {
            b.classList.remove('active');
          }
        });

        drawChart();
      });
    });

    drawChart();
    let chartResizeTimeout;
    window.addEventListener('resize', () => {
      clearTimeout(chartResizeTimeout);
      chartResizeTimeout = setTimeout(drawChart, 100);
    }, { passive: true });
  });

  // --- Dynamic Weather Stepper Timeline Navigation ---
  const timelineNodes = document.querySelectorAll('.timeline-node');
  const fillBar = document.querySelector('.timeline-progress-fill');
  
  function updateTimelineProgress() {
    if (!timelineNodes.length || !fillBar) return;
    let activeIdx = 0;
    timelineNodes.forEach((node, idx) => {
      if (node.classList.contains('active')) {
        activeIdx = idx;
      }
    });
    
    const pct = (activeIdx / (timelineNodes.length - 1)) * 100;
    fillBar.style.width = pct + '%';
  }

  timelineNodes.forEach((node, index) => {
    node.addEventListener('click', () => {
      var container = node.closest('.day-tabs-container');
      if (!container) return;
      
      container.querySelectorAll('.timeline-node').forEach(t => t.classList.remove('active'));
      node.classList.add('active');
      
      // Update color scheme of the body gradient
      const relLabel = node.querySelector('.timeline-node-label').textContent;
      shiftAtmosphereTheme(relLabel);
      
      // Hide and show day panels
      const targetId = node.dataset.target;
      const targetPanel = document.getElementById(targetId);
      if (targetPanel) {
        document.querySelectorAll('.day-panel').forEach(p => p.classList.remove('active'));
        targetPanel.classList.add('active');
        triggerCounters(targetPanel);
      }
      
      // Notify map frames to update layout when tabs change
      document.querySelectorAll('.map-frame').forEach(iframe => {
        if (iframe.contentWindow) {
          iframe.contentWindow.postMessage({ type: 'invalidateSize' }, '*');
        }
      });
      
      updateTimelineProgress();
    });
  });

  updateTimelineProgress();

  // --- Dynamic Particle Layer Switchers ---
  document.querySelectorAll('.layer-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.layer-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeLayer = btn.dataset.layer;
      initParticles();

      // Sync active chart parameter chip
      document.querySelectorAll('.chart-wrapper').forEach(wrapper => {
        const chips = wrapper.querySelectorAll('.chart-chip');
        chips.forEach(chip => {
          if (chip.dataset.var === state.activeLayer) {
            if (!chip.classList.contains('active')) {
              chip.click();
            }
          }
        });
      });

      // Sync the embedded map iframe layer
      const mapFrame = document.querySelector('.map-frame');
      if (mapFrame && mapFrame.contentWindow) {
        mapFrame.contentWindow.postMessage({ type: 'switchLayer', layer: state.activeLayer }, '*');
      }
    });
  });

  // Align active classes on DOM Load
  document.addEventListener('DOMContentLoaded', () => {
    // Update active class on layer buttons
    document.querySelectorAll('.layer-btn').forEach(btn => {
      if (btn.dataset.layer === state.activeLayer) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });

    // Update active class on chart chips
    document.querySelectorAll('.chart-wrapper').forEach(wrapper => {
      const chips = wrapper.querySelectorAll('.chart-chip');
      chips.forEach(chip => {
        if (chip.dataset.var === state.activeLayer) {
          chip.classList.add('active');
        } else {
          chip.classList.remove('active');
        }
      });
    });
  });

  // --- Mobile Bottom Sheet Modal ---
  document.querySelectorAll('.day-panel').forEach((panel) => {
    const sheet = panel.querySelector('.mobile-bottom-sheet');
    const sheetTrigger = panel.querySelector('.hourly-toggle');
    
    if (sheet && sheetTrigger) {
      sheetTrigger.addEventListener('click', (ev) => {
        // If mobile, prevent default details collapse and show sliding bottom sheet instead
        if (window.innerWidth < 768) {
          ev.preventDefault();
          sheet.classList.add('open');
        }
      });
      
      const handle = sheet.querySelector('.mobile-bottom-sheet-handle');
      if (handle) {
        handle.addEventListener('click', () => {
          sheet.classList.remove('open');
        });
      }
    }
  });

  // --- Apple Vision Pro Spatial Coordinates Zoom Scanner ---
  function runSpatialZoomScanner() {
    const isMainPage = document.querySelector('.hero') !== null;
    const sessionKey = 'langit_v65_spatial_scanner_run';
    
    if (!isMainPage || safeStorage.getItem(sessionKey) || prefersReducedMotion) {
      // Clean up scanner markup and display page immediately if already run or reduced motion
      const overlay = document.getElementById('spatial-overlay');
      if (overlay) overlay.remove();
      return;
    }

    // Insert overlay markup dynamically if not in HTML
    let overlay = document.getElementById('spatial-overlay');
    if (!overlay) {
      const activeLocName = config.location_name || 'ITB Bandung';
      overlay = document.createElement('div');
      overlay.id = 'spatial-overlay';
      overlay.innerHTML = `
        <canvas id="spatial-canvas"></canvas>
        <div class="spatial-hud">
          <div class="spatial-title">MENYIAPKAN DATA CUACA</div>
          <div class="spatial-status" id="spatial-status">MEMUAT DATA PRAKIRAAN...</div>
        </div>
        <button class="spatial-skip">Lewati</button>
      `;
      document.body.appendChild(overlay);
    }

    const canvas = document.getElementById('spatial-canvas');
    const statusText = document.getElementById('spatial-status');
    const skipBtn = overlay.querySelector('.spatial-skip');
    
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let width = canvas.width = window.innerWidth;
    let height = canvas.height = window.innerHeight;
    
    window.addEventListener('resize', () => {
      width = canvas.width = window.innerWidth;
      height = canvas.height = window.innerHeight;
    }, { passive: true });

    let scanProgress = 0;
    let frame = 0;
    
    const telemetrySteps = [
      { p: 0.0, txt: 'Menghubungkan ke satelit cuaca...' },
      { p: 0.2, txt: 'Telemetri aman. Memetakan orbit...' },
      { p: 0.45, txt: 'Menginisialisasi pencarian koordinat wilayah Indonesia...' },
      { p: 0.7, txt: 'Memindai wilayah: ' + (config.location_name || 'ITB').toUpperCase() },
      { p: 0.9, txt: 'Koordinat terkunci: Kampus ' + (config.location_name || 'ITB').toUpperCase() }
    ];

    let requestID = null;

    function drawScanner() {
      frame++;
      scanProgress += 0.014;
      if (scanProgress >= 1.0) scanProgress = 1.0;

      // Draw High Tech background grid
      ctx.fillStyle = '#030712';
      ctx.fillRect(0, 0, width, height);

      // Star particles
      ctx.fillStyle = 'rgba(255, 255, 255, 0.2)';
      for (let i = 0; i < 50; i++) {
        const sx = (Math.sin(i * 123.4) * 0.5 + 0.5) * width;
        const sy = (Math.cos(i * 567.8) * 0.5 + 0.5) * height;
        ctx.fillRect(sx, sy, 1.5, 1.5);
      }

      // Globe Grid sequence
      const centerX = width / 2;
      const centerY = height / 2;
      const radiusBase = Math.min(width, height) * 0.35;
      
      // Zoom factor increases over time
      const zoom = 1 + Math.pow(scanProgress, 2.5) * 8;
      const currentRadius = radiusBase * zoom;

      // Update HUD telemetry
      const step = telemetrySteps.find((s, idx) => {
        const next = telemetrySteps[idx + 1];
        return scanProgress >= s.p && (!next || scanProgress < next.p);
      });
      if (step) {
        statusText.textContent = step.txt;
      }

      ctx.strokeStyle = 'rgba(6, 182, 212, ' + (1 - scanProgress * 0.8) + ')';
      ctx.lineWidth = 1;

      if (scanProgress < 0.7) {
        // Draw rotating sphere wireframe (Globe Search)
        const rotY = frame * 0.012;
        ctx.beginPath();
        ctx.arc(centerX, centerY, currentRadius, 0, Math.PI * 2);
        ctx.stroke();

        // Latitude / Longitude lines
        const lines = 8;
        for (let j = 0; j < lines; j++) {
          // Horizontal slices
          const yOffset = Math.sin((j / lines - 0.5) * Math.PI) * currentRadius;
          const sliceRad = Math.cos((j / lines - 0.5) * Math.PI) * currentRadius;
          ctx.beginPath();
          ctx.ellipse(centerX, centerY + yOffset, sliceRad, sliceRad * 0.25, 0, 0, Math.PI * 2);
          ctx.stroke();

          // Vertical slices
          ctx.beginPath();
          ctx.ellipse(centerX, centerY, currentRadius * 0.25, currentRadius, rotY + (j / lines) * Math.PI, 0, Math.PI * 2);
          ctx.stroke();
        }

        // Radar target sweeps
        ctx.beginPath();
        ctx.moveTo(centerX, centerY);
        ctx.arc(centerX, centerY, currentRadius, rotY, rotY + 0.5);
        ctx.fillStyle = 'rgba(6, 182, 212, 0.03)';
        ctx.fill();
      } else {
        // Spatial coordinates mapping focus
        ctx.lineWidth = 1.5;
        const opacity = 1 - (scanProgress - 0.7) / 0.3;
        ctx.strokeStyle = 'rgba(16, 185, 129, ' + opacity + ')'; // green lock-on color

        // Focused radar rings
        ctx.beginPath();
        ctx.arc(centerX, centerY, 50, 0, Math.PI * 2);
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(centerX, centerY, 150 + Math.sin(frame * 0.1) * 8, 0, Math.PI * 2);
        ctx.stroke();

        // Crosshairs
        ctx.beginPath();
        ctx.moveTo(centerX - 250, centerY);
        ctx.lineTo(centerX - 30, centerY);
        ctx.moveTo(centerX + 30, centerY);
        ctx.lineTo(centerX + 250, centerY);
        ctx.moveTo(centerX, centerY - 200);
        ctx.lineTo(centerX, centerY - 30);
        ctx.moveTo(centerX, centerY + 30);
        ctx.lineTo(centerX, centerY + 200);
        ctx.stroke();

        // Retrieve coordinates from window config
        const lat = config.latitude !== undefined && config.latitude !== null ? config.latitude : -6.8830;
        const lon = config.longitude !== undefined && config.longitude !== null ? config.longitude : 107.6130;
        
        const alt = config.altitude !== undefined && config.altitude !== null ? config.altitude : '832';

        const latSign = lat < 0 ? 'S' : 'N';
        const lonSign = lon < 0 ? 'W' : 'E';

        ctx.fillStyle = 'rgba(255, 255, 255, ' + opacity * 0.8 + ')';
        ctx.font = '10px monospace';
        ctx.fillText('LAT: ' + Math.abs(lat).toFixed(4) + '° ' + latSign, centerX + 60, centerY - 40);
        ctx.fillText('LON: ' + Math.abs(lon).toFixed(4) + '° ' + lonSign, centerX + 60, centerY - 20);
        ctx.fillText('ALT: ' + alt + 'm [MSL]', centerX + 60, centerY);
        ctx.fillText('BEARING: 122.4° SE', centerX + 60, centerY + 20);
      }

      if (scanProgress < 1.0) {
        requestID = window.requestAnimationFrame(drawScanner);
      } else {
        closeScanner();
      }
    }

    function closeScanner() {
      if (requestID) window.cancelAnimationFrame(requestID);
      safeStorage.setItem(sessionKey, 'true');
      overlay.style.opacity = '0';
      overlay.style.transform = 'scale(1.05)';
      setTimeout(() => {
        overlay.remove();
        // Trigger page counter reveals
        const activePanel = document.querySelector('.day-panel.active');
        if (activePanel) triggerCounters(activePanel);
      }, 800);
    }

    skipBtn.addEventListener('click', closeScanner);
    drawScanner();
  }

  // Auto-run Spatial Scan overlay sequence on launch
  runSpatialZoomScanner();

  // --- Hamburger Menu Toggle ---
  const navToggle = document.querySelector('.nav-toggle');
  const navLinks = document.querySelector('.nav-links');
  if (navToggle && navLinks) {
    navToggle.addEventListener('click', () => {
      const isOpen = navLinks.classList.toggle('open');
      navToggle.setAttribute('aria-expanded', isOpen.toString());
    });
    // Close menu when clicking a nav link
    navLinks.querySelectorAll('.nav-link').forEach(link => {
      link.addEventListener('click', () => {
        navLinks.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
      });
    });
    // Close menu when clicking outside
    document.addEventListener('click', (e) => {
      if (!navToggle.contains(e.target) && !navLinks.contains(e.target)) {
        navLinks.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // --- Debounced Resize Handler ---
  let resizeTimer;
  function handleResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      // Recalculate atmospheric canvas
      if (atmoCanvas && atmoCtx) {
        resizeAtmo();
        initParticles();
      }
      // Redraw all charts
      document.querySelectorAll('.chart-wrapper').forEach(wrapper => {
        const chips = wrapper.querySelectorAll('.chart-chip');
        const activeChip = wrapper.querySelector('.chart-chip.active');
        if (activeChip) activeChip.click();
      });
      // Close mobile nav if viewport expanded
      if (window.innerWidth >= 768 && navLinks) {
        navLinks.classList.remove('open');
        if (navToggle) navToggle.setAttribute('aria-expanded', 'false');
      }
    }, 150);
  }
  window.addEventListener('resize', handleResize, { passive: true });

  // --- Orientation Change Handler ---
  window.addEventListener('orientationchange', () => {
    setTimeout(() => {
      if (atmoCanvas && atmoCtx) {
        resizeAtmo();
        initParticles();
      }
      // Notify embedded map iframe to invalidate size
      const mapFrame = document.querySelector('.map-frame');
      if (mapFrame && mapFrame.contentWindow) {
        mapFrame.contentWindow.postMessage({ type: 'invalidateSize' }, '*');
      }
    }, 300);
  });

})();
'''



# ---------------------------------------------------------------------------
# v65 HTML Components
# ---------------------------------------------------------------------------

def v65_nav(api: Dict[str, Any], active: str, root: bool = False) -> str:
    if root:
        items = [("Lokasi", "index.html", "locations"), ("Peta", "langit_portal_map.html", "map")]
        subtitle = f"Portal · {VERSION}"
        href = "index.html"
    else:
        items = [("Hari ini", "langit_app.html", "today"), ("3 hari ke depan", "langit_3day.html", "3day"), ("Panduan Aktivitas", "langit_activity.html", "activity"), ("Peta", "langit_map_room.html", "map")]
        subtitle = f'{api["location_name"]} · {VERSION}'
        href = "../index.html"
    links_arr = []
    for label, url, key in items:
        active_cls = "active" if key == active else ""
        aria = ' aria-current="page"' if key == active else ""
        links_arr.append(f'<a class="nav-link {active_cls}"{aria} href="{esc(url)}">{esc(label)}</a>')
    links = "".join(links_arr)
    return f'''<header class="nav-bar">
  <a class="nav-brand" href="{href}">
    <span class="nav-logo"></span>
    <span><span class="nav-title">{BRAND}</span><span class="nav-sub">{esc(subtitle)}</span></span>
  </a>
  <button class="nav-toggle" aria-label="Menu" aria-expanded="false"><span></span><span></span><span></span></button>
  <nav class="nav-links">{links}</nav>
</header>'''


def v65_document(api: Dict[str, Any], active: str, title: str, body: str, root: bool = False) -> str:
    footer_links = ""
    if not root:
        footer_links = '''<div class="footer-links">
      <a class="footer-link" href="keandalan_data.html">Keandalan data</a>
      <a class="footer-link" href="akurasi_data.html">Akurasi</a>
      <a class="footer-link" href="langit_api_v1.json">JSON</a>
      <a class="footer-link" href="langit_location.geojson">GeoJSON</a>
    </div>'''
    
    # Inject dynamic metadata config for client-side spatial scripts
    import json
    slug = api.get("location_slug", "portal")
    alt = 832
    if "dago" in slug:
        alt = 760
    elif "jatinangor" in slug:
        alt = 725
    elif "arjawinangun" in slug:
        alt = 10
    config = {
        "location_name": api.get("location_name", "ITB Bandung"),
        "location_slug": slug,
        "latitude": api.get("latitude"),
        "longitude": api.get("longitude"),
        "altitude": alt,
        "risk_class": api.get("today", {}).get("risk_class", "watch") if api.get("today") else "watch",
        "condition": api.get("today", {}).get("condition", "Berawan") if api.get("today") else "Berawan",
        "is_portal": root
    }
    config_js = f"window.LANGIT_CONFIG = {json.dumps(config, ensure_ascii=False)};"

    return f'''<!doctype html><html lang="id"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="theme-color" content="#030712">
<meta name="description" content="{esc(title)} — Prakiraan cuaca premium untuk Indonesia">
<style>{CSS_V65}</style>
</head><body class="theme-noon">
<div class="atmo">
  <canvas id="atmo-canvas"></canvas>
</div>
{v65_nav(api, active, root=root)}
<main class="page">
{body}
<footer class="footer">
  <div class="container">
    <div class="footer-brand">{BRAND}</div>
    <div class="footer-version">{VERSION} · {esc(api.get("generated_at", fmt_update()))}</div>
    {footer_links}
  </div>
</footer>
</main>
<script>{config_js}</script>
<script>{JS_V65}</script>
</body></html>'''


def v65_hero(api: Dict[str, Any], heading: str, subtitle: str, day: Dict[str, Any], show_metrics: bool = True) -> str:
    cls = day.get("risk_class", "watch")
    metrics = ""
    if show_metrics:
        metrics = f'''<div class="hero-metrics reveal reveal-delay-3">
        <div class="hero-metric">
          <div class="hero-metric-value" data-animate-value="{num(day.get("avg_temp_c"), 0)}" data-animate-suffix="°C" data-animate-decimals="1">{deg(day.get("avg_temp_c"))}</div>
          <div class="hero-metric-label">Suhu rata-rata</div>
        </div>
        <div class="hero-metric">
          <div class="hero-metric-value" data-animate-value="{prob(day.get("peak_rain_probability"), 0)}" data-animate-suffix="%">{pct(day.get("peak_rain_probability"))}</div>
          <div class="hero-metric-label">Puncak hujan</div>
        </div>
        <div class="hero-metric">
          <div class="hero-metric-value" style="color:{risk_color(cls)}">{esc(day.get("risk_label"))}</div>
          <div class="hero-metric-label">Status hari ini</div>
        </div>
      </div>'''
    return f'''<section class="hero">
    <div class="hero-glow hero-glow-1"></div>
    <div class="hero-glow hero-glow-2"></div>
    <div class="hero-glow hero-glow-3"></div>
    <div class="container">
      <div class="hero-label reveal">
        <span class="hero-dot"></span>
        <span>{esc(day.get("date_label", ""))}</span>
      </div>
      <h1 class="hero-title reveal reveal-delay-1">{esc(heading)}</h1>
      <p class="hero-subtitle reveal reveal-delay-2">{esc(subtitle)}</p>
      {metrics}
    </div>
    <div class="hero-scroll">
      <span>Scroll</span>
      <div class="hero-scroll-line"></div>
    </div>
  </section>'''


def v65_notice() -> str:
    return f'<div class="container"><div class="notice reveal">{esc(DISCLAIMER)}</div></div>'


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
        <p class="decision-desc">{esc(day.get("date_label"))}. {esc(rain_phrase(day))}. Jam nyaman: {esc(windows)}.</p>
      </article>
      <aside class="kpi-grid">
        <div class="kpi-card"><div class="kpi-label">Risiko</div><div class="kpi-value" data-animate-value="{round(clamp(day.get("risk_score"))):.0f}" data-animate-suffix="/100">{round(clamp(day.get("risk_score"))):.0f}<span style="font-size:16px;color:var(--mist)">/100</span></div><div class="kpi-sub">{esc(day.get("risk_label"))}</div></div>
        <div class="kpi-card"><div class="kpi-label">Puncak hujan</div><div class="kpi-value" data-animate-value="{prob(day.get("peak_rain_probability"), 0)}" data-animate-suffix="%">{pct(day.get("peak_rain_probability"))}</div><div class="kpi-sub">{esc(day.get("peak_rain_hour","—"))}</div></div>
        <div class="kpi-card"><div class="kpi-label">Jam nyaman</div><div class="kpi-value" style="font-size:16px">{esc(windows)}</div><div class="kpi-sub">aktivitas</div></div>
        <div class="kpi-card"><div class="kpi-label">Panas terasa</div><div class="kpi-value" data-animate-value="{num(day.get("max_heat_c"), 0)}" data-animate-suffix="°C" data-animate-decimals="1">{deg(day.get("max_heat_c"))}</div><div class="kpi-sub">maksimum</div></div>
      </aside>
    </div>
  </div></section>'''


def v65_timeline(hours: List[Dict[str, Any]], title: str = "Timeline prakiraan", note: str = "") -> str:
    import json
    data_str = esc(json.dumps(hours[:12], ensure_ascii=False))
    return f'''<section class="section-compact"><div class="container">
    <div class="chart-wrapper reveal">
      <div class="section-header">
        <div class="section-overline">Timeline</div>
        <h2 class="section-title">{esc(title)}</h2>
        <p class="section-desc">{esc(note)}</p>
      </div>
      
      <!-- Multi-Variable Switches -->
      <div class="chart-selector">
        <button class="chart-chip active" data-var="rain">Peluang Hujan</button>
        <button class="chart-chip" data-var="temp">Suhu</button>
        <button class="chart-chip" data-var="humidity">Kelembapan</button>
        <button class="chart-chip" data-var="wind">Kecepatan Angin</button>
      </div>
      
      <div class="chart-svg-container" data-points="{data_str}">
        <svg class="rain-curve" viewBox="0 0 900 200" preserveAspectRatio="none"></svg>
        <div class="chart-tooltip">
          <div class="chart-tooltip-time">00:00</div>
          <div class="chart-tooltip-value">0%</div>
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
      <h2 class="section-title">Saran hari ini</h2>
      <p class="section-desc">Ringkas dan praktis.</p>
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
    <div class="mobile-bottom-sheet">
      <div class="mobile-bottom-sheet-handle"></div>
      <h3 style="font-family:var(--font-display);font-weight:800;font-size:18px;margin-bottom:16px;text-align:center;color:var(--white)">Prakiraan Per Jam</h3>
      <div class="hourly-list">{rows_html}</div>
    </div>
  </div></section>'''


def v65_command_center(api: Dict[str, Any]) -> str:
    return f'''<section class="section-compact"><div class="container">
    <div class="section-header reveal">
      <div class="section-overline">Peta Cuaca</div>
      <h2 class="section-title">Peta Prakiraan Cuaca</h2>
      <p class="section-desc">Peta prakiraan wilayah Kampus ITB dengan kendali lapisan cuaca.</p>
    </div>
    
    <div class="command-center reveal">
      <!-- Left: Map Frame wrapper -->
      <div class="map-wrapper">
        <iframe class="map-frame" src="langit_map_room.html" loading="lazy"></iframe>
      </div>
      
      <!-- Right: Controls & Diagnostics -->
      <div class="command-sidebar">
        <div class="glass glass-static">
          <div class="section-overline">Lapisan Cuaca</div>
          <h3 style="font-size:18px;font-weight:800;margin:8px 0 16px;font-family:var(--font-display);">Lapisan Cuaca</h3>
          
          <div class="layer-controls">
            <button class="layer-btn active" data-layer="rain" style="--active-bg:rgba(6,182,212,0.12); --active-color:var(--noon); --active-border:rgba(6,182,212,0.3);">Hujan</button>
            <button class="layer-btn" data-layer="temp" style="--active-bg:rgba(249,115,22,0.12); --active-color:var(--dawn); --active-border:rgba(249,115,22,0.3);">Suhu</button>
            <button class="layer-btn" data-layer="wind" style="--active-bg:rgba(16,185,129,0.12); --active-color:var(--safe); --active-border:rgba(16,185,129,0.3);">Angin</button>
            <button class="layer-btn" data-layer="cloud" style="--active-bg:rgba(148,190,235,0.12); --active-color:var(--cloud); --active-border:rgba(148,190,235,0.3);">Awan</button>
            <button class="layer-btn" data-layer="humidity" style="--active-bg:rgba(99,102,241,0.12); --active-color:var(--limited); --active-border:rgba(99,102,241,0.3);">Kelembapan</button>
          </div>
          <p style="font-size:12px;color:var(--mist);line-height:1.6;margin-top:12px;">Memilih parameter di atas akan mengubah visualisasi cuaca pada peta secara langsung.</p>
        </div>
        
        <div class="glass glass-static">
          <div class="section-overline">Status Data</div>
          <h3 style="font-size:18px;font-weight:800;margin:8px 0 12px;font-family:var(--font-display);">Keandalan Data</h3>
          <p style="font-size:13px;color:var(--cloud);line-height:1.6;margin-bottom:16px;">Monitoring otomatis keandalan dan tingkat akurasi verifikasi data cuaca.</p>
          <div style="display:flex;gap:12px;">
            <a class="btn btn-primary" href="keandalan_data.html">Keandalan Data</a>
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
      <h2 class="section-title">Peta risiko</h2>
      <p class="section-desc">Warna berubah mengikuti jam.</p>
    </div>
    <div class="map-wrapper reveal">
      <iframe class="map-frame" src="{esc(href)}" loading="lazy"></iframe>
      <div class="map-actions">
        <a class="btn btn-primary" href="{esc(href)}">Buka peta penuh</a>
        <a class="btn" href="langit_location.geojson">GeoJSON</a>
      </div>
    </div>
  </div></section>'''


def v65_share(api: Dict[str, Any], day: Dict[str, Any]) -> str:
    msg = f"LANGIT — {api['location_name']}\n{day['date_label']}\n{decision_sentence(api['location_name'], day, short=True)}\nPeluang hujan tertinggi: {pct(day.get('peak_rain_probability'))} sekitar pukul {day.get('peak_rain_hour','—')} WIB."
    return f'''<section class="section-compact"><div class="container">
    <div class="share-grid reveal">
      <div class="glass glass-static">
        <div class="section-overline">Bagikan</div>
        <h3 style="font-size:18px;font-weight:700;margin:8px 0 12px">Format singkat</h3>
        <textarea class="share-text" readonly>{esc(msg)}</textarea>
      </div>
      <div class="glass glass-static">
        <div class="section-overline">Pemberitahuan</div>
        <h3 style="font-size:18px;font-weight:700;margin:8px 0 12px">Catatan penggunaan</h3>
        <p style="color:var(--mist);font-size:14px;line-height:1.7">Prakiraan cuaca bersifat dinamis dan dapat berubah sewaktu-waktu. Untuk cuaca ekstrem, selalu pantau informasi resmi BMKG serta kondisi di sekitar lokasi Anda.</p>
      </div>
    </div>
  </div></section>'''


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
          <div class="location-stat"><div class="location-stat-value">{round(clamp(d.get("risk_score"))):.0f}</div><div class="location-stat-label">Risiko</div></div>
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
    lat = num(api.get("latitude"), -6.2)
    lon = num(api.get("longitude"), 106.8)
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
                    "wind_kmh": num(h.get("wind_kmh"), 0.0),
                    "wind_dir": num(h.get("wind_dir"), 0.0),
                    "cloud_pct": num(h.get("cloud_pct"), 0.0),
                },
            })
    return {"type": "FeatureCollection", "features": features}
def v65_map_page(title: str, geojson: Dict[str, Any], back_href: str) -> str:
    data = json.dumps(geojson, ensure_ascii=False)
    css = r'''html,body,#map{height:100%;margin:0;background:#030712;color:#f8fbff;font-family:'Inter',system-ui,-apple-system,sans-serif;overflow:hidden}
#particle-canvas{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:800}
.hud{position:absolute;z-index:1000;left:24px;top:24px;width:min(340px,calc(100% - 48px));padding:24px;border-radius:24px;background:rgba(7,14,30,0.75);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,0.08);box-shadow:0 24px 80px rgba(0,0,0,0.5),inset 0 1px 1px rgba(255,255,255,0.1);animation:slideIn 0.6s cubic-bezier(0.16,1,0.3,1) both}
.hud-brand{font-family:'Outfit',sans-serif;font-weight:800;font-size:10px;letter-spacing:0.1em;color:#32b7ff;text-transform:uppercase}
.hud-version{font-family:'Outfit',sans-serif;font-weight:600;font-size:10px;color:rgba(255,255,255,0.3)}
.hud h1{font-family:'Outfit',sans-serif;font-size:24px;font-weight:800;letter-spacing:-0.02em;line-height:1.2;margin:12px 0 4px;color:#fff}
.hud-meta{color:#6b8ab5;font-size:13px;font-weight:500}
.hud-divider{height:1px;background:linear-gradient(90deg,rgba(255,255,255,0.08) 0%,rgba(255,255,255,0) 100%);margin:16px 0}
.hud-stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.hud-stat-box{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);padding:10px 14px;border-radius:14px;display:flex;flex-direction:column;gap:2px}
.hud-stat-val{font-family:'Outfit',sans-serif;font-size:18px;font-weight:800;color:#fff}
.hud-stat-lbl{font-size:10px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.05em}
.parameter-tabs{display:flex;gap:4px;margin-top:16px;background:rgba(3,7,18,0.6);padding:3px;border-radius:12px;border:1px solid rgba(255,255,255,0.05)}
.param-tab{flex:1;border:none;background:none;color:#6b8ab5;font-family:inherit;font-size:10px;font-weight:700;padding:8px 0;border-radius:9px;cursor:pointer;transition:all 0.2s ease;text-align:center}
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
.time-pill span.day-label{font-size:9px;color:#64748b;text-transform:uppercase;font-weight:600}
.time-pill.active{background:linear-gradient(135deg,rgba(50,183,255,0.2) 0%,rgba(50,183,255,0.05) 100%);border-color:rgba(50,183,255,0.4);color:#32b7ff;box-shadow:0 4px 12px rgba(50,183,255,0.15)}
.legend{position:absolute;right:24px;bottom:24px;z-index:1000;background:rgba(7,14,30,0.75);border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:16px;font-size:11px;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);box-shadow:0 12px 40px rgba(0,0,0,0.4);width:150px;animation:slideIn 0.6s cubic-bezier(0.16,1,0.3,1) both}
.legend-title{font-family:'Outfit',sans-serif;font-weight:800;color:#fff;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.05em;font-size:9px}
.legend div{display:flex;gap:10px;align-items:center;margin:8px 0;color:#a8c4e0;font-weight:500}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.leaflet-control-attribution{background:rgba(3,7,12,0.85)!important;color:#6b8ab5!important;font-size:10px!important}
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
    js = r'''
const geojson = __DATA__;
const colors = {safe:'#10b981', watch:'#f59e0b', rain:'#ff9d42', danger:'#ef4444', limited:'#94a3b8'};
const features = (geojson && geojson.features) ? geojson.features : [];

// Find first coordinate center
const first = features[0] || {geometry:{coordinates:[106.8,-6.2]}, properties:{location_name:'Lokasi'}};
const center = first.geometry && first.geometry.coordinates ? [first.geometry.coordinates[1], first.geometry.coordinates[0]] : [-6.2,106.8];

// Particle Engine live states
const state = {
  windSpeed: 2.0,
  cloudPct: 40.0,
  tempC: 22.0,
  humidityPct: 70.0,
  rainProb: 0.0,
  activeLayer: 'risiko'
};

function getTempColor(t) {
  if (t <= 18) return '#3b82f6';
  if (t <= 22) return '#60a5fa';
  if (t <= 25) return '#f59e0b';
  if (t <= 28) return '#f97316';
  return '#ef4444';
}

function getHumidityColor(h) {
  if (h <= 55) return '#93c5fd';
  if (h <= 70) return '#38bdf8';
  if (h <= 85) return '#0ea5e9';
  return '#0284c7';
}

function getCloudColor(c) {
  if (c <= 30) return '#475569';
  if (c <= 60) return '#94a3b8';
  return '#f1f5f9';
}

function getWindColor(w) {
  if (w <= 5) return '#10b981';
  if (w <= 12) return '#3b82f6';
  if (w <= 20) return '#f59e0b';
  return '#ef4444';
}

function updateHUDValues(p) {
  document.getElementById('hud-location-name').textContent = p.location_name || 'Lokasi';
  document.getElementById('hud-time-label').textContent = `${p.relative || 'Prakiraan'} · ${p.hour || ''}`;
  document.getElementById('val-temp').textContent = p.temp_c != null ? `${Math.round(p.temp_c)}°C` : '—';
  document.getElementById('val-rain').textContent = p.rain_probability != null ? `${Math.round(p.rain_probability)}%` : '—';
  document.getElementById('val-wind').textContent = p.wind_kmh != null ? `${p.wind_kmh.toFixed(1)} km/jam` : '—';
  document.getElementById('val-humidity').textContent = p.humidity_pct != null ? `${Math.round(p.humidity_pct)}%` : '—';
}

function makeSparkline(locationSlug, dateIso, activeHour) {
  const locFeatures = features.filter(f => f.properties.slug === locationSlug && f.properties.date_iso === dateIso);
  if (locFeatures.length < 2) return '';
  locFeatures.sort((a,b) => {
    const ha = (a.properties && a.properties.hour) || '';
    const hb = (b.properties && b.properties.hour) || '';
    return ha.localeCompare(hb);
  });
  const temps = locFeatures.map(f => f.properties.temp_c || 20);
  const minTemp = Math.min(...temps);
  const maxTemp = Math.max(...temps);
  const range = maxTemp - minTemp || 1;
  const points = locFeatures.map((f, i) => {
    const x = (i / (locFeatures.length - 1)) * 180;
    const y = 25 - ((f.properties.temp_c - minTemp) / range) * 20;
    return `${x},${y}`;
  }).join(' ');
  const activeIdx = locFeatures.findIndex(f => f.properties.hour === activeHour);
  const activePt = activeIdx !== -1 ? `${(activeIdx / (locFeatures.length - 1)) * 180},${25 - ((locFeatures[activeIdx].properties.temp_c - minTemp) / range) * 20}` : '';
  let activeMarker = '';
  if (activePt) {
    const [ax, ay] = activePt.split(',');
    activeMarker = `<circle cx="${ax}" cy="${ay}" r="3" fill="#32b7ff" stroke="#fff" stroke-width="1.5" />`;
  }
  return `
    <div class="sparkline-wrap" style="margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.06)">
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#64748b;margin-bottom:4px">
        <span>Tren Temp Hari Ini</span>
        <span>${minTemp}°–${maxTemp}°C</span>
      </div>
      <svg width="180" height="30" style="overflow:visible">
        <polyline fill="none" stroke="rgba(50, 183, 255, 0.4)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" points="${points}" />
        ${activeMarker}
      </svg>
    </div>
  `;
}

function ptxt(p) {
  if (!p) return '';
  const sparklineHtml = makeSparkline(p.slug, p.date_iso, p.hour);
  const riskColor = colors[p.risk_class || 'limited'];
  return `
    <div class="popup-card">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px">
        <b style="font-family:'Outfit'; font-size:14px; color:#fff">${p.location_name}</b>
        <span class="popup-badge" style="background:${riskColor}20; color:${riskColor}; border:1px solid ${riskColor}30">${p.risk_label || '—'}</span>
      </div>
      <div style="font-size:11px; color:#6b8ab5; margin-bottom:10px">${p.date} · ${p.hour} WIB</div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; font-size:11px">
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#64748b; font-size:9px">SUHU</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.temp_c != null ? p.temp_c + '°C' : '—'}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#64748b; font-size:9px">HUJAN</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.rain_probability != null ? p.rain_probability + '%' : '—'}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#64748b; font-size:9px">LEMBAP</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.humidity_pct != null ? p.humidity_pct + '%' : '—'}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#64748b; font-size:9px">ANGIN</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.wind_kmh != null ? p.wind_kmh + ' km/jam' : '—'}</div>
        </div>
      </div>
      <div style="margin-top:10px; font-size:11px; color:#a8c4e0; font-style:italic; line-height:1.4">
        ${p.condition}
      </div>
      ${sparklineHtml}
    </div>
  `;
}

function updateLegend() {
  const legendEl = document.getElementById('map-legend');
  let legendHtml = '';
  if (state.activeLayer === 'risiko') {
    legendHtml = `
      <div class="legend-title">Indeks Risiko</div>
      <div><i class="dot" style="background:#10b981"></i>Aman</div>
      <div><i class="dot" style="background:#f59e0b"></i>Dipantau</div>
      <div><i class="dot" style="background:#ff9d42"></i>Waspada</div>
      <div><i class="dot" style="background:#ef4444"></i>Bahaya</div>
    `;
  } else if (state.activeLayer === 'temp') {
    legendHtml = `
      <div class="legend-title">Temperatur</div>
      <div><i class="dot" style="background:#3b82f6"></i>&le; 18°C (Sejuk)</div>
      <div><i class="dot" style="background:#60a5fa"></i>19°C - 22°C</div>
      <div><i class="dot" style="background:#f59e0b"></i>23°C - 25°C</div>
      <div><i class="dot" style="background:#f97316"></i>26°C - 28°C</div>
      <div><i class="dot" style="background:#ef4444"></i>&gt; 28°C (Hangat)</div>
    `;
  } else if (state.activeLayer === 'rain') {
    legendHtml = `
      <div class="legend-title">Peluang Hujan</div>
      <div><i class="dot" style="background:#3b82f6"></i>Rendah (&le; 20%)</div>
      <div><i class="dot" style="background:#f59e0b"></i>Sedang (21% - 50%)</div>
      <div><i class="dot" style="background:#ef4444"></i>Tinggi (&gt; 50%)</div>
    `;
  } else if (state.activeLayer === 'wind') {
    legendHtml = `
      <div class="legend-title">Kecepatan Angin</div>
      <div><i class="dot" style="background:#10b981"></i>&le; 5 km/jam (Teduh)</div>
      <div><i class="dot" style="background:#3b82f6"></i>6 - 12 km/jam (Sepoi)</div>
      <div><i class="dot" style="background:#f59e0b"></i>13 - 20 km/jam (Kencang)</div>
      <div><i class="dot" style="background:#ef4444"></i>&gt; 20 km/jam (Bahaya)</div>
    `;
  } else if (state.activeLayer === 'humidity') {
    legendHtml = `
      <div class="legend-title">Kelembapan Nisbi</div>
      <div><i class="dot" style="background:#93c5fd"></i>Kering (&le; 55%)</div>
      <div><i class="dot" style="background:#38bdf8"></i>Nyaman (56% - 70%)</div>
      <div><i class="dot" style="background:#0ea5e9"></i>Lembap (71% - 85%)</div>
      <div><i class="dot" style="background:#0284c7"></i>Sangat Lembap</div>
    `;
  } else if (state.activeLayer === 'cloud') {
    legendHtml = `
      <div class="legend-title">Tutupan Awan</div>
      <div><i class="dot" style="background:#475569"></i>Cerah (&le; 30%)</div>
      <div><i class="dot" style="background:#94a3b8"></i>Berawan (31% - 60%)</div>
      <div><i class="dot" style="background:#f1f5f9"></i>Mendung (&gt; 60%)</div>
    `;
  }
  legendEl.innerHTML = legendHtml;
}

let activeTimeIndex = 0;

function drawForTime(dateIso, activeHour) {
  layer.clearLayers();
  const selected = features.filter(f => f.properties.date_iso === dateIso && f.properties.hour === activeHour);
  if (!selected.length) return;

  const activeLoc = selected[0].properties;
  state.windSpeed = activeLoc.wind_kmh || 2.0;
  state.cloudPct = activeLoc.cloud_pct || 40.0;
  state.tempC = activeLoc.temp_c || 24.0;
  state.humidityPct = activeLoc.humidity_pct || 70.0;
  state.rainProb = activeLoc.rain_probability || 0.0;

  updateHUDValues(activeLoc);

  selected.forEach(f => {
    const p = f.properties;
    const coords = f.geometry.coordinates;
    const latlng = [coords[1], coords[0]];

    if (state.activeLayer === 'risiko') {
      const cls = p.risk_class || 'limited';
      const color = colors[cls];
      const size = 12 + (p.risk_score || 0) * 0.18;
      const iconHtml = `
        <div class="pulse-marker" style="--marker-color:${color}; --pulse-size:${size * 3}px">
          <div class="pulse-dot" style="width:${size}px; height:${size}px"></div>
          <div class="pulse-ring"></div>
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: 'custom-leaflet-marker',
          iconSize: [size * 3, size * 3],
          iconAnchor: [size * 1.5, size * 1.5]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === 'temp') {
      const temp = p.temp_c || 24;
      const color = getTempColor(temp);
      const iconHtml = `
        <div class="temp-badge" style="background:${color}; box-shadow:0 0 15px ${color}80">
          ${Math.round(temp)}°C
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: 'custom-leaflet-marker',
          iconSize: [45, 24],
          iconAnchor: [22, 12]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === 'rain') {
      const rain = p.rain_probability || 0;
      const radarColor = rain > 50 ? '#ef4444' : (rain > 20 ? '#f59e0b' : '#3b82f6');
      const iconHtml = `
        <div class="radar-marker" style="--radar-color:${radarColor}">
          <div class="radar-ping"></div>
          <div class="radar-core"></div>
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: 'custom-leaflet-marker',
          iconSize: [60, 60],
          iconAnchor: [30, 30]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === 'wind') {
      const wind = p.wind_kmh || 0;
      const color = getWindColor(wind);
      const rotation = (coords[0] * 33 + coords[1] * 77) % 360;
      const iconHtml = `
        <div class="wind-badge" style="background:${color}; box-shadow:0 0 15px ${color}80; display:flex; align-items:center; gap:4px; padding:4px 8px; border-radius:12px; border:1px solid rgba(255,255,255,0.2); color:#fff; font-family:'Outfit',sans-serif; font-size:11px; font-weight:800; white-space:nowrap;">
          <span style="display:inline-block; transform:rotate(${rotation}deg);">➔</span>
          ${wind.toFixed(1)} km/jam
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: 'custom-leaflet-marker',
          iconSize: [80, 24],
          iconAnchor: [40, 12]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === 'humidity') {
      const hum = p.humidity_pct || 70;
      const color = getHumidityColor(hum);
      const size = 15 + hum * 0.15;
      const iconHtml = `
        <div class="humidity-halo" style="background:${color}; opacity:${0.1 + (hum/200)}; width:${size}px; height:${size}px; filter:blur(4px)">
          <div style="width:6px; height:6px; background:#fff; border-radius:50%; margin:auto; position:absolute; inset:0"></div>
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: 'custom-leaflet-marker',
          iconSize: [size, size],
          iconAnchor: [size/2, size/2]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === 'cloud') {
      const cld = p.cloud_pct || 40;
      const color = getCloudColor(cld);
      const opacity = 0.2 + (cld / 150);
      const iconHtml = `
        <div class="cloud-glow" style="background:${color}; opacity:${opacity}; width:32px; height:32px; border-radius:50%; filter:blur(6px); box-shadow:0 0 10px ${color}">
          <div style="width:4px; height:4px; background:#fff; border-radius:50%; margin:auto; position:absolute; inset:0"></div>
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: 'custom-leaflet-marker',
          iconSize: [32, 32],
          iconAnchor: [16, 16]
        })
      }).bindPopup(ptxt(p)).addTo(layer);
    }
  });
}

function switchLayer(layerName) {
  state.activeLayer = layerName;
  document.querySelectorAll('.param-tab').forEach(tab => {
    if (tab.getAttribute('data-layer') === layerName) {
      tab.classList.add('active');
    } else {
      tab.classList.remove('active');
    }
  });
  
  updateLegend();
  
  const pills = document.querySelectorAll('.time-pill');
  if (pills.length && pills[activeTimeIndex]) {
    const activePill = pills[activeTimeIndex];
    const dateIso = activePill.getAttribute('data-date');
    const hourVal = activePill.getAttribute('data-hour');
    drawForTime(dateIso, hourVal);
  }
}

let playInterval = null;
function togglePlay() {
  const playBtn = document.getElementById('play-btn');
  if (playInterval) {
    clearInterval(playInterval);
    playInterval = null;
    playBtn.innerHTML = '&#9654;';
  } else {
    playBtn.innerHTML = '&#10074;&#10074;';
    const pills = document.querySelectorAll('.time-pill');
    playInterval = setInterval(() => {
      activeTimeIndex = (activeTimeIndex + 1) % pills.length;
      pills[activeTimeIndex].click();
      pills[activeTimeIndex].scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    }, 2500);
  }
}

let map;
try {
  map = L.map('map',{
    scrollWheelZoom:true,
    worldCopyJump:false,
    maxBounds:[[-11.25,94],[6.45,141.25]],
    maxBoundsViscosity:.8,
    minZoom:5,
    zoomControl:false
  });
  
  const baseTile = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
    maxZoom:19,
    attribution:'&copy; OpenStreetMap & CARTO'
  });
  
  baseTile.on('tileerror', function() {
    console.warn("Tile loading failed.");
    if (!document.getElementById('tile-warning')) {
      document.body.insertAdjacentHTML('beforeend', '<div id="tile-warning" style="position:absolute; bottom:80px; left:24px; z-index:1001; background:rgba(239,68,68,0.85); color:#fff; padding:8px 16px; border-radius:12px; font-size:12px; font-family:sans-serif;">Peta dasar belum dapat dimuat. Data lokasi tetap tersedia.</div>');
      setTimeout(() => {
        const warn = document.getElementById('tile-warning');
        if (warn) warn.remove();
      }, 5000);
    }
  });
  baseTile.addTo(map);
  
  L.control.zoom({ position: 'topright' }).addTo(map);
  
  var layer = L.layerGroup().addTo(map);
  
  // Sort and build timeline
  features.sort((a,b) => {
    const da = (a.properties && a.properties.date_iso) || '';
    const db = (b.properties && b.properties.date_iso) || '';
    if (da !== db) {
      return da.localeCompare(db);
    }
    const ha = (a.properties && a.properties.hour) || '';
    const hb = (b.properties && b.properties.hour) || '';
    return ha.localeCompare(hb);
  });
  
  // Filter to get a single unique location's time slots
  const firstLocSlug = (first.properties && first.properties.slug) || '';
  const locTimeline = features.filter(f => f.properties && f.properties.slug === firstLocSlug);
  const scrubber = document.getElementById('time-scrubber');
  
  locTimeline.forEach((tFeature, idx) => {
    const p = tFeature.properties;
    const pill = document.createElement('button');
    pill.className = 'time-pill' + (idx === 0 ? ' active' : '');
    pill.setAttribute('data-date', p.date_iso);
    pill.setAttribute('data-hour', p.hour);
    pill.innerHTML = `
      <span class="day-label">${p.relative}</span>
      <strong>${p.hour}</strong>
    `;
    pill.onclick = () => {
      document.querySelectorAll('.time-pill').forEach(x => x.classList.remove('active'));
      pill.classList.add('active');
      activeTimeIndex = idx;
      drawForTime(p.date_iso, p.hour);
    };
    scrubber.appendChild(pill);
  });
  
  updateLegend();
  if (locTimeline.length) {
    drawForTime(locTimeline[0].properties.date_iso, locTimeline[0].properties.hour);
  } else {
    document.getElementById('hud-location-name').textContent = 'Peta Prakiraan Cuaca';
    document.getElementById('hud-time-label').textContent = 'Data prakiraan tidak tersedia';
    document.getElementById('val-temp').textContent = '—';
    document.getElementById('val-rain').textContent = '—';
    document.getElementById('val-wind').textContent = '—';
    document.getElementById('val-humidity').textContent = '—';
  }

  // Fit map bounds to show all markers automatically
  if (features.length) {
    const coordsList = features.map(f => [f.geometry.coordinates[1], f.geometry.coordinates[0]]);
    const bounds = L.latLngBounds(coordsList);
    map.fitBounds(bounds, { padding: [50, 50], maxZoom: 13 });
  } else {
    map.setView(center, 12);
  }

  // Keyboard controls
  document.addEventListener('keydown', (e) => {
    const pills = document.querySelectorAll('.time-pill');
    if (!pills.length) return;
    if (e.key === 'ArrowRight') {
      activeTimeIndex = (activeTimeIndex + 1) % pills.length;
      pills[activeTimeIndex].click();
      pills[activeTimeIndex].scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    } else if (e.key === 'ArrowLeft') {
      activeTimeIndex = (activeTimeIndex - 1 + pills.length) % pills.length;
      pills[activeTimeIndex].click();
      pills[activeTimeIndex].scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    }
  });

} catch(e) {
  console.error("Map initialization failed", e);
  document.body.insertAdjacentHTML('beforeend','<div style="position:absolute;inset:0;display:grid;place-items:center;color:#6b8ab5;background:#030712;z-index:9999;font-family:sans-serif;padding:20px;text-align:center;">Peta dasar belum dapat dimuat. Data lokasi tetap tersedia.</div>');
}

// Sync with postMessage from parent dashboard window
window.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'switchLayer') {
    switchLayer(e.data.layer);
  }
});

// Hide the back button if embedded inside an iframe
if (window.self !== window.top) {
  const backBtn = document.getElementById('hud-back-btn');
  if (backBtn) backBtn.style.display = 'none';
}
'''.replace("__DATA__", data)
    
    hud_html = f'''  <section class="hud">
    <div style="display:flex; justify-content:space-between; align-items:center">
      <span class="hud-brand">{BRAND} Cuaca</span>
      <span class="hud-version">Peta Prakiraan</span>
    </div>
    <h1 id="hud-location-name">Memuat...</h1>
    <div class="hud-meta" id="hud-time-label">Memuat data prakiraan cuaca.</div>
    
    <div class="hud-divider"></div>
    
    <div class="hud-stats-grid">
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-temp">—</span>
        <span class="hud-stat-lbl">Suhu</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-rain">—</span>
        <span class="hud-stat-lbl">Peluang Hujan</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-wind">—</span>
        <span class="hud-stat-lbl">Kecepatan Angin</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-humidity">—</span>
        <span class="hud-stat-lbl">Kelembapan</span>
      </div>
    </div>

    <div class="hud-divider"></div>

    <div class="parameter-tabs">
      <button class="param-tab active" data-layer="risiko" onclick="switchLayer('risiko')">Risiko</button>
      <button class="param-tab" data-layer="temp" onclick="switchLayer('temp')">Suhu</button>
      <button class="param-tab" data-layer="rain" onclick="switchLayer('rain')">Hujan</button>
      <button class="param-tab" data-layer="wind" onclick="switchLayer('wind')">Angin</button>
      <button class="param-tab" data-layer="humidity" onclick="switchLayer('humidity')">Lembap</button>
      <button class="param-tab" data-layer="cloud" onclick="switchLayer('cloud')">Awan</button>
    </div>

    <div style="margin-top:20px; display:flex">
      <a class="btn" id="hud-back-btn" style="flex:1; margin-top:0" href="{esc(back_href)}">Kembali</a>
    </div>
  </section>'''

    timeline_html = '''  <div class="timeline-container">
    <button class="play-btn" id="play-btn" onclick="togglePlay()">&#9654;</button>
    <div class="time-scrubber" id="time-scrubber"></div>
  </div>'''

    legend_html = '<div class="legend" id="map-legend"></div>'

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
  <canvas id="particle-canvas"></canvas>
  {hud_html}
  {timeline_html}
  {legend_html}
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>{js}</script>
</body>
</html>'''
def v65_today_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    heading = f"Prakiraan\n{api['location_name']}"
    sub = f"{day['date_label']}. {decision_sentence(api['location_name'], day, short=True)}"
    body = v65_hero(api, heading, sub, day)
    body += v65_notice()
    
    # High-Tech Stepper Timeline Morph Navigation
    body += f'''<section class="section-compact"><div class="container">
    <div class="day-tabs-container reveal">
      <div class="timeline-progress-container">
        <div class="timeline-progress-bar"></div>
        <div class="timeline-progress-fill"></div>
        
        <button class="timeline-node active" data-target="day-0">
          <div class="timeline-node-dot"></div>
          <span class="timeline-node-label">Hari ini</span>
        </button>
        <button class="timeline-node" data-target="day-1">
          <div class="timeline-node-dot"></div>
          <span class="timeline-node-label">Besok</span>
        </button>
        <button class="timeline-node" data-target="day-2">
          <div class="timeline-node-dot"></div>
          <span class="timeline-node-label">Lusa</span>
        </button>
      </div>
    </div>
  </div></section>'''

    # Panels containing details for each day
    body += '<div class="day-panels">'
    for i, d in enumerate(api["days"]):
        active_class = "active" if i == 0 else ""
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
    body = v65_hero(api, "Saran\naktivitas", f"{day['date_label']}. Fokus pada jam yang perlu dipantau.", day)
    body += v65_notice()
    body += v65_decision(api, day)
    body += v65_activities(day)
    body += v65_timeline(day["hours"], "Jam rawan", "Lihat kurva, bukan tabel.")
    body += v65_periods(day)
    body += v65_hours(day)
    body += '<div class="divider"></div>'
    body += v65_share(api, day)
    return v65_document(api, "activity", f"LANGIT Aktivitas — {api['location_name']}", body)


def v65_planner_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    rows = day.get("hours") or []
    options = "".join(f'<option value="{esc(x.get("hour"))}">{esc(x.get("hour"))} · {esc(x.get("condition"))} · hujan {pct(x.get("rain_probability"))}</option>' for x in rows)
    data = json.dumps(day, ensure_ascii=False)
    body = v65_hero(api, "Planner\ncuaca", f"{day['date_label']}. Cek jam terbaik untuk aktivitas.", day, show_metrics=False)
    body += v65_notice()
    body += f'''<section class="section-compact"><div class="container">
    <div class="glass glass-static reveal">
      <div class="section-header">
        <div class="section-overline">Planner</div>
        <h2 style="font-size:24px;font-weight:800">Cek jam terbaik</h2>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:12px;margin-bottom:24px">
        <select id="act" class="btn" style="background:var(--glass);text-align:left;min-height:48px">
          <option>Motor</option><option>Jalan kaki</option><option>Jemur</option><option>Outdoor</option><option>Olahraga</option><option>Foto</option>
        </select>
        <select id="hh" class="btn" style="background:var(--glass);text-align:left;min-height:48px">{options}</select>
        <button class="btn btn-primary" type="button" onclick="decidePlanner()" style="min-height:48px">Cek</button>
      </div>
      <div class="decision-main" style="min-height:140px;margin:0;--accent-glow:rgba(50,183,255,0.06)">
        <div>
          <span id="plannerBadge" class="status-badge status-watch">Pilih jam</span>
          <h2 id="plannerOut" class="decision-title" style="font-size:clamp(24px,4vw,40px)">Cek sebelum berangkat.</h2>
        </div>
        <p id="plannerWhy" class="decision-desc">Pilih aktivitas dan jam. Sistem akan pakai peluang hujan, risiko, dan panas terasa.</p>
      </div>
    </div>
  </div></section>
<script>
const plannerDay={data};
function plannerRiskClass(x){{return x==='danger'?'Tinggi':x==='rain'?'Waspada':x==='watch'?'Pantau':x==='safe'?'Aman':'Terbatas';}}
function decidePlanner(){{
  const act=document.getElementById('act').value;
  const val=document.getElementById('hh').value;
  const h=(plannerDay.hours||[]).find(x=>x.hour===val)||{{}};
  const p=Math.round(Number(h.rain_probability||0));
  const r=Math.round(Number(h.risk_score||0));
  const hi=Number(h.heat_index_c||h.temp_c||0);
  let msg='Bisa', cls='safe';
  let why='Tetap lihat kondisi sekitar.';
  if(r>=78 || p>=70){{msg='Tunda'; cls='danger'; why='Risiko tinggi pada jam ini.';}}
  else if(r>=55 || p>=45){{msg='Siapkan plan B'; cls='rain'; why='Awan/hujan perlu diwaspadai.';}}
  else if(r>=25 || p>=25){{msg='Masih bisa, pantau'; cls='watch'; why='Masih aman bersyarat.';}}
  if((act==='Jemur'||act==='Foto') && p>=25){{msg='Pilih jam lain'; cls='watch'; why='Hujan/awan bisa mengganggu.';}}
  if((act==='Olahraga'||act==='Jalan kaki') && hi>=36){{msg='Cari jam lebih teduh'; cls='watch'; why='Panas terasa tinggi.';}}
  const badge=document.getElementById('plannerBadge');
  badge.className='status-badge status-'+cls;
  badge.textContent=plannerRiskClass(h.risk_class||cls);
  document.getElementById('plannerOut').textContent=act+' '+val+': '+msg;
  document.getElementById('plannerWhy').textContent=why+' Hujan '+p+'%, risiko '+r+'/100, kondisi '+(h.condition||'-')+'.';
}}
</script>'''
    body += v65_timeline(day["hours"], "Timeline hari ini", day["date_label"])
    body += v65_activities(day)
    return v65_document(api, "activity", f"LANGIT Planner — {api['location_name']}", body)


def v65_data_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    sources = api.get("sources") or []
    active = sum(1 for row in sources if text(pick(row, "active", "is_active", "used", "ok", default=""), "").lower() in {"yes", "true", "1", "aktif", "ok"})
    total = len(sources) or 1
    pct_val = round(active / total * 100)
    rows_html = "".join(
        f'<tr><td>{esc(pick(r,"model","source","name",default="—"))}</td><td>{esc(pick(r,"provider","origin",default="—"))}</td><td>{esc(pick(r,"active","is_active","used","ok",default="—"))}</td><td>{esc(pick(r,"weight","score","quality",default="—"))}</td></tr>'
        for r in sources
    ) or '<tr><td colspan="4" style="color:var(--mist)">Belum ada tabel sumber.</td></tr>'

    body = v65_hero(api, "Keandalan\ndata", f"{day['date_label']}. Ringkasan sumber prakiraan.", day, show_metrics=False)
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

    body = v65_hero(api, "Akurasi", f"{day['date_label']}. Skor muncul setelah data cukup.", day, show_metrics=False)
    body += v65_notice()
    body += f'''<section class="section-compact"><div class="container">
    <div class="glass glass-static reveal">
      <div class="section-header">
        <div class="section-overline">Verifikasi</div>
        <h2 style="font-size:24px;font-weight:800">{"Data akurasi cukup" if matched >= target else "Belum cukup data"}</h2>
        <p style="font-size:14px;color:var(--mist);margin-top:4px">{matched}/{target} pasangan</p>
      </div>
      <div class="trust-bar"><div class="trust-fill" data-width="{pct_done:.0f}%" style="width:0"></div></div>
    </div>
  </div></section>'''

    if matched >= target:
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
            mean_forecast = r.get("mean_forecast_probability") or r.get("mean_forecast_pct")
            obs_freq = r.get("observed_rain_frequency") or r.get("observed_frequency_pct")
            
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
          <p class="section-desc">Bandingkan seberapa sering prakiraan peluang hujan Sentinel X terbukti di dunia nyata.</p>
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


def v65_portal_page(apis: List[Dict[str, Any]], root: Path) -> str:
    today = apis[0]["today"] if apis else summarize_day("Hari ini", local_now().date(), [])
    dummy = {"location_name": "Portal", "generated_at": fmt_update(), "today": today}

    body = v65_hero(dummy, "LANGIT", "Prakiraan cuaca untuk wilayah Institut Teknologi Bandung.", today, show_metrics=False)
    body += v65_notice()

    # Location cards
    cards = []
    for i, api in enumerate(sorted(apis, key=lambda a: clamp(a["today"].get("risk_score"), default=0), reverse=True)):
        d = api["today"]
        cls = d.get("risk_class", "limited")
        cards.append(f'''<a class="location-card reveal reveal-delay-{(i % 3) + 1}" href="{esc(api["location_slug"])}/langit_app.html" style="--accent-color:{risk_color(cls)};--accent-glow:{_accent_glow(cls)}">
        <span class="status-badge status-{esc(cls)}">{esc(d.get("risk_label"))}</span>
        <h3 class="location-name">{esc(api.get("location_name"))}</h3>
        <p class="location-desc">{esc(d.get("date_label"))}. {esc(decision_sentence(api.get("location_name",""), d, short=True))}</p>
        <div class="location-stats">
          <div class="location-stat"><div class="location-stat-value">{pct(d.get("peak_rain_probability"))}</div><div class="location-stat-label">Hujan</div></div>
          <div class="location-stat"><div class="location-stat-value">{esc(d.get("peak_rain_hour"))}</div><div class="location-stat-label">Puncak</div></div>
          <div class="location-stat"><div class="location-stat-value">{round(clamp(d.get("risk_score"))):.0f}</div><div class="location-stat-label">Risiko</div></div>
        </div>
        <div class="location-actions">
          <span class="btn btn-primary">Buka</span>
          <span class="btn" onclick="event.preventDefault();window.location.href=\'{esc(api["location_slug"])}/langit_3day.html\'">3 hari</span>
          <span class="btn" onclick="event.preventDefault();window.location.href=\'{esc(api["location_slug"])}/langit_activity.html\'">Aktivitas</span>
        </div>
      </a>''')

    body += f'''<section class="section"><div class="container">
    <div class="section-header reveal">
      <div class="section-overline">Lokasi</div>
      <h2 class="section-title">Pilih lokasi</h2>
      <p class="section-desc">Diurutkan dari yang paling perlu dipantau.</p>
    </div>
    <div class="location-grid">{"".join(cards)}</div>
  </div></section>'''

    body += f'''<section class="section-compact"><div class="container">
    <div class="section-header reveal">
      <div class="section-overline">Peta</div>
      <h2 class="section-title">Peta lokasi</h2>
      <p class="section-desc">Warna mengikuti risiko hari ini.</p>
    </div>
    <div class="map-wrapper reveal">
      <iframe class="map-frame" src="langit_portal_map.html" loading="lazy"></iframe>
      <div class="map-actions">
        <a class="btn btn-primary" href="langit_portal_map.html">Buka peta penuh</a>
        <a class="btn" href="langit_all_locations.geojson">GeoJSON</a>
      </div>
    </div>
  </div></section>'''

    body += f'''<section class="section-compact"><div class="container">
    <div class="glass glass-static reveal">
      <div class="section-header">
        <div class="section-overline">Data</div>
        <h2 style="font-size:24px;font-weight:800">Data publik</h2>
        <p style="font-size:14px;color:var(--mist);margin-top:4px">Untuk arsip dan integrasi.</p>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px">
        <a class="btn" href="forecast_all_locations.csv">Forecast CSV</a>
        <a class="btn" href="source_status_all_locations.csv">Sumber CSV</a>
        <a class="btn" href="langit_portal_manifest.json">Manifest</a>
      </div>
    </div>
  </div></section>'''

    return v65_document(dummy, "locations", "LANGIT Portal", body, root=True)


def v65_portal_geo(apis: List[Dict[str, Any]]) -> Dict[str, Any]:
    features = []
    for api in apis:
        lat = num(api.get("latitude"), None)
        lon = num(api.get("longitude"), None)
        if lat is None or lon is None:
            continue
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
                        "wind_kmh": num(h.get("wind_kmh"), 0.0),
                        "wind_dir": num(h.get("wind_dir"), 0.0),
                        "cloud_pct": num(h.get("cloud_pct"), 0.0),
                    },
                })
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Rebuild + Verify
# ---------------------------------------------------------------------------

def verify(root: Path) -> int:
    sanitize_existing_public_files(root)
    required_root = [root / "index.html", root / "langit_portal_map.html", root / "langit_portal_manifest.json"]
    missing = [str(p) for p in required_root if not p.exists()]
    for d in location_dirs(root):
        for name in ["langit_app.html", "langit_3day.html", "langit_activity.html", "langit_map_room.html", "keandalan_data.html", "akurasi_data.html", "langit_api_v1.json", "langit_location.geojson"]:
            if not (d / name).exists():
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
    meta = metadata_by_slug(root)
    dirs = location_dirs(root)
    if not dirs:
        print("ERROR: tidak ada folder lokasi di outputs/. Jalankan forecast dulu.", file=sys.stderr)
        return 2
    apis: List[Dict[str, Any]] = []
    for d in dirs:
        api = load_location_api(d, meta.get(d.name, {"slug": d.name}))
        apis.append(api)
        gj = v65_geo_for_api(api)
        write_json(d / "langit_api_v1.json", api)
        write_json(d / "langit_location.geojson", gj)
        write_json(d / "langit_map_layers.json", {"brand": BRAND, "version": VERSION, "geojson": gj})
        # Main pages
        write_text(d / "langit_app.html", v65_today_page(api))
        write_text(d / "langit_3day.html", v65_three_day_page(api))
        write_text(d / "langit_activity.html", v65_activity_page(api))
        write_text(d / "keandalan_data.html", v65_data_page(api))
        acc_html = v65_accuracy_page(api, d)
        write_text(d / "akurasi_data.html", acc_html)
        write_text(d / "sentinel_x_accuracy_public.html", acc_html)
        map_html = v65_map_page(f"LANGIT Map — {api['location_name']}", gj, "langit_app.html")
        write_text(d / "langit_map_room.html", map_html)
        # Overwrite legacy pages/utilities
        write_text(d / "langit_planner.html", v65_planner_page(api))
        write_text(d / "langit_whatsapp_brief.txt", f"LANGIT — {api['location_name']}\n{api['today']['date_label']}\n{decision_sentence(api['location_name'], api['today'], short=True)}\n")

    pgeo = v65_portal_geo(apis)
    write_json(root / "langit_all_locations.geojson", pgeo)
    write_json(root / "langit_portal_manifest.json", {"brand": BRAND, "version": VERSION, "generated_at": fmt_update(), "public_base_url": public_base_url, "locations": [{"slug": a["location_slug"], "name": a["location_name"]} for a in apis]})
    write_text(root / "langit_portal_map.html", v65_map_page("LANGIT Portal Map", pgeo, "index.html"))
    write_text(root / "index.html", v65_portal_page(apis, root))
    print(f"OK: {VERSION} rebuild selesai. lokasi={len(apis)}")
    return verify(root)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild LANGIT v65 cinematic public HTML layer.")
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
