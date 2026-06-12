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
    ("Window ", "Jam "), ("window ", "jam "),
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
    if path.suffix.lower() in {".html", ".htm", ".txt", ".json"}:
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
        "hour": hh, "temp_c": temp, "humidity_pct": rh, "heat_index_c": heat,
        "rain_probability": rain, "wind_kmh": wind, "risk_score": round(score),
        "risk_class": cls, "risk_label": risk_label(cls), "condition": cond, "valid": valid,
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
            meta.setdefault(slug, {}).update({"slug": slug, "location_name": props.get("location_name") or props.get("name"), "longitude": coords[0] if len(coords) >= 1 else None, "latitude": coords[1] if len(coords) >= 2 else None})
    return meta


def location_dirs(root: Path) -> List[Path]:
    if not root.exists():
        return []
    out = []
    sentinel_files = ["anemos_app.html", "langit_hourly_intelligence.csv", "anemos_hourly_compact.csv", "langit_api_v1.json", "anemos_api_v1.json", "forecast.csv", "forecast_all_locations.csv"]
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
    for fname in ["langit_hourly_intelligence.csv", "anemos_hourly_compact.csv", "anemos_risk_timeline.csv", "forecast.csv", "forecast_all_locations.csv"]:
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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');

:root {
  --void: #050a14;
  --abyss: #0a1628;
  --ocean: #0f2240;
  --steel: #1a3358;
  --mist: #6b8ab5;
  --cloud: #a8c4e0;
  --snow: #eaf3ff;
  --white: #f8fbff;
  --dawn: #ff8f5c;
  --noon: #32b7ff;
  --dusk: #9b6dff;
  --safe: #35e8a4;
  --watch: #ffd052;
  --alert: #ff9346;
  --danger: #ff4778;
  --limited: #9ba8ff;
  --glass: rgba(255,255,255,0.04);
  --glass-border: rgba(148,190,235,0.12);
  --glass-hover: rgba(255,255,255,0.08);
  --radius-sm: 12px;
  --radius-md: 20px;
  --radius-lg: 28px;
  --radius-xl: 36px;
  --shadow-sm: 0 4px 20px rgba(0,0,0,0.15);
  --shadow-md: 0 12px 40px rgba(0,0,0,0.25);
  --shadow-lg: 0 24px 80px rgba(0,0,0,0.35);
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html {
  scroll-behavior: smooth;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

body {
  background: var(--void);
  color: var(--snow);
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
  font-feature-settings: 'cv01', 'cv02', 'cv03';
  letter-spacing: -0.01em;
  line-height: 1.5;
  overflow-x: hidden;
}

a { color: inherit; text-decoration: none; }

/* --- ATMOSPHERIC BACKGROUND --- */
.atmo {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
}
.atmo::before {
  content: '';
  position: absolute;
  width: 200%;
  height: 200%;
  top: -50%;
  left: -50%;
  background:
    radial-gradient(ellipse 600px 400px at 20% 20%, rgba(50,183,255,0.08), transparent),
    radial-gradient(ellipse 500px 500px at 80% 10%, rgba(155,109,255,0.06), transparent),
    radial-gradient(ellipse 800px 300px at 50% 80%, rgba(53,232,164,0.04), transparent);
  animation: atmo-drift 30s ease-in-out infinite alternate;
}
.atmo::after {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(circle 2px at 15% 25%, rgba(255,255,255,0.15), transparent 3px),
    radial-gradient(circle 1.5px at 45% 15%, rgba(255,255,255,0.1), transparent 2px),
    radial-gradient(circle 2px at 75% 35%, rgba(255,255,255,0.12), transparent 3px),
    radial-gradient(circle 1px at 85% 55%, rgba(255,255,255,0.08), transparent 2px),
    radial-gradient(circle 1.5px at 25% 65%, rgba(255,255,255,0.1), transparent 2px),
    radial-gradient(circle 1px at 55% 75%, rgba(255,255,255,0.07), transparent 2px),
    radial-gradient(circle 2px at 35% 85%, rgba(255,255,255,0.09), transparent 3px);
  animation: stars-twinkle 8s ease-in-out infinite alternate;
}

@keyframes atmo-drift {
  0% { transform: translate(0, 0) rotate(0deg); }
  100% { transform: translate(30px, -20px) rotate(2deg); }
}
@keyframes stars-twinkle {
  0% { opacity: 0.4; }
  100% { opacity: 0.8; }
}

/* --- NAVIGATION --- */
.nav-bar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 clamp(20px, 4vw, 64px);
  height: 64px;
  background: rgba(5,10,20,0.7);
  backdrop-filter: blur(24px) saturate(1.4);
  -webkit-backdrop-filter: blur(24px) saturate(1.4);
  border-bottom: 1px solid rgba(148,190,235,0.08);
  transition: background 0.4s ease;
}
.nav-brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.nav-logo {
  width: 32px;
  height: 32px;
  border-radius: 10px;
  background: linear-gradient(135deg, #176bff, #21c7ff 50%, #38e7a2);
  box-shadow: 0 0 24px rgba(50,183,255,0.3);
  position: relative;
  overflow: hidden;
}
.nav-logo::after {
  content: '';
  position: absolute;
  top: 20%;
  left: 25%;
  width: 30%;
  height: 30%;
  border-radius: 50%;
  background: rgba(255,255,255,0.5);
  filter: blur(2px);
}
.nav-title {
  font-weight: 800;
  font-size: 16px;
  letter-spacing: -0.03em;
}
.nav-sub {
  font-size: 11px;
  color: var(--mist);
  font-weight: 500;
}
.nav-links {
  display: flex;
  gap: 6px;
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
}
.nav-link:hover {
  background: var(--glass-hover);
  color: var(--white);
}
.nav-link.active {
  background: rgba(50,183,255,0.15);
  color: var(--noon);
  border-color: rgba(50,183,255,0.3);
}

/* --- SCROLL REVEAL --- */
.reveal {
  opacity: 0;
  transform: translateY(32px);
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

@media (prefers-reduced-motion: reduce) {
  .reveal { opacity: 1; transform: none; transition: none; }
  .atmo::before, .atmo::after { animation: none; }
  @keyframes atmo-drift { 0%, 100% { transform: none; } }
  @keyframes stars-twinkle { 0%, 100% { opacity: 0.6; } }
}

/* --- LAYOUT --- */
.page { position: relative; z-index: 1; padding-top: 64px; }
.container { width: min(1120px, calc(100% - 48px)); margin: 0 auto; }
.section { padding: 80px 0; }
.section-compact { padding: 40px 0; }

/* --- HERO --- */
.hero {
  min-height: calc(100vh - 64px);
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  text-align: center;
  padding: 80px 24px 120px;
  position: relative;
  overflow: hidden;
}
.hero-glow {
  position: absolute;
  width: 600px;
  height: 600px;
  border-radius: 50%;
  filter: blur(120px);
  opacity: 0.3;
  animation: hero-pulse 8s ease-in-out infinite alternate;
}
.hero-glow-1 { background: var(--noon); top: -200px; left: 10%; }
.hero-glow-2 { background: var(--dusk); bottom: -200px; right: 10%; animation-delay: 3s; }
.hero-glow-3 { background: var(--safe); top: 50%; left: 50%; transform: translate(-50%,-50%); width: 400px; height: 400px; animation-delay: 5s; }

@keyframes hero-pulse {
  0% { opacity: 0.15; transform: scale(0.9); }
  100% { opacity: 0.35; transform: scale(1.1); }
}

.hero-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 16px;
  border-radius: 999px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  font-size: 12px;
  font-weight: 600;
  color: var(--cloud);
  margin-bottom: 32px;
  backdrop-filter: blur(12px);
}
.hero-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--safe);
  animation: dot-pulse 2s ease-in-out infinite;
}
@keyframes dot-pulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(53,232,164,0.4); }
  50% { opacity: 0.7; box-shadow: 0 0 0 6px rgba(53,232,164,0); }
}
.hero-title {
  font-size: clamp(48px, 8vw, 96px);
  font-weight: 900;
  letter-spacing: -0.04em;
  line-height: 0.9;
  margin-bottom: 20px;
  background: linear-gradient(135deg, var(--white) 0%, var(--cloud) 50%, var(--noon) 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.hero-subtitle {
  font-size: clamp(16px, 2vw, 22px);
  color: var(--mist);
  font-weight: 400;
  max-width: 560px;
  line-height: 1.6;
  margin-bottom: 48px;
}
.hero-metrics {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  justify-content: center;
}
.hero-metric {
  padding: 20px 28px;
  border-radius: var(--radius-lg);
  background: var(--glass);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(20px);
  text-align: center;
  min-width: 140px;
  transition: all 0.4s var(--ease-out);
}
.hero-metric:hover {
  background: var(--glass-hover);
  transform: translateY(-4px);
  box-shadow: var(--shadow-md);
}
.hero-metric-value {
  font-size: 36px;
  font-weight: 800;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.hero-metric-label {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--mist);
  margin-top: 4px;
}
.hero-scroll {
  position: absolute;
  bottom: 40px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  color: var(--mist);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  animation: scroll-hint 2s ease-in-out infinite;
}
.hero-scroll-line {
  width: 1px;
  height: 40px;
  background: linear-gradient(to bottom, var(--mist), transparent);
}
@keyframes scroll-hint {
  0%, 100% { opacity: 0.5; transform: translateX(-50%) translateY(0); }
  50% { opacity: 1; transform: translateX(-50%) translateY(8px); }
}

/* --- STATUS BADGE --- */
.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.02em;
  border: 1px solid currentColor;
  backdrop-filter: blur(8px);
}
.status-safe { color: var(--safe); background: rgba(53,232,164,0.08); }
.status-watch { color: var(--watch); background: rgba(255,208,82,0.08); }
.status-rain { color: var(--alert); background: rgba(255,147,70,0.08); }
.status-danger { color: var(--danger); background: rgba(255,71,120,0.08); }
.status-limited { color: var(--limited); background: rgba(155,168,255,0.08); }

/* --- GLASS CARD --- */
.glass {
  background: var(--glass);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-lg);
  padding: 28px;
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  transition: all 0.4s var(--ease-out);
}
.glass:hover {
  background: var(--glass-hover);
  border-color: rgba(148,190,235,0.2);
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}
.glass-static:hover { transform: none; }

/* --- SECTION HEADERS --- */
.section-header {
  margin-bottom: 40px;
}
.section-overline {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--noon);
  margin-bottom: 12px;
}
.section-title {
  font-size: clamp(28px, 4vw, 44px);
  font-weight: 800;
  letter-spacing: -0.03em;
  line-height: 1.1;
  margin-bottom: 12px;
}
.section-desc {
  font-size: 16px;
  color: var(--mist);
  max-width: 520px;
  line-height: 1.6;
}

/* --- FORECAST MORPH TABS --- */
.day-tabs {
  display: flex;
  gap: 4px;
  padding: 4px;
  border-radius: 999px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  width: fit-content;
  margin: 0 auto 48px;
}
.day-tab {
  padding: 10px 24px;
  border-radius: 999px;
  font-size: 14px;
  font-weight: 600;
  color: var(--mist);
  cursor: pointer;
  transition: all 0.3s var(--ease-out);
  border: none;
  background: transparent;
  font-family: inherit;
}
.day-tab:hover { color: var(--snow); }
.day-tab.active {
  background: rgba(50,183,255,0.15);
  color: var(--white);
  box-shadow: 0 4px 16px rgba(50,183,255,0.15);
}
.day-panel { display: none; }
.day-panel.active { display: block; animation: panel-in 0.6s var(--ease-out); }
@keyframes panel-in {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: translateY(0); }
}

/* --- DECISION BLOCK --- */
.decision {
  display: grid;
  grid-template-columns: 1fr 360px;
  gap: 20px;
  margin-top: 32px;
}
.decision-main {
  border-radius: var(--radius-xl);
  padding: 40px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(20px);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 240px;
  position: relative;
  overflow: hidden;
}
.decision-main::before {
  content: '';
  position: absolute;
  top: -100px;
  right: -100px;
  width: 300px;
  height: 300px;
  border-radius: 50%;
  background: var(--accent-glow, rgba(50,183,255,0.06));
  filter: blur(60px);
}
.decision-title {
  font-size: clamp(28px, 4vw, 48px);
  font-weight: 800;
  letter-spacing: -0.03em;
  line-height: 1.05;
  margin-top: 16px;
  position: relative;
}
.decision-desc {
  color: var(--cloud);
  font-size: 15px;
  line-height: 1.6;
  margin-top: 16px;
  position: relative;
}
.kpi-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.kpi-card {
  border-radius: var(--radius-md);
  padding: 20px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(16px);
}
.kpi-label {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--mist);
}
.kpi-value {
  font-size: 24px;
  font-weight: 800;
  letter-spacing: -0.02em;
  margin-top: 8px;
  font-variant-numeric: tabular-nums;
}
.kpi-sub {
  font-size: 12px;
  color: var(--cloud);
  margin-top: 2px;
}

/* --- SVG RAIN CURVE --- */
.curve-container {
  position: relative;
  padding: 24px 0;
}
.rain-curve {
  width: 100%;
  height: 200px;
}
.rain-curve path.curve-fill {
  opacity: 0.15;
}
.rain-curve path.curve-line {
  fill: none;
  stroke-width: 2.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.rain-curve circle.curve-dot {
  stroke: var(--abyss);
  stroke-width: 2;
  cursor: pointer;
  transition: r 0.2s ease;
}
.rain-curve circle.curve-dot:hover { r: 7; }
.rain-curve text.curve-label {
  fill: var(--mist);
  font-size: 11px;
  font-weight: 600;
  font-family: 'Inter', system-ui;
  text-anchor: middle;
}
.rain-curve text.curve-value {
  fill: var(--snow);
  font-size: 12px;
  font-weight: 700;
  font-family: 'Inter', system-ui;
  text-anchor: middle;
}

/* --- PERIOD CARDS --- */
.period-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.period-card {
  border-radius: var(--radius-lg);
  padding: 24px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(16px);
  position: relative;
  overflow: hidden;
  transition: all 0.4s var(--ease-out);
}
.period-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--shadow-md);
}
.period-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--accent-color, var(--noon));
  border-radius: 3px 3px 0 0;
  opacity: 0.6;
}
.period-name {
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--mist);
  margin-bottom: 12px;
}
.period-condition {
  font-size: 18px;
  font-weight: 700;
  margin-bottom: 16px;
}
.period-stats {
  display: flex;
  gap: 16px;
}
.period-stat-value {
  font-size: 18px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.period-stat-label {
  font-size: 10px;
  color: var(--mist);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

/* --- ACTIVITY CARDS --- */
.activity-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}
.activity-card {
  border-radius: var(--radius-lg);
  padding: 24px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(16px);
  border-left: 3px solid var(--accent-color, var(--noon));
  transition: all 0.4s var(--ease-out);
}
.activity-card:hover {
  transform: translateY(-2px);
  background: var(--glass-hover);
}
.activity-name {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 8px;
}
.activity-status {
  font-size: 20px;
  font-weight: 800;
  margin-bottom: 6px;
}
.activity-advice {
  font-size: 13px;
  color: var(--mist);
}

/* --- HOURLY DETAIL --- */
.hourly-section {
  border-radius: var(--radius-xl);
  background: var(--glass);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(16px);
  overflow: hidden;
}
.hourly-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 28px;
  cursor: pointer;
  font-weight: 700;
  font-size: 15px;
  color: var(--snow);
  user-select: none;
  transition: background 0.3s ease;
  border: none;
  background: transparent;
  width: 100%;
  text-align: left;
  font-family: inherit;
}
.hourly-toggle:hover { background: var(--glass-hover); }
.hourly-chevron {
  transition: transform 0.4s var(--ease-out);
  color: var(--mist);
}
.hourly-section[open] .hourly-chevron { transform: rotate(180deg); }
.hourly-list { padding: 0 20px 20px; display: grid; gap: 8px; }
.hour-row {
  display: grid;
  grid-template-columns: 72px 1fr repeat(4, minmax(80px, 0.5fr));
  gap: 12px;
  align-items: center;
  padding: 14px 16px;
  border-radius: var(--radius-md);
  border-left: 3px solid var(--accent-color, var(--noon));
  background: rgba(255,255,255,0.02);
  transition: background 0.3s ease;
}
.hour-row:hover { background: rgba(255,255,255,0.04); }
.hour-time {
  font-size: 20px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.hour-condition {
  font-size: 14px;
  font-weight: 600;
}
.hour-status {
  font-size: 12px;
  color: var(--mist);
}
.hour-box {
  text-align: center;
  padding: 8px;
  border-radius: var(--radius-sm);
  background: rgba(15,34,64,0.6);
  border: 1px solid rgba(74,133,196,0.2);
}
.hour-box-value {
  font-size: 15px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.hour-box-label {
  font-size: 9px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--mist);
}

/* --- MAP --- */
.map-wrapper {
  border-radius: var(--radius-xl);
  overflow: hidden;
  border: 1px solid var(--glass-border);
  background: var(--void);
}
.map-frame {
  width: 100%;
  height: 480px;
  border: 0;
  display: block;
}
.map-actions {
  display: flex;
  gap: 8px;
  padding: 16px;
  background: var(--glass);
}

/* --- LOCATION PORTAL CARDS --- */
.location-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 20px;
}
.location-card {
  border-radius: var(--radius-xl);
  padding: 28px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(20px);
  position: relative;
  overflow: hidden;
  transition: all 0.5s var(--ease-out);
  cursor: pointer;
}
.location-card:hover {
  transform: translateY(-6px);
  box-shadow: 0 20px 60px rgba(0,0,0,0.3);
  border-color: var(--accent-color, rgba(50,183,255,0.3));
}
.location-card::before {
  content: '';
  position: absolute;
  top: -60px;
  right: -60px;
  width: 200px;
  height: 200px;
  border-radius: 50%;
  background: var(--accent-glow, rgba(53,232,164,0.05));
  filter: blur(40px);
  transition: opacity 0.5s ease;
}
.location-card:hover::before { opacity: 1.5; }
.location-name {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: -0.02em;
  margin: 12px 0 8px;
  position: relative;
}
.location-desc {
  font-size: 14px;
  color: var(--cloud);
  line-height: 1.5;
  margin-bottom: 20px;
  position: relative;
}
.location-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-bottom: 20px;
  position: relative;
}
.location-stat {
  padding: 10px;
  border-radius: var(--radius-sm);
  background: rgba(15,34,64,0.5);
  text-align: center;
}
.location-stat-value {
  font-size: 18px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.location-stat-label {
  font-size: 9px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--mist);
  margin-top: 2px;
}
.location-actions {
  display: flex;
  gap: 8px;
  position: relative;
}

/* --- BUTTONS --- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 10px 20px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 700;
  font-family: inherit;
  border: 1px solid var(--glass-border);
  background: var(--glass);
  color: var(--snow);
  cursor: pointer;
  transition: all 0.3s var(--ease-out);
  text-decoration: none;
  backdrop-filter: blur(8px);
}
.btn:hover {
  background: var(--glass-hover);
  transform: translateY(-1px);
}
.btn-primary {
  background: linear-gradient(135deg, rgba(50,183,255,0.2), rgba(50,183,255,0.1));
  border-color: rgba(50,183,255,0.3);
  color: var(--noon);
}
.btn-primary:hover {
  background: linear-gradient(135deg, rgba(50,183,255,0.3), rgba(50,183,255,0.15));
  box-shadow: 0 8px 24px rgba(50,183,255,0.15);
}

/* --- NOTICE --- */
.notice {
  padding: 12px 20px;
  border-radius: 999px;
  border: 1px solid rgba(255,208,82,0.2);
  background: rgba(255,208,82,0.04);
  color: var(--watch);
  font-size: 12px;
  font-weight: 600;
  text-align: center;
  margin: 24px 0;
}

/* --- SHARE BOX --- */
.share-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.share-text {
  width: 100%;
  min-height: 120px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-md);
  background: rgba(5,10,20,0.6);
  color: var(--snow);
  padding: 16px;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.5;
  resize: none;
}

/* --- TRUST SECTION --- */
.trust-bar {
  height: 4px;
  border-radius: 4px;
  background: rgba(255,255,255,0.06);
  overflow: hidden;
  margin-top: 12px;
}
.trust-fill {
  height: 100%;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--noon), var(--safe));
  transition: width 1.2s var(--ease-out);
}

/* --- FOOTER --- */
.footer {
  padding: 60px 0 40px;
  text-align: center;
}
.footer-brand {
  font-size: 28px;
  font-weight: 900;
  letter-spacing: -0.04em;
  background: linear-gradient(135deg, var(--cloud), var(--mist));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin-bottom: 12px;
}
.footer-version {
  font-size: 12px;
  color: var(--mist);
}
.footer-links {
  display: flex;
  gap: 16px;
  justify-content: center;
  flex-wrap: wrap;
  margin-top: 20px;
}
.footer-link {
  font-size: 12px;
  color: var(--noon);
  font-weight: 600;
  opacity: 0.7;
  transition: opacity 0.3s ease;
}
.footer-link:hover { opacity: 1; }

/* --- 3-DAY CARDS --- */
.day-card-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}
.day-overview-card {
  border-radius: var(--radius-xl);
  padding: 28px;
  background: var(--glass);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(16px);
  transition: all 0.4s var(--ease-out);
}
.day-overview-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--shadow-md);
}

/* --- SOURCE TABLE --- */
.source-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0 6px;
}
.source-table th {
  text-align: left;
  padding: 8px 14px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--noon);
}
.source-table td {
  padding: 12px 14px;
  font-size: 14px;
  background: rgba(255,255,255,0.02);
}
.source-table tr td:first-child { border-radius: var(--radius-sm) 0 0 var(--radius-sm); }
.source-table tr td:last-child { border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }

/* --- DIVIDER --- */
.divider {
  width: 60px;
  height: 2px;
  background: linear-gradient(90deg, var(--noon), transparent);
  margin: 60px auto;
  opacity: 0.4;
}

/* --- RESPONSIVE --- */
@media (max-width: 1024px) {
  .decision { grid-template-columns: 1fr; }
  .kpi-grid { grid-template-columns: 1fr 1fr; }
  .period-grid { grid-template-columns: repeat(2, 1fr); }
  .activity-grid { grid-template-columns: repeat(2, 1fr); }
  .day-card-grid { grid-template-columns: 1fr; }
  .share-grid { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  .hero { padding: 60px 20px 100px; min-height: auto; }
  .hero-title { font-size: clamp(40px, 10vw, 64px); }
  .hero-metrics { flex-direction: column; align-items: center; }
  .hero-metric { width: 100%; max-width: 280px; }
  .container { width: calc(100% - 32px); }
  .section { padding: 48px 0; }
  .nav-links { gap: 4px; }
  .nav-link { padding: 6px 12px; font-size: 12px; }
  .period-grid { grid-template-columns: 1fr 1fr; }
  .activity-grid { grid-template-columns: 1fr; }
  .location-grid { grid-template-columns: 1fr; }
  .hour-row {
    grid-template-columns: 60px 1fr;
    gap: 8px;
  }
  .hour-box { display: none; }
  .hour-box.hour-box-rain { display: block; grid-column: 1 / -1; }
  .map-frame { height: 360px; }
  .day-tabs { flex-wrap: wrap; justify-content: center; }
}

@media (max-width: 480px) {
  .hero-title { font-size: 40px; }
  .nav-bar { padding: 0 16px; height: 56px; }
  .nav-sub { display: none; }
  .period-grid { grid-template-columns: 1fr; }
  .kpi-grid { grid-template-columns: 1fr; }
}
'''

# ---------------------------------------------------------------------------
# v65 CINEMATIC JAVASCRIPT
# ---------------------------------------------------------------------------

JS_V65 = r'''
(function(){
  'use strict';

  /* --- Scroll Reveal --- */
  const revealEls = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    const obs = new IntersectionObserver(function(entries) {
      entries.forEach(function(e) {
        if (e.isIntersecting) {
          e.target.classList.add('visible');
          obs.unobserve(e.target);
        }
      });
    }, { threshold: 0.08, rootMargin: '0px 0px -40px 0px' });
    revealEls.forEach(function(el) { obs.observe(el); });
  } else {
    revealEls.forEach(function(el) { el.classList.add('visible'); });
  }

  /* --- Day Tabs --- */
  document.querySelectorAll('.day-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      var group = tab.closest('.day-tabs-container');
      if (!group) return;
      group.querySelectorAll('.day-tab').forEach(function(t) { t.classList.remove('active'); });
      group.querySelectorAll('.day-panel').forEach(function(p) { p.classList.remove('active'); });
      tab.classList.add('active');
      var target = group.querySelector('#' + tab.dataset.target);
      if (target) target.classList.add('active');
    });
  });

  /* --- Trust Bar Animation --- */
  document.querySelectorAll('.trust-fill').forEach(function(bar) {
    var w = bar.dataset.width || '0%';
    if ('IntersectionObserver' in window) {
      var obs2 = new IntersectionObserver(function(entries) {
        entries.forEach(function(e) {
          if (e.isIntersecting) {
            bar.style.width = w;
            obs2.unobserve(bar);
          }
        });
      }, { threshold: 0.2 });
      obs2.observe(bar);
    } else {
      bar.style.width = w;
    }
  });

  /* --- Nav scroll effect --- */
  var nav = document.querySelector('.nav-bar');
  if (nav) {
    var lastScroll = 0;
    window.addEventListener('scroll', function() {
      var y = window.pageYOffset || document.documentElement.scrollTop;
      if (y > 100) {
        nav.style.background = 'rgba(5,10,20,0.92)';
      } else {
        nav.style.background = 'rgba(5,10,20,0.7)';
      }
      lastScroll = y;
    }, { passive: true });
  }

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
        items = [("Hari ini", "anemos_app.html", "today"), ("3 hari ke depan", "anemos_3day.html", "3day"), ("Panduan Aktivitas", "anemos_activity.html", "activity"), ("Peta", "langit_map_room.html", "map")]
        subtitle = f'{api["location_name"]} · {VERSION}'
        href = "../index.html"
    links = "".join(f'<a class="nav-link {"active" if key == active else ""}" href="{esc(url)}">{esc(label)}</a>' for label, url, key in items)
    return f'''<header class="nav-bar">
  <a class="nav-brand" href="{href}">
    <span class="nav-logo"></span>
    <span><span class="nav-title">{BRAND}</span><span class="nav-sub">{esc(subtitle)}</span></span>
  </a>
  <nav class="nav-links">{links}</nav>
</header>'''


def v65_document(api: Dict[str, Any], active: str, title: str, body: str, root: bool = False) -> str:
    footer_links = ""
    if not root:
        footer_links = '''<div class="footer-links">
      <a class="footer-link" href="langit_model_court.html">Keandalan data</a>
      <a class="footer-link" href="sentinel_x_accuracy_public.html">Akurasi</a>
      <a class="footer-link" href="langit_api_v1.json">JSON</a>
      <a class="footer-link" href="langit_location.geojson">GeoJSON</a>
    </div>'''
    return f'''<!doctype html><html lang="id"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="theme-color" content="#050a14">
<meta name="description" content="{esc(title)} — Prakiraan cuaca premium untuk Indonesia">
<style>{CSS_V65}</style>
</head><body>
<div class="atmo"></div>
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
<script>{JS_V65}</script>
</body></html>'''


def v65_hero(api: Dict[str, Any], heading: str, subtitle: str, day: Dict[str, Any], show_metrics: bool = True) -> str:
    cls = day.get("risk_class", "watch")
    metrics = ""
    if show_metrics:
        metrics = f'''<div class="hero-metrics reveal reveal-delay-3">
        <div class="hero-metric">
          <div class="hero-metric-value">{deg(day.get("avg_temp_c"))}</div>
          <div class="hero-metric-label">Suhu udara rata-rata</div>
        </div>
        <div class="hero-metric">
          <div class="hero-metric-value">{pct(day.get("peak_rain_probability"))}</div>
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
    colors = {"safe": "rgba(53,232,164,0.08)", "watch": "rgba(255,208,82,0.08)", "rain": "rgba(255,147,70,0.08)", "danger": "rgba(255,71,120,0.08)", "limited": "rgba(155,168,255,0.08)"}
    return colors.get(cls, "rgba(50,183,255,0.06)")


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
        <div class="kpi-card"><div class="kpi-label">Risiko</div><div class="kpi-value">{round(clamp(day.get("risk_score"))):.0f}<span style="font-size:16px;color:var(--mist)">/100</span></div><div class="kpi-sub">{esc(day.get("risk_label"))}</div></div>
        <div class="kpi-card"><div class="kpi-label">Puncak hujan</div><div class="kpi-value">{pct(day.get("peak_rain_probability"))}</div><div class="kpi-sub">{esc(day.get("peak_rain_hour","—"))}</div></div>
        <div class="kpi-card"><div class="kpi-label">Jam nyaman</div><div class="kpi-value" style="font-size:16px">{esc(windows)}</div><div class="kpi-sub">aktivitas</div></div>
        <div class="kpi-card"><div class="kpi-label">Panas terasa</div><div class="kpi-value">{deg(day.get("max_heat_c"))}</div><div class="kpi-sub">maksimum</div></div>
      </aside>
    </div>
  </div></section>'''


def _svg_rain_curve(hours: List[Dict[str, Any]]) -> str:
    if not hours:
        return ""
    n = len(hours)
    w, h = 900, 160
    pad_x, pad_top, pad_bot = 50, 20, 40
    inner_w = w - 2 * pad_x
    inner_h = h - pad_top - pad_bot
    max_rain = max(prob(x.get("rain_probability"), 0) or 0 for x in hours) or 100

    points = []
    for i, hr in enumerate(hours):
        x = pad_x + (i / max(1, n - 1)) * inner_w if n > 1 else w / 2
        rain_val = prob(hr.get("rain_probability"), 0) or 0
        y = pad_top + inner_h - (rain_val / max(max_rain, 1)) * inner_h
        points.append((x, y, hr, rain_val))

    if not points:
        return ""

    # Build smooth curve path using Catmull-Rom → cubic bezier approximation
    def catmull_to_bezier(pts: List[Tuple[float, float]]) -> str:
        if len(pts) < 2:
            return f"M {pts[0][0]},{pts[0][1]}" if pts else ""
        d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
        if len(pts) == 2:
            d += f" L {pts[1][0]:.1f},{pts[1][1]:.1f}"
            return d
        for i in range(len(pts) - 1):
            p0 = pts[max(0, i - 1)]
            p1 = pts[i]
            p2 = pts[min(len(pts) - 1, i + 1)]
            p3 = pts[min(len(pts) - 1, i + 2)]
            cp1x = p1[0] + (p2[0] - p0[0]) / 6
            cp1y = p1[1] + (p2[1] - p0[1]) / 6
            cp2x = p2[0] - (p3[0] - p1[0]) / 6
            cp2y = p2[1] - (p3[1] - p1[1]) / 6
            d += f" C {cp1x:.1f},{cp1y:.1f} {cp2x:.1f},{cp2y:.1f} {p2[0]:.1f},{p2[1]:.1f}"
        return d

    xy_pts = [(p[0], p[1]) for p in points]
    curve_path = catmull_to_bezier(xy_pts)

    # Fill path (close to bottom)
    fill_path = curve_path + f" L {points[-1][0]:.1f},{h - pad_bot} L {points[0][0]:.1f},{h - pad_bot} Z"

    # Determine dominant risk color
    worst_cls = max(hours, key=lambda z: clamp(z.get("risk_score"), default=0)).get("risk_class", "safe")
    color = risk_color(worst_cls)

    # Build dots and labels
    dots = ""
    labels = ""
    values = ""
    for i, (x, y, hr, rain_val) in enumerate(points):
        cls = hr.get("risk_class", "safe")
        c = risk_color(cls)
        dots += f'<circle class="curve-dot" cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{c}"/>'
        labels += f'<text class="curve-label" x="{x:.1f}" y="{h - 8}">{esc(hr.get("hour",""))}</text>'
        if rain_val > 0:
            values += f'<text class="curve-value" x="{x:.1f}" y="{max(12, y - 12)}">{round(rain_val)}%</text>'

    return f'''<div class="curve-container">
    <svg class="rain-curve" viewBox="0 0 {w} {h}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="curveGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="{color}" stop-opacity="0.3"/>
          <stop offset="100%" stop-color="{color}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <path class="curve-fill" d="{fill_path}" fill="url(#curveGrad)"/>
      <path class="curve-line" d="{curve_path}" stroke="{color}"/>
      {dots}
      {values}
      {labels}
    </svg>
  </div>'''


def v65_timeline(hours: List[Dict[str, Any]], title: str = "Timeline hujan", note: str = "") -> str:
    svg = _svg_rain_curve(hours[:12])
    return f'''<section class="section-compact"><div class="container">
    <div class="glass glass-static reveal">
      <div class="section-header">
        <div class="section-overline">Timeline</div>
        <h2 style="font-size:24px;font-weight:800;letter-spacing:-0.02em">{esc(title)}</h2>
        <p style="font-size:14px;color:var(--mist);margin-top:4px">{esc(note)}</p>
      </div>
      {svg}
    </div>
  </div></section>'''


def v65_periods(day: Dict[str, Any]) -> str:
    cards = []
    period_gradients = {"Pagi": "rgba(255,143,92,0.08)", "Siang": "rgba(50,183,255,0.08)", "Sore": "rgba(155,109,255,0.08)", "Malam": "rgba(107,138,181,0.05)"}
    for i, p in enumerate(day.get("periods", [])):
        cls = p.get("risk_class", "limited")
        bg = period_gradients.get(p.get("name", ""), "transparent")
        cards.append(f'''<div class="period-card reveal reveal-delay-{i+1}" style="--accent-color:{risk_color(cls)};background:linear-gradient(180deg,{bg},var(--glass))">
        <div class="period-name">{esc(p.get("name"))}</div>
        <div class="period-condition">{esc(p.get("condition"))}</div>
        <div class="period-stats">
          <div><div class="period-stat-value">{deg(p.get("temp_c"))}</div><div class="period-stat-label">Suhu udara</div></div>
          <div><div class="period-stat-value">{pct(p.get("rain_probability"))}</div><div class="period-stat-label">Peluang hujan</div></div>
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
        <div class="hour-box"><div class="hour-box-value">{deg(x.get("temp_c"))}</div><div class="hour-box-label">Suhu udara</div></div>
        <div class="hour-box"><div class="hour-box-value">{pct(x.get("humidity_pct"))}</div><div class="hour-box-label">Kelembapan</div></div>
        <div class="hour-box"><div class="hour-box-value">{deg(x.get("heat_index_c"))}</div><div class="hour-box-label">Indeks panas</div></div>
        <div class="hour-box hour-box-rain"><div class="hour-box-value">{pct(x.get("rain_probability"))}</div><div class="hour-box-label">Peluang hujan</div></div>
      </div>''')
    return f'''<section class="section-compact"><div class="container">
    <details class="hourly-section reveal">
      <summary class="hourly-toggle">
        <span>Detail per jam · {esc(day.get("date_label"))}</span>
        <svg class="hourly-chevron" width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M5 8l5 5 5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
      </summary>
      <div class="hourly-list">{"".join(rows)}</div>
    </details>
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
                },
            })
    return {"type": "FeatureCollection", "features": features}


def v65_map_page(title: str, geojson: Dict[str, Any], back_href: str) -> str:
    data = json.dumps(geojson, ensure_ascii=False)
    css = r'''
html,body,#map{height:100%;margin:0;background:#050a14;color:#f8fbff;font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif}
.hud{position:absolute;z-index:900;left:24px;top:24px;width:min(340px,calc(100% - 48px));padding:24px;border-radius:24px;background:rgba(5,10,20,0.85);backdrop-filter:blur(24px);border:1px solid rgba(148,190,235,0.12);box-shadow:0 24px 80px rgba(0,0,0,0.4)}
.hud h1{font-size:22px;font-weight:800;letter-spacing:-0.02em;line-height:1.1;margin:0 0 8px}
.hud p{margin:0;color:#6b8ab5;font-size:13px;line-height:1.5}
.btn{display:inline-flex;margin-top:16px;padding:10px 20px;border-radius:999px;background:rgba(50,183,255,0.15);border:1px solid rgba(50,183,255,0.3);color:#32b7ff;text-decoration:none;font-weight:700;font-size:13px;transition:all 0.3s ease}
.btn:hover{background:rgba(50,183,255,0.25)}
.timebar{position:absolute;z-index:900;left:50%;bottom:24px;transform:translateX(-50%);display:flex;gap:6px;max-width:calc(100% - 48px);overflow:auto;padding:8px;border-radius:999px;background:rgba(5,10,20,0.85);border:1px solid rgba(148,190,235,0.12);backdrop-filter:blur(16px)}
.tbtn{border:1px solid rgba(148,190,235,0.2);background:rgba(255,255,255,0.04);color:#a8c4e0;border-radius:999px;padding:8px 14px;font-weight:700;font-size:13px;cursor:pointer;font-family:inherit;transition:all 0.3s ease}
.tbtn.active{background:rgba(50,183,255,0.2);border-color:rgba(50,183,255,0.4);color:#32b7ff}
.tbtn:hover{background:rgba(255,255,255,0.08)}
.legend{position:absolute;right:24px;bottom:24px;z-index:901;background:rgba(5,10,20,0.85);border:1px solid rgba(148,190,235,0.12);border-radius:16px;padding:14px;font-size:12px;backdrop-filter:blur(16px)}
.legend div{display:flex;gap:8px;align-items:center;margin:4px 0;color:#a8c4e0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--c)}
.leaflet-control-attribution{background:rgba(5,10,20,0.8)!important;color:#6b8ab5!important;font-size:10px!important}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:rgba(10,22,40,0.95);color:#f8fbff;border:1px solid rgba(148,190,235,0.15);backdrop-filter:blur(12px)}
.leaflet-popup-content{font-family:'Inter',system-ui;font-size:13px;line-height:1.5}
.leaflet-popup-content b{font-size:15px;font-weight:800}
@media(max-width:700px){.hud{left:12px;top:12px;width:calc(100% - 24px)}.legend{right:12px;bottom:80px}.timebar{bottom:16px}}
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
    const cls = p.risk_class || 'limited', color = colors[cls] || '#32b7ff';
    const risk = Math.max(Number(p.risk_score||0), Number(p.rain_probability||0));
    const radius = 1300 + risk * 34;
    L.circle(latlng, {radius:radius, color:color, weight:2, opacity:.8, fillColor:color, fillOpacity:.12}).bindPopup(ptxt(p)).addTo(layer);
    L.circleMarker(latlng, {radius:6, color:'#fff', weight:1, fillColor:color, fillOpacity:1}).bindPopup(ptxt(p)).addTo(layer);
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
  document.body.insertAdjacentHTML('beforeend','<div style="position:absolute;inset:0;display:grid;place-items:center;color:#6b8ab5">Peta gagal ditampilkan. Coba muat ulang halaman.</div>');
}
'''.replace("__DATA__", data)
    legend = '<div class="legend"><div><i class="dot" style="--c:#35e8a4"></i>Aman</div><div><i class="dot" style="--c:#ffd052"></i>Perlu diperhatikan</div><div><i class="dot" style="--c:#ff9346"></i>Waspada</div><div><i class="dot" style="--c:#ff4778"></i>Berpotensi signifikan</div></div>'
    return f'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>{css}</style></head><body><div id="map"></div><section class="hud"><h1>{esc(title)}</h1><p>Klik titik lokasi untuk rincian prakiraan.</p><a class="btn" href="{esc(back_href)}">Kembali</a></section><div id="timebar" class="timebar"></div>{legend}<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>{js}</script></body></html>'''


# ---------------------------------------------------------------------------
# v65 Page builders
# ---------------------------------------------------------------------------

def v65_today_page(api: Dict[str, Any]) -> str:
    day = api["today"]
    heading = f"Prakiraan\n{api['location_name']}"
    sub = f"{day['date_label']}. {decision_sentence(api['location_name'], day, short=True)}"
    body = v65_hero(api, heading, sub, day)
    body += v65_notice()
    body += v65_decision(api, day)
    body += v65_timeline(day["hours"], "Timeline hujan", day["date_label"])
    body += v65_periods(day)
    body += v65_activities(day)
    body += v65_map_embed()
    body += v65_hours(day)
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
    for name in ["sentinel_x_accuracy_summary.json", "verification_summary.json", "accuracy_summary.json", "sentinel_verification_summary.json"]:
        obj = read_json(directory / name, {})
        if isinstance(obj, dict) and obj:
            summary = obj
            break
    matched = int(num(pick(summary, "matched_cases", "pairs", "n", default=0), 0) or 0)
    target = int(num(pick(summary, "target_cases", "minimum_cases", default=30), 30) or 30)
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
        body += f'''<section class="section-compact"><div class="container">
      <div class="day-card-grid reveal">
        <div class="glass glass-static"><div class="kpi-label">Error suhu</div><div class="kpi-value" style="margin-top:8px">{esc(pick(summary,"mae_temp","temperature_mae",default="—"))}</div></div>
        <div class="glass glass-static"><div class="kpi-label">Skor hujan</div><div class="kpi-value" style="margin-top:8px">{esc(pick(summary,"rain_score","brier_score",default="—"))}</div></div>
        <div class="glass glass-static"><div class="kpi-label">Alarm keliru</div><div class="kpi-value" style="margin-top:8px">{esc(pick(summary,"false_alarm_rate",default="—"))}</div></div>
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
        cards.append(f'''<a class="location-card reveal reveal-delay-{(i % 3) + 1}" href="{esc(api["location_slug"])}/anemos_app.html" style="--accent-color:{risk_color(cls)};--accent-glow:{_accent_glow(cls)}">
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
          <span class="btn" onclick="event.preventDefault();window.location.href=\'{esc(api["location_slug"])}/anemos_3day.html\'">3 hari</span>
          <span class="btn" onclick="event.preventDefault();window.location.href=\'{esc(api["location_slug"])}/anemos_activity.html\'">Aktivitas</span>
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
        d = api["today"]
        h = d.get("peak_rain_hour") or (d.get("hours") or [{}])[0].get("hour", "00:00")
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "location_name": api.get("location_name"), "slug": api.get("location_slug"),
                "date": d.get("date_label"), "hour": h,
                "rain_probability": d.get("peak_rain_probability"), "risk_score": d.get("risk_score"),
                "risk_class": d.get("risk_class"), "risk_label": d.get("risk_label"),
                "condition": d.get("condition"), "temp_c": d.get("avg_temp_c"),
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
        gj = v65_geo_for_api(api)
        write_json(d / "langit_api_v1.json", api)
        write_json(d / "langit_location.geojson", gj)
        write_json(d / "langit_map_layers.json", {"brand": BRAND, "version": VERSION, "geojson": gj})
        # Main pages
        write_text(d / "anemos_app.html", v65_today_page(api))
        write_text(d / "langit_app.html", v65_today_page(api))
        write_text(d / "anemos_today.html", v65_today_page(api))
        write_text(d / "anemos_3day.html", v65_three_day_page(api))
        write_text(d / "langit_3day.html", v65_three_day_page(api))
        write_text(d / "anemos_activity.html", v65_activity_page(api))
        write_text(d / "langit_activity.html", v65_activity_page(api))
        write_text(d / "langit_model_court.html", v65_data_page(api))
        write_text(d / "sentinel_x_accuracy_public.html", v65_accuracy_page(api, d))
        map_html = v65_map_page(f"LANGIT Map — {api['location_name']}", gj, "anemos_app.html")
        write_text(d / "langit_map_room.html", map_html)
        write_text(d / "anemos_map.html", map_html)
        # Overwrite legacy pages
        write_text(d / "command_center_sentinel_x.html", v65_today_page(api))
        write_text(d / "langit_planer.html", v65_planner_page(api))
        write_text(d / "langit_planner.html", v65_planner_page(api))
        write_text(d / "anemos_commute_advice.html", v65_activity_page(api))
        write_text(d / "anemos_laundry_advice.html", v65_activity_page(api))
        write_text(d / "anemos_public_landing.html", v65_today_page(api))
        write_text(d / "langit_public_landing.html", v65_today_page(api))
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
