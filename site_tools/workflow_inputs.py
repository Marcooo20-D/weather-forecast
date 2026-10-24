"""Validate workflow input outside shell interpolation."""
import os
from pathlib import Path

ALLOWED = {"dago", "jatinangor", "arjawinangun"}


def resolve(locations: str, debug: str) -> tuple[str, str]:
    locations = (locations or "all").strip().lower()
    slugs = locations.split(",")
    if locations != "all" and (not slugs or any(slug.strip() not in ALLOWED for slug in slugs)):
        raise ValueError("Locations must be all or a comma-separated list of known locations")
    if debug not in {"", "true", "false"}:
        raise ValueError("Debug must be true or false")
    locations = locations if locations == "all" else ",".join(dict.fromkeys(s.strip() for s in slugs))
    return locations, "--debug" if debug == "true" else "--no-debug"


if __name__ == "__main__":
    locations, flag = resolve(os.getenv("LOCATIONS_INPUT", ""), os.getenv("DEBUG_INPUT", ""))
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as stream:
        stream.write(f"LOCATIONS={locations}\nDEBUG_FLAG={flag}\n")
