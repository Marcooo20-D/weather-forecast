"""Scientific contracts for the independent observation/reference pathway."""
import copy
import csv
from datetime import datetime, timedelta, timezone
import gzip
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forecast_tools import observations as obs
from forecast_tools import verification as verify
from site_tools import verification_panel

UTC = timezone.utc
END = datetime(2026, 10, 25, 1, tzinfo=UTC)


def station_payload(source="metar"):
    buffer = io.StringIO()
    fields = ["year", "month", "day", "hour", "temp", "temp_source", "rhum", "rhum_source", "wspd", "wspd_source", "prcp", "prcp_source"]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerow(dict(zip(fields, [2026, 10, 25, 0, 26, source, 80, source, 7.2, source, 3, source])))
    return gzip.compress(buffer.getvalue().encode())


def forecast_case():
    members = []
    for family in ["GFS", "ECMWF"]:
        for i, value in enumerate([0, .1, .2, 1, 2]):
            members.append({"family": family, "model": family.lower(), "member": str(i), "value": value, "weight": .1, "fetched_at": obs.iso(END - timedelta(hours=3))})
    start = END - timedelta(hours=1)
    group = {"unit": "mm", "interval_minutes": 60, "interval_start": obs.iso(start), "interval_end": obs.iso(END), "members": members}
    row = {"target_datetime": obs.iso(END), "probability_basis": verify.METHOD, "ensemble_status": "usable", "rain_interval_start": obs.iso(start), "rain_interval_end": obs.iso(END), "prob_rain_raw": 60, "ensemble_distribution": {"rain_mm": group}, "calibration_status": "RAW_UNCALIBRATED"}
    doc = {"issued_at": obs.iso(END - timedelta(hours=3)), "location_slug": "dago", "hourly": [row]}
    reference = {"schema_version": "LANGIT.satellite-reference.v1", "interval_start": obs.iso(start), "interval_end": obs.iso(END), "reference_type": "satellite_gauge_precipitation_estimate", "fetched_at": obs.iso(END + timedelta(days=1)), "source_url": "https://example.test/reference.nc", "source_sha256": "test-source", "points": {"dago": {**obs.LOCATIONS["dago"], "cell_latitude": -6.875, "cell_longitude": 107.625, "rain_mm": 2, "qc": "usable_reference"}}}
    return doc, row, reference


class StationSourceContracts(unittest.TestCase):
    def setUp(self):
        self.metadata = {"id": "96781", "location": {"latitude": -6.9, "longitude": 107.5833, "elevation": 740}, "timezone": "Asia/Jakarta"}

    def test_model_filled_station_values_never_become_observations(self):
        rows, status = obs.station_rows(station_payload("metno_forecast"), self.metadata, END - timedelta(hours=1), END, END + timedelta(days=1), "https://example.test/source")
        self.assertEqual(rows, [])
        self.assertEqual(status["excluded_field_values_by_source"], {"metno_forecast": 3})

    def test_measured_fields_use_explicit_units_and_no_invented_rain_interval(self):
        rows, _ = obs.station_rows(station_payload(), self.metadata, END - timedelta(hours=1), END, END + timedelta(days=1), "https://example.test/source")
        self.assertEqual(rows[0]["temp_c"], 26)
        self.assertEqual(rows[0]["temp_unit"], "C")
        self.assertEqual(rows[0]["wind_unit"], "km/h")
        self.assertEqual(rows[0]["rain_mm"], "")
        self.assertNotIn("qc", rows[0])

    def test_unlabelled_sources_are_rejected(self):
        rows, status = obs.station_rows(station_payload(""), self.metadata, END - timedelta(hours=1), END, END + timedelta(days=1), "https://example.test/source")
        self.assertEqual(rows, [])
        self.assertEqual(status["measurement_rows"], 0)

    def test_future_measurements_are_not_ingested(self):
        rows, _ = obs.station_rows(station_payload(), self.metadata, END - timedelta(hours=1), END, END - timedelta(hours=2), "https://example.test/source")
        self.assertEqual(rows, [])

    def test_naive_timestamps_are_not_silently_assumed_utc(self):
        with self.assertRaises(ValueError): obs.timestamp("2026-10-25T00:00:00")


class TimeAndScoreContracts(unittest.TestCase):
    def test_inclusive_noaa_last_second_becomes_exclusive_interval_end(self):
        start, end = obs.cf_interval(END.timestamp() - 3600, END.timestamp() - 1, "PT1H")
        self.assertEqual(end, END)
        self.assertEqual((end - start).total_seconds(), 3600)

    def test_non_hourly_reference_is_rejected(self):
        with self.assertRaises(ValueError): obs.cf_interval(0, 7200, "PT2H")

    def test_weighted_crps_has_correct_units_and_two_member_result(self):
        self.assertAlmostEqual(verify.weighted_crps([{"value": 0, "weight": .5}, {"value": 2, "weight": .5}], 1), .5)
        self.assertEqual(verify.weighted_crps([{"value": 2, "weight": 1}], 2), 0)

    def test_native_members_match_probability_in_percent(self):
        doc, row, reference = forecast_case()
        pair = verify.make_pair(doc, row, reference, "dago", evaluated_at=END + timedelta(days=2))
        self.assertAlmostEqual(pair["brier"], .16)
        self.assertAlmostEqual(pair["rain_ensemble_mean_mm"], .66)
        self.assertEqual(pair["rain_median_mm"], .2)

    def test_after_event_forecasts_are_excluded(self):
        doc, row, reference = forecast_case();doc["issued_at"] = obs.iso(END - timedelta(minutes=15))
        with self.assertRaisesRegex(ValueError, "not_pre_event"): verify.make_pair(doc, row, reference, "dago", evaluated_at=END + timedelta(days=2))

    def test_different_rain_intervals_are_not_nearest_matched(self):
        doc, row, reference = forecast_case();reference["interval_start"] = obs.iso(END - timedelta(hours=2))
        with self.assertRaisesRegex(ValueError, "rain_interval_mismatch"): verify.make_pair(doc, row, reference, "dago", evaluated_at=END + timedelta(days=2))

    def test_missing_grid_cell_is_not_dry(self):
        doc, row, reference = forecast_case();reference["points"]["dago"]["rain_mm"] = None
        with self.assertRaisesRegex(ValueError, "unavailable_reference"): verify.make_pair(doc, row, reference, "dago", evaluated_at=END + timedelta(days=2))

    def test_heuristic_probability_is_not_graded_as_native_ensemble(self):
        doc, row, reference = forecast_case();row["probability_basis"] = "deterministic_heuristic"
        with self.assertRaisesRegex(ValueError, "not_usable_native_ensemble"): verify.make_pair(doc, row, reference, "dago", evaluated_at=END + timedelta(days=2))

    def test_probability_mismatch_is_rejected(self):
        doc, row, reference = forecast_case();row["prob_rain_raw"] = .6
        with self.assertRaisesRegex(ValueError, "probability_member_mismatch"): verify.make_pair(doc, row, reference, "dago", evaluated_at=END + timedelta(days=2))

    def test_duplicate_member_identifiers_are_not_additional_evidence(self):
        doc, row, reference = forecast_case();row["ensemble_distribution"]["rain_mm"]["members"].append(copy.copy(row["ensemble_distribution"]["rain_mm"]["members"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"): verify.make_pair(doc, row, reference, "dago", evaluated_at=END + timedelta(days=2))

    def test_empty_metrics_are_null_instead_of_zero(self):
        self.assertIsNone(verify.metrics([])["brier_score"])
        self.assertIsNone(verify.reliability([])[0]["reference_frequency_pct"])

    def test_oldest_issue_per_hour_and_lead_is_selected_once(self):
        doc, row, reference = forecast_case()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);obs.write_json(root / "observations/cmorph/hour.json", reference)
            obs.write_json(root / "dago/forecast_archive/old.json", doc)
            newer = copy.deepcopy(doc);newer["issued_at"] = obs.iso(END - timedelta(hours=2))
            obs.write_json(root / "dago/forecast_archive/new.json", newer)
            summary = verify.evaluate_location(root, "dago", evaluated_at=END + timedelta(days=2))
            self.assertEqual(summary["all_leads"]["cases"], 1)
            with (root / "dago/satellite_verification_pairs.csv").open() as h: pairs = list(csv.DictReader(h))
            self.assertEqual(pairs[0]["issued_at"], doc["issued_at"])
            self.assertFalse(summary["station_calibration_changed"])

    def test_public_panel_names_the_reference_and_preserves_missing_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            obs.write_json(p / "satellite_verification.json", {"schema_version": "LANGIT.satellite-verification.v1", "all_leads": {"cases": 0}})
            html = verification_panel.render(p)
            self.assertIn("bukan penakar hujan", html)
            self.assertNotIn("0.000", html)


class EngineObservationBridge(unittest.TestCase):
    def test_station_adapter_rows_pass_engine_metadata_and_unit_qc(self):
        import weather_ensemble_multi_location as engine
        metadata = {"id": "96781", "location": {"latitude": -6.9, "longitude": 107.5833, "elevation": 740}, "timezone": "Asia/Jakarta"}
        rows, _ = obs.station_rows(station_payload(), metadata, END - timedelta(hours=1), END, END + timedelta(days=1), "https://example.test/source")
        args = engine.clone_args_for_location(engine.build_arg_parser().parse_args([]), engine.LOCATION_PRESETS["dago"])
        result, report = engine.v61_quality_control_observations(rows, args)
        self.assertEqual(result[0]["temp_c_qc"], "passed")
        self.assertEqual(result[0]["temp_c_source"], "metar")
        self.assertAlmostEqual(result[0]["wind_kmh"], 7.2)

    def test_self_labelled_model_source_cannot_pass_station_qc(self):
        import weather_ensemble_multi_location as engine
        args = engine.clone_args_for_location(engine.build_arg_parser().parse_args([]), engine.LOCATION_PRESETS["dago"])
        row = {"station_id": "fixture", "observation_type": "station_observation", "source_id": "metno_forecast", "observed_datetime": obs.iso(END), "available_at": obs.iso(END + timedelta(days=1)), "latitude": -6.9, "longitude": 107.5833, "elevation_m": 740, "timezone": "Asia/Jakarta", "temp_c": 25, "temp_unit": "C"}
        result, _ = engine.v61_quality_control_observations([row], args)
        self.assertNotEqual(result[0]["temp_c_qc"], "passed")

    def test_model_reference_mode_remains_separate(self):
        import weather_ensemble_multi_location as engine
        self.assertEqual(engine.build_arg_parser().parse_args(["--mode", "sync-references"]).mode, "sync-references")
        self.assertTrue(callable(engine.sync_model_references))


@unittest.skipUnless(importlib.util.find_spec("netCDF4"), "Install requirements.txt for the native NetCDF integration check")
class NativeGridContracts(unittest.TestCase):
    def test_netcdf_scale_missing_mask_grid_and_time_are_read_together(self):
        import netCDF4
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.nc"
            with netCDF4.Dataset(path, "w") as d:
                for name, size in [("time", 1), ("nv", 2), ("lat", 2), ("lon", 2)]:d.createDimension(name, size)
                t = d.createVariable("time", "i8", ("time",));t.units = "seconds since 1970-01-01 00:00:00 0:00";t[:] = [END.timestamp() - 3600]
                b = d.createVariable("time_bounds", "i8", ("time", "nv"));b[:] = [[END.timestamp() - 3600, END.timestamp() - 1]]
                lat = d.createVariable("lat", "f4", ("lat",));lat[:] = [-6.875, -6.625]
                lon = d.createVariable("lon", "f4", ("lon",));lon[:] = [107.625, 108.375]
                lb = d.createVariable("lat_bounds", "f4", ("lat", "nv"));lb[:] = [[-7, -6.75], [-6.75, -6.5]]
                ob = d.createVariable("lon_bounds", "f4", ("lon", "nv"));ob[:] = [[107.5, 107.75], [108.25, 108.5]]
                rain = d.createVariable("cmorph", "i2", ("time", "lat", "lon"), fill_value=-999)
                rain.units = "mm/hr";rain.scale_factor = .01;rain[:] = np.ma.array([[[2.34, 0], [0, 0]]], mask=[[[False, True], [False, False]]])
                d.time_coverage_duration = "PT1H";d.product_version = "Version 0.x";d.date_created = "2026-10-26"
            result = obs.extract_cmorph(path.read_bytes(), "https://example.test/source.nc", {"dago": obs.LOCATIONS["dago"]}, END + timedelta(days=1))
            self.assertAlmostEqual(result["points"]["dago"]["rain_mm"], 2.34, places=5)
            self.assertEqual(result["interval_end"], obs.iso(END))
            self.assertEqual(result["reference_type"], "satellite_gauge_precipitation_estimate")


if __name__ == "__main__": unittest.main()
