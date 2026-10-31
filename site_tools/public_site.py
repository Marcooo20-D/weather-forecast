"""Build a bounded public site without publishing the forecast training archive.

Run: python -m site_tools.public_site --source outputs --destination _site
All source data stays in outputs/. Only the deployment copy is filtered.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT_DATA = {
    "langit_all_locations.geojson", "langit_portal_manifest.json",
    "dim_locations.csv", "dim_sources.csv", "ensemble_all_locations.csv",
    "ensemble_fact_all_locations.csv", "ensemble_long_all_locations.csv",
    "forecast_all_locations.csv", "source_status_all_locations.csv",
}
LOCATION_DATA = {
    "langit_api_v1.json", "langit_location.geojson", "langit_map_layers.json",
    "langit_whatsapp_brief.txt", "langit_hourly_intelligence.csv",
    "langit_daily_outlook.csv", "langit_activity_matrix.csv",
    "forecast.csv", "ensemble.csv", "bmkg.csv", "source_status.csv",
    "sentinel_x_verification_summary.json", "sentinel_x_reliability.csv",
    "probabilistic_verification.json", "activity_verification.json",
}
REQUIRED_ROOT = {"index.html", "langit_portal_map.html", "langit_portal_manifest.json"}
REQUIRED_LOCATION = {
    "langit_app.html", "langit_3day.html", "langit_activity.html",
    "langit_map_room.html", "langit_map.html", "langit_planner.html",
    "keandalan_data.html", "akurasi_data.html", "langit_api_v1.json",
    "langit_location.geojson",
}
PUBLIC_LOCATION_PAGES = {name for name in REQUIRED_LOCATION if name.endswith(".html")} | {
    "anemos_3day.html", "anemos_activity.html", "anemos_commute_advice.html",
    "anemos_laundry_advice.html", "anemos_app.html", "anemos_today.html",
    "anemos_public_landing.html", "anemos_map.html", "langit_public_landing.html",
    "command_center_sentinel_x.html", "sentinel_x_report.html", "langit_planer.html",
    "langit_model_court.html", "sentinel_x_accuracy_public.html",
    "offline.html",
}


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.urls.append(value)
            elif name == "srcset" and value:
                # Generated picture sources use plain local paths plus widths.
                # Include every variant in both staging and link validation.
                self.urls.extend(candidate.strip().split()[0]
                                 for candidate in value.split(",") if candidate.strip())


def reject_json_constant(value: str):
    raise ValueError("Non-finite JSON constant: " + value)


def location_slugs(root: Path) -> list[str]:
    doc = json.loads((root / "langit_portal_manifest.json").read_text(encoding="utf-8"))
    slugs = [item["slug"] for item in doc["locations"]]
    if not slugs or len(slugs) != len(set(slugs)):
        raise ValueError("Public manifest needs unique locations")
    if any(not slug or slug in {".", ".."} or Path(slug).name != slug or "\\" in slug for slug in slugs):
        raise ValueError("Invalid location slug in public manifest")
    return slugs


def validate_location_contract(root: Path, slug: str, version) -> None:
    """Catch semantic drift as well as syntactically valid but false data."""
    directory = root / slug
    api = json.loads((directory / "langit_api_v1.json").read_text())
    if api.get("location_slug") != slug or api.get("version") != version:
        raise ValueError(f"{slug}: API identity/version differs from manifest")
    days = api.get("days")
    if not isinstance(days, list) or len(days) != 3:
        raise ValueError(f"{slug}: expected three explicit forecast days")
    ranges = {"temp_c": (-80, 60), "humidity_pct": (0, 100),
              "rain_probability": (0, 100), "wind_kmh": (0, 500),
              "cloud_pct": (0, 100), "wind_dir": (0, 360)}
    expected = {}
    dates = []
    for day in days:
        date = dt.date.fromisoformat(day["date_iso"])
        dates.append(date)
        for hour in day["hours"]:
            key = (day["date_iso"], hour["hour"])
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", hour["hour"]):
                raise ValueError(f"{slug}: invalid valid hour")
            if key in expected or hour.get("date_iso") != day["date_iso"]:
                raise ValueError(f"{slug}: duplicate/misaligned forecast hour {key}")
            for field, (lower, upper) in ranges.items():
                value = hour.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value) or not lower <= value <= upper):
                    raise ValueError(f"{slug}: invalid {field} at {key}")
            if hour.get("valid") and any(hour.get(field) is None for field in ("temp_c", "rain_probability", "wind_kmh")):
                raise ValueError(f"{slug}: missing core data advertised as valid at {key}")
            expected[key] = hour
    if any(b - a != dt.timedelta(days=1) for a, b in zip(dates, dates[1:])):
        raise ValueError(f"{slug}: forecast days are not consecutive")
    geo = json.loads((directory / "langit_location.geojson").read_text())
    actual = {}
    for feature in geo["features"]:
        coordinates = feature["geometry"]["coordinates"]
        if feature["geometry"]["type"] != "Point" or len(coordinates) != 2 or not (
                -180 <= coordinates[0] <= 180 and -90 <= coordinates[1] <= 90):
            raise ValueError(f"{slug}: invalid GeoJSON coordinates")
        props = feature["properties"]
        key = (props["date_iso"], props["hour"])
        if key in actual:
            raise ValueError(f"{slug}: duplicate GeoJSON forecast")
        actual[key] = props
    # A location with unknown coordinates legitimately has an empty map.
    if api.get("latitude") is not None and api.get("longitude") is not None:
        if actual.keys() != expected.keys():
            raise ValueError(f"{slug}: GeoJSON/API time coverage differs")
        for key, hour in expected.items():
            for field in ranges:
                if actual[key].get(field) != hour.get(field):
                    raise ValueError(f"{slug}: GeoJSON/API {field} differs at {key}")
    table = directory / "langit_hourly_intelligence.csv"
    if table.exists():
        with table.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != len(expected):
            raise ValueError(f"{slug}: CSV/API time coverage differs")
        seen = set()
        for row in rows:
            key = (row["date"], row["hour"])
            if key not in expected or key in seen:
                raise ValueError(f"{slug}: duplicate/unknown CSV forecast hour")
            seen.add(key)
            for field in ("temp_c", "humidity_pct", "heat_index_c", "rain_probability", "wind_kmh", "risk_score"):
                value = row.get(field)
                number = None if value in (None, "") else float(value)
                if number != expected[key].get(field):
                    raise ValueError(f"{slug}: CSV/API {field} differs at {key}")


def validate_site(root: Path, *, max_bytes: int = 50 * 1024 * 1024) -> dict:
    root = root.resolve()
    errors = []
    for name in REQUIRED_ROOT:
        if not (root / name).is_file() or (root / name).stat().st_size == 0:
            errors.append("Missing " + name)
    if errors:
        raise ValueError("\n".join(errors))
    manifest = json.loads((root / "langit_portal_manifest.json").read_text())
    slugs = location_slugs(root)
    for slug in slugs:
        for name in REQUIRED_LOCATION:
            if not (root / slug / name).is_file() or (root / slug / name).stat().st_size == 0:
                errors.append(f"Missing {slug}/{name}")
        try:
            validate_location_contract(root, slug, manifest.get("version"))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append(f"Invalid public data contract: {exc}")
    files = [p for p in root.rglob("*") if p.is_file()]
    for path in files:
        if path.is_symlink():
            errors.append("Symlink in public output: " + str(path))
            continue
        if path.suffix in {".json", ".geojson"}:
            try:
                json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
            except (ValueError, OSError) as exc:
                errors.append(f"Invalid JSON {path.relative_to(root)}: {exc}")
        if path.suffix != ".html":
            continue
        parser = Links()
        parser.feed(path.read_text(encoding="utf-8"))
        for url in parser.urls:
            parts = urlsplit(url)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            target = (path.parent / unquote(parts.path)).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                errors.append(f"Broken local URL {path.relative_to(root)} -> {url}")
    total = sum(p.stat().st_size for p in files)
    if total > max_bytes:
        errors.append(f"Public site is {total:,} bytes; budget is {max_bytes:,}")
    if len(files) > 1000:
        errors.append(f"Public site has {len(files)} files; budget is 1000")
    if errors:
        raise ValueError("\n".join(errors[:60]))
    return {"files": len(files), "bytes": total}


def build_site(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Source and destination must be separate directories")
    if destination.exists():
        raise ValueError("Destination already exists; use a new staging directory")
    selected = []
    for path in source.iterdir():
        if path.is_file() and (path.name in ROOT_DATA or path.name in REQUIRED_ROOT):
            selected.append(path)
    for slug in location_slugs(source):
        for path in (source / slug).iterdir():
            if path.is_file() and (path.name in PUBLIC_LOCATION_PAGES or path.name in LOCATION_DATA):
                selected.append(path)
    # Publish only assets referenced by this generation. Old hashed assets stay
    # in the source tree for reproducibility but no longer inflate each release.
    asset_paths = set()
    for path in selected:
        if path.suffix != ".html":
            continue
        links = Links()
        links.feed(path.read_text(encoding="utf-8"))
        for url in links.urls:
            parts = urlsplit(url)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            target = path.parent / unquote(parts.path)
            if target.resolve().is_relative_to((source / "assets").resolve()):
                asset_paths.add(target)
    selected.extend(sorted(asset_paths))
    created = False
    try:
        destination.mkdir(parents=True)
        created = True
        for path in selected:
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                raise ValueError("Refusing to publish a symlink: " + str(path))
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (destination / ".nojekyll").touch()
        stats = validate_site(destination)
    except Exception:
        # Only remove the new deployment copy. Never touch source data.
        if created and destination.exists():
            shutil.rmtree(destination)
        raise
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("outputs"))
    parser.add_argument("--destination", type=Path, default=Path("_site"))
    args = parser.parse_args(argv)
    stats = build_site(args.source, args.destination)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

