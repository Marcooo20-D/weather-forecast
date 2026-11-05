"""Collect station measurements and satellite rainfall references separately.

No model-filled value is admitted as a station observation. CMORPH is an areal
satellite/gauge precipitation estimate, never an in-situ station measurement.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

UTC = timezone.utc
WIB = ZoneInfo("Asia/Jakarta")
CMORPH_BASE = "https://ftp.cpc.ncep.noaa.gov/precip/CMORPH_RT/ICDR/0.25deg-HLY/"
STATION_BASE = "https://data.meteostat.net/"
SOURCE_TYPES = {"dwd_hourly", "dwd_poi", "eccc_hourly", "isd_lite", "metar"}
NETCDF_LOCK = threading.Lock()  # netCDF/HDF5 libraries are not thread safe.
LOCATIONS = {
    "dago": {"name": "Dago, Bandung", "latitude": -6.889, "longitude": 107.61, "station_id": "96781"},
    "jatinangor": {"name": "Jatinangor, Sumedang", "latitude": -6.938, "longitude": 107.7556, "station_id": "96781"},
    "arjawinangun": {"name": "Arjawinangun, Cirebon", "latitude": -6.6453, "longitude": 108.4103, "station_id": "96791"},
}
STATION_FIELDS = ["station_id", "source_id", "observation_type", "observed_datetime", "available_at", "latitude", "longitude", "elevation_m", "timezone", "temp_c", "temp_unit", "temp_c_source", "rh_pct", "rh_unit", "rh_pct_source", "wind_kmh", "wind_unit", "wind_kmh_source", "rain_mm", "rain_unit", "source_url", "source_sha256"]


def iso(dt):
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def timestamp(text):
    d = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return d.astimezone(UTC)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def download(url, *, timeout=18, maximum_bytes=8_000_000):
    request = Request(url, headers={"User-Agent": "LANGIT-weather-research/1.0", "Accept-Encoding": "identity"})
    with urlopen(request, timeout=timeout) as response:
        data = response.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise ValueError("Source exceeds bounded download size")
    return data


def finite(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def station_rows(payload, metadata, start, end, fetched_at, source_url):
    """Accept only explicitly identified measured fields; never infer rain periods."""
    text = gzip.decompress(payload).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    required = {"year", "month", "day", "hour", "temp_source", "rhum_source", "wspd_source"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("Station dataset lacks explicit per-variable source labels")
    selected, excluded, seen = [], {}, 0
    sha = hashlib.sha256(payload).hexdigest()
    for raw in reader:
        dt = datetime(*(int(raw[k]) for k in ("year", "month", "day", "hour")), tzinfo=UTC)
        if not start <= dt < end or dt > fetched_at:
            continue
        seen += 1
        row = {"station_id": "meteostat:" + metadata["id"], "source_id": "meteostat_measurements", "observation_type": "station_observation", "observed_datetime": iso(dt), "available_at": iso(fetched_at), "latitude": metadata["location"]["latitude"], "longitude": metadata["location"]["longitude"], "elevation_m": metadata["location"]["elevation"], "timezone": metadata["timezone"], "source_url": source_url, "source_sha256": sha, "rain_mm": "", "rain_unit": "mm"}
        values = 0
        for original, field, unit in [("temp", "temp_c", "C"), ("rhum", "rh_pct", "%"), ("wspd", "wind_kmh", "km/h")]:
            provider = raw.get(original + "_source", "")
            value = finite(raw.get(original))
            if value is not None and provider in SOURCE_TYPES:
                row[field], row[field + "_source"] = value, provider
                row[{"temp_c": "temp_unit", "rh_pct": "rh_unit", "wind_kmh": "wind_unit"}[field]] = unit
                values += 1
            elif value is not None:
                excluded[provider or "missing_source"] = excluded.get(provider or "missing_source", 0) + 1
        # Rainfall source labels alone do not establish the accumulation interval.
        # Use the existing explicit station CSV importer for measured rain amounts.
        if values:
            selected.append(row)
    return selected, {"rows_considered": seen, "measurement_rows": len(selected), "excluded_field_values_by_source": excluded, "rain_policy": "No hourly rain is inferred without explicit accumulation boundaries", "source_sha256": sha}


def station_sync(root, slug, start, end, *, timeout=18):
    root = Path(root)
    cfg = LOCATIONS[slug]
    sid = cfg["station_id"]
    now = datetime.now(UTC)
    cache = root.parent / ".cache" / "langit-observations" / "stations"
    cache.mkdir(parents=True, exist_ok=True)
    rows, reports = [], []
    metadata_url = STATION_BASE + "stations/" + sid + ".json"
    try:
        metadata = json.loads(download(metadata_url, timeout=timeout))
        for year in range(start.year, (end - timedelta(microseconds=1)).year + 1):
            url = STATION_BASE + f"hourly/{year}/{sid}.csv.gz"
            path = cache / f"{year}-{sid}.csv.gz"
            evidence_path = cache / f"{year}-{sid}.json"
            evidence = read_json(evidence_path, {})
            fresh = evidence.get("fetched_at") and (now - timestamp(evidence["fetched_at"])).total_seconds() < 21600
            if fresh and path.exists():
                data = path.read_bytes()
                fetched = timestamp(evidence["fetched_at"])
            else:
                data = download(url, timeout=timeout)
                path.write_bytes(data)
                fetched = now
                write_json(evidence_path, {"fetched_at": iso(fetched), "source_url": url})
            subset, report = station_rows(data, metadata, start, end, fetched, url)
            rows.extend(subset)
            reports.append({"source_url": url, "fetched_at": iso(fetched), **report})
        directory = root / slug / "observations"
        directory.mkdir(parents=True, exist_ok=True)
        incoming = directory / "station_incoming.csv"
        with incoming.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=STATION_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        status = {"schema_version": "LANGIT.station-source.v1", "status": "measurements_available" if rows else "no_station_measurements", "location_slug": slug, "station_id": sid, "station_metadata": metadata, "metadata_url": metadata_url, "rows": len(rows), "source_reports": reports, "checked_at": iso(now), "limitations": "Nearby station coverage is not an on-campus measurement; source-labelled model values are excluded"}
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError, OSError) as exc:
        status = {"schema_version": "LANGIT.station-source.v1", "status": "source_unavailable", "location_slug": slug, "station_id": sid, "rows": 0, "checked_at": iso(now), "error": str(exc), "source_reports": reports}
    write_json(root / slug / "station_source_status.json", status)
    return status


def cf_interval(start_seconds, end_seconds, duration):
    """NOAA CMORPH CF bounds use inclusive last second (00:59:59)."""
    start = datetime.fromtimestamp(float(start_seconds), UTC)
    raw_end = datetime.fromtimestamp(float(end_seconds), UTC)
    seconds = (raw_end - start).total_seconds()
    if duration != "PT1H" or seconds not in (3599, 3600):
        raise ValueError("CMORPH interval is not a documented one-hour period")
    return start, raw_end + (timedelta(seconds=1) if seconds == 3599 else timedelta())


def extract_cmorph(data, source_url, locations, fetched_at=None):
    """Read the native CF grid, nearest cell, scale/mask, and exact time bounds."""
    import netCDF4
    fetched_at = fetched_at or datetime.now(UTC)
    with NETCDF_LOCK:
        with netCDF4.Dataset("cmorph-memory", memory=data) as dataset:
            bounds = dataset.variables["time_bounds"][:].ravel()
            start, end = cf_interval(bounds[0], bounds[1], dataset.time_coverage_duration)
            time = dataset.variables["time"]
            if time.units != "seconds since 1970-01-01 00:00:00 0:00" or float(time[0]) != float(bounds[0]):
                raise ValueError("Unexpected CMORPH time units or reference timestamp")
            if end > fetched_at:
                raise ValueError("Reference period lies in the future")
            variable = dataset.variables["cmorph"]
            if variable.units != "mm/hr" or variable.dimensions != ("time", "lat", "lon"):
                raise ValueError("Unexpected CMORPH rainfall unit or grid dimensions")
            latitudes = dataset.variables["lat"][:]
            longitudes = dataset.variables["lon"][:]
            points = {}
            for slug, location in locations.items():
                lat, lon = location["latitude"], location["longitude"] % 360
                if not -60 <= lat <= 60:
                    raise ValueError("Location outside CMORPH domain")
                iy = min(range(len(latitudes)), key=lambda k: abs(float(latitudes[k]) - lat))
                ix = min(range(len(longitudes)), key=lambda k: abs(float(longitudes[k]) - lon))
                value = variable[0, iy, ix]  # netCDF4 applies scale_factor and missing mask once.
                amount = None if getattr(value, "mask", False) else finite(value)
                if amount is not None and not 0 <= amount <= 327.67:
                    amount = None
                points[slug] = {"latitude": lat, "longitude": location["longitude"], "cell_latitude": float(latitudes[iy]), "cell_longitude": float(longitudes[ix]), "cell_latitude_bounds": dataset.variables["lat_bounds"][iy].tolist(), "cell_longitude_bounds": dataset.variables["lon_bounds"][ix].tolist(), "rain_mm": round(amount, 2) if amount is not None else None, "qc": "usable_reference" if amount is not None else "missing_reference", "sampling": "nearest_native_cell_no_interpolation"}
            return {"schema_version": "LANGIT.satellite-reference.v1", "reference_type": "satellite_gauge_precipitation_estimate", "provider": "NOAA CPC", "product": "CMORPH ICDR ADJ", "product_version": dataset.product_version, "source_url": source_url, "source_sha256": hashlib.sha256(data).hexdigest(), "source_created_date": dataset.date_created, "fetched_at": iso(fetched_at), "interval_start": iso(start), "interval_end": iso(end), "native_inclusive_bounds_seconds": [float(bounds[0]), float(bounds[1])], "interval_minutes": 60, "native_unit": "mm/hr", "output_unit": "mm", "grid_resolution_degrees": 0.25, "native_packing_scale_factor": float(variable.scale_factor), "points": points, "limitations": "Areal satellite/gauge estimate; not a local gauge, not radar, not an independent in-situ validation, and not used to train station calibration"}


def reference_path(root, start):
    return Path(root) / "observations" / "cmorph" / (start.strftime("%Y%m%dT%H") + ".json")


def reference_sync(root, start, end, *, workers=3, timeout=18):
    """Archive compact native-cell subsets; never commit global NetCDF files."""
    root = Path(root)
    now = datetime.now(UTC)
    start = start.replace(minute=0, second=0, microsecond=0)
    if end <= start or (end - start).total_seconds() > 93 * 86400:
        raise ValueError("Reference request must cover 1 hour to 93 days")
    times = []
    while start < end and start + timedelta(hours=1) <= now:
        times.append(start)
        start += timedelta(hours=1)

    def one(dt):
        path = reference_path(root, dt)
        if path.exists():
            obj = read_json(path)
            if obj.get("interval_start") != iso(dt) or obj.get("schema_version") != "LANGIT.satellite-reference.v1":
                raise ValueError("Cached reference identity is inconsistent")
            if all(slug in obj.get("points", {}) and obj["points"][slug]["latitude"] == cfg["latitude"] and obj["points"][slug]["longitude"] == cfg["longitude"] for slug, cfg in LOCATIONS.items()):
                return {"hour": iso(dt), "status": "cached", "path": str(path), "fetched_at": obj["fetched_at"]}
        url = CMORPH_BASE + "CMORPH_V0.x_ADJ_0.25deg-HLY_" + dt.strftime("%Y%m%d%H") + ".nc"
        failure_path = root.parent / ".cache" / "langit-observations" / "missing" / (dt.strftime("%Y%m%dT%H") + ".json")
        previous_failure = read_json(failure_path, {})
        if previous_failure.get("checked_at") and (now - timestamp(previous_failure["checked_at"])).total_seconds() < 1800:
            return {"hour": iso(dt), "status": "unavailable", "error": previous_failure["error"], "source_url": url}
        try:
            data = download(url, timeout=timeout)
            obj = extract_cmorph(data, url, LOCATIONS)
            if timestamp(obj["interval_start"]) != dt:
                raise ValueError("Source interval does not match requested hour")
            write_json(path, obj)
            return {"hour": iso(dt), "status": "downloaded", "path": str(path), "fetched_at": obj["fetched_at"]}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            write_json(failure_path, {"checked_at": iso(now), "source_url": url, "error": str(exc)})
            return {"hour": iso(dt), "status": "unavailable", "error": str(exc), "source_url": url}
    with ThreadPoolExecutor(max_workers=max(1, min(4, workers))) as executor:
        results = list(executor.map(one, times))
    summary = {"schema_version": "LANGIT.reference-sync.v1", "generated_at": iso(now), "requested_hours": len(times), "available_hours": sum(x["status"] in {"cached", "downloaded"} for x in results), "missing_hours": sum(x["status"] == "unavailable" for x in results), "reference_type": "satellite_gauge_precipitation_estimate", "records": results}
    write_json(root / "observations" / "cmorph_sync_summary.json", summary)
    return summary


def sync_location(args, output_dir):
    """Adapter for the existing engine's --mode sync-observations entry point."""
    directory = Path(output_dir)
    slug = args.location_slug
    if slug not in LOCATIONS:
        raise ValueError("Automatic station mapping is not configured for " + slug)
    root = directory.parent
    now = datetime.now(UTC)
    end = timestamp(args.end_date + "T00:00:00Z") + timedelta(days=1) if args.end_date else now.replace(minute=0, second=0, microsecond=0)
    start = timestamp(args.start_date + "T00:00:00Z") if args.start_date else end - timedelta(days=min(getattr(args, "lookback_days", 14), 14))
    station = station_sync(root, slug, start, end, timeout=getattr(args, "http_timeout", 18))
    reference = reference_sync(root, start, end, timeout=getattr(args, "http_timeout", 18))
    return {"station": station, "reference": reference}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("outputs"))
    parser.add_argument("--locations", default="all")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=18)
    options = parser.parse_args()
    now = datetime.now(UTC)
    end = timestamp(options.end_date + "T00:00:00Z") + timedelta(days=1) if options.end_date else now.replace(minute=0, second=0, microsecond=0)
    start = timestamp(options.start_date + "T00:00:00Z") if options.start_date else end - timedelta(days=options.lookback_days)
    slugs = list(LOCATIONS) if options.locations == "all" else options.locations.split(",")
    if any(slug not in LOCATIONS for slug in slugs):
        parser.error("Unknown location")
    for slug in slugs:
        status = station_sync(options.root, slug, start, end, timeout=options.timeout)
        print(slug + ": " + status["status"], flush=True)
    summary = reference_sync(options.root, start, end, workers=options.workers, timeout=options.timeout)
    print(json.dumps({k: summary[k] for k in ("requested_hours", "available_hours", "missing_hours")}))
    return 0 if summary["available_hours"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
