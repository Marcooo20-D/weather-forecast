#!/usr/bin/env python3
"""
LANGIT Omega Product Reconstruction

Post-processes existing LANGIT outputs into a compact, data-driven weather
intelligence interface. This file does not fetch weather data and does not
change the forecast engine; it reads the current outputs/ contract and rewrites
public HTML/JSON artefacts.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from langit_v65_cinematic_rebuild import (
    BRAND,
    VERSION,
    TZ_NAME,
    DISCLAIMER,
    clamp,
    esc,
    fmt_date,
    fmt_update,
    hour,
    hour_int,
    load_location_api,
    location_dirs,
    metadata_by_slug,
    num,
    pct,
    prob,
    read_csv,
    read_json,
    risk_color,
    risk_label,
    safe_find_file,
    slugify,
    text,
    v65_geo_for_api,
    v65_map_page,
    v65_portal_geo,
    write_json,
    write_text,
)

OMEGA_VERSION = "LANGIT Omega 1.0"


VARIABLE_MODEL: Dict[str, Dict[str, Any]] = {
    "rain": {
        "label": "Hujan",
        "field": "rainProbability",
        "source_fields": ["rain_probability", "prob_rain", "peluang_hujan_pct", "rain_mean"],
        "unit": "%",
        "priority": "P0",
        "display": "utama",
        "chart": "bar-area",
        "map_layer": "rain",
        "level": 1,
    },
    "temperature": {
        "label": "Suhu",
        "field": "temperature",
        "source_fields": ["temp_c", "temp_p50", "suhu_C", "temp_mean"],
        "unit": "C",
        "priority": "P0",
        "display": "utama",
        "chart": "line",
        "map_layer": "temp",
        "level": 1,
    },
    "risk": {
        "label": "Risiko",
        "field": "riskScore",
        "source_fields": ["risk_score", "rain_threat_score", "forecast_stress_index"],
        "unit": "/100",
        "priority": "P0",
        "display": "utama",
        "chart": "bar",
        "map_layer": "risiko",
        "level": 1,
    },
    "wind": {
        "label": "Angin",
        "field": "windSpeed",
        "source_fields": ["wind_kmh", "wind_p50", "angin_kmh"],
        "unit": "km/jam",
        "priority": "P1",
        "display": "compact",
        "chart": "line",
        "map_layer": "wind",
        "level": 1,
    },
    "humidity": {
        "label": "Kelembapan",
        "field": "humidity",
        "source_fields": ["humidity_pct", "rh_p50", "kelembapan_RH_pct", "rh_mean"],
        "unit": "%",
        "priority": "P1",
        "display": "compact",
        "chart": "line",
        "map_layer": "humidity",
        "level": 2,
    },
    "heat": {
        "label": "Indeks panas",
        "field": "heatIndex",
        "source_fields": ["heat_index_c", "heat_index_p50", "heat_index_C", "apparent_temperature_c"],
        "unit": "C",
        "priority": "P1",
        "display": "compact",
        "chart": "line",
        "map_layer": None,
        "level": 2,
    },
    "cloud": {
        "label": "Awan",
        "field": "cloudCover",
        "source_fields": ["cloud_pct", "cloud_p50", "tutupan_awan_pct"],
        "unit": "%",
        "priority": "P2",
        "display": "detail",
        "chart": "line",
        "map_layer": "cloud",
        "level": 3,
    },
    "rain_amount": {
        "label": "Curah hujan",
        "field": "rainAmount",
        "source_fields": ["rainAmount", "curah_hujan_p50_mm", "rain_p50", "rain_mm"],
        "unit": "mm",
        "priority": "P2",
        "display": "detail",
        "chart": "bar",
        "map_layer": "rain",
        "level": 3,
    },
    "confidence": {
        "label": "Keandalan",
        "field": "confidence",
        "source_fields": ["confidence", "confidence_score", "coverage_fraction", "confidence_score_base"],
        "unit": "%",
        "priority": "P2",
        "display": "detail",
        "chart": "line",
        "map_layer": None,
        "level": 4,
    },
    "pressure": {
        "label": "Tekanan",
        "field": "pressure",
        "source_fields": ["pressure_msl_hpa", "surface_pressure_hpa", "tekanan_udara_hpa"],
        "unit": "hPa",
        "priority": "P4",
        "display": "advanced",
        "chart": "line",
        "map_layer": None,
        "level": 4,
    },
    "visibility": {
        "label": "Jarak pandang",
        "field": "visibility",
        "source_fields": ["visibility_p10", "jarak_pandang_p10_m"],
        "unit": "m",
        "priority": "P4",
        "display": "advanced",
        "chart": "line",
        "map_layer": None,
        "level": 4,
    },
    "dew_point": {
        "label": "Dew point",
        "field": "dewPoint",
        "source_fields": ["dew_point_c", "dew_point_C"],
        "unit": "C",
        "priority": "P4",
        "display": "advanced",
        "chart": "line",
        "map_layer": None,
        "level": 4,
    },
    "gust": {
        "label": "Gust",
        "field": "windGust",
        "source_fields": ["gust_p90", "hembusan_angin_p90_kmh"],
        "unit": "km/jam",
        "priority": "P4",
        "display": "advanced",
        "chart": "line",
        "map_layer": None,
        "level": 4,
    },
    "uv": {
        "label": "UV",
        "field": "uvIndex",
        "source_fields": ["uv_index"],
        "unit": "index",
        "priority": "P4",
        "display": "advanced",
        "chart": "line",
        "map_layer": None,
        "level": 4,
    },
}


def now_wib() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return dt.datetime.now()


def parse_update_label(label: Any) -> Optional[dt.datetime]:
    raw = text(label)
    if not raw:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(20\d{2}),?\s+(\d{1,2}):(\d{2})", raw)
    if m:
        return dt.datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5)))
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})[T\s](\d{1,2}):(\d{2})", raw)
    if m:
        return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
    month_id = {
        "januari": 1,
        "februari": 2,
        "maret": 3,
        "april": 4,
        "mei": 5,
        "juni": 6,
        "juli": 7,
        "agustus": 8,
        "september": 9,
        "oktober": 10,
        "november": 11,
        "desember": 12,
    }
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)(?:\s+(20\d{2}))?,?\s+(\d{1,2}):(\d{2})", raw)
    if m and m.group(2).lower() in month_id:
        year = int(m.group(3) or now_wib().year)
        return dt.datetime(year, month_id[m.group(2).lower()], int(m.group(1)), int(m.group(4)), int(m.group(5)))
    return None


def format_update_label(value: Any) -> str:
    parsed = parse_update_label(value)
    if not parsed:
        return text(value, fmt_update())
    months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    return f"Diperbarui {parsed.day} {months[parsed.month - 1]} {parsed.year}, {parsed.hour:02d}:{parsed.minute:02d} WIB"


def update_source_for_location(root: Path, directory: Path) -> Optional[str]:
    batch = read_json(root / "forecast_batch_summary.json", {}) or {}
    if text(batch.get("generated_at")):
        return text(batch.get("generated_at"))
    summary = read_json(directory / "run_summary.json", {}) or {}
    if text(summary.get("generated_at")):
        return text(summary.get("generated_at"))
    return None


def target_dates_for_location(root: Path, slug: str) -> List[dt.date]:
    batch = read_json(root / "forecast_batch_summary.json", {}) or {}
    for loc in batch.get("locations", []) if isinstance(batch.get("locations"), list) else []:
        if text(loc.get("location_slug")) == slug:
            raw = text(loc.get("target_date"))
            dates: List[dt.date] = []
            for item in re.split(r"[,;\s]+", raw):
                if re.match(r"20\d{2}-\d{2}-\d{2}$", item):
                    try:
                        dates.append(dt.date.fromisoformat(item))
                    except Exception:
                        pass
            if dates:
                return dates[:3]
    summary = read_json(root / slug / "run_summary.json", {}) or {}
    raw = text(summary.get("target_date"))
    if re.match(r"20\d{2}-\d{2}-\d{2}$", raw):
        try:
            base = dt.date.fromisoformat(raw)
            return [base + dt.timedelta(days=i) for i in range(3)]
        except Exception:
            pass
    return []


def lock_api_dates(api: Dict[str, Any], target_dates: List[dt.date]) -> None:
    if not target_dates:
        return
    relatives = ["Hari ini", "Besok", "Lusa"]
    for i, day in enumerate(api.get("days", [])[: len(target_dates)]):
        d = target_dates[i]
        day["relative"] = relatives[i] if i < len(relatives) else f"H+{i}"
        day["date_iso"] = d.isoformat()
        day["date_label"] = fmt_date(d)
        day["date_short"] = fmt_date(d, False)
        for h in day.get("hours", []):
            h["date_iso"] = d.isoformat()
            h["date_label"] = day["date_label"]
            h["date_short"] = day["date_short"]
            h["relative"] = day["relative"]
    if api.get("today") and api.get("days"):
        api["today"] = api["days"][0]


def fmt_number(value: Any, decimals: int = 0, fallback: str = "-") -> str:
    x = num(value, None)
    if x is None:
        return fallback
    if decimals <= 0:
        return f"{round(x):.0f}"
    return f"{x:.{decimals}f}"


def non_empty_rate(values: Iterable[Any]) -> float:
    vals = list(values)
    if not vals:
        return 0.0
    ok = [v for v in vals if text(v) != "" and text(v).lower() not in {"nan", "none", "null"}]
    return len(ok) / len(vals)


def first_available(row: Dict[str, Any], fields: Iterable[str]) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in fields:
        if name in row and text(row.get(name)) != "":
            return row.get(name)
        key = name.lower()
        if key in lower and text(lower.get(key)) != "":
            return lower.get(key)
    return None


def enrich_from_sentinel(directory: Path, api: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for fname in ["sentinel_x.csv", "sentinel_x_variables.csv"]:
        for row in read_csv(safe_find_file(directory, fname)):
            date_key = text(row.get("target_date") or row.get("date") or api.get("today", {}).get("date_iso"))
            hour_key = hour(row.get("jam") or row.get("hour") or row.get("target_hour"))
            if not date_key or not hour_key:
                continue
            out.setdefault((date_key, hour_key), {}).update(row)
    return out


def semantic_point(api: Dict[str, Any], day: Dict[str, Any], h: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    date_iso = text(day.get("date_iso"))
    hr = hour(h.get("hour"))
    ts = f"{date_iso}T{hr}:00+07:00" if date_iso else hr
    rain_amount = first_available(extra, ["curah_hujan_p50_mm", "rain_p50", "rain_mm"])
    confidence_raw = first_available(extra, ["confidence_score_base", "confidence_score", "coverage_fraction"])
    confidence = num(confidence_raw, None)
    if confidence is not None and confidence <= 1:
        confidence *= 100
    return {
        "id": f"{api.get('location_slug')}-{date_iso}-{hr}",
        "locationName": api.get("location_name"),
        "locationSlug": api.get("location_slug"),
        "latitude": api.get("latitude"),
        "longitude": api.get("longitude"),
        "timestamp": ts,
        "dateIso": date_iso,
        "dateLabel": day.get("date_label"),
        "relative": day.get("relative"),
        "hour": hr,
        "updateTime": api.get("generated_at"),
        "condition": h.get("condition") or first_available(extra, ["cuaca", "dominant_category"]),
        "temperature": num(h.get("temp_c"), num(first_available(extra, ["suhu_C", "temp_p50"]))),
        "temperatureMin": None,
        "temperatureMax": None,
        "apparentTemperature": num(first_available(extra, ["apparent_temperature_c", "terasa_seperti_C"]), num(h.get("heat_index_c"))),
        "heatIndex": num(h.get("heat_index_c"), num(first_available(extra, ["heat_index_C", "heat_index_p50"]))),
        "humidity": num(h.get("humidity_pct"), num(first_available(extra, ["kelembapan_RH_pct", "rh_p50"]))),
        "dewPoint": num(first_available(extra, ["dew_point_c", "dew_point_C"])),
        "pressure": num(first_available(extra, ["pressure_msl_hpa", "surface_pressure_hpa", "tekanan_udara_hpa"])),
        "windSpeed": num(h.get("wind_kmh"), num(first_available(extra, ["angin_kmh", "wind_p50"]))),
        "windDirection": num(first_available(extra, ["wind_direction_deg", "arah_angin_derajat"])),
        "windGust": num(first_available(extra, ["gust_p90", "hembusan_angin_p90_kmh"])),
        "rainProbability": prob(h.get("rain_probability"), prob(first_available(extra, ["peluang_hujan_pct", "prob_rain"]))),
        "rainAmount": num(rain_amount),
        "rainIntensity": num(first_available(extra, ["rain_p90", "curah_hujan_p90_mm"])),
        "precipitationTotal": num(rain_amount),
        "cloudCover": num(h.get("cloud_pct"), num(first_available(extra, ["cloud_p50", "tutupan_awan_pct"]))),
        "visibility": num(first_available(extra, ["visibility_p10", "jarak_pandang_p10_m"])),
        "uvIndex": num(first_available(extra, ["uv_index"])),
        "solarRadiation": num(first_available(extra, ["solar_radiation"])),
        "weatherCode": None,
        "riskScore": num(h.get("risk_score"), num(first_available(extra, ["rain_threat_score", "forecast_stress_index"]))),
        "riskClass": h.get("risk_class"),
        "riskLabel": h.get("risk_label"),
        "dataQuality": text(first_available(extra, ["trust_level", "operational_status", "predictability"]), "public"),
        "source": text(first_available(extra, ["source_ids"]), "LANGIT ensemble"),
        "confidence": confidence,
    }


def build_semantic_pack(directory: Path, api: Dict[str, Any]) -> Dict[str, Any]:
    extra_by_time = enrich_from_sentinel(directory, api)
    points: List[Dict[str, Any]] = []
    for day in api.get("days", []):
        for h in day.get("hours", []):
            key = (text(day.get("date_iso")), hour(h.get("hour")))
            points.append(semantic_point(api, day, h, extra_by_time.get(key, {})))
    variables = variable_inventory_for_points(points)
    update_dt = parse_update_label(api.get("generated_at"))
    age_hours = None
    freshness = "unknown"
    if update_dt:
        age_hours = max(0.0, (now_wib().replace(tzinfo=None) - update_dt).total_seconds() / 3600)
        freshness = "fresh" if age_hours <= 24 else "stale" if age_hours <= 72 else "old"
    return {
        "brand": BRAND,
        "version": OMEGA_VERSION,
        "location": {
            "name": api.get("location_name"),
            "slug": api.get("location_slug"),
            "latitude": api.get("latitude"),
            "longitude": api.get("longitude"),
        },
        "generatedAt": api.get("generated_at"),
        "freshness": {"status": freshness, "ageHours": round(age_hours, 1) if age_hours is not None else None},
        "contract": data_contract(),
        "variables": variables,
        "points": points,
        "sources": api.get("sources", []),
    }


def data_contract() -> Dict[str, Any]:
    fields = [
        "id",
        "locationName",
        "latitude",
        "longitude",
        "timestamp",
        "updateTime",
        "condition",
        "temperature",
        "temperatureMin",
        "temperatureMax",
        "apparentTemperature",
        "heatIndex",
        "humidity",
        "dewPoint",
        "pressure",
        "windSpeed",
        "windDirection",
        "windGust",
        "rainProbability",
        "rainAmount",
        "rainIntensity",
        "precipitationTotal",
        "cloudCover",
        "visibility",
        "uvIndex",
        "solarRadiation",
        "weatherCode",
        "riskScore",
        "dataQuality",
        "source",
        "confidence",
    ]
    return {
        "name": "LangitWeatherPoint",
        "timezone": "WIB",
        "units": {
            "temperature": "C",
            "rainProbability": "%",
            "rainAmount": "mm",
            "windSpeed": "km/jam",
            "humidity": "%",
            "pressure": "hPa",
            "visibility": "m",
            "uvIndex": "index",
        },
        "fields": fields,
    }


def variable_inventory_for_points(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for key, meta in VARIABLE_MODEL.items():
        values = [p.get(meta["field"]) for p in points]
        available = non_empty_rate(values) > 0.15
        missing = 1.0 - non_empty_rate(values)
        recommended = meta["display"] if available else "disabled"
        rows.append({
            "key": key,
            "label": meta["label"],
            "available": available,
            "field": meta["field"],
            "sourceFields": meta["source_fields"],
            "unit": meta["unit"],
            "temporalResolution": "1-3 jam",
            "spatialResolution": "titik lokasi kampus",
            "missingRate": round(missing, 3),
            "currentUIUsage": "parsial",
            "recommendedUIUsage": recommended,
            "priority": meta["priority"] if available else "P5",
            "chart": meta["chart"],
            "mapLayer": meta["map_layer"] if available else None,
            "level": meta["level"],
        })
    return rows


def file_family(path: Path) -> str:
    n = path.name.lower()
    if n.startswith("forecast"):
        return "Forecast per provider"
    if n.startswith("ensemble") or "ensemble_fact" in n:
        return "Ensemble"
    if "langit_api" in n or "hourly_intelligence" in n or "daily_outlook" in n:
        return "Public API"
    if "map" in n or path.suffix.lower() == ".geojson":
        return "Map"
    if "sentinel" in n:
        return "Advanced/Sentinel"
    if "source" in n or "health" in n:
        return "Source status"
    if "raw_payload" in str(path).lower():
        return "Raw payload"
    if path.suffix.lower() == ".html":
        return "Generated HTML"
    return "Other"


def inspect_data_file(path: Path, root: Path) -> Dict[str, Any]:
    rel = str(path.relative_to(root)).replace("\\", "/")
    row: Dict[str, Any] = {
        "dataSource": file_family(path),
        "filePath": rel,
        "format": "".join(path.suffixes).lstrip("."),
        "sizeBytes": path.stat().st_size,
        "records": "",
        "fields": [],
        "usedNow": "yes" if path.name in {"langit_api_v1.json", "langit_location.geojson", "langit_map_layers.json", "langit_portal_manifest.json"} else "partial/indirect",
        "potentialUse": "",
        "risk": "",
        "notes": "",
    }
    try:
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            row["records"] = len(rows)
            row["fields"] = reader.fieldnames or []
        elif path.suffix.lower() in {".json", ".geojson"}:
            data = read_json(path, {})
            if isinstance(data, dict):
                row["fields"] = list(data.keys())
                if isinstance(data.get("features"), list):
                    row["records"] = len(data["features"])
                elif isinstance(data.get("geojson"), dict):
                    row["records"] = len(data["geojson"].get("features", []))
                elif isinstance(data.get("days"), list):
                    row["records"] = sum(len(d.get("hours", [])) for d in data["days"] if isinstance(d, dict))
                elif isinstance(data.get("hourly"), list):
                    row["records"] = len(data["hourly"])
                else:
                    row["records"] = 1
        elif path.suffix.lower() == ".html":
            txt = path.read_text(encoding="utf-8", errors="replace")
            row["records"] = "html"
            m = re.search(r"<title>(.*?)</title>", txt, flags=re.I | re.S)
            row["fields"] = [f"title: {html.unescape(m.group(1)).strip()}"] if m else []
        else:
            row["records"] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception as exc:
        row["risk"] = f"inspect failed: {type(exc).__name__}"
        row["notes"] = str(exc)[:140]
    family = row["dataSource"]
    if family == "Forecast per provider":
        row["potentialUse"] = "advanced comparison, source transparency"
    elif family == "Ensemble":
        row["potentialUse"] = "confidence, spread, dominant weather"
    elif family == "Public API":
        row["potentialUse"] = "primary UI contract"
    elif family == "Map":
        row["potentialUse"] = "map layer and marker/timeline"
    elif family == "Advanced/Sentinel":
        row["potentialUse"] = "advanced panel, risk/confidence, exploration"
    elif family == "Source status":
        row["potentialUse"] = "data status bar and reliability"
    elif family == "Raw payload":
        row["potentialUse"] = "debug/archive only"
        row["usedNow"] = "no"
        row["risk"] = "do not expose raw public payload"
    return row


def audit_outputs(root: Path, packs: List[Dict[str, Any]]) -> Dict[str, Any]:
    suffixes = {".csv", ".json", ".geojson", ".html", ".txt", ".md"}
    files = [p for p in root.rglob("*") if p.is_file() and (p.suffix.lower() in suffixes or "".join(p.suffixes[-2:]).lower() == ".json.gz")]
    atlas = [inspect_data_file(p, root) for p in sorted(files)]
    variables: Dict[str, Dict[str, Any]] = {}
    for pack in packs:
        for var in pack["variables"]:
            key = var["key"]
            cur = variables.setdefault(key, dict(var))
            cur["available"] = bool(cur["available"] or var["available"])
            cur["missingRate"] = round(min(cur["missingRate"], var["missingRate"]), 3)
    hierarchy = {
        "P0": ["condition", "temperature", "rainProbability", "riskScore", "windSpeed", "updateTime", "locationName"],
        "P1": ["humidity", "heatIndex", "cloudCover", "peakRainHour", "safeWindow"],
        "P2": ["hourlyTrend", "dailyTrend", "rainAmount", "confidence", "locationComparison"],
        "P3": ["variableExplorer", "mapLayer", "timeline"],
        "P4": ["source", "coverage", "pressure", "dewPoint", "visibility", "gust", "rawField"],
        "P5": ["raw payload", "debug-only metadata", "internal manifests"],
    }
    copy_audit = [
        {"currentText": "Konsol Cuaca Spasial", "location": "legacy/generated HTML", "problem": "terlalu futuristik", "newText": "Peta Cuaca", "reason": "lebih kredibel"},
        {"currentText": "Command Center", "location": "legacy map section", "problem": "terasa dashboard taktis", "newText": "Peta Cuaca", "reason": "fungsi jelas"},
        {"currentText": "Tactical Weather / Intelligence OS", "location": "legacy copy", "problem": "overclaim", "newText": "Ringkasan Cuaca", "reason": "pendek dan faktual"},
        {"currentText": "real-time", "location": "klaim lama bila muncul", "problem": "data bukan live sensor", "newText": "Diperbarui", "reason": "tidak overclaim"},
        {"currentText": "sensor", "location": "klaim lama bila muncul", "problem": "sumber adalah model/API", "newText": "sumber data", "reason": "akurasi istilah"},
        {"currentText": "Monitoring otomatis keandalan dan tingkat akurasi verifikasi data cuaca.", "location": "keandalan_data", "problem": "terlalu panjang", "newText": "Keandalan sumber.", "reason": "copy ringkas"},
    ]
    return {
        "version": OMEGA_VERSION,
        "generatedAt": now_wib().isoformat(),
        "dataAtlas": atlas,
        "variableInventory": list(variables.values()),
        "dataHierarchy": hierarchy,
        "copyAudit": copy_audit,
    }


def report_markdown(audit: Dict[str, Any]) -> str:
    atlas = audit["dataAtlas"]
    var_rows = audit["variableInventory"]
    family_counts: Dict[str, int] = {}
    for row in atlas:
        family_counts[row["dataSource"]] = family_counts.get(row["dataSource"], 0) + 1
    lines = [
        "# LANGIT Omega Product Reconstruction Report",
        "",
        "## DATA ATLAS",
        "| Data Source | Files | Potential Use | Risk |",
        "|---|---:|---|---|",
    ]
    for family, count in sorted(family_counts.items()):
        sample = next((x for x in atlas if x["dataSource"] == family), {})
        lines.append(f"| {family} | {count} | {sample.get('potentialUse','')} | {sample.get('risk','')} |")
    lines += [
        "",
        "## VARIABLE INVENTORY",
        "| Variable | Available | Unit | Missing Rate | Recommended UI Usage | Priority |",
        "|---|---:|---|---:|---|---|",
    ]
    for v in sorted(var_rows, key=lambda x: (x.get("priority", "P9"), x.get("label", ""))):
        lines.append(f"| {v['label']} | {str(v['available']).lower()} | {v['unit']} | {v['missingRate']:.0%} | {v['recommendedUIUsage']} | {v['priority']} |")
    lines += [
        "",
        "## DATA HIERARCHY",
    ]
    for key, fields in audit["dataHierarchy"].items():
        lines.append(f"- {key}: {', '.join(fields)}")
    lines += [
        "",
        "## COPY REDUCTION REPORT",
        "| Current Text | Location | Problem | New Text | Reason |",
        "|---|---|---|---|---|",
    ]
    for r in audit["copyAudit"]:
        lines.append(f"| {r['currentText']} | {r['location']} | {r['problem']} | {r['newText']} | {r['reason']} |")
    lines += [
        "",
        "## INFORMATION ARCHITECTURE FINAL",
        "1. Hero compact",
        "2. Nowcast summary",
        "3. Intelligence strip",
        "4. Dynamic map",
        "5. Forecast timeline",
        "6. Variable explorer",
        "7. Location comparison",
        "8. Advanced data panel",
        "9. Data status",
        "",
        "## COMPONENT PLAN",
        "- MainWeatherHero, CurrentConditionPanel, MetricStrip, WeatherMapShell, LayerControl, ForecastTimeline, VariableExplorer, AdaptiveChartPanel, LocationComparison, AdvancedDataDrawer, DataStatusBar, EmptyState, ErrorState, LoadingState.",
        "",
        "## MAP DATA PLAN",
        "- Active: risk, rain, temperature, wind speed, humidity, cloud cover.",
        "- Disabled/advanced when missing: pressure, UV, visibility, gust direction vector.",
        "- Data is point-based; map uses markers, halos, local influence fields, and legends. It does not claim national radar.",
        "",
        "## RESPONSIVE PLAN",
        "- Desktop: map and side intelligence panel side by side.",
        "- Tablet: stacked map with horizontal controls.",
        "- Mobile: summary first, map full width, chips scroll horizontally, advanced panel collapsed.",
        "",
        "## LIMITATION REPORT",
        "- Pressure, dew point, UV, visibility, gust, and wind direction are not consistently available in public data. They are kept disabled or advanced-only.",
        "- Current output is forecast/model data, not live sensor data. UI uses update/freshness language, not real-time claims.",
    ]
    return "\n".join(lines) + "\n"


OMEGA_CSS = r"""
:root{
  --bg:#07080b;--panel:#101318;--panel-2:#151a22;--line:rgba(255,255,255,.09);
  --text:#f4f7fb;--muted:#8f9aaa;--soft:#c8d1de;--cyan:#4cc9f0;--green:#37d399;
  --amber:#f5b84b;--orange:#f47c48;--red:#ef4f5f;--blue:#6aa8ff;--violet:#9a8cff;
  --r-sm:8px;--r-md:12px;--r-lg:18px;--shadow:0 22px 80px rgba(0,0,0,.38);
  --max:1380px;--pad:clamp(14px,3vw,36px);--gap:14px;
}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 60% -20%,rgba(76,201,240,.10),transparent 34%),linear-gradient(180deg,#090a0e,#07080b 46%,#0b0d10);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.45;letter-spacing:0;overflow-x:hidden}
a{color:inherit;text-decoration:none}button,input{font:inherit}.app{min-height:100vh}.shell{width:min(var(--max),calc(100% - var(--pad)*2));margin:0 auto}
.topbar{position:sticky;top:0;z-index:50;border-bottom:1px solid var(--line);background:rgba(7,8,11,.78);backdrop-filter:blur(18px)}
.topbar .shell{height:64px;display:flex;align-items:center;justify-content:space-between;gap:12px}.brand{display:flex;align-items:center;gap:10px}.mark{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,var(--cyan),var(--green));box-shadow:0 0 24px rgba(76,201,240,.35)}.brand b{display:block;font-size:14px}.brand span span{display:block;color:var(--muted);font-size:11px}.nav{display:flex;gap:6px;align-items:center}.nav a,.btn{border:1px solid var(--line);background:rgba(255,255,255,.035);border-radius:999px;color:var(--soft);padding:8px 11px;font-size:12px;font-weight:700;cursor:pointer}.nav a.active,.btn.primary{background:#f4f7fb;color:#08090c;border-color:#f4f7fb}.btn:disabled,.chip:disabled{opacity:.35;cursor:not-allowed}
.hero{padding:26px 0 14px}.hero-grid{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:var(--gap);align-items:stretch}.hero-main{min-height:270px;border:1px solid var(--line);border-radius:var(--r-lg);padding:24px;background:linear-gradient(140deg,rgba(255,255,255,.075),rgba(255,255,255,.025)),radial-gradient(circle at 78% 22%,rgba(76,201,240,.18),transparent 38%);box-shadow:var(--shadow);display:flex;flex-direction:column;justify-content:space-between}.eyebrow{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:12px;font-weight:750}.pill{border:1px solid var(--line);border-radius:999px;padding:6px 9px;background:rgba(255,255,255,.035)}.title-row{display:flex;align-items:end;justify-content:space-between;gap:16px}.temp{font-size:clamp(58px,9vw,112px);font-weight:820;line-height:.85;letter-spacing:0}.condition{font-size:clamp(22px,3vw,38px);font-weight:780;max-width:560px}.microcopy{color:var(--muted);font-size:13px;max-width:540px;margin-top:10px}.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:var(--green);box-shadow:0 0 18px currentColor}.summary{border:1px solid var(--line);border-radius:var(--r-lg);background:rgba(16,19,24,.86);padding:16px;display:grid;gap:10px}.summary-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px;border-radius:var(--r-sm);background:rgba(255,255,255,.03)}.summary-row small,.metric small,.compare small{color:var(--muted);font-size:11px;font-weight:750;text-transform:uppercase}.summary-row b{font-size:18px}.risk-badge{border:1px solid currentColor;border-radius:999px;padding:6px 10px;font-size:12px;font-weight:800;width:max-content}
.strip{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:10px 0 18px}.metric{border:1px solid var(--line);background:rgba(255,255,255,.035);border-radius:var(--r-md);padding:13px;min-height:86px}.metric b{display:block;font-size:22px;margin-top:8px}.metric em{display:block;color:var(--muted);font-style:normal;font-size:12px;margin-top:2px}
.stage{display:grid;grid-template-columns:minmax(0,1.55fr) 430px;gap:var(--gap);align-items:start}.panel{border:1px solid var(--line);border-radius:var(--r-lg);background:rgba(16,19,24,.78);box-shadow:0 18px 60px rgba(0,0,0,.20)}.panel-head{padding:16px 16px 0;display:flex;align-items:end;justify-content:space-between;gap:12px}.overline{color:var(--cyan);font-size:11px;text-transform:uppercase;font-weight:850;letter-spacing:.08em}.panel h2{font-size:19px;margin:3px 0 0}.mapbox{padding:12px}.mapbox iframe{width:100%;height:580px;border:0;border-radius:12px;background:#07080b}.layerbar,.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{border:1px solid var(--line);border-radius:999px;background:rgba(255,255,255,.04);color:var(--soft);padding:8px 11px;font-size:12px;font-weight:800;cursor:pointer}.chip.active{background:var(--text);color:var(--bg);border-color:var(--text)}
.side-stack{display:grid;gap:var(--gap)}.explorer{padding:16px}.variable-now{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:end;margin:16px 0}.variable-now b{font-size:38px;line-height:1}.variable-now small{color:var(--muted)}.insight{min-height:44px;color:var(--soft);font-size:14px;border-left:3px solid var(--cyan);padding:8px 0 8px 12px}.chart{height:210px;margin-top:12px;border:1px solid var(--line);border-radius:var(--r-md);background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.015));padding:10px}.chart svg{width:100%;height:100%;overflow:visible}.axis{color:var(--muted);font-size:11px}
.timeline{padding:16px;margin:14px 0}.day-tabs{display:flex;gap:8px;overflow:auto;padding-bottom:2px}.timeline-grid{display:grid;grid-template-columns:repeat(9,1fr);gap:8px;margin-top:14px;align-items:end}.timebar{min-height:96px;display:flex;flex-direction:column;justify-content:end;gap:6px}.bar{border-radius:8px 8px 3px 3px;min-height:8px;background:linear-gradient(180deg,var(--cyan),rgba(76,201,240,.12))}.timebar b{font-size:11px}.timebar span{color:var(--muted);font-size:11px}.compare-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.compare{padding:13px;border:1px solid var(--line);border-radius:var(--r-md);background:rgba(255,255,255,.03)}.compare b{display:block;font-size:17px;margin:6px 0}
.advanced{margin:14px 0 30px}.advanced summary{cursor:pointer;padding:16px;font-weight:850}.advanced-body{border-top:1px solid var(--line);padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:14px}.tablewrap{overflow:auto;border:1px solid var(--line);border-radius:var(--r-md)}table{width:100%;border-collapse:collapse;min-width:620px}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:12px}th{color:var(--muted);text-transform:uppercase;font-size:10px}.statusbar{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:10px 0 24px}.empty{padding:16px;color:var(--muted);border:1px dashed var(--line);border-radius:var(--r-md)}
@media(max-width:1279px){.hero-grid,.stage{grid-template-columns:1fr}.mapbox iframe{height:520px}.strip{grid-template-columns:repeat(3,1fr)}}
@media(max-width:767px){.topbar .shell{height:auto;min-height:58px;padding:10px 0;align-items:flex-start}.nav{max-width:54vw;overflow:auto;justify-content:flex-end}.hero{padding-top:16px}.hero-main{min-height:230px;padding:18px}.title-row{display:block}.condition{margin-top:10px}.summary{padding:12px}.strip{grid-template-columns:repeat(2,1fr);overflow:visible}.stage{gap:12px}.panel-head{display:block}.mapbox iframe{height:430px}.layerbar,.chips{overflow:auto;flex-wrap:nowrap;padding-bottom:2px}.chip{white-space:nowrap;min-height:40px}.variable-now b{font-size:32px}.timeline-grid{display:flex;overflow:auto;align-items:end}.timebar{min-width:54px}.advanced-body{grid-template-columns:1fr}.compare-grid{grid-template-columns:1fr}.microcopy{font-size:12px}}
@media(max-width:420px){.shell{width:calc(100% - 22px)}.temp{font-size:62px}.strip{grid-template-columns:1fr 1fr}.metric{padding:11px}.metric b{font-size:19px}.mapbox{padding:8px}.mapbox iframe{height:390px}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
"""


OMEGA_JS = r"""
const DATA = window.LANGIT_OMEGA_DATA;
const mapLayerFor = {rain:'rain',temperature:'temp',risk:'risiko',wind:'wind',humidity:'humidity',cloud:'cloud'};
let activeVar = (DATA.variables.find(v => v.available && v.priority === 'P0') || DATA.variables.find(v => v.available) || {key:'rain'}).key;
let activeDay = 0;
function n(v){ const x=Number(v); return Number.isFinite(x)?x:null; }
function fmt(v, unit){ const x=n(v); if(x===null) return '-'; const d=(unit==='C'||unit==='km/jam'||unit==='mm')?1:0; return x.toFixed(d).replace(/\.0$/,'') + (unit==='C'?'&deg;C':unit?(' '+unit):''); }
function pointsForDay(){ const days=[...new Set(DATA.points.map(p=>p.dateIso))]; return DATA.points.filter(p=>p.dateIso===days[activeDay]); }
function variable(){ return DATA.variables.find(v=>v.key===activeVar) || DATA.variables[0]; }
function insight(v, pts){
  if(!v || !pts.length) return 'Data belum cukup untuk ringkasan variabel ini.';
  const vals=pts.map(p=>n(p[v.field])).filter(x=>x!==null); if(!vals.length) return 'Data belum cukup untuk ringkasan variabel ini.';
  const max=Math.max(...vals), min=Math.min(...vals), avg=vals.reduce((a,b)=>a+b,0)/vals.length;
  const peak=pts.find(p=>n(p[v.field])===max);
  if(v.key==='rain') return max>=60 ? `Peluang hujan meningkat sekitar ${peak.hour} WIB.` : max>=25 ? `Hujan masih perlu dipantau sekitar ${peak.hour} WIB.` : 'Peluang hujan relatif rendah pada periode ini.';
  if(v.key==='temperature') return (max-min)>=5 ? `Suhu berubah sekitar ${Math.round(max-min)}&deg;C sepanjang hari.` : 'Suhu udara relatif stabil pada periode ini.';
  if(v.key==='wind') return max>=20 ? `Angin lebih terasa sekitar ${peak.hour} WIB.` : 'Angin bertiup lemah hingga sedang.';
  if(v.key==='humidity') return avg>=80 ? 'Kelembapan cukup tinggi, terutama pagi dan malam hari.' : 'Kelembapan berada pada rentang moderat.';
  if(v.key==='cloud') return max>=75 ? 'Tutupan awan cenderung dominan.' : 'Tutupan awan tidak dominan sepanjang periode ini.';
  if(v.key==='risk') return max>=55 ? `Risiko cuaca tertinggi sekitar ${peak.hour} WIB.` : 'Risiko cuaca relatif terkendali.';
  return 'Variabel tersedia untuk eksplorasi detail.';
}
function drawChart(){
  const v=variable(); const pts=pointsForDay(); const vals=pts.map(p=>n(p[v.field]));
  const valid=vals.filter(x=>x!==null); const el=document.getElementById('chart');
  if(!el || !v || !valid.length){ if(el) el.innerHTML='<div class="empty">Data belum tersedia.</div>'; return; }
  const min=Math.min(...valid), max=Math.max(...valid); const range=max-min || 1; const W=360,H=150,P=20;
  const coords=pts.map((p,i)=>{ const val=n(p[v.field]); const x=P+(i/(Math.max(1,pts.length-1)))*(W-P*2); const y=val===null?null:H-P-((val-min)/range)*(H-P*2); return {x,y,p,val}; });
  let path=''; coords.forEach((c)=>{ if(c.y===null) return; path += (path?' L':'M')+c.x.toFixed(1)+' '+c.y.toFixed(1); });
  const bars = v.chart.includes('bar') ? coords.map(c=>c.y===null?'':`<rect x="${c.x-7}" y="${c.y}" width="14" height="${H-P-c.y}" rx="4" fill="rgba(76,201,240,.38)"/>`).join('') : '';
  const dots=coords.map(c=>c.y===null?'':`<circle cx="${c.x}" cy="${c.y}" r="3.5" fill="#f4f7fb"><title>${c.p.hour} - ${fmt(c.val,v.unit).replace(/<[^>]+>/g,'')}</title></circle>`).join('');
  const labels=coords.filter((_,i)=>i%2===0).map(c=>`<text x="${c.x}" y="${H-2}" text-anchor="middle" fill="#8f9aaa" font-size="10">${c.p.hour.slice(0,2)}</text>`).join('');
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}" stroke="rgba(255,255,255,.12)"/>${bars}<path d="${path}" fill="none" stroke="#4cc9f0" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>${dots}${labels}</svg>`;
  const latest=pts.find(p=>n(p[v.field])!==null) || pts[0];
  const maxPt=pts.find(p=>n(p[v.field])===Math.max(...valid)) || latest;
  document.getElementById('varValue').innerHTML=fmt(maxPt[v.field],v.unit);
  document.getElementById('varLabel').textContent=`Puncak ${v.label.toLowerCase()} - ${maxPt.hour} WIB`;
  document.getElementById('insight').innerHTML=insight(v,pts);
}
function renderTimeline(){
  const pts=pointsForDay(); const wrap=document.getElementById('timelineGrid'); if(!wrap) return;
  wrap.innerHTML=pts.map(p=>{ const rain=n(p.rainProbability)||0; const h=Math.max(8,Math.round(rain)); return `<div class="timebar"><div class="bar" style="height:${h}px;background:${rain>=55?'linear-gradient(180deg,#ef4f5f,rgba(239,79,95,.16))':'linear-gradient(180deg,#4cc9f0,rgba(76,201,240,.12))'}"></div><b>${p.hour}</b><span>${Math.round(rain)}%</span></div>`}).join('');
}
function switchMapLayer(key){
  const layer=mapLayerFor[key] || 'risiko'; const frame=document.querySelector('.mapbox iframe');
  if(frame && frame.contentWindow) frame.contentWindow.postMessage({type:'switchLayer',layer},'*');
}
function bind(){
  document.querySelectorAll('[data-var]').forEach(btn=>btn.addEventListener('click',()=>{ if(btn.disabled) return; activeVar=btn.dataset.var; document.querySelectorAll('[data-var]').forEach(b=>b.classList.toggle('active',b.dataset.var===activeVar)); drawChart(); switchMapLayer(activeVar); }));
  document.querySelectorAll('[data-day]').forEach(btn=>btn.addEventListener('click',()=>{ activeDay=Number(btn.dataset.day)||0; document.querySelectorAll('[data-day]').forEach(b=>b.classList.toggle('active',Number(b.dataset.day)===activeDay)); renderTimeline(); drawChart(); }));
  drawChart(); renderTimeline(); switchMapLayer(activeVar);
}
document.addEventListener('DOMContentLoaded', bind);
"""


def risk_style(cls: str) -> str:
    return f"color:{risk_color(cls)}"


def value_with_unit(value: Any, unit: str, decimals: int = 0) -> str:
    x = num(value, None)
    if x is None:
        return "-"
    val = f"{x:.{decimals}f}".replace(".0", "")
    if unit == "C":
        return f"{val}&deg;C"
    return f"{val}{unit if unit.startswith('%') or unit.startswith('/') else ' ' + unit}"


def main_point(pack: Dict[str, Any]) -> Dict[str, Any]:
    pts = pack.get("points", [])
    return pts[0] if pts else {}


def peak_point(points: List[Dict[str, Any]], field: str) -> Dict[str, Any]:
    valid = [p for p in points if num(p.get(field), None) is not None]
    if not valid:
        return points[0] if points else {}
    return max(valid, key=lambda p: num(p.get(field), -999) or -999)


def source_health(api: Dict[str, Any]) -> Tuple[int, int]:
    sources = api.get("sources") or []
    total = len(sources)
    ok = 0
    for s in sources:
        state = text(s.get("success") or s.get("ok") or s.get("state")).lower()
        if state in {"true", "1", "ok", "success", "aktif"}:
            ok += 1
    return ok, total


def variable_buttons(pack: Dict[str, Any]) -> str:
    out = []
    first = True
    for v in pack["variables"]:
        if v["priority"] == "P5" and not v["available"]:
            continue
        disabled = "" if v["available"] else " disabled"
        active = " active" if first and v["available"] else ""
        if first and v["available"]:
            first = False
        out.append(f'<button class="chip{active}" data-var="{esc(v["key"])}"{disabled}>{esc(v["label"])}</button>')
    return "".join(out)


def metrics_html(pack: Dict[str, Any], api: Dict[str, Any]) -> str:
    p0 = main_point(pack)
    pts = pack["points"]
    rain_peak = peak_point(pts, "rainProbability")
    risk_peak = peak_point(pts, "riskScore")
    ok, total = source_health(api)
    items = [
        ("Risiko", value_with_unit(risk_peak.get("riskScore"), "/100"), risk_peak.get("hour", "-")),
        ("Hujan", value_with_unit(rain_peak.get("rainProbability"), "%"), rain_peak.get("hour", "-")),
        ("Angin", value_with_unit(p0.get("windSpeed"), "km/jam", 1), "saat ini"),
        ("Lembap", value_with_unit(p0.get("humidity"), "%"), "saat ini"),
        ("Terasa", value_with_unit(p0.get("heatIndex"), "C", 1), "indeks panas"),
        ("Sumber", f"{ok}/{total}" if total else "-", "berhasil"),
    ]
    return '<section class="shell strip">' + "".join(f'<div class="metric"><small>{esc(k)}</small><b>{v}</b><em>{esc(str(s))}</em></div>' for k, v, s in items) + "</section>"


def advanced_tables(pack: Dict[str, Any], audit: Dict[str, Any], api: Dict[str, Any], root_prefix: str = "") -> str:
    var_rows = "".join(
        f"<tr><td>{esc(v['label'])}</td><td>{'Ya' if v['available'] else 'Tidak'}</td><td>{esc(v['unit'])}</td><td>{v['missingRate']:.0%}</td><td>{esc(v['recommendedUIUsage'])}</td><td>{esc(v['priority'])}</td></tr>"
        for v in pack["variables"]
    )
    sources = api.get("sources") or []
    src_rows = "".join(
        f"<tr><td>{esc(s.get('source_id') or s.get('provider') or '-')}</td><td>{esc(s.get('provider') or '-')}</td><td>{esc(s.get('success') or s.get('state') or '-')}</td><td>{esc(s.get('points_collected') or '-')}</td><td>{esc(s.get('duration_ms') or '-')}</td></tr>"
        for s in sources[:20]
    ) or '<tr><td colspan="5">Belum ada status sumber.</td></tr>'
    return f"""
    <details class="shell panel advanced">
      <summary>Advanced data</summary>
      <div class="advanced-body">
        <div>
          <div class="overline">Variable Inventory</div>
          <div class="tablewrap"><table><thead><tr><th>Variable</th><th>Ada</th><th>Unit</th><th>Missing</th><th>UI</th><th>Prioritas</th></tr></thead><tbody>{var_rows}</tbody></table></div>
        </div>
        <div>
          <div class="overline">Source Status</div>
          <div class="tablewrap"><table><thead><tr><th>ID</th><th>Provider</th><th>Status</th><th>Points</th><th>ms</th></tr></thead><tbody>{src_rows}</tbody></table></div>
        </div>
      </div>
    </details>
    <div class="shell statusbar">
      <span>Contract: LangitWeatherPoint</span>
      <span>Report: <a href="{root_prefix}langit_omega_report.md">langit_omega_report.md</a></span>
      <span>{esc(DISCLAIMER)}</span>
    </div>
    """


def compare_html(apis: List[Dict[str, Any]], current_slug: Optional[str], root_prefix: str) -> str:
    cards = []
    for api in sorted(apis, key=lambda a: clamp(a.get("today", {}).get("risk_score"), default=0), reverse=True):
        d = api.get("today", {})
        href = f'{root_prefix}{api["location_slug"]}/langit_app.html' if root_prefix == "" else f'../{api["location_slug"]}/langit_app.html'
        if current_slug == api["location_slug"]:
            href = "langit_app.html"
        cards.append(f"""
        <a class="compare" href="{esc(href)}">
          <small>{esc(d.get('risk_label','-'))}</small>
          <b>{esc(api.get('location_name'))}</b>
          <span>{pct(d.get('peak_rain_probability'))} hujan - {esc(d.get('peak_rain_hour','-'))}</span>
        </a>""")
    return f"""<section class="shell panel timeline"><div class="panel-head"><div><div class="overline">Perbandingan lokasi</div><h2>Prioritas pantau</h2></div></div><div class="compare-grid" style="padding:16px">{''.join(cards)}</div></section>"""


def omega_document(title: str, pack: Dict[str, Any], api: Dict[str, Any], body: str, root_prefix: str = "") -> str:
    config = json.dumps(pack, ensure_ascii=False)
    nav_base = root_prefix
    return f"""<!doctype html><html lang="id"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><meta name="theme-color" content="#07080b">
<meta name="description" content="LANGIT - prakiraan cuaca ringkas berbasis data untuk Institut Teknologi Bandung.">
<style>{OMEGA_CSS}</style></head><body>
<div class="app">
  <header class="topbar"><div class="shell">
    <a class="brand" href="{nav_base}index.html"><span class="mark"></span><span><b>LANGIT</b><span>{esc(OMEGA_VERSION)}</span></span></a>
    <nav class="nav"><a class="active" href="{nav_base}index.html">Lokasi</a><a href="{nav_base}langit_portal_map.html">Peta</a><a href="{nav_base}langit_omega_report.md">Data</a></nav>
  </div></header>
  {body}
</div>
<script>window.LANGIT_OMEGA_DATA={config};</script>
<script>{OMEGA_JS}</script>
</body></html>"""


def location_page(pack: Dict[str, Any], api: Dict[str, Any], all_apis: List[Dict[str, Any]], audit: Dict[str, Any]) -> str:
    p = main_point(pack)
    pts = pack["points"]
    rain_peak = peak_point(pts, "rainProbability")
    risk_peak = peak_point(pts, "riskScore")
    cls = text(p.get("riskClass"), api.get("today", {}).get("risk_class", "watch"))
    stale = pack.get("freshness", {}).get("status") in {"stale", "old"}
    freshness_label = "Perlu diperbarui" if stale else "Data diperbarui"
    day_buttons = []
    dates = []
    for point in pts:
        if point.get("dateIso") not in dates:
            dates.append(point.get("dateIso"))
    for i, d in enumerate(dates[:3]):
        label = next((x.get("relative") for x in pts if x.get("dateIso") == d), f"H+{i}")
        day_buttons.append(f'<button class="chip{" active" if i == 0 else ""}" data-day="{i}">{esc(label)}</button>')
    body = f"""
  <main>
    <section class="hero"><div class="shell hero-grid">
      <div class="hero-main">
        <div class="eyebrow"><span class="pill">{esc(api.get('location_name'))}</span><span class="pill">{esc(api.get('generated_at'))}</span><span class="pill" style="{risk_style(cls)}"><span class="status-dot"></span> {esc(freshness_label)}</span></div>
        <div>
          <div class="title-row"><div><div class="temp">{value_with_unit(p.get('temperature'), 'C', 1)}</div><div class="condition">{esc(p.get('condition') or api.get('today', {}).get('condition','Prakiraan tersedia'))}</div></div><span class="risk-badge" style="{risk_style(cls)}">{esc(p.get('riskLabel') or api.get('today', {}).get('risk_label','Dipantau'))}</span></div>
          <div class="microcopy">Puncak hujan {value_with_unit(rain_peak.get('rainProbability'), '%')} sekitar {esc(rain_peak.get('hour','-'))} WIB. Risiko tertinggi {value_with_unit(risk_peak.get('riskScore'), '/100')}.</div>
        </div>
      </div>
      <aside class="summary">
        <div class="summary-row"><small>Lokasi aktif</small><b>{esc(api.get('location_name'))}</b></div>
        <div class="summary-row"><small>Hujan</small><b>{value_with_unit(p.get('rainProbability'), '%')}</b></div>
        <div class="summary-row"><small>Angin</small><b>{value_with_unit(p.get('windSpeed'), 'km/jam', 1)}</b></div>
        <div class="summary-row"><small>Kelembapan</small><b>{value_with_unit(p.get('humidity'), '%')}</b></div>
        <div class="summary-row"><small>Status data</small><b>{esc(freshness_label)}</b></div>
      </aside>
    </div></section>
    {metrics_html(pack, api)}
    <section class="shell stage command-center">
      <div class="panel">
        <div class="panel-head"><div><div class="overline">Lapisan Cuaca</div><h2>Peta Prakiraan</h2></div><div class="layerbar"><button class="chip layer-btn active" data-var="rain">Hujan</button><button class="chip layer-btn" data-var="temperature">Suhu</button><button class="chip layer-btn" data-var="risk">Risiko</button><button class="chip layer-btn" data-var="wind">Angin</button><button class="chip layer-btn" data-var="humidity">Lembap</button><button class="chip layer-btn" data-var="cloud">Awan</button></div></div>
        <div class="mapbox"><iframe class="map-frame" src="langit_map_room.html" loading="lazy"></iframe></div>
      </div>
      <aside class="side-stack">
        <div class="panel explorer">
          <div class="overline">Variable explorer</div><h2>Pilih variabel</h2>
          <div class="chips" style="margin-top:12px">{variable_buttons(pack)}</div>
          <div class="variable-now"><div><b id="varValue">-</b><small id="varLabel">-</small></div></div>
          <div id="insight" class="insight">Memuat ringkasan.</div>
          <div id="chart" class="chart"></div>
        </div>
      </aside>
    </section>
    <section class="shell panel timeline">
      <div class="panel-head"><div><div class="overline">Forecast timeline</div><h2>Periode tersedia</h2></div><div class="day-tabs">{''.join(day_buttons)}</div></div>
      <div id="timelineGrid" class="timeline-grid"></div>
    </section>
    {compare_html(all_apis, api.get('location_slug'), '../')}
    {advanced_tables(pack, audit, api, '../')}
  </main>"""
    return omega_document(f"LANGIT - {api.get('location_name')}", pack, api, body, "../")


def portal_page(portal_pack: Dict[str, Any], apis: List[Dict[str, Any]], audit: Dict[str, Any]) -> str:
    primary = max(apis, key=lambda a: clamp(a.get("today", {}).get("risk_score"), default=0)) if apis else {}
    p_api = primary or {"location_name": "Portal", "today": {}}
    p_pack = portal_pack
    p = main_point(p_pack)
    cls = text(p.get("riskClass"), p_api.get("today", {}).get("risk_class", "watch"))
    body = f"""
  <main>
    <section class="hero"><div class="shell hero-grid">
      <div class="hero-main">
        <div class="eyebrow"><span class="pill">Institut Teknologi Bandung</span><span class="pill">{len(apis)} lokasi</span><span class="pill">{esc(p_api.get('generated_at',''))}</span></div>
        <div>
          <div class="title-row"><div><div class="condition">LANGIT</div><div class="microcopy">Prakiraan cuaca kampus: ringkas, berlapis, dan berbasis data yang tersedia.</div></div><span class="risk-badge" style="{risk_style(cls)}">{esc(p_api.get('today',{}).get('risk_label','Dipantau'))}</span></div>
        </div>
      </div>
      <aside class="summary">
        <div class="summary-row"><small>Prioritas pantau</small><b>{esc(p_api.get('location_name','-'))}</b></div>
        <div class="summary-row"><small>Puncak hujan</small><b>{pct(p_api.get('today',{}).get('peak_rain_probability'))}</b></div>
        <div class="summary-row"><small>Jam rawan</small><b>{esc(p_api.get('today',{}).get('peak_rain_hour','-'))}</b></div>
        <div class="summary-row"><small>Risiko</small><b>{fmt_number(p_api.get('today',{}).get('risk_score'))}/100</b></div>
      </aside>
    </div></section>
    {metrics_html(p_pack, p_api)}
    <section class="shell stage command-center">
      <div class="panel">
        <div class="panel-head"><div><div class="overline">Lapisan Cuaca</div><h2>Peta Prakiraan</h2></div><div class="layerbar"><button class="chip layer-btn active" data-var="risk">Risiko</button><button class="chip layer-btn" data-var="rain">Hujan</button><button class="chip layer-btn" data-var="temperature">Suhu</button><button class="chip layer-btn" data-var="wind">Angin</button><button class="chip layer-btn" data-var="humidity">Lembap</button></div></div>
        <div class="mapbox"><iframe class="map-frame" src="langit_portal_map.html" loading="lazy"></iframe></div>
      </div>
      <aside class="side-stack">
        <div class="panel explorer">
          <div class="overline">Variable explorer</div><h2>Portal</h2>
          <div class="chips" style="margin-top:12px">{variable_buttons(p_pack)}</div>
          <div class="variable-now"><div><b id="varValue">-</b><small id="varLabel">-</small></div></div>
          <div id="insight" class="insight">Memuat ringkasan.</div>
          <div id="chart" class="chart"></div>
        </div>
      </aside>
    </section>
    {compare_html(apis, None, '')}
    <section class="shell panel timeline">
      <div class="panel-head"><div><div class="overline">Forecast timeline</div><h2>Prioritas portal</h2></div><div class="day-tabs"><button class="chip active" data-day="0">Hari ini</button><button class="chip" data-day="1">Besok</button><button class="chip" data-day="2">Lusa</button></div></div>
      <div id="timelineGrid" class="timeline-grid"></div>
    </section>
    {advanced_tables(p_pack, audit, p_api, '')}
  </main>"""
    return omega_document("LANGIT Portal", p_pack, p_api, body, "")


def portal_semantic_pack(apis: List[Dict[str, Any]], packs: List[Dict[str, Any]]) -> Dict[str, Any]:
    points: List[Dict[str, Any]] = []
    for pack in packs:
        today = pack["points"][:9]
        points.extend(today)
    points = sorted(points, key=lambda p: (p.get("dateIso") or "", p.get("hour") or "", p.get("locationName") or ""))
    return {
        "brand": BRAND,
        "version": OMEGA_VERSION,
        "location": {"name": "Portal", "slug": "portal", "latitude": None, "longitude": None},
        "generatedAt": fmt_update(),
        "freshness": {"status": "mixed", "ageHours": None},
        "contract": data_contract(),
        "variables": variable_inventory_for_points(points),
        "points": points,
        "sources": [],
    }


def rebuild(root: Path, public_base_url: str = "") -> int:
    meta = metadata_by_slug(root)
    dirs = location_dirs(root)
    if not dirs:
        print("ERROR: tidak ada folder lokasi di outputs/. Jalankan forecast dulu.")
        return 2
    apis: List[Dict[str, Any]] = []
    packs: List[Dict[str, Any]] = []
    for d in dirs:
        api = load_location_api(d, meta.get(d.name, {"slug": d.name}))
        update_source = update_source_for_location(root, d)
        if update_source:
            api["generated_at"] = format_update_label(update_source)
        lock_api_dates(api, target_dates_for_location(root, api.get("location_slug") or d.name))
        pack = build_semantic_pack(d, api)
        apis.append(api)
        packs.append(pack)
    audit = audit_outputs(root, packs)
    write_json(root / "langit_data_atlas.json", audit)
    write_text(root / "langit_omega_report.md", report_markdown(audit))

    for d, api, pack in zip(dirs, apis, packs):
        gj = v65_geo_for_api(api)
        write_json(d / "langit_api_v1.json", api)
        write_json(d / "langit_semantic_v1.json", pack)
        write_json(d / "langit_location.geojson", gj)
        write_json(d / "langit_map_layers.json", {"brand": BRAND, "version": OMEGA_VERSION, "geojson": gj, "variables": pack["variables"]})
        write_text(d / "langit_map_room.html", v65_map_page(f"LANGIT Map - {api['location_name']}", gj, "langit_app.html"))
        page = location_page(pack, api, apis, audit)
        write_text(d / "langit_app.html", page)
        write_text(d / "langit_public_landing.html", page)
        write_text(d / "langit_3day.html", page)
        write_text(d / "langit_activity.html", page)
        write_text(d / "keandalan_data.html", page)
        write_text(d / "akurasi_data.html", page)

    pgeo = v65_portal_geo(apis)
    portal_pack = portal_semantic_pack(apis, packs)
    write_json(root / "langit_all_locations.geojson", pgeo)
    write_json(root / "langit_semantic_portal_v1.json", portal_pack)
    write_json(root / "langit_portal_manifest.json", {
        "brand": BRAND,
        "version": OMEGA_VERSION,
        "generated_at": fmt_update(),
        "public_base_url": public_base_url,
        "locations": [{"slug": a["location_slug"], "name": a["location_name"]} for a in apis],
        "data_atlas": "langit_data_atlas.json",
        "semantic_contract": "langit_semantic_portal_v1.json",
    })
    write_text(root / "langit_portal_map.html", v65_map_page("LANGIT Portal Map", pgeo, "index.html"))
    write_text(root / "index.html", portal_page(portal_pack, apis, audit))
    print(f"OK: {OMEGA_VERSION} rebuild selesai. lokasi={len(apis)} data_files={len(audit['dataAtlas'])}")
    return verify(root)


def verify(root: Path) -> int:
    required = [
        root / "index.html",
        root / "langit_portal_map.html",
        root / "langit_portal_manifest.json",
        root / "langit_data_atlas.json",
        root / "langit_omega_report.md",
        root / "langit_semantic_portal_v1.json",
    ]
    missing = [str(p) for p in required if not p.exists()]
    for d in location_dirs(root):
        for name in ["langit_app.html", "langit_map_room.html", "langit_semantic_v1.json", "langit_location.geojson"]:
            if not (d / name).exists():
                missing.append(str(d / name))
    if missing:
        print("ERROR: output kurang:")
        for p in missing[:40]:
            print(" -", p)
        return 2
    banned = ["Command Center", "Tactical Weather", "Intelligence OS", "real-time", "Real Atmospheric Field Map Engine"]
    hits = []
    check_files = [root / "index.html"] + [d / "langit_app.html" for d in location_dirs(root)]
    for path in check_files:
        txt = path.read_text(encoding="utf-8", errors="replace")
        for token in banned:
            if token in txt:
                hits.append((str(path), token))
    if hits:
        print("ERROR: copy lama masih muncul:")
        for path, token in hits:
            print(" -", path, token)
        return 3
    for path in [root / "langit_semantic_portal_v1.json"] + [d / "langit_semantic_v1.json" for d in location_dirs(root)]:
        data = read_json(path, {})
        if not data.get("points") or not data.get("variables"):
            print(f"ERROR: semantic data kosong: {path}")
            return 4
    print("OK: Omega public output verified.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild LANGIT Omega public interface from existing outputs.")
    parser.add_argument("--root", default="outputs")
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = Path(args.root)
    if args.verify_only:
        return verify(root)
    return rebuild(root, args.public_base_url)


if __name__ == "__main__":
    raise SystemExit(main())
