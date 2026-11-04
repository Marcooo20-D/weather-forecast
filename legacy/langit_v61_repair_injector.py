#!/usr/bin/env python3
"""
LANGIT v61 repair injector.

Cara pakai:
  python langit_v61_repair_injector.py

Script ini akan:
1. backup weather_ensemble_multi_location.py
2. menyisipkan LANGIT v61 hotfix block tepat SEBELUM if __name__ == "__main__"
3. menjalankan py_compile agar syntax error ketahuan sebelum commit
"""

from __future__ import annotations

from pathlib import Path
import py_compile
import re
import shutil
import sys
from datetime import datetime

TARGET = Path("weather_ensemble_multi_location.py")
START = "# ---------- LANGIT v61.0 PRODUCT REBUILD HOTFIX: START ----------"
END = "# ---------- LANGIT v61.0 PRODUCT REBUILD HOTFIX: END ----------"

HOTFIX = r"""# ---------- LANGIT v61.0 PRODUCT REBUILD HOTFIX: START ----------
# This block intentionally overrides the late v60.x public renderer.
# It does not change the fetcher; it repairs the public API, data integrity,
# HTML, maps, portal, and accuracy pages after the forecast data is collected.

LANGIT_PUBLIC_VERSION = "LANGIT v61.0"
LANGIT_BRAND_NAME = "LANGIT"
LANGIT_DISCLAIMER = "Bukan peringatan resmi. Untuk cuaca ekstrem, ikuti informasi BMKG dan kondisi setempat."


def _v61_clamp(x, lo=0, hi=100):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return lo
        return max(lo, min(hi, float(x)))
    except Exception:
        return lo


def _v61_float(value, default=None):
    try:
        if value is None or value == "" or value == "—":
            return default
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _v61_int(value, default=0):
    x = _v61_float(value, None)
    return default if x is None else int(round(x))


def _v61_text(value, default=""):
    if value is None:
        return default
    s = str(value).strip()
    if not s or s.lower() in {"none", "nan", "null"}:
        return default
    return s


def _v61_esc(value):
    return html.escape(_v61_text(value), quote=True)


def _v61_hour(value, default="—"):
    s = _v61_text(value, default)
    if len(s) >= 5 and s[2] == ":":
        return s[:5]
    if len(s) >= 2 and s[:2].isdigit():
        return f"{s[:2]}:00"
    return default


def _v61_hour_int(value):
    s = _v61_hour(value, "00:00")
    try:
        return int(s[:2])
    except Exception:
        return 0


def _v61_fmt_num(value, suffix="", digits=1, blank="—"):
    x = _v61_float(value, None)
    if x is None:
        return blank
    if digits <= 0:
        return f"{int(round(x))}{suffix}"
    return f"{round(x, digits):.{digits}f}{suffix}"


def _v61_fmt_pct(value, blank="—"):
    x = _v61_float(value, None)
    if x is None:
        return blank
    return f"{int(round(_v61_clamp(x)))}%"


def _v61_safe_join(values, sep=" · ", blank="—"):
    if values is None:
        return blank
    if isinstance(values, str):
        return values or blank
    if isinstance(values, (list, tuple, set)):
        vals = [_v61_text(v) for v in values if _v61_text(v)]
        return sep.join(vals) if vals else blank
    return _v61_text(values, blank)


def _v61_risk_class(score=None, rain=None, limited=False):
    if limited:
        return "limited"
    score = _v61_float(score, None)
    rain = _v61_float(rain, None)
    metric = max([x for x in [score, rain] if x is not None], default=0)
    if metric >= 75:
        return "danger"
    if metric >= 55:
        return "rain"
    if metric >= 30:
        return "watch"
    return "safe"


def _v61_risk_label(cls):
    return {
        "safe": "Aman",
        "watch": "Dipantau",
        "rain": "Waspada hujan",
        "danger": "Risiko tinggi",
        "limited": "Data terbatas",
    }.get(_v61_text(cls), "Dipantau")


def _v61_condition_from_hour(hour, temp, rh, rain, limited=False):
    h = _v61_hour_int(hour)
    if limited:
        return "Data terbatas"
    p = _v61_float(rain, 0) or 0
    t = _v61_float(temp, None)
    r = _v61_float(rh, None)
    if p >= 75:
        return "Hujan lokal kuat"
    if p >= 55:
        return "Hujan lokal"
    if p >= 35:
        return "Awan tumbuh, potensi hujan"
    if r is not None and r >= 88 and (h <= 8 or h >= 19):
        return "Lembap pagi" if h <= 8 else "Lembap malam"
    if 10 <= h <= 15 and t is not None and t >= 28:
        return "Cerah berawan"
    if 16 <= h <= 18 and r is not None and r >= 75:
        return "Berawan sore"
    return "Berawan dipantau"


def _v61_hour_advice(cls, hour, rain, temp=None, rh=None):
    p = _v61_float(rain, None)
    if cls == "limited":
        return "Data inti belum lengkap; jangan baca sebagai kondisi aman penuh."
    if cls == "danger":
        return "Hindari aktivitas luar ruang yang tidak mendesak; siapkan rute/titik berteduh."
    if cls == "rain":
        return "Payung atau jas hujan sebaiknya siap; hujan lokal bisa bergeser cepat."
    if cls == "watch":
        return "Masih bisa, tetapi pantau awan, angin, dan perubahan lokal."
    if p is not None and p <= 10:
        return "Relatif aman, tetap pantau kondisi sekitar."
    return "Kondisi relatif aman."


def _v61_source_quality(api):
    court = api.get("source_court") if isinstance(api, dict) else None
    rows = []
    if isinstance(court, dict):
        rows = court.get("sources") or []
    total = len(rows) or len(globals().get("ALL_SOURCE_CONFIGS", []) or []) or 9
    active = 0
    for s in rows:
        if not isinstance(s, dict):
            continue
        verdict = _v61_text(s.get("verdict") or s.get("decision") or s.get("status")).lower()
        pts = _v61_float(s.get("points") or s.get("point") or s.get("matched_points"), 0) or 0
        http_status = _v61_text(s.get("http_status"))
        if verdict in {"aktif", "active", "ok", "success"} or pts > 0 or http_status == "200":
            active += 1
    # Several older APIs omit source_court. In that case do not punish too hard.
    if not rows:
        active = max(3, active)
    ratio = active / max(1, total)
    if active >= 5:
        level = "Tinggi"
    elif active >= 3:
        level = "Sedang"
    elif active >= 1:
        level = "Rendah"
    else:
        level = "Kritis"
    return {"active": active, "total": total, "ratio": ratio, "level": level, "rows": rows}


def _v61_hour_has_core_data(h):
    if not isinstance(h, dict):
        return False
    core_keys = ["temp_c", "temperature_c", "avg_temperature_c", "humidity_pct", "relative_humidity", "heat_index_c", "rain_probability_raw", "rain_probability", "wind_kmh"]
    return any(_v61_float(h.get(k), None) is not None for k in core_keys)


def _v61_repair_hour(h, api_quality):
    raw = dict(h or {}) if isinstance(h, dict) else {}
    hour = _v61_hour(raw.get("hour") or raw.get("jam") or raw.get("time"), "00:00")
    temp = _v61_float(raw.get("temp_c"), _v61_float(raw.get("temperature_c"), None))
    rh = _v61_float(raw.get("humidity_pct"), _v61_float(raw.get("relative_humidity"), None))
    heat = _v61_float(raw.get("heat_index_c"), temp)
    wind = _v61_float(raw.get("wind_kmh"), None)
    rain_raw = _v61_float(raw.get("rain_probability_raw"), None)
    rain = rain_raw if rain_raw is not None else _v61_float(raw.get("rain_probability"), None)
    has_core = _v61_hour_has_core_data(raw)
    # If every meteorological value is missing, do not convert missing data into 0% and Aman.
    limited = (not has_core) or (api_quality.get("active", 0) < 2 and rain is None)
    if limited:
        rain_display = None
        score = 38 if api_quality.get("active", 0) < 2 else 30
        cls = "limited"
        label = _v61_risk_label(cls)
        cond = _v61_text(raw.get("condition"), "Data terbatas")
        if cond in {"Aman", "Cerah", "Berawan"}:
            cond = "Data terbatas"
    else:
        rain_display = _v61_clamp(rain if rain is not None else 0)
        heat_penalty = max(0, ((_v61_float(heat, 0) or 0) - 30) * 3)
        humid_penalty = max(0, ((_v61_float(rh, 0) or 0) - 85) * 0.35)
        wind_penalty = max(0, ((_v61_float(wind, 0) or 0) - 18) * 1.2)
        source_penalty = 16 if api_quality.get("active", 0) < 3 else 6 if api_quality.get("active", 0) < 5 else 0
        score = _v61_clamp((rain_display * 0.82) + heat_penalty + humid_penalty + wind_penalty + source_penalty)
        cls = _v61_risk_class(score, rain_display, limited=False)
        # Low source confidence must not claim strong Aman unless the hour has enough data and score is very low.
        if api_quality.get("active", 0) < 3 and cls == "safe":
            cls = "watch"
        label = _v61_risk_label(cls)
        cond = _v61_condition_from_hour(hour, temp, rh, rain_display, limited=False)
    raw.update({
        "hour": hour,
        "temp_c": round(temp, 1) if temp is not None else None,
        "humidity_pct": round(rh, 0) if rh is not None else None,
        "heat_index_c": round(heat, 1) if heat is not None else None,
        "wind_kmh": round(wind, 1) if wind is not None else None,
        "rain_probability_raw": round(rain, 0) if rain is not None else None,
        "rain_probability": round(rain_display, 0) if rain_display is not None else None,
        "condition": cond,
        "data_valid": bool(has_core),
        "data_limited": bool(limited),
        "risk_score": round(score, 0),
        "risk_class": cls,
        "risk_label": label,
        "advice": _v61_hour_advice(cls, hour, rain_display, temp, rh),
    })
    return raw


def _v61_pick_key_hours(hours):
    if not hours:
        return []
    # Keep all 24 hourly rows when available; otherwise keep existing rows.
    cleaned = sorted(hours, key=lambda h: _v61_hour_int(h.get("hour")))
    seen = set()
    out = []
    for h in cleaned:
        hh = _v61_hour(h.get("hour"))
        if hh not in seen:
            seen.add(hh)
            out.append(h)
    return out


def _v61_best_windows(hours):
    valid = [h for h in hours if h.get("data_valid") and h.get("risk_class") in {"safe", "watch"}]
    safe = [h for h in valid if h.get("risk_class") == "safe"] or valid
    preferred = [h for h in safe if _v61_hour_int(h.get("hour")) in {6, 7, 8, 9, 10, 11, 12, 15, 16, 17, 18}]
    selected = (preferred or safe)[:4]
    return [_v61_hour(h.get("hour")) for h in selected] or ["Pantau manual"]


def _v61_period_summary(name, hours):
    if not hours:
        return {"name": name, "condition": "Data terbatas", "attention_hour": "—", "temp_c": None, "rain_probability": None, "risk_label": "Data terbatas", "risk_class": "limited"}
    valid = [h for h in hours if h.get("data_valid")]
    basis = valid or hours
    worst = max(basis, key=lambda h: _v61_float(h.get("risk_score"), 0) or 0)
    vals_t = [_v61_float(h.get("temp_c"), None) for h in basis]
    vals_p = [_v61_float(h.get("rain_probability"), None) for h in basis]
    vals_t = [x for x in vals_t if x is not None]
    vals_p = [x for x in vals_p if x is not None]
    limited = not valid
    return {
        "name": name,
        "condition": _v61_text(worst.get("condition"), "Data terbatas" if limited else "Dipantau"),
        "attention_hour": _v61_hour(worst.get("hour")),
        "temp_c": round(sum(vals_t) / len(vals_t), 1) if vals_t else None,
        "rain_probability": round(max(vals_p), 0) if vals_p else None,
        "risk_label": _v61_risk_label("limited") if limited else _v61_text(worst.get("risk_label"), "Dipantau"),
        "risk_class": "limited" if limited else _v61_text(worst.get("risk_class"), "watch"),
    }


def _v61_build_periods(hours):
    groups = {
        "Pagi": [h for h in hours if 5 <= _v61_hour_int(h.get("hour")) <= 10],
        "Siang": [h for h in hours if 11 <= _v61_hour_int(h.get("hour")) <= 14],
        "Sore": [h for h in hours if 15 <= _v61_hour_int(h.get("hour")) <= 18],
        "Malam": [h for h in hours if _v61_hour_int(h.get("hour")) >= 19 or _v61_hour_int(h.get("hour")) <= 4],
    }
    return [_v61_period_summary(name, groups[name]) for name in ["Pagi", "Siang", "Sore", "Malam"]]


def _v61_activity_matrix(day):
    label = _v61_text(day.get("risk_label"), "Dipantau")
    cls = _v61_text(day.get("risk_class"), "watch")
    peak = _v61_hour(day.get("peak_rain_hour"), "jam rawan")
    best = _v61_safe_join(day.get("best_activity_window"), " · ", "Pantau manual")
    if cls == "limited":
        raw = [
            ("Perjalanan / motor", "Jangan terlalu percaya angka", "Data masih terbatas; cek BMKG/radar dan langit lokal sebelum berangkat.", peak, "limited"),
            ("Jalan kaki", "Aman bersyarat", "Pilih rute teduh dan mudah berteduh; jangan hanya mengandalkan 0% hujan.", best, "limited"),
            ("Jemur pakaian", "Perlu dipantau", "Jangan ditinggal lama; angka hujan belum cukup kuat untuk keputusan final.", "pagi–siang", "limited"),
            ("Olahraga outdoor", "Boleh jika fleksibel", "Pilih durasi pendek dan cek kondisi awan sebelum mulai.", best, "limited"),
            ("Acara outdoor", "Wajib plan B ringan", "Siapkan opsi teduh karena confidence data belum tinggi.", peak, "limited"),
            ("Foto / city walk", "Cek langit dulu", "Cahaya bisa bagus, tapi tetap pantau awan lokal.", best, "limited"),
        ]
    elif cls in {"danger", "rain"}:
        raw = [
            ("Perjalanan / motor", "Bawa jas hujan", f"Hindari mendekati {peak} jika memungkinkan; jalan dapat licin.", peak, cls),
            ("Jalan kaki", "Pilih rute berteduh", f"Cari rute yang mudah berteduh sekitar {peak}.", peak, cls),
            ("Jemur pakaian", "Tidak ideal", "Utamakan pagi, angkat lebih awal, jangan ditinggal lama.", "pagi", "watch"),
            ("Olahraga outdoor", "Pilih window aman", f"Gunakan window lebih aman: {best}.", best, "watch"),
            ("Acara outdoor", "Siapkan plan B", f"Tenda/indoor perlu siap terutama sekitar {peak}.", peak, cls),
            ("Foto / city walk", "Pantau awan", "Bawa pelindung elektronik; hujan lokal bisa berubah cepat.", peak, "watch"),
        ]
    else:
        raw = [
            ("Perjalanan / motor", "Aman dipantau", "Kondisi relatif aman; tetap perhatikan perubahan lokal.", best, "safe"),
            ("Jalan kaki", "Cocok", f"Jam nyaman: {best}.", best, "safe"),
            ("Jemur pakaian", "Cukup aman", "Angkat sebelum sore jika awan mulai gelap.", "pagi–siang", "safe"),
            ("Olahraga outdoor", "Aman dipantau", "Pagi atau sore biasanya lebih nyaman.", best, "safe"),
            ("Acara outdoor", "Bisa dilanjutkan", "Tetap siapkan opsi teduh ringan untuk antisipasi.", best, "safe"),
            ("Foto / city walk", "Cocok", "Pantau cahaya dan awan lokal sebelum berangkat.", best, "safe"),
        ]
    return [{"activity": a, "status": s, "advice": adv, "priority_hour": pr, "risk_class": rc} for a, s, adv, pr, rc in raw]


def _v61_repair_day(day, index, api, args, api_quality):
    d = dict(day or {}) if isinstance(day, dict) else {}
    loc = _v61_text(api.get("location_name"), getattr(args, "location_name", "Lokasi"))
    raw_hours = d.get("key_hours") or d.get("hours") or d.get("hourly") or []
    repaired_hours = [_v61_repair_hour(h, api_quality) for h in raw_hours if isinstance(h, dict)]
    repaired_hours = _v61_pick_key_hours(repaired_hours)
    valid = [h for h in repaired_hours if h.get("data_valid")]
    missing_ratio = 1 - (len(valid) / max(1, len(repaired_hours))) if repaired_hours else 1
    probs = [_v61_float(h.get("rain_probability"), None) for h in valid]
    probs = [p for p in probs if p is not None]
    scores = [_v61_float(h.get("risk_score"), None) for h in repaired_hours]
    scores = [s for s in scores if s is not None]
    peak_prob = max(probs) if probs else None
    peak_hour = _v61_hour((max(valid, key=lambda h: _v61_float(h.get("rain_probability"), -1)).get("hour") if probs and valid else d.get("peak_rain_hour")), "—")
    avg_temp_vals = [_v61_float(h.get("temp_c"), None) for h in valid]
    avg_rh_vals = [_v61_float(h.get("humidity_pct"), None) for h in valid]
    heat_vals = [_v61_float(h.get("heat_index_c"), None) for h in valid]
    wind_vals = [_v61_float(h.get("wind_kmh"), None) for h in valid]
    low_conf = api_quality.get("active", 0) < 3 or missing_ratio > 0.35
    base_score = max(scores) if scores else (38 if low_conf else 18)
    if low_conf and (peak_prob is None or peak_prob < 35):
        risk_cls = "limited" if missing_ratio > 0.55 or api_quality.get("active", 0) < 2 else "watch"
    else:
        risk_cls = _v61_risk_class(base_score, peak_prob, limited=False)
    risk_label = _v61_risk_label(risk_cls)
    best = _v61_best_windows(repaired_hours)
    if risk_cls == "limited":
        sentence = f"{loc}: data cuaca masih terbatas. Jangan baca 0% sebagai pasti aman; cek BMKG/radar dan kondisi langit sebelum aktivitas."
    elif risk_cls in {"rain", "danger"}:
        sentence = f"{loc}: potensi hujan lokal perlu diantisipasi sekitar {peak_hour}. Siapkan payung/jas hujan dan rencana cadangan."
    elif risk_cls == "watch":
        sentence = f"{loc}: kondisi masih bisa dipakai, tetapi confidence belum penuh. Window relatif nyaman: {_v61_safe_join(best)}."
    else:
        sentence = f"{loc}: kondisi relatif aman untuk aktivitas harian. Window nyaman: {_v61_safe_join(best)}."
    d.update({
        "day_index": index,
        "day_tag": _v61_text(d.get("day_tag"), ["Hari ini", "Besok", "Lusa"][index] if index < 3 else f"Hari +{index}"),
        "date": _v61_text(d.get("date"), ""),
        "date_label": _v61_text(d.get("date_label"), _v61_text(d.get("date"), "")),
        "key_hours": repaired_hours,
        "periods": _v61_build_periods(repaired_hours),
        "peak_rain_probability": round(peak_prob, 0) if peak_prob is not None else None,
        "peak_rain_hour": peak_hour,
        "avg_temperature_c": round(sum(avg_temp_vals) / len(avg_temp_vals), 1) if avg_temp_vals else None,
        "avg_humidity_pct": round(sum(avg_rh_vals) / len(avg_rh_vals), 0) if avg_rh_vals else None,
        "max_heat_index_c": round(max(heat_vals), 1) if heat_vals else None,
        "max_wind_kmh": round(max(wind_vals), 1) if wind_vals else None,
        "risk_score": round(base_score, 0),
        "risk_class": risk_cls,
        "risk_label": risk_label,
        "condition": _v61_text((valid[-1].get("condition") if valid else None), "Data terbatas" if risk_cls == "limited" else "Dipantau"),
        "best_activity_window": best,
        "missing_ratio": round(missing_ratio, 3),
        "confidence_level": "Rendah" if low_conf else api_quality.get("level", "Sedang"),
        "decision_sentence": sentence,
        "nowcast": {"summary": "Confidence rendah karena sumber aktif terbatas." if low_conf else "Pantau perubahan awan lokal, terutama menjelang sore."},
    })
    d["activity_matrix"] = _v61_activity_matrix(d)
    return d


def _v61_repair_api(api, args):
    if not isinstance(api, dict):
        api = {}
    api = dict(api)
    api["brand"] = LANGIT_BRAND_NAME
    api["version"] = LANGIT_PUBLIC_VERSION
    api["location_name"] = _v61_text(api.get("location_name"), getattr(args, "location_name", "Lokasi"))
    api["location_slug"] = _v61_text(api.get("location_slug"), getattr(args, "location_slug", "location"))
    api["generated_at"] = _v61_text(api.get("generated_at"), now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).isoformat())
    api["updated_label"] = now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).strftime("%A, %d %B %Y, %H:%M WIB")
    q = _v61_source_quality(api)
    court = api.get("source_court") if isinstance(api.get("source_court"), dict) else {}
    court["status"] = "Aktif" if q["active"] >= 3 else "Terbatas"
    court["confidence_level"] = q["level"]
    court["summary"] = f"{q['active']}/{q['total']} sumber aktif. Confidence operasional: {q['level']}."
    api["source_court"] = court
    raw_days = api.get("days") or []
    api["days"] = [_v61_repair_day(d, i, api, args, q) for i, d in enumerate(raw_days[:3])]
    if not api["days"]:
        api["days"] = [_v61_repair_day({"key_hours": []}, 0, api, args, q)]
    api["data_integrity"] = {
        "source_active": q["active"],
        "source_total": q["total"],
        "source_confidence": q["level"],
        "rule": "missing_data_never_becomes_safe_or_zero_rain",
    }
    return api


def _v61_status_dot(cls):
    return f'<span class="dot {html.escape(_v61_text(cls), quote=True)}"></span>'


def _v61_kpi(title, value, sub=""):
    return f'<div class="kpi"><span>{_v61_esc(title)}</span><strong>{_v61_esc(value)}</strong><small>{_v61_esc(sub)}</small></div>'


def _v61_topbar(api, active="today"):
    loc = _v61_esc(api.get("location_name"))
    def nav(label, href, key):
        cls = "active" if active == key else ""
        return f'<a class="nav {cls}" href="{href}">{label}</a>'
    return f'''
<header class="topbar">
  <a class="brand" href="../index.html"><span class="logo"></span><span><b>LANGIT</b><small>{loc} · v61</small></span></a>
  <nav>
    {nav('Hari ini','anemos_app.html','today')}
    {nav('3 hari','anemos_3day.html','3day')}
    {nav('Aktivitas','anemos_activity.html','activity')}
    {nav('Peta','langit_map_room.html','map')}
    {nav('Model','langit_model_court.html','court')}
    {nav('Akurasi','sentinel_x_accuracy_public.html','accuracy')}
    {nav('Lokasi','../index.html','portal')}
  </nav>
</header>'''


def _v61_css():
    return '''
:root{--bg:#06111f;--bg2:#081b31;--panel:#102238;--panel2:#162b43;--line:#254765;--text:#f4f8ff;--muted:#9fb5cc;--blue:#23a8ff;--green:#28df8f;--amber:#ffc857;--orange:#ff8b3d;--red:#ff3f6e;--limited:#9da8ff;--shadow:0 24px 90px rgba(0,0,0,.35)}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 72% 12%,rgba(35,168,255,.28),transparent 30%),radial-gradient(circle at 5% 0%,rgba(40,223,143,.10),transparent 24%),linear-gradient(180deg,#06111f,#071a2f 55%,#06111f);color:var(--text);font-family:Inter,Plus Jakarta Sans,Manrope,system-ui,-apple-system,Segoe UI,sans-serif;letter-spacing:-.015em}.topbar{position:sticky;top:0;z-index:30;display:flex;justify-content:space-between;align-items:center;padding:18px clamp(18px,6vw,90px);background:rgba(6,17,31,.82);backdrop-filter:blur(16px);border-bottom:1px solid rgba(76,139,190,.24)}.brand{display:flex;align-items:center;gap:12px;color:var(--text);text-decoration:none}.brand small{display:block;color:var(--muted);font-size:12px}.logo{width:36px;height:36px;border-radius:13px;background:conic-gradient(from 210deg,#1cdf94,#25a8ff,#1756ff,#1cdf94);box-shadow:0 0 34px rgba(35,168,255,.35)}nav{display:flex;flex-wrap:wrap;gap:8px}.nav{color:var(--text);text-decoration:none;border:1px solid var(--line);background:rgba(16,34,56,.72);padding:9px 14px;border-radius:999px;font-weight:800;font-size:13px}.nav.active,.btn.primary{background:linear-gradient(135deg,#1687ff,#19c5ff);border-color:#54c9ff;box-shadow:0 14px 35px rgba(35,168,255,.25)}main{width:min(1180px,92vw);margin:28px auto 70px}.grid-hero{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:18px}.hero{position:relative;overflow:hidden;min-height:250px;padding:42px;border:1px solid #2a6290;border-radius:30px;background:linear-gradient(135deg,#0a2d69,#1266c0 55%,#24baf2);box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;right:-60px;bottom:-90px;width:310px;height:310px;border-radius:50%;background:rgba(255,255,255,.18)}.hero .pills{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}.pill{border:1px solid rgba(255,255,255,.36);background:rgba(255,255,255,.12);border-radius:999px;padding:7px 11px;font-size:12px;font-weight:900}.hero h1{font-size:clamp(42px,6vw,72px);line-height:.9;margin:0 0 18px;max-width:820px}.hero p{font-size:17px;line-height:1.55;margin:0;max-width:820px}.side-kpis{display:grid;grid-template-columns:1fr 1fr;gap:12px}.side-kpis .big{grid-column:1/-1}.kpi{background:linear-gradient(180deg,rgba(22,43,67,.94),rgba(13,30,50,.94));border:1px solid var(--line);border-radius:22px;padding:18px;min-height:92px}.kpi span{display:block;text-transform:uppercase;color:#8fd1ff;font-size:11px;font-weight:950;letter-spacing:.09em}.kpi strong{display:block;margin:7px 0 4px;font-size:clamp(24px,2.4vw,36px);line-height:1}.kpi small{color:var(--muted)}.notice{margin:14px 0 20px;padding:10px 14px;border:1px solid rgba(255,200,87,.52);background:rgba(255,200,87,.08);border-radius:16px;color:#ffd98a;font-weight:800}.section{margin-top:20px;padding:24px;border:1px solid var(--line);border-radius:26px;background:rgba(12,29,49,.78);box-shadow:0 18px 60px rgba(0,0,0,.18)}.section-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:16px}.section h2{font-size:24px;margin:0}.section .hint{color:var(--muted);font-size:13px}.decision{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px}.decision-main{padding:28px;border-radius:24px;background:linear-gradient(135deg,#071a31,#0d2d53);border:1px solid #255d88}.decision-main h2{font-size:clamp(34px,4vw,54px);line-height:1;margin:12px 0}.badge{display:inline-flex;gap:7px;align-items:center;border-radius:999px;padding:7px 11px;font-weight:950;font-size:12px;background:rgba(255,255,255,.09);border:1px solid var(--line)}.dot{width:10px;height:10px;border-radius:50%;display:inline-block;background:var(--blue)}.dot.safe{background:var(--green)}.dot.watch{background:var(--amber)}.dot.rain{background:var(--orange)}.dot.danger{background:var(--red)}.dot.limited{background:var(--limited)}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.card{border:1px solid var(--line);border-top:4px solid var(--green);background:linear-gradient(180deg,rgba(27,47,70,.96),rgba(16,34,56,.96));border-radius:22px;padding:18px}.card.watch{border-top-color:var(--amber)}.card.rain{border-top-color:var(--orange)}.card.danger{border-top-color:var(--red)}.card.limited{border-top-color:var(--limited)}.card h3{margin:6px 0 8px;font-size:22px}.mini-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px}.mini{border:1px solid #285175;background:#0d2948;border-radius:13px;padding:10px}.mini small{color:var(--muted);display:block}.mini b{font-size:18px}.map-frame{width:100%;height:430px;border:0;border-radius:22px;background:#07111f}.chart{display:flex;align-items:flex-end;gap:8px;height:170px;padding-top:16px}.bar-wrap{flex:1;text-align:center;color:var(--muted);font-size:12px}.bar{min-height:5px;border-radius:9px 9px 3px 3px;background:linear-gradient(180deg,var(--green),#12a66d)}.bar.watch{background:linear-gradient(180deg,var(--amber),#d89700)}.bar.rain{background:linear-gradient(180deg,var(--orange),#da5e1b)}.bar.danger{background:linear-gradient(180deg,var(--red),#c91243)}.bar.limited{background:linear-gradient(180deg,var(--limited),#626ee6)}.hours{display:grid;gap:10px}.hour-row{display:grid;grid-template-columns:80px minmax(230px,1fr) repeat(5,105px);gap:10px;align-items:center;border-left:5px solid var(--green);background:rgba(28,48,72,.88);border-radius:18px;padding:12px}.hour-row.watch{border-left-color:var(--amber)}.hour-row.rain{border-left-color:var(--orange)}.hour-row.danger{border-left-color:var(--red)}.hour-row.limited{border-left-color:var(--limited)}.hour-row .time{font-size:20px;font-weight:950}.cond b{display:block}.cond small{color:var(--muted)}.cell{background:#0d2948;border:1px solid #285175;border-radius:13px;padding:10px}.cell b{font-size:18px}.cell small{display:block;color:var(--muted);font-size:11px}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.activity{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.model-table{width:100%;border-collapse:collapse}.model-table th,.model-table td{padding:11px;border-bottom:1px solid var(--line);text-align:left}.model-table th{color:#8fd1ff;text-transform:uppercase;font-size:11px;letter-spacing:.08em}.sharebox{width:100%;min-height:130px;background:#05111f;color:#eaf7ff;border:1px solid var(--line);border-radius:14px;padding:14px;font-family:ui-monospace,Menlo,Consolas,monospace}.btn{display:inline-block;text-decoration:none;color:#fff;border:1px solid var(--line);background:#17304e;border-radius:999px;padding:10px 14px;font-weight:900;margin:4px 6px 0 0}@media(max-width:900px){.grid-hero,.decision,.two{grid-template-columns:1fr}.cards,.activity{grid-template-columns:1fr}.hour-row{grid-template-columns:70px 1fr 1fr 1fr}.hour-row .cond{grid-column:2/-1}.topbar{align-items:flex-start;gap:12px;flex-direction:column}.hero{padding:28px}.side-kpis{grid-template-columns:1fr 1fr}}
'''


def _v61_doc(title, api, active, body):
    return f'''<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_v61_esc(title)}</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;700;800;900&display=swap" rel="stylesheet"><style>{_v61_css()}</style></head>
<body>{_v61_topbar(api, active)}<main>{body}</main><footer style="text-align:center;color:#7f96ae;margin:60px 0 30px;font-size:13px">LANGIT · v61.0 · Data integrity first</footer></body></html>'''


def _v61_hero(api, page_title, subtitle=None):
    d = api.get("days", [{}])[0]
    side = '<div class="side-kpis">' + _v61_kpi("Cuaca", _v61_fmt_num(d.get("avg_temperature_c"), "°C", 1), _v61_text(d.get("condition"), "lokal")) + _v61_kpi("Hujan", _v61_fmt_pct(d.get("peak_rain_probability")), "puncak") + _v61_kpi("Status", d.get("risk_label"), "ringkasan") + '</div>'
    hero = f'''<div class="grid-hero"><section class="hero"><div class="pills"><span class="pill">LANGIT</span><span class="pill">v61.0</span><span class="pill">Diperbarui {_v61_esc(api.get('updated_label'))}</span></div><h1>{_v61_esc(page_title)}</h1><p>{_v61_esc(subtitle or d.get('decision_sentence') or 'Cuaca lokal untuk keputusan harian.')}</p></section>{side}</div><div class="notice">{_v61_esc(LANGIT_DISCLAIMER)}</div>'''
    return hero


def _v61_decision(api):
    d = api.get("days", [{}])[0]
    cls = _v61_text(d.get("risk_class"), "watch")
    body = f'''<section class="section decision"><div class="decision-main"><span class="badge">{_v61_status_dot(cls)}{_v61_esc(d.get('risk_label'))}</span><h2>{_v61_esc(d.get('decision_sentence'))}</h2><p>{_v61_esc((d.get('nowcast') or {}).get('summary'))}</p></div><div class="side-kpis">{_v61_kpi('Risk score', _v61_fmt_num(d.get('risk_score'), '/100', 0), d.get('confidence_level'))}{_v61_kpi('Puncak hujan', _v61_fmt_pct(d.get('peak_rain_probability')), 'sekitar '+_v61_hour(d.get('peak_rain_hour')))}{_v61_kpi('Window nyaman', _v61_safe_join(d.get('best_activity_window')), 'aktivitas')}{_v61_kpi('Confidence', d.get('confidence_level'), 'data')}</div></section>'''
    return body


def _v61_day_cards(api):
    cards = []
    for d in (api.get("days") or [])[:3]:
        cls = _v61_text(d.get("risk_class"), "watch")
        cards.append(f'''<article class="card {cls}"><small>{_v61_esc(d.get('day_tag'))} · {_v61_esc(d.get('date_label') or d.get('date'))}</small><h3>{_v61_esc(d.get('risk_label'))}</h3><p>{_v61_esc(d.get('decision_sentence'))}</p><div class="mini-grid"><div class="mini"><small>Hujan</small><b>{_v61_fmt_pct(d.get('peak_rain_probability'))}</b></div><div class="mini"><small>Jam</small><b>{_v61_hour(d.get('peak_rain_hour'))}</b></div><div class="mini"><small>Score</small><b>{_v61_fmt_num(d.get('risk_score'), '', 0)}</b></div></div></article>''')
    return f'''<section class="section"><div class="section-head"><h2>Ringkasan 3 hari</h2><span class="hint">Tidak mengubah data kosong menjadi aman.</span></div><div class="cards">{''.join(cards)}</div></section>'''


def _v61_map_section(api):
    return '''<section class="section"><div class="section-head"><h2>Map Room</h2><span class="hint">Peta dibatasi Indonesia, zona lokal, dan time slider.</span></div><iframe class="map-frame" src="langit_map_room.html" loading="lazy"></iframe><a class="btn primary" href="langit_map_room.html">Buka peta penuh</a><a class="btn" href="langit_location.geojson">GeoJSON</a><a class="btn" href="langit_map_layers.json">Map layers JSON</a></section>'''


def _v61_rain_chart(day):
    bars = []
    for h in day.get("key_hours", []):
        p = _v61_float(h.get("rain_probability"), 0) or 0
        cls = _v61_text(h.get("risk_class"), "limited")
        height = 5 if h.get("data_limited") else max(5, min(145, p * 1.55 + 8))
        label = _v61_fmt_pct(h.get("rain_probability"))
        bars.append(f'<div class="bar-wrap"><b>{label}</b><div class="bar {cls}" style="height:{height}px"></div><small>{_v61_hour(h.get("hour"))}</small></div>')
    return f'''<section class="section"><div class="section-head"><h2>Peluang hujan</h2><span class="hint">Jika data terbatas, angka tidak dipaksa menjadi 0%.</span></div><div class="chart">{''.join(bars)}</div></section>'''


def _v61_metrics(day):
    return f'''<section class="section"><div class="section-head"><h2>Kondisi utama</h2><span class="hint">Variabel untuk keputusan harian.</span></div><div class="cards"><div class="card">{_v61_kpi('Suhu', _v61_fmt_num(day.get('avg_temperature_c'),'°C',1), 'rata-rata')}</div><div class="card">{_v61_kpi('Terasa', _v61_fmt_num(day.get('max_heat_index_c'),'°C',1), 'heat index')}</div><div class="card">{_v61_kpi('RH', _v61_fmt_pct(day.get('avg_humidity_pct')), 'kelembapan')}</div></div></section>'''


def _v61_periods(day):
    cards = []
    for p in day.get("periods", []):
        cls = _v61_text(p.get("risk_class"), "limited")
        cards.append(f'''<article class="card {cls}"><h3>{_v61_esc(p.get('name'))}</h3><b>{_v61_esc(p.get('condition'))}</b><p>Jam perhatian: {_v61_hour(p.get('attention_hour'))}</p><div class="mini-grid"><div class="mini"><small>Suhu</small><b>{_v61_fmt_num(p.get('temp_c'),'°C',1)}</b></div><div class="mini"><small>Hujan</small><b>{_v61_fmt_pct(p.get('rain_probability'))}</b></div><div class="mini"><small>Risiko</small><b>{_v61_esc(p.get('risk_label'))}</b></div></div></article>''')
    return f'''<section class="section"><div class="section-head"><h2>Pagi, siang, sore, malam</h2><span class="hint">Ringkasan cepat tanpa membaca semua jam.</span></div><div class="cards">{''.join(cards)}</div></section>'''


def _v61_activity(day):
    cards = []
    for a in day.get("activity_matrix", []):
        cls = _v61_text(a.get("risk_class"), "watch")
        cards.append(f'''<article class="card {cls}"><h3>{_v61_esc(a.get('activity'))}</h3><b>{_v61_esc(a.get('status'))}</b><p>{_v61_esc(a.get('advice'))}</p><small>Fokus: {_v61_esc(a.get('priority_hour'))}</small></article>''')
    return f'''<section class="section"><div class="section-head"><h2>Saran aktivitas</h2><span class="hint">Bahasa praktis, bukan klaim palsu.</span></div><div class="activity">{''.join(cards)}</div></section>'''


def _v61_hours(day, title="Jam penting", risky_only=False):
    hours = day.get("key_hours", [])
    if risky_only:
        risky = [h for h in hours if h.get("risk_class") in {"watch", "rain", "danger", "limited"} or (_v61_float(h.get("risk_score"), 0) or 0) >= 30]
        hours = risky[:8] or hours[:8]
    rows = []
    for h in hours:
        cls = _v61_text(h.get("risk_class"), "limited")
        rows.append(f'''<div class="hour-row {cls}"><div class="time">{_v61_hour(h.get('hour'))}</div><div class="cond"><b>{_v61_esc(h.get('condition'))}</b><small>{_v61_esc(h.get('advice'))}</small></div><div class="cell"><b>{_v61_fmt_num(h.get('temp_c'),'°C',1)}</b><small>Suhu</small></div><div class="cell"><b>{_v61_fmt_pct(h.get('humidity_pct'))}</b><small>RH</small></div><div class="cell"><b>{_v61_fmt_num(h.get('heat_index_c'),'°C',1)}</b><small>Terasa</small></div><div class="cell"><b>{_v61_fmt_pct(h.get('rain_probability'))}</b><small>Hujan</small></div><div class="cell"><b>{_v61_esc(h.get('risk_label'))}</b><small>Risiko</small></div></div>''')
    return f'''<section class="section"><div class="section-head"><h2>{_v61_esc(title)}</h2><span class="hint">Kondisi, angka, dan risiko dipisah agar tidak menumpuk.</span></div><div class="hours">{''.join(rows) or '<p>Data jam belum tersedia.</p>'}</div></section>'''


def _v61_model(api):
    court = api.get("source_court") or {}
    rows = ""
    for s in court.get("sources", []) or []:
        if not isinstance(s, dict):
            continue
        rows += f'<tr><td>{_v61_esc(s.get("source_id") or s.get("model") or s.get("name"))}</td><td>{_v61_esc(s.get("verdict") or s.get("status") or "Dipantau")}</td><td>{_v61_esc(s.get("points") or s.get("point") or "0")}</td><td>{_v61_esc(s.get("http_status") or "—")}</td><td>{_v61_esc(s.get("latency_ms") or "—")}</td></tr>'
    if not rows:
        rows = '<tr><td colspan="5">Status sumber belum tersedia.</td></tr>'
    return f'''<section class="section"><div class="section-head"><h2>Weather Brain</h2><span class="hint">Source court dan confidence.</span></div><div class="two"><div class="card"><small>Microclimate</small><h3>{_v61_esc((api.get('microclimate') or {}).get('profile') or 'Lokal')}</h3><p>{_v61_esc((api.get('microclimate') or {}).get('note') or 'Efek lokal tetap perlu dipantau.')}</p></div><div class="card"><small>Source court</small><h3>{_v61_esc(court.get('status'))}</h3><p>{_v61_esc(court.get('summary'))}</p></div></div><table class="model-table"><thead><tr><th>Model</th><th>Putusan</th><th>Point</th><th>HTTP</th><th>ms</th></tr></thead><tbody>{rows}</tbody></table></section>'''


def _v61_share_text(api):
    d = api.get("days", [{}])[0]
    return f"LANGIT · {_v61_text(api.get('location_name'))}\n{_v61_text(d.get('date_label'), _v61_text(d.get('date')))}\n{_v61_text(d.get('decision_sentence'))}\nPuncak hujan: {_v61_fmt_pct(d.get('peak_rain_probability'))} sekitar {_v61_hour(d.get('peak_rain_hour'))}.\nConfidence: {_v61_text(d.get('confidence_level'))}.\nBukan peringatan resmi; untuk cuaca ekstrem ikuti BMKG."


def _v61_share(api):
    return f'''<section class="section two"><div><h2>Share singkat</h2><textarea class="sharebox" readonly>{_v61_esc(_v61_share_text(api))}</textarea></div><div><h2>Catatan penggunaan</h2><ul><li>Missing data tidak diubah menjadi aman.</li><li>0% hanya kuat bila data inti tersedia.</li><li>Hujan lokal bisa bergeser beberapa kilometer atau beberapa jam.</li><li>Untuk cuaca ekstrem, ikuti BMKG dan kondisi setempat.</li></ul></div></section>'''


def _v61_page(api, args, page="today"):
    d = api.get("days", [{}])[0]
    if page == "3day":
        body = _v61_hero(api, "Prakiraan 3 hari", f"Bandingkan risiko {api.get('location_name')} hari ini, besok, dan lusa.") + _v61_day_cards(api)
        for dd in api.get("days", [])[:3]:
            body += _v61_periods(dd) + _v61_hours(dd, f"Detail jam · {_v61_text(dd.get('day_tag'))}")
        return _v61_doc("LANGIT — 3 hari", api, "3day", body)
    if page == "activity":
        body = _v61_hero(api, "Saran aktivitas", f"Rekomendasi praktis untuk {api.get('location_name')}.") + _v61_decision(api) + _v61_activity(d) + _v61_hours(d, "Jam rawan untuk aktivitas", True) + _v61_share(api)
        return _v61_doc("LANGIT — Aktivitas", api, "activity", body)
    if page == "court":
        body = _v61_hero(api, "Model Court", "Ringkasan sumber data, confidence, dan integritas keputusan.") + _v61_model(api) + _v61_map_section(api) + _v61_share(api) + _v61_hours(d, "Jam penting")
        return _v61_doc("LANGIT — Model Court", api, "court", body)
    if page == "map":
        body = _v61_hero(api, "Map Room", "Peta risiko lokal dengan batas Indonesia.") + _v61_map_section(api) + _v61_rain_chart(d) + _v61_metrics(d)
        return _v61_doc("LANGIT — Map", api, "map", body)
    body = _v61_hero(api, f"Prakiraan {api.get('location_name')}") + _v61_decision(api) + _v61_day_cards(api) + _v61_map_section(api) + _v61_rain_chart(d) + _v61_metrics(d) + _v61_periods(d) + _v61_activity(d) + _v61_model(api) + _v61_share(api) + _v61_hours(d, "Jam penting")
    return _v61_doc("LANGIT — Hari ini", api, "today", body)


def _v61_geojson(api, args):
    lat = _v61_float(getattr(args, "latitude", None), DEFAULT_LATITUDE)
    lon = _v61_float(getattr(args, "longitude", None), DEFAULT_LONGITUDE)
    d = api.get("days", [{}])[0]
    features = [{"type":"Feature","geometry":{"type":"Point","coordinates":[lon,lat]},"properties":{"kind":"center","name":api.get("location_name"),"risk_class":d.get("risk_class"),"risk_label":d.get("risk_label"),"risk_score":d.get("risk_score"),"rain_probability":d.get("peak_rain_probability"),"hour":"now","radius_m":650}}]
    offsets = [("Utara / orografis",0.012,0,1700),("Pusat aktivitas",0,0,900),("Koridor timur",0.001,0.018,1500),("Koridor barat",-0.002,-0.018,1500)]
    for h in d.get("key_hours", []) or []:
        for name, dy, dx, radius in offsets:
            score = _v61_clamp((_v61_float(h.get("risk_score"), 0) or 0) + (8 if "orografis" in name.lower() else 3))
            features.append({"type":"Feature","geometry":{"type":"Point","coordinates":[lon+dx,lat+dy]},"properties":{"kind":"zone","name":name,"hour":_v61_hour(h.get("hour")),"risk_class":_v61_risk_class(score, h.get("rain_probability"), h.get("data_limited")),"risk_label":_v61_risk_label(_v61_risk_class(score, h.get("rain_probability"), h.get("data_limited"))),"risk_score":round(score,0),"rain_probability":h.get("rain_probability"),"condition":h.get("condition"),"radius_m":radius}})
    return {"type":"FeatureCollection","features":features}


def _v61_leaflet_html(title, geojson, back="anemos_app.html", portal=False):
    data = json.dumps(geojson, ensure_ascii=False)
    center = [0.4, 117.0] if portal else None
    if not portal and geojson.get("features"):
        c = geojson["features"][0]["geometry"]["coordinates"]
        center = [c[1], c[0]]
    zoom = 5 if portal else 12
    min_zoom = 5 if portal else 10
    return f'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_v61_esc(title)}</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><style>html,body,#map{{height:100%;margin:0;background:#06111f;font-family:Inter,system-ui,sans-serif;color:#fff}}#panel{{position:absolute;z-index:900;left:18px;top:18px;width:min(390px,calc(100vw - 36px));padding:20px;border-radius:22px;background:rgba(6,17,31,.88);border:1px solid #2b5375;box-shadow:0 18px 55px rgba(0,0,0,.45)}}#panel h1{{font-size:24px;line-height:1;margin:0 0 10px}}#panel p{{color:#b7c8db;margin:0 0 14px;line-height:1.45}}.btn{{display:inline-block;background:#20a8ff;color:#fff;text-decoration:none;border-radius:999px;padding:10px 15px;font-weight:900}}#slider{{position:absolute;z-index:900;left:50%;bottom:22px;transform:translateX(-50%);display:flex;gap:8px;flex-wrap:wrap;justify-content:center;width:min(780px,90vw);padding:13px;border-radius:999px;background:rgba(6,17,31,.82);border:1px solid #2b5375}}#slider button{{border:1px solid #466b8e;background:#13283f;color:#eaf6ff;border-radius:999px;padding:10px 13px;font-weight:900}}#slider button.active{{background:#20a8ff}}.legend{{position:absolute;z-index:900;right:20px;bottom:28px;background:rgba(6,17,31,.86);border:1px solid #2b5375;border-radius:16px;padding:12px;font-size:13px}}.i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}}.safe{{background:#28df8f}}.watch{{background:#ffc857}}.rain{{background:#ff8b3d}}.danger{{background:#ff3f6e}}.limited{{background:#9da8ff}}</style></head><body><div id="map"></div><div id="panel"><h1>{_v61_esc(title)}</h1><p>Peta dibatasi Indonesia. Zona berubah mengikuti jam pada time slider. Data terbatas tidak dibaca sebagai aman.</p><a class="btn" href="{html.escape(back,quote=True)}">Kembali</a></div><div id="slider"></div><div class="legend"><div><span class="i safe"></span>Aman</div><div><span class="i watch"></span>Dipantau</div><div><span class="i rain"></span>Waspada</div><div><span class="i danger"></span>Risiko tinggi</div><div><span class="i limited"></span>Data terbatas</div></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>const DATA={data};const center={json.dumps(center)};const indonesiaBounds=[[-11.2,94.5],[6.4,141.5]];const color=c=>({{safe:'#28df8f',watch:'#ffc857',rain:'#ff8b3d',danger:'#ff3f6e',limited:'#9da8ff'}}[c]||'#23a8ff');const map=L.map('map',{{center:center,zoom:{zoom},minZoom:{min_zoom},maxZoom:16,maxBounds:indonesiaBounds,maxBoundsViscosity:1,worldCopyJump:false}});L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',{{subdomains:'abcd',maxZoom:19,noWrap:true,attribution:'&copy; OpenStreetMap & CARTO'}}).addTo(map);let layer=L.layerGroup().addTo(map);const hours=[...new Set(DATA.features.map(f=>f.properties.hour).filter(h=>h&&h!=='now'))];function draw(hour){{layer.clearLayers();DATA.features.filter(f=>f.properties.kind==='center'||!hour||f.properties.hour===hour).forEach(f=>{{const p=f.properties,latlng=[f.geometry.coordinates[1],f.geometry.coordinates[0]],col=color(p.risk_class);L.circle(latlng,{{radius:p.radius_m||650,color:col,fillColor:col,fillOpacity:.22,weight:2}}).addTo(layer).bindPopup(`<b>${{p.name||'Zona'}}</b><br>${{p.condition||p.risk_label||''}}<br>Hujan: ${{p.rain_probability??'—'}}%<br>Score: ${{p.risk_score??'—'}}`);L.circleMarker(latlng,{{radius:p.kind==='center'?9:6,color:col,fillColor:col,fillOpacity:.95,weight:2}}).addTo(layer);}});}}const slider=document.getElementById('slider');(hours.length?hours:['now']).forEach((h,i)=>{{const b=document.createElement('button');b.textContent=h;b.onclick=()=>{{document.querySelectorAll('#slider button').forEach(x=>x.classList.remove('active'));b.classList.add('active');draw(h)}};if(i===0)b.classList.add('active');slider.appendChild(b)}});draw(hours[0]);try{{const group=L.featureGroup();DATA.features.filter(f=>f.properties.kind==='center').forEach(f=>group.addLayer(L.marker([f.geometry.coordinates[1],f.geometry.coordinates[0]])));if(group.getLayers().length>1)map.fitBounds(group.getBounds().pad(.25));}}catch(e){{}}</script></body></html>'''


def _v61_write_maps(api, args):
    geo = _v61_geojson(api, args)
    write_json(path_output("langit_location.geojson"), geo)
    write_json(path_output("langit_map_layers.json"), {"brand": LANGIT_BRAND_NAME, "version": LANGIT_PUBLIC_VERSION, "bounds": "Indonesia", "geojson": geo})
    doc = _v61_leaflet_html(f"LANGIT Map Room — {api.get('location_name')}", geo, "anemos_app.html", portal=False)
    atomic_write_text(path_output("langit_map_room.html"), lambda f: f.write(doc))
    atomic_write_text(path_output("anemos_map.html"), lambda f: f.write(doc))


def _v61_write_outputs(args, forecast_dates=None):
    api = _lg_build_api(args, forecast_dates)
    api = _v61_repair_api(api, args)
    _v61_write_maps(api, args)
    write_json(path_output("langit_api_v1.json"), api)
    write_json(path_output("anemos_api_v1.json"), api)  # compatibility
    write_json(path_output("langit_intelligence.json"), {"brand": LANGIT_BRAND_NAME, "version": LANGIT_PUBLIC_VERSION, "data_integrity": api.get("data_integrity"), "source_court": api.get("source_court"), "days": api.get("days")})
    daily_rows, hourly_rows, activity_rows = [], [], []
    for d in api.get("days", []) or []:
        daily_rows.append({"date": d.get("date"), "day_tag": d.get("day_tag"), "risk_score": d.get("risk_score"), "risk_label": d.get("risk_label"), "risk_class": d.get("risk_class"), "peak_rain_probability": d.get("peak_rain_probability"), "peak_rain_hour": d.get("peak_rain_hour"), "confidence_level": d.get("confidence_level"), "best_activity_window": _v61_safe_join(d.get("best_activity_window")), "summary": d.get("decision_sentence")})
        for h in d.get("key_hours", []) or []:
            hourly_rows.append({"date": d.get("date"), "day_tag": d.get("day_tag"), "hour": h.get("hour"), "condition": h.get("condition"), "data_valid": h.get("data_valid"), "data_limited": h.get("data_limited"), "temp_c": h.get("temp_c"), "humidity_pct": h.get("humidity_pct"), "heat_index_c": h.get("heat_index_c"), "rain_probability_raw": h.get("rain_probability_raw"), "rain_probability": h.get("rain_probability"), "wind_kmh": h.get("wind_kmh"), "risk_score": h.get("risk_score"), "risk_label": h.get("risk_label"), "risk_class": h.get("risk_class")})
        for a in d.get("activity_matrix", []) or []:
            row = {"date": d.get("date"), "day_tag": d.get("day_tag")}
            if isinstance(a, dict):
                row.update(a)
            activity_rows.append(row)
    _lg_write_dict_csv(path_output("langit_daily_outlook.csv"), ["date","day_tag","risk_score","risk_label","risk_class","peak_rain_probability","peak_rain_hour","confidence_level","best_activity_window","summary"], daily_rows)
    _lg_write_dict_csv(path_output("langit_hourly_intelligence.csv"), ["date","day_tag","hour","condition","data_valid","data_limited","temp_c","humidity_pct","heat_index_c","rain_probability_raw","rain_probability","wind_kmh","risk_score","risk_label","risk_class"], hourly_rows)
    _lg_write_dict_csv(path_output("langit_activity_matrix.csv"), ["date","day_tag","activity","status","advice","priority_hour","risk_class"], activity_rows)
    pages = {
        "anemos_app.html": _v61_page(api, args, "today"),
        "langit_app.html": _v61_page(api, args, "today"),
        AETHER_DASHBOARD_FILENAME: _v61_page(api, args, "today"),
        "anemos_today.html": _v61_page(api, args, "today"),
        "anemos_3day.html": _v61_page(api, args, "3day"),
        "langit_3day.html": _v61_page(api, args, "3day"),
        "anemos_activity.html": _v61_page(api, args, "activity"),
        "langit_activity.html": _v61_page(api, args, "activity"),
        "langit_model_court.html": _v61_page(api, args, "court"),
        "langit_map.html": _v61_page(api, args, "map"),
        "langit_planner.html": _v61_page(api, args, "activity"),
        "anemos_commute_advice.html": _v61_page(api, args, "activity"),
        "anemos_laundry_advice.html": _v61_page(api, args, "activity"),
    }
    for name, doc in pages.items():
        atomic_write_text(path_output(name), lambda f, doc=doc: f.write(doc))
    atomic_write_text(path_output("langit_whatsapp_brief.txt"), lambda f: f.write(_v61_share_text(api)))
    atomic_write_text(path_output("anemos_whatsapp_brief.txt"), lambda f: f.write(_v61_share_text(api)))
    manifest = {"brand": LANGIT_BRAND_NAME, "version": LANGIT_PUBLIC_VERSION, "generated_at": api.get("generated_at"), "location": api.get("location_name"), "files": list(pages.keys()) + ["langit_api_v1.json", "langit_location.geojson", "langit_map_layers.json"], "data_integrity": api.get("data_integrity")}
    write_json(path_output("langit_manifest.json"), manifest)
    write_json(path_output("anemos_public_manifest.json"), manifest)
    return {"brand": LANGIT_BRAND_NAME, "version": LANGIT_PUBLIC_VERSION, "dashboard": path_output("anemos_app.html"), "map": path_output("langit_map_room.html"), "days": len(api.get("days", []))}


def anemos_write_multiday_public_pages(args, forecast_dates=None, source_state_rows=None):
    return _v61_write_outputs(args, forecast_dates)


def _v61_accuracy_html(rows, args):
    try:
        result = sentinel_compute_verification(rows or [], args)
        summary = result[0] if isinstance(result, tuple) else (result or {})
        reliability = result[2] if isinstance(result, tuple) and len(result) > 2 else summary.get("reliability_bins", [])
    except Exception:
        summary, reliability = {}, []
    matched = _v61_int(summary.get("matched_cases"), 0)
    target = max(1, int(getattr(args, "verification_min_cases", 30) or 30))
    pct = min(100, round(matched / target * 100))
    api = {"location_name": getattr(args, "location_name", "Lokasi"), "updated_label": now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).strftime("%A, %d %B %Y, %H:%M WIB"), "days": [{"risk_label": "Data belum cukup", "risk_class": "limited", "decision_sentence": "Akurasi belum bisa dinilai sampai pasangan prakiraan dan observasi cukup.", "confidence_level": "Belum tersedia", "peak_rain_probability": None, "peak_rain_hour": "—", "avg_temperature_c": None, "condition": "Evaluasi"}]}
    rel = "<p>Reliability table disembunyikan sampai data cukup.</p>"
    if matched >= target:
        trs = "".join(f"<tr><td>{_v61_esc(r.get('probability_bin') or r.get('bin'))}</td><td>{_v61_esc(r.get('n') or r.get('cases') or 0)}</td><td>{_v61_esc(r.get('mean_forecast_probability') or r.get('mean_forecast_pct') or '—')}</td><td>{_v61_esc(r.get('observed_rain_frequency') or r.get('observed_frequency_pct') or '—')}</td></tr>" for r in reliability if isinstance(r, dict))
        rel = f"<table class='model-table'><thead><tr><th>Kelompok peluang</th><th>Kasus</th><th>Rata-rata prakiraan</th><th>Hujan benar terjadi</th></tr></thead><tbody>{trs}</tbody></table>"
    body = _v61_hero(api, "Status akurasi", "Halaman ini baru mengklaim akurasi setelah pasangan observasi cukup.") + f"<section class='section decision'><div class='decision-main'><span class='badge'>{_v61_status_dot('limited')}Evaluasi</span><h2>{'Akurasi mulai bisa dibaca' if matched >= target else 'Belum bisa dinilai.'}</h2><p>Target awal {target} pasangan. Saat ini {matched} pasangan.</p></div><div class='side-kpis'>{_v61_kpi('Pasangan data', f'{matched}/{target}', 'prakiraan-observasi')}{_v61_kpi('Progress', f'{pct}%', 'menuju minimum')}{_v61_kpi('Error suhu','—','lebih kecil lebih baik')}{_v61_kpi('Skor hujan','—','lebih kecil lebih baik')}</div></section><section class='section'><h2>Bukti peluang hujan</h2>{rel}</section>"
    return _v61_doc("LANGIT — Status akurasi", api, "accuracy", body)


def aether_write_public_accuracy_page(args):
    rows = []
    try:
        if os.path.exists(path_output(AETHER_CSV_FILENAME)):
            rows = read_dict_csv(path_output(AETHER_CSV_FILENAME))
    except Exception:
        rows = []
    atomic_write_text(path_output("sentinel_x_accuracy_public.html"), lambda f: f.write(_v61_accuracy_html(rows, args)))
    return path_output("sentinel_x_accuracy_public.html")


def _v61_portal_card(loc, base_url=""):
    slug = sanitize_filename(getattr(loc, "slug", "location"))
    name = getattr(loc, "location_name", slug)
    api = read_json(os.path.join(root_output_dir(), slug, "langit_api_v1.json"), default=None) or read_json(os.path.join(root_output_dir(), slug, "anemos_api_v1.json"), default={}) or {}
    fake = type("Args", (), {"location_name": name, "timezone": DEFAULT_TIMEZONE, "location_slug": slug, "latitude": getattr(loc, "latitude", None), "longitude": getattr(loc, "longitude", None)})()
    api = _v61_repair_api(api if isinstance(api, dict) else {}, fake)
    d = api.get("days", [{}])[0]
    prefix = f"{base_url}/{slug}/" if base_url else f"{slug}/"
    cls = _v61_text(d.get("risk_class"), "limited")
    return f'''<article class="card {cls}"><h3>{_v61_esc(name)}</h3><p>{_v61_esc(d.get('decision_sentence'))}</p><div class="mini-grid"><div class="mini"><small>Hujan</small><b>{_v61_fmt_pct(d.get('peak_rain_probability'))}</b></div><div class="mini"><small>Jam</small><b>{_v61_hour(d.get('peak_rain_hour'))}</b></div><div class="mini"><small>Status</small><b>{_v61_esc(d.get('risk_label'))}</b></div></div><a class="btn primary" href="{prefix}anemos_app.html">Buka</a><a class="btn" href="{prefix}anemos_3day.html">3 hari</a><a class="btn" href="{prefix}anemos_activity.html">Aktivitas</a><a class="btn" href="{prefix}langit_map_room.html">Peta</a></article>'''


def sentinel_write_root_public_index(locations, run_rows, args):
    base_url = (getattr(args, "public_base_url", "") or "").rstrip("/")
    updated = now_local(getattr(args, "timezone", DEFAULT_TIMEZONE)).strftime("%A, %d %B %Y, %H:%M WIB")
    cards = "".join(_v61_portal_card(loc, base_url) for loc in locations)
    features = []
    for loc in locations:
        slug = sanitize_filename(getattr(loc, "slug", "location"))
        name = getattr(loc, "location_name", slug)
        api = read_json(os.path.join(root_output_dir(), slug, "langit_api_v1.json"), default={}) or {}
        fake = type("Args", (), {"location_name": name, "timezone": DEFAULT_TIMEZONE, "location_slug": slug, "latitude": getattr(loc, "latitude", None), "longitude": getattr(loc, "longitude", None)})()
        try:
            fixed = _v61_repair_api(api, fake)
            features.append(_v61_geojson(fixed, fake)["features"][0])
        except Exception:
            pass
    geo = {"type":"FeatureCollection", "features": features}
    atomic_write_text(root_output_path("langit_portal_map.html"), lambda f: f.write(_v61_leaflet_html("LANGIT Portal Map", geo, "index.html", portal=True)))
    write_json(root_output_path("langit_all_locations.geojson"), geo)
    api = {"location_name":"Portal", "updated_label": updated, "days":[{"risk_label":"Dipantau","risk_class":"watch","decision_sentence":"Pilih lokasi untuk melihat prakiraan yang sudah dicek integritas datanya.","confidence_level":"Per lokasi","peak_rain_probability":None,"peak_rain_hour":"—","avg_temperature_c":None,"condition":"Cuaca lokal"}]}
    body = _v61_hero(api, "Cuaca lokal yang langsung bisa dipakai", "Pilih lokasi, lihat risiko, peta, aktivitas, dan data publik.") + f"<section class='section'><div class='section-head'><h2>Pilih lokasi</h2><span class='hint'>Ringkasan cepat untuk tiap wilayah.</span></div><div class='cards'>{cards}</div></section><section class='section'><div class='section-head'><h2>Peta lokasi</h2><span class='hint'>Peta Indonesia, bukan world map bebas.</span></div><iframe class='map-frame' src='langit_portal_map.html'></iframe><a class='btn primary' href='langit_portal_map.html'>Buka peta penuh</a><a class='btn' href='langit_all_locations.geojson'>GeoJSON semua lokasi</a></section><section class='section'><h2>Data publik</h2><a class='btn' href='forecast_all_locations.csv'>Forecast CSV</a><a class='btn' href='source_status_all_locations.csv'>Status sumber</a><a class='btn' href='langit_portal_manifest.json'>Manifest</a></section>"
    atomic_write_text(root_output_path("index.html"), lambda f: f.write(_v61_doc("LANGIT Portal", api, "portal", body)))
    manifest = {"brand": LANGIT_BRAND_NAME, "version": LANGIT_PUBLIC_VERSION, "generated_at": updated, "locations": [getattr(loc, "slug", "location") for loc in locations], "index": root_output_path("index.html"), "map": root_output_path("langit_portal_map.html"), "geojson": root_output_path("langit_all_locations.geojson"), "disclaimer": LANGIT_DISCLAIMER}
    write_json(root_output_path("langit_portal_manifest.json"), manifest)
    write_json(root_output_path("anemos_portal_manifest.json"), manifest)
    return root_output_path("index.html")
# ---------- LANGIT v61.0 PRODUCT REBUILD HOTFIX: END ----------
"""


def remove_old_hotfix(text: str) -> str:
    pattern = re.compile(
        re.escape(START) + r".*?" + re.escape(END) + r"\n?",
        flags=re.DOTALL,
    )
    return pattern.sub("", text)


def insert_before_main_guard(text: str, block: str) -> str:
    text = remove_old_hotfix(text).rstrip() + "\n\n"
    matches = list(re.finditer(r"(?m)^if\s+__name__\s*==\s*[\"']__main__[\"']\s*:", text))
    if not matches:
        return text + "\n" + block.strip() + "\n"
    pos = matches[-1].start()
    return text[:pos].rstrip() + "\n\n" + block.strip() + "\n\n" + text[pos:]


def main() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} tidak ditemukan. Jalankan dari root repo weather-forecast.", file=sys.stderr)
        return 2

    original = TARGET.read_text(encoding="utf-8")
    backup = TARGET.with_suffix(f".backup_v61_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py")
    shutil.copy2(TARGET, backup)

    repaired = insert_before_main_guard(original, HOTFIX)
    TARGET.write_text(repaired, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except Exception as exc:
        TARGET.write_text(original, encoding="utf-8")
        print("ERROR: hasil patch syntax error. File asli dikembalikan.", file=sys.stderr)
        print(exc, file=sys.stderr)
        print(f"Backup tetap tersedia: {backup}", file=sys.stderr)
        return 3

    print("OK: LANGIT v61 hotfix sudah dipasang.")
    print(f"Backup: {backup}")
    print("Langkah berikutnya:")
    print("  git add weather_ensemble_multi_location.py")
    print('  git commit -m "Upgrade LANGIT v61 data integrity and public UI"')
    print("  git push origin main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
