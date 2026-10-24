"""Offline regressions for deployment and weather data integrity."""
import contextlib
import datetime as dt
import gzip
import io
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import langit_v65_cinematic_rebuild as ui
import weather_ensemble_multi_location as engine
from site_tools.public_site import build_site, validate_site
from site_tools.workflow_inputs import resolve


class RenderingTests(unittest.TestCase):
    def test_probability_percent_is_not_guessed(self):
        self.assertEqual(ui.prob(0.6535947712418301), 0.6535947712418301)
        self.assertEqual(ui.prob(0), 0)
        self.assertEqual(ui.prob(1), 1)
        self.assertEqual(ui.prob("50%"), 50)
        self.assertIsNone(ui.prob(None))
        self.assertIsNone(ui.prob(float("nan")))

    def test_iso_time_does_not_parse_the_year(self):
        self.assertEqual(ui.hour("2026-10-23T21:37:29+07:00"), "21:37")
        self.assertEqual(ui.hour("2026-10-23 09:15:00"), "09:15")
        self.assertEqual(ui.hour("06:00"), "06:00")
        self.assertEqual(ui.hour("25:75", "missing"), "missing")

    def test_update_timezone_and_unknown(self):
        self.assertEqual(ui.fmt_update("2026-10-23T14:37:29Z"), "Diperbarui 23 Oktober, 21:37 WIB")
        self.assertEqual(ui.fmt_update(None), "Waktu pembaruan belum tersedia")

    def test_zero_and_missing_values_remain_distinct(self):
        row = ui.row_to_hour({"jam": "09:00", "temp_p50": 0, "wind_p50": 0, "prob_rain": 0})
        self.assertEqual(row["temp_c"], 0)
        self.assertEqual(row["wind_kmh"], 0)
        self.assertEqual(row["rain_probability"], 0)
        self.assertIsNone(row["cloud_pct"])
        self.assertIsNone(row["wind_dir"])
        self.assertFalse(ui.row_to_hour({"temp_c": 25})["valid"])
        self.assertEqual(ui.row_to_hour({"temp_c": 25})["risk_class"], "limited")

    def test_limited_engine_output_is_not_advertised_as_safe(self):
        row = ui.row_to_hour({"temp_p50": 25, "wind_p50": 1, "prob_rain": 0, "data_quality": "limited"})
        self.assertFalse(row["valid"])
        self.assertEqual(row["risk_class"], "limited")

    def test_missing_middle_day_does_not_shift_later_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ui.write_json(root / "langit_api_v1.json", {
                "target_date": "2026-10-23", "location_name": "Test", "days": []})
            for day, temp in [(23, 20), (25, 30)]:
                ui.write_json(root / f"sentinel_x_202610{day}.json", {"hourly": [
                    {"target_date": f"2026-10-{day}", "jam": "09:00", "temp_p50": temp,
                     "prob_rain": 0, "wind_p50": 0, "data_quality": "usable"}]})
            result = ui.load_location_api(root, {})
            self.assertEqual(result["days"][1]["date_iso"], "2026-10-24")
            self.assertEqual(result["days"][1]["valid_points"], 0)
            self.assertEqual(result["days"][2]["hours"][0]["date_iso"], "2026-10-25")
            self.assertEqual(result["days"][2]["hours"][0]["temp_c"], 30)

    def test_raw_issue_time_wins_over_legacy_formatted_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ui.write_json(root / "langit_api_v1.json", {
                "target_date": "2026-10-23", "generated_at": "Diperbarui 23 Oktober, 20:00 WIB"})
            ui.write_json(root / "sentinel_x_20261023.json", {
                "issued_at": "2026-10-23T21:37:29+07:00", "hourly": []})
            result = ui.load_location_api(root, {})
            self.assertEqual(result["generated_at"], "Diperbarui 23 Oktober, 21:37 WIB")
            self.assertEqual(result["generated_at_iso"], "2026-10-23T21:37:29+07:00")

    def test_no_good_window_bridges_unobserved_hours(self):
        rows = [{"hour": "06:00", "risk_class": "safe", "valid": True},
                {"hour": "09:00", "risk_class": "safe", "valid": True}]
        self.assertEqual(ui.best_windows(rows), ["06:00", "09:00"])

    def test_json_cannot_close_inline_script(self):
        data = {"location_name": "</script><script>bad()</script>"}
        encoded = ui.inline_json(data)
        self.assertNotIn("</script>", encoded)
        self.assertEqual(json.loads(encoded), data)


class EngineTests(unittest.TestCase):
    def test_circular_wind_average(self):
        with patch.object(engine, "point_weight", return_value=1):
            points = [SimpleNamespace(wind_direction_deg=d) for d in (359, 1)]
            angle = engine._weighted_wind_direction(points)
            self.assertAlmostEqual(min(angle, 360 - angle), 0)
            opposing = [SimpleNamespace(wind_direction_deg=d) for d in (90, 270)]
            self.assertIsNone(engine._weighted_wind_direction(opposing))
            self.assertIsNone(engine._weighted_wind_direction([]))

    def test_legacy_self_tests(self):
        args = engine.build_arg_parser().parse_args([])
        engine.run_self_tests(args)
        self.assertTrue(engine.aether_self_test())

    def test_compressed_archives_preserve_data_and_are_read_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, "ACTIVE_OUTPUT_DIR", tmp):
            directory = Path(engine._quality_archive_dir())
            doc = {"run_id": "test", "issued_at": "2026-10-23T08:00:00+07:00",
                   "location_slug": "test", "hourly": [{"target_date": "2026-10-24", "prob_rain": 0,
                   "ensemble_distribution": {"rain_mm": {"members": [{"value": 0}]}}}]}
            engine.write_json(str(directory / "a.json"), doc)
            engine.write_json_gz(str(directory / "a.json.gz"), doc)
            with gzip.open(directory / "a.json.gz", "rt") as stream:
                self.assertEqual(json.load(stream), doc)
            self.assertEqual(len(list(engine._quality_archive_documents())), 1)
            (directory / "a.json").unlink()
            rows = list(engine._quality_historical_rows())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ensemble_distribution"], doc["hourly"][0]["ensemble_distribution"])

    def test_new_archive_is_immutable_lossless_gzip(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine, "ACTIVE_OUTPUT_DIR", tmp):
            args = engine.clone_args_for_location(engine.build_arg_parser().parse_args([]), engine.LOCATION_PRESETS["dago"])
            archive = engine._quality_archive_run("new_run", dt.date(2026, 10, 24), [], [], args)
            self.assertTrue(archive.endswith(".json.gz"))
            self.assertEqual(engine.read_json(archive)["run_id"], "new_run")
            with self.assertRaisesRegex(ValueError, "already archived"):
                engine._quality_archive_run("new_run", dt.date(2026, 10, 24), [], [], args)

    def test_history_can_be_consumed_without_loading_every_issue(self):
        visited = []
        def documents():
            for n in range(10):
                visited.append(n)
                yield {"run_id": str(n), "hourly": [{"prob_rain": 0}]}
        with patch.object(engine, "_quality_archive_documents", documents):
            rows = engine._quality_historical_rows()
            self.assertEqual(visited, [])
            self.assertEqual(next(rows)["run_id"], "0")
            self.assertEqual(visited, [0])


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "outputs"
        self.root.mkdir()
        for name in ["forecast_all_locations.csv", "source_status_all_locations.csv"]:
            (self.root / name).write_text("location_slug,value\ndago,0\n")
        location = self.root / "dago"
        ui.write_json(location / "langit_api_v1.json", {
            "location_name": "Test Dago", "location_slug": "dago", "latitude": -6.89, "longitude": 107.61,
            "target_date": "2026-10-24", "issued_at": "2026-10-24T00:00:00+07:00", "days": []})
        for offset in range(3):
            date = dt.date(2026, 10, 24) + dt.timedelta(days=offset)
            ui.write_json(location / f"sentinel_x_{date:%Y%m%d}.json", {
                "issued_at": "2026-10-24T00:00:00+07:00", "hourly": [
                    {"target_date": date.isoformat(), "jam": f"{h:02d}:00", "temp_p50": 25,
                     "prob_rain": 0.65, "wind_p50": 0, "data_quality": "usable"}
                    for h in range(24)]})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.rebuild(self.root), 0)
        self.dest = Path(self.tmp.name) / "public"

    def test_staging_preserves_source_and_excludes_internal_data(self):
        private = self.root / "dago" / "forecast_archive" / "issue.json"
        private.parent.mkdir()
        private.write_text('{"important":true}')
        (self.root / "dago" / "sentinel_x_ledger.sqlite").write_bytes(b"private")
        (self.root / "dago" / "source_health.json").write_text('{}')
        stats = build_site(self.root, self.dest)
        self.assertTrue(private.exists())
        self.assertFalse((self.dest / "dago" / "forecast_archive").exists())
        self.assertFalse((self.dest / "dago" / "sentinel_x_ledger.sqlite").exists())
        self.assertFalse((self.dest / "dago" / "source_health.json").exists())
        self.assertTrue((self.dest / ".nojekyll").exists())
        self.assertLess(stats["bytes"], 3_000_000)

    def test_missing_required_file_fails_and_discards_only_staging(self):
        (self.root / "dago" / "langit_3day.html").unlink()
        with self.assertRaisesRegex(ValueError, "Missing dago/langit_3day.html"):
            build_site(self.root, self.dest)
        self.assertTrue((self.root / "index.html").exists())
        self.assertFalse(self.dest.exists())

    def test_broken_links_are_detected(self):
        with (self.root / "index.html").open("a") as stream:
            stream.write('<a href="missing.html">Broken</a>')
        with self.assertRaisesRegex(ValueError, "Broken local URL"):
            build_site(self.root, self.dest)

    def test_nan_json_is_rejected(self):
        (self.root / "dago" / "langit_map_layers.json").write_text('{"value":NaN}')
        with self.assertRaisesRegex(ValueError, "Invalid JSON"):
            build_site(self.root, self.dest)

    def test_cannot_overwrite_source_or_existing_destination(self):
        with self.assertRaisesRegex(ValueError, "separate directories"):
            build_site(self.root, self.root / "public")
        self.dest.mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            build_site(self.root, self.dest)

    def test_verify_is_read_only_and_detects_empty_files(self):
        index = self.root / "index.html"
        index.write_text(index.read_text() + "<!-- visual-first -->")
        before = index.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.verify(self.root), 3)
        self.assertEqual(index.read_bytes(), before)
        (self.root / "dago" / "langit_app.html").write_text("")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.verify(self.root), 2)

    def test_timeline_keeps_all_24_hours(self):
        api = json.loads((self.root / "dago" / "langit_api_v1.json").read_text())
        self.assertEqual(len(api["days"][0]["hours"]), 24)
        timeline = ui.v65_timeline(api["days"][0]["hours"])
        self.assertIn("23:00", timeline)

    def test_source_success_schema_is_rendered(self):
        api = json.loads((self.root / "dago" / "langit_api_v1.json").read_text())
        api["sources"] = [{"source_id": "GFS", "success": "yes", "provider": "NOAA", "effective_base_weight": 1},
                          {"source_id": "METNO", "success": "no", "provider": "MET Norway"}]
        page = ui.v65_data_page(api)
        self.assertIn("1/2 sumber aktif", page)
        self.assertIn("GFS", page)

    def test_sample_count_alone_does_not_certify_accuracy(self):
        api = json.loads((self.root / "dago" / "langit_api_v1.json").read_text())
        directory = self.root / "dago"
        ui.write_json(directory / "sentinel_x_verification_summary.json", {
            "matched_cases": 100, "verification_min_cases": 30,
            "independent_days": 1, "sample_status": "INSUFFICIENT_DATA", "temperature_mae_c": 1})
        page = ui.v65_accuracy_page(api, directory)
        self.assertIn("Belum cukup bukti verifikasi", page)
        self.assertNotIn("Error Suhu (MAE)", page)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for generated JavaScript checks")
    def test_every_generated_script_parses(self):
        from html.parser import HTMLParser
        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__(); self.inside = False; self.parts = []
            def handle_starttag(self, tag, attrs):
                if tag == "script": self.inside = not dict(attrs).get("src")
            def handle_endtag(self, tag):
                if tag == "script": self.inside = False
            def handle_data(self, data):
                if self.inside: self.parts.append(data)
        for path in self.root.rglob("*.html"):
            parser = Scripts(); parser.feed(path.read_text())
            for code in parser.parts:
                run = subprocess.run(["node", "--check"], input=code, text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, f"{path}: {run.stderr}")
        for path in (self.root / "assets").glob("*.js"):
            run = subprocess.run(["node", "--check", str(path)], text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)


class WorkflowTests(unittest.TestCase):
    def test_known_location_inputs(self):
        self.assertEqual(resolve("", ""), ("all", "--no-debug"))
        self.assertEqual(resolve(" DAGO, jatinangor,dago ", "true"), ("dago,jatinangor", "--debug"))

    def test_shell_and_multiline_inputs_are_rejected(self):
        for value in ['$(touch /tmp/bad)', 'dago\nOTHER=bad', 'unknown', 'all,dago', 'dago,']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve(value, "false")
        with self.assertRaises(ValueError):
            resolve("dago", 'true\nOTHER=bad')


if __name__ == "__main__":
    unittest.main()
