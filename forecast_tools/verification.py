"""Verify immutable pre-event rainfall forecasts against CMORPH reference cells.

This report measures agreement with an areal satellite/gauge estimate. It does
not promote satellite estimates into station observations or local calibration.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path

from .observations import LOCATIONS, finite, iso, read_json, timestamp, write_json

THRESHOLD_MM = 0.1
METHOD = "family_balanced_real_member_empirical_distribution"
PAIR_FIELDS = ["location_slug", "valid_time", "interval_start", "interval_end", "issued_at", "lead_hours", "lead_bin", "rain_probability_pct", "rain_ensemble_mean_mm", "rain_median_mm", "reference_rain_mm", "reference_event", "brier", "crps_mm", "error_mm", "absolute_error_mm", "cell_latitude", "cell_longitude", "reference_type", "reference_url", "reference_sha256", "reference_fetched_at", "archive_path", "archive_sha256", "archive_revision", "archive_git_blob_sha", "calibration_status"]


def weighted_crps(members, observed):
    pairs = sorted((float(m["value"]), float(m["weight"])) for m in members)
    total = sum(w for x, w in pairs)
    if total <= 0 or any(w <= 0 or not math.isfinite(x + w) for x, w in pairs):
        raise ValueError("Invalid distribution weights or values")
    prefix_weight = prefix_value = spread = absolute = 0.0
    for value, raw_weight in pairs:
        weight = raw_weight / total
        absolute += weight * abs(value - observed)
        spread += weight * (value * prefix_weight - prefix_value)
        prefix_weight += weight
        prefix_value += weight * value
    return max(0.0, absolute - spread)


def distribution(row):
    group = (row.get("ensemble_distribution") or {}).get("rain_mm") or {}
    members = group.get("members") or []
    clean = []
    identities = set()
    groups = defaultdict(int)
    for m in members:
        value, weight = finite(m.get("value")), finite(m.get("weight"))
        identity = (m.get("family"), m.get("model"), m.get("member"))
        if value is None or value < 0 or weight is None or weight <= 0 or not all(identity) or identity in identities:
            raise ValueError("Invalid or duplicate native ensemble member")
        identities.add(identity)
        groups[identity[:2]] += 1
        clean.append(m)
    if len({family for family, model in groups}) < 2 or any(n < 5 for n in groups.values()):
        raise ValueError("Insufficient native model families or members")
    return group, clean


def make_pair(doc, row, reference, slug, *, evaluated_at):
    valid = timestamp(row.get("target_datetime") or row.get("valid_time"))
    issued = timestamp(doc.get("issued_at"))
    if valid > evaluated_at or issued > valid - timedelta(hours=1):
        raise ValueError("not_pre_event")
    if row.get("probability_basis") != METHOD or row.get("ensemble_status") not in {"usable", "ok", "available"}:
        raise ValueError("not_usable_native_ensemble")
    start, end = timestamp(row.get("rain_interval_start")), timestamp(row.get("rain_interval_end"))
    ref_start, ref_end = timestamp(reference["interval_start"]), timestamp(reference["interval_end"])
    if start != ref_start or end != ref_end or end != valid or (end - start).total_seconds() != 3600:
        raise ValueError("rain_interval_mismatch")
    point = reference["points"][slug]
    cfg = LOCATIONS[slug]
    if point["latitude"] != cfg["latitude"] or point["longitude"] != cfg["longitude"]:
        raise ValueError("location_mismatch")
    if reference.get("reference_type") != "satellite_gauge_precipitation_estimate" or point.get("qc") != "usable_reference":
        raise ValueError("unusable_reference")
    observed = finite(point.get("rain_mm"))
    if observed is None or observed < 0 or timestamp(reference["fetched_at"]) > evaluated_at:
        raise ValueError("unavailable_reference")
    group, members = distribution(row)
    if group.get("unit") != "mm" or group.get("interval_minutes") != 60 or timestamp(group.get("interval_start")) != start or timestamp(group.get("interval_end")) != end:
        raise ValueError("distribution_interval_or_unit_mismatch")
    if any(m.get("fetched_at") and timestamp(m["fetched_at"]) > valid for m in members):
        raise ValueError("member_fetched_after_valid_time")
    total = sum(m["weight"] for m in members)
    probability = finite(row.get("prob_rain_raw"))
    native_probability = sum(m["weight"] for m in members if m["value"] > THRESHOLD_MM) / total
    if probability is None or not 0 <= probability <= 100 or abs(probability / 100 - native_probability) > 1e-6:
        raise ValueError("probability_member_mismatch")
    mean = sum(m["value"] * m["weight"] for m in members) / total
    ordered = sorted(members, key=lambda m: m["value"])
    cumulative, median = 0.0, ordered[-1]["value"]
    for member in ordered:
        cumulative += member["weight"] / total
        if cumulative >= 0.5 - 1e-12:
            median = member["value"]
            break
    lead = (valid - issued).total_seconds() / 3600
    lead_bin = "1-24h" if lead <= 24 else "24-48h" if lead <= 48 else "48h+"
    event = int(observed > THRESHOLD_MM)
    return {"location_slug": slug, "valid_time": iso(valid), "interval_start": iso(start), "interval_end": iso(end), "issued_at": iso(issued), "lead_hours": lead, "lead_bin": lead_bin, "rain_probability_pct": probability, "rain_ensemble_mean_mm": mean, "rain_median_mm": median, "reference_rain_mm": observed, "reference_event": event, "brier": (probability / 100 - event) ** 2, "crps_mm": weighted_crps(members, observed), "error_mm": mean - observed, "absolute_error_mm": abs(mean - observed), "cell_latitude": point["cell_latitude"], "cell_longitude": point["cell_longitude"], "reference_type": reference["reference_type"], "reference_url": reference["source_url"], "reference_sha256": reference["source_sha256"], "reference_fetched_at": reference["fetched_at"], "archive_path": doc.get("archive_source_path", ""), "archive_sha256": doc.get("archive_sha256", ""), "archive_revision": doc.get("archive_source_commit", ""), "archive_git_blob_sha": doc.get("archive_git_blob_sha", ""), "calibration_status": row.get("calibration_status", "UNKNOWN")}


def metrics(pairs):
    n = len(pairs)
    if not n:
        return {"cases": 0, "mae_mm": None, "rmse_mm": None, "bias_mm": None, "brier_score": None, "crps_mm": None, "observed_event_frequency": None}
    return {"cases": n, "unique_valid_hours": len({p["valid_time"] for p in pairs}), "unique_dates_utc": len({p["valid_time"][:10] for p in pairs}), "mae_mm": sum(p["absolute_error_mm"] for p in pairs) / n, "rmse_mm": math.sqrt(sum(p["error_mm"] ** 2 for p in pairs) / n), "bias_mm": sum(p["error_mm"] for p in pairs) / n, "brier_score": sum(p["brier"] for p in pairs) / n, "crps_mm": sum(p["crps_mm"] for p in pairs) / n, "observed_event_frequency": sum(p["reference_event"] for p in pairs) / n, "period_start": min(p["interval_start"] for p in pairs), "period_end": max(p["interval_end"] for p in pairs)}


def reliability(pairs):
    bins = []
    for i in range(10):
        subset = [p for p in pairs if min(9, int(p["rain_probability_pct"] // 10)) == i]
        n = len(subset)
        bins.append({"lower_percent": i * 10, "upper_percent": (i + 1) * 10, "cases": n, "mean_probability_pct": sum(p["rain_probability_pct"] for p in subset) / n if n else None, "reference_frequency_pct": 100 * sum(p["reference_event"] for p in subset) / n if n else None})
    return bins


def archive_documents(directory):
    entries = {}
    for path in Path(directory).glob("*"):
        if path.is_file() and path.name.endswith((".json", ".json.gz")):
            identity = path.name.removesuffix(".gz")
            if identity not in entries or path.name.endswith(".json"):
                entries[identity] = path
    for identity in sorted(entries):
        path = entries[identity]
        raw = path.read_bytes()
        payload = gzip.decompress(raw) if path.suffix == ".gz" else raw
        doc = json.loads(payload)
        doc.setdefault("archive_source_path", str(path))
        doc.setdefault("archive_sha256", hashlib.sha256(raw).hexdigest())
        yield doc


def evaluate_location(root, slug, *, archive_root=None, evaluated_at=None):
    root = Path(root)
    evaluated_at = evaluated_at or datetime.now(timezone.utc)
    references = {}
    for path in sorted((root / "observations" / "cmorph").glob("*.json")):
        obj = read_json(path)
        if obj.get("schema_version") != "LANGIT.satellite-reference.v1":
            raise ValueError("Unexpected reference schema: " + str(path))
        key = obj["interval_end"]
        if key in references and references[key]["source_sha256"] != obj["source_sha256"]:
            raise ValueError("Conflicting reference revisions require explicit selection")
        references[key] = obj
    directory = (Path(archive_root) / slug / "forecast_archive") if archive_root else root / slug / "forecast_archive"
    selected, rejected = {}, Counter()
    documents, source_cases = 0, 0
    for doc in archive_documents(directory):
        documents += 1
        if doc.get("location_slug") != slug:
            raise ValueError("Archive location does not match directory")
        for row in doc.get("hourly", []):
            source_cases += 1
            try:
                valid = timestamp(row.get("target_datetime") or row.get("valid_time"))
                reference = references.get(iso(valid))
                if reference is None:
                    rejected["missing_reference_hour"] += 1
                    continue
                pair = make_pair(doc, row, reference, slug, evaluated_at=evaluated_at)
                key = (pair["valid_time"], pair["lead_bin"])
                # Oldest eligible issue per valid hour/lead bin avoids revision cherry-picking.
                if key not in selected or timestamp(pair["issued_at"]) < timestamp(selected[key]["issued_at"]):
                    selected[key] = pair
            except (ValueError, TypeError, KeyError) as exc:
                rejected[str(exc)] += 1
    pairs = sorted(selected.values(), key=lambda p: (p["valid_time"], p["lead_hours"]))
    status = read_json(root / slug / "station_source_status.json", {})
    summary = {"schema_version": "LANGIT.satellite-verification.v1", "generated_at": iso(evaluated_at), "location_slug": slug, "reference_type": "satellite_gauge_precipitation_estimate", "reference_product": "NOAA CPC CMORPH ICDR ADJ", "grid_resolution_degrees": 0.25, "rain_interval_minutes": 60, "event_threshold_mm": THRESHOLD_MM, "forecast_method": METHOD, "forecast_selection": "Earliest eligible issue in each valid-hour/lead bin; issue precedes valid time by at least one hour", "scope": "raw native ensemble rainfall; statistics measure agreement with a grid-cell satellite/gauge estimate", "station_source_status": status.get("status", "not_checked"), "automatic_station_measurement_rows": status.get("rows", 0), "station_calibration_changed": False, "local_station_skill_status": "NOT_ESTABLISHED_BY_THIS_REPORT", "archive_documents": documents, "candidate_rows": source_cases, "rejected_rows_by_reason": dict(rejected), "all_leads": metrics(pairs), "by_lead": {lead: metrics([p for p in pairs if p["lead_bin"] == lead]) for lead in ("1-24h", "24-48h", "48h+")}, "reliability": reliability(pairs), "limitations": ["Reference is an areal satellite/gauge estimate, not an on-campus gauge or radar", "A 0.25-degree grid cannot establish campus-scale accuracy", "Multiple lead bins share reference hours; cases are not independent observations", "No weights, calibration, or hyperparameters are fitted using these results", "Station temperature, humidity, and wind need actual measured data; model-filled station series remain excluded"], "sources": ["https://ftp.cpc.ncep.noaa.gov/precip/CMORPH_RT/ICDR/0.25deg-HLY/", "https://www.ncei.noaa.gov/products/climate-data-records/precipitation-cmorph", "https://dev.meteostat.net/quality.html"]}
    if pairs:
        summary["reference_cell"] = {k: pairs[0][k] for k in ("cell_latitude", "cell_longitude")}
    destination = root / slug
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / "satellite_verification.json", summary)
    with (destination / "satellite_verification_pairs.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PAIR_FIELDS)
        writer.writeheader()
        writer.writerows(pairs)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("outputs"))
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--locations", default="all")
    args = parser.parse_args()
    slugs = list(LOCATIONS) if args.locations == "all" else args.locations.split(",")
    if any(slug not in LOCATIONS for slug in slugs):
        parser.error("Unknown location")
    results = []
    for slug in slugs:
        summary = evaluate_location(args.root, slug, archive_root=args.archive_root)
        results.append({"location": slug, **summary["all_leads"]})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
