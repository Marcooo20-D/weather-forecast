"""Build a bounded public site without publishing the forecast training archive.

Run: python -m site_tools.public_site --source outputs --destination _site
All source data stays in outputs/. Only the deployment copy is filtered.
"""
from __future__ import annotations

import argparse
import json
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


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.urls.append(value)


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


def validate_site(root: Path, *, max_bytes: int = 50 * 1024 * 1024) -> dict:
    root = root.resolve()
    errors = []
    for name in REQUIRED_ROOT:
        if not (root / name).is_file():
            errors.append("Missing " + name)
    if errors:
        raise ValueError("\n".join(errors))
    for slug in location_slugs(root):
        for name in REQUIRED_LOCATION:
            if not (root / slug / name).is_file():
                errors.append(f"Missing {slug}/{name}")
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
    assets = source / "assets"
    if assets.is_dir():
        selected.extend(p for p in assets.rglob("*") if p.is_file())
    for slug in location_slugs(source):
        for path in (source / slug).iterdir():
            if path.is_file() and (path.suffix == ".html" or path.name in LOCATION_DATA):
                selected.append(path)
    try:
        destination.mkdir(parents=True)
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
        if destination.exists():
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
