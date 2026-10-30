"""Regressions reproduced by the 30 October audit."""
import datetime as dt
import contextlib
import csv
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import langit_v65_cinematic_rebuild as ui
import weather_ensemble_multi_location as engine
from site_tools.public_site import build_site, validate_site


class WeatherContracts(unittest.TestCase):
    def test_invalid_probability_is_missing_not_clipped(self):
        for value in (-1, 101, float("inf"), float("nan"), True):
            with self.subTest(value=value):
                self.assertIsNone(ui.prob(value))
        self.assertEqual(ui.prob(0), 0)
        self.assertEqual(ui.prob(100), 100)

    def test_utc_valid_time_rolls_into_next_wib_day(self):
        for key in ("target_datetime", "datetime", "timestamp", "valid_time", "time"):
            row = ui.row_to_hour({key: "2026-10-30T23:00:00Z",
                                  "temp_c": 25, "wind_kmh": 0, "rain_probability": 0})
            self.assertEqual(row["hour"], "06:00")
            self.assertEqual(row["date_iso"], "2026-10-31")
            self.assertTrue(row["valid"])
        self.assertEqual(ui.hour("2026-10-30T23:00:00Z"), "06:00")

    def test_missing_or_invalid_hour_never_becomes_valid_midnight(self):
        for hour in (None, "", "25:00", "not-a-time"):
            with self.subTest(hour=hour):
                row = ui.row_to_hour({"hour": hour, "date": "2026-10-30",
                                      "temp_c": 25, "wind_kmh": 0, "rain_probability": 0})
                self.assertFalse(row["valid"])
                self.assertNotEqual(row["hour"], "00:00")

    def test_physical_ranges_are_flagged_without_fabrication(self):
        row = ui.row_to_hour({"hour": "09:00", "date": "2026-10-30",
                              "temp_c": 999, "wind_kmh": -3, "rain_probability": 120,
                              "humidity_pct": 130, "cloud_pct": -4})
        for field in ("temp_c", "wind_kmh", "rain_probability", "humidity_pct", "cloud_pct"):
            self.assertIsNone(row[field])
        self.assertFalse(row["valid"])
        self.assertTrue(row["validation_flags"])

    def test_calm_wind_has_no_direction(self):
        with patch.object(engine, "point_weight", return_value=1):
            self.assertIsNone(engine._weighted_wind_direction([
                SimpleNamespace(wind_direction_deg=120, wind_kmh=0)]))
        row = ui.row_to_hour({"hour": "09:00", "wind_kmh": 0, "wind_dir": 120})
        self.assertIsNone(row["wind_dir"])

    def test_probability_does_not_imply_rain_intensity(self):
        condition = ui.condition_label("15:00", 99, 25, 80, 25, True)
        self.assertNotIn("kuat", condition.lower())
        self.assertNotIn("lebat", condition.lower())
        self.assertIn("peluang", condition.lower())

    def test_sub_one_percent_is_not_displayed_as_zero(self):
        self.assertEqual(ui.pct(0.01), "<1%")
        self.assertEqual(ui.pct(0), "0%")

    def test_date_without_time_is_not_a_valid_midnight(self):
        self.assertEqual(ui.hour("2026-10-30", "missing"), "missing")

    def test_unavailable_day_has_no_invented_risk_index(self):
        day = ui.summarize_day("Test", dt.date(2026, 10, 30), [])
        self.assertIsNone(day["risk_score"])
        self.assertEqual(ui.score_text(day["risk_score"]), "—")

    def test_today_activity_windows_exclude_elapsed_hours(self):
        now = dt.datetime.fromisoformat("2026-10-30T16:00:00+07:00")
        rows = [{"date_iso": "2026-10-30", "hour": hour, "valid": True, "risk_class": "safe"}
                for hour in ("10:00", "21:00")]
        with patch.object(ui, "local_now", return_value=now):
            self.assertEqual(ui.best_windows(rows), ["21:00"])

    def test_ensemble_duplicate_times_reject_all_variables(self):
        args = engine.build_arg_parser().parse_args([])
        payload = {"hourly": {"time": ["2026-10-30T09:00", "2026-10-30T09:00"]}}
        with self.assertRaisesRegex(ValueError, "unique"):
            engine.v61_parse_ensemble_payload(payload, "ncep_gefs_seamless", dt.date(2026, 10, 30), args)

    def test_missing_cloud_not_derived_from_weather_category(self):
        date = dt.date(2026, 10, 30)
        args = engine.clone_args_for_location(engine.build_arg_parser().parse_args([]), engine.LOCATION_PRESETS["dago"])
        valid = engine.parse_local_hour_string(date, "09:00", args.timezone)
        point = engine.ForecastPoint("ECMWF", "test", "09:00", valid, 25, 80, 0, 0,
                                     "Berawan", "test", 0)
        with patch.object(engine, "_V61_ENSEMBLES", {}), \
                patch.object(engine, "aether_analog_probability", return_value=(None, 0)), \
                patch.object(engine, "point_weight", return_value=1), \
                patch.object(engine, "source_health_factor", return_value=1):
            rows = engine.aether_build_rows([point], [], date, args)
        self.assertIsNone(engine.safe_float(next(row for row in rows if row["jam"] == "09:00")["cloud_p50"]))

    def test_partial_combined_export_preserves_other_locations(self):
        fields = ["location_slug", "target_date", "value"]
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, "root_output_dir", return_value=tmp):
            old = [{"location_slug": slug, "target_date": "2026-10-30", "value": 1}
                   for slug in ("dago", "jatinangor", "arjawinangun")]
            engine.write_combined_csv("forecast_all_locations", fields, old)
            engine.write_combined_csv("forecast_all_locations", fields, [
                {"location_slug": "dago", "target_date": "2026-10-30", "value": 0}])
            result = engine.read_dict_csv(str(Path(tmp) / "forecast_all_locations.csv"))
            self.assertEqual({r["location_slug"] for r in result}, {"dago", "jatinangor", "arjawinangun"})
            self.assertEqual(next(r for r in result if r["location_slug"] == "dago")["value"], "0")

    def test_rain_probability_boundaries_and_extremes(self):
        def members(values):
            return [{"model": "fixture", "family": "fixture", "member": str(i), "value": value}
                    for i, value in enumerate(values)]
        for values, expected in [([0, 0], 0), ([1, 20, 1000], 100)]:
            rain = engine.v61_rain_two_stage_summary(members(values))
            self.assertAlmostEqual(rain["probability_percent"], expected)
            self.assertAlmostEqual(sum(rain["bin_probabilities_percent"].values()), 100)
        tiny = engine.v61_rain_two_stage_summary(members([0] * 200 + [1]))
        self.assertGreater(tiny["probability_percent"], 0)
        self.assertLess(tiny["probability_percent"], 1)
        self.assertEqual(engine.v61_distribution_summary(members([1, 20, 1000]))["quantiles"]["p95"], 1000)

    def test_single_member_does_not_override_probability(self):
        args = engine.build_arg_parser().parse_args([])
        valid = "2026-10-30T09:00:00+07:00"
        row = {"target_datetime": valid, "prob_rain": 40}
        payload = {"hourly": {valid: {"variables": {"rain_mm": {
            "member_count": 1, "family_count": 1, "interval_minutes": 60}}}}}
        engine.v61_enrich_probabilistic_rows([row], payload, args)
        self.assertEqual(row["prob_rain"], 40)
        self.assertFalse(row["rain_two_stage"]["available"])

    def test_manual_crps_and_family_balance(self):
        members = [{"value": 0, "weight": 0.5}, {"value": 2, "weight": 0.5}]
        self.assertAlmostEqual(engine.v61_crps(members, 1), 0.5)
        native = [{"model": "A", "family": "A", "member": str(i), "value": 0} for i in range(20)]
        native += [{"model": "B", "family": "B", "member": "0", "value": 1}]
        balanced = engine.v61_family_balanced_members(native)
        self.assertAlmostEqual(sum(m["weight"] for m in balanced if m["family"] == "A"), 0.5)
        self.assertAlmostEqual(sum(m["weight"] for m in balanced if m["family"] == "B"), 0.5)

    def test_complete_provider_outage_remains_explicit(self):
        args = engine.clone_args_for_location(engine.build_arg_parser().parse_args([]), engine.LOCATION_PRESETS["dago"])
        failed = [engine.SourceResult(c["source_id"], c["provider"], False, {}, error="fixture outage")
                  for c in engine.ACTIVE_SOURCE_CONFIGS]
        def backup(date, config, options):
            return engine.SourceResult(config["source_id"], config["provider"], False, {}, error="fixture outage")
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, "ACTIVE_OUTPUT_DIR", tmp), \
                patch.object(engine, "_V61_BASE_COLLECT_SOURCES", return_value=failed), \
                patch.object(engine, "fetch_source", side_effect=backup), \
                patch.object(engine, "v61_fetch_ensembles", return_value={"status": "unavailable", "models": {}, "hourly": {}}), \
                contextlib.redirect_stdout(io.StringIO()):
            result = engine.collect_all_sources(dt.date(2026, 10, 30), args)
            availability = engine.read_json(str(Path(tmp) / "source_availability.json"))
        self.assertFalse(any(source.success for source in result))
        self.assertEqual(availability["successful_sources"], 0)
        self.assertFalse(availability["target_met"])

    def test_missing_heat_index_is_not_replaced_with_air_temperature(self):
        row = ui.row_to_hour({"hour": "09:00", "date": "2026-10-30", "temp_c": 25,
                              "rain_probability": 0, "wind_kmh": 0})
        self.assertIsNone(row["heat_index_c"])


class PackageContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "outputs"
        self.directory = self.root / "dago"
        self.dest = Path(self.tmp.name) / "public"
        ui.write_json(self.directory / "langit_api_v1.json", {
            "location_slug": "dago", "location_name": "Dago", "latitude": -6.89, "longitude": 107.61,
            "target_date": "2026-10-30"})
        for name in ("forecast_all_locations.csv", "source_status_all_locations.csv"):
            (self.root / name).write_text("location_slug,value\ndago,0\n")
        for offset in range(3):
            date = dt.date(2026, 10, 30) + dt.timedelta(days=offset)
            ui.write_json(self.directory / f"sentinel_x_{date:%Y%m%d}.json", {
                "issued_at": "2026-10-30T10:00:00+07:00",
                "source_states": [{"source_id": f"day-{offset}", "success": True}],
                "hourly": [{"target_date": date.isoformat(), "jam": "09:00",
                            "temp_p50": 25, "wind_p50": 0, "prob_rain": 0, "data_quality": "usable"}]})
        self.clock = patch.object(ui, "local_now", return_value=dt.datetime.fromisoformat("2026-10-30T08:00:00+07:00"))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with contextlib.redirect_stdout(io.StringIO()):
            ui.rebuild(self.root)

    def test_portal_preserves_null_wind_and_cloud(self):
        api = ui.read_json(self.directory / "langit_api_v1.json")
        location = ui.v65_geo_for_api(api)
        portal = ui.v65_portal_geo([api])
        self.assertEqual(location, portal)
        self.assertIsNone(portal["features"][0]["properties"]["cloud_pct"])
        self.assertIsNone(portal["features"][0]["properties"]["wind_dir"])

    def test_csv_and_geojson_contracts_reject_value_drift(self):
        build_site(self.root, self.dest)
        path = self.dest / "dago" / "langit_location.geojson"
        geo = ui.read_json(path)
        geo["features"][0]["properties"]["rain_probability"] = 100
        ui.write_json(path, geo)
        with self.assertRaisesRegex(ValueError, "GeoJSON/API rain_probability differs"):
            validate_site(self.dest)

    def test_csv_null_and_zero_match_api(self):
        build_site(self.root, self.dest)
        path = self.dest / "dago" / "langit_hourly_intelligence.csv"
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["rain_probability"], "0.0")
        rows[0]["rain_probability"] = "100"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "CSV/API rain_probability differs"):
            validate_site(self.dest)

    def test_unreferenced_assets_and_internal_html_are_not_published(self):
        (self.root / "assets" / "retired.js").write_text("retired")
        (self.directory / "internal_audit.html").write_text("internal")
        build_site(self.root, self.dest)
        self.assertFalse((self.dest / "assets" / "retired.js").exists())
        self.assertFalse((self.dest / "dago" / "internal_audit.html").exists())

    def test_empty_required_html_fails_validation(self):
        (self.directory / "langit_app.html").write_text("")
        with self.assertRaisesRegex(ValueError, "Missing dago/langit_app.html"):
            build_site(self.root, self.dest)

    def test_staging_collision_does_not_delete_another_build(self):
        mkdir = Path.mkdir
        def collision(path, *args, **kwargs):
            if path == self.dest:
                mkdir(path, *args, **kwargs)
                (path / "other-build").write_text("preserve")
                raise FileExistsError("Another process created staging")
            return mkdir(path, *args, **kwargs)
        with patch.object(Path, "mkdir", collision), self.assertRaises(FileExistsError):
            build_site(self.root, self.dest)
        self.assertEqual((self.dest / "other-build").read_text(), "preserve")

    def test_source_status_uses_the_current_forecast_day(self):
        api = ui.read_json(self.directory / "langit_api_v1.json")
        self.assertEqual(api["sources"][0]["source_id"], "day-0")
        self.assertEqual(api["source_status_date"], "2026-10-30")
        self.assertEqual(api["today"]["issued_at"], "2026-10-30T10:00:00+07:00")

    def test_conflicting_duplicate_hours_fail_rebuild(self):
        path = self.directory / "sentinel_x_20261030.json"
        payload = ui.read_json(path)
        payload["hourly"].append(dict(payload["hourly"][0], prob_rain=90))
        ui.write_json(path, payload)
        with self.assertRaisesRegex(ValueError, "Duplicate forecast hour"):
            ui.load_location_api(self.directory, {})

    def test_corrupt_source_is_not_silently_replaced_by_old_api(self):
        (self.directory / "sentinel_x_20261030.json").write_text('{"hourly":')
        with self.assertRaisesRegex(ValueError, "Invalid forecast input"):
            ui.load_location_api(self.directory, {})


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend contracts")
class FrontendContracts(unittest.TestCase):
    def test_numeric_and_chart_contracts(self):
        result = subprocess.run(["node", "--test", "tests/public_data.test.js"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_generated_scripts_run_against_dom_contract(self):
        def feature(slug, hour, temp):
            return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [107.6, -6.9]},
                    "properties": {"slug": slug, "location_name": slug, "date_iso": "2026-10-30",
                                   "hour": hour, "temp_c": temp, "rain_probability": 0,
                                   "wind_kmh": 0, "humidity_pct": None, "risk_class": "limited"}}
        geo = {"type": "FeatureCollection", "features": [
            feature("dago", "09:00", 0), feature("other", "10:00", 29)]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scripts.json"
            path.write_text(json.dumps({"map": ui.v65_map_page("Test", geo, "index.html"),
                                        "shared": ui.JS_V65}))
            result = subprocess.run(["node", "tests/rendered_contracts.js", str(path)],
                                    capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
