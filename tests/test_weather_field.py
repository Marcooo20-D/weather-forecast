import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from site_tools import weather_field as field


def cells():
    times = [f"2026-11-04T{h:02}:00" for h in range(24)]
    units = dict(zip(field.VARIABLES, ["°C", "%", "m/s", "°", "%", "mm"]))
    return [
        {
            "latitude": lat,
            "longitude": lon,
            "utc_offset_seconds": 25200,
            "hourly_units": dict(units),
            "hourly": {
                "time": times,
                "temperature_2m": [25] * 24,
                "relative_humidity_2m": [80] * 24,
                "wind_speed_10m": [5] * 24,
                "wind_direction_10m": [270] * 24,
                "cloud_cover": [60] * 24,
                "precipitation": [0] * 24,
            },
        }
        for lat in [-7.1, -7, -6.9]
        for lon in [107.5, 107.6, 107.7]
    ]


class NativeField(unittest.TestCase):
    def test_meteorological_from_direction(self):
        self.assertEqual(field.vector(5, 270), (5, 0))
        self.assertEqual(field.vector(5, 0), (0, -5))
        self.assertEqual(field.vector(5, 90), (-5, 0))
        self.assertEqual(field.vector(None, 90), (None, None))

    def test_native_axes_zero_units_and_retrieval_provenance(self):
        doc = field.normalize(cells())
        self.assertEqual(doc["grid"]["nx"] * doc["grid"]["ny"], 9)
        self.assertEqual(doc["fields"]["precipitation"][0], [0] * 9)
        self.assertEqual(doc["fields"]["u"][0], [5] * 9)
        self.assertTrue(doc["times"][0].endswith("+07:00"))
        self.assertEqual(doc["model"]["resolution_km"], 13)
        self.assertNotIn("issued_at", doc)

    def test_missing_values_do_not_become_calm_or_dry(self):
        data = cells()
        data[0]["hourly"]["wind_speed_10m"][0] = None
        data[0]["hourly"]["precipitation"][0] = None
        doc = field.normalize(data)
        for key in ["u", "v", "precipitation"]:
            self.assertIsNone(doc["fields"][key][0][0])

    def test_unavailable_model_and_nonrectangular_mesh_rejected(self):
        data = cells()
        data[0]["hourly_units"]["temperature_2m"] = "undefined"
        with self.assertRaises(ValueError):
            field.normalize(data)
        with self.assertRaises(ValueError):
            field.normalize(cells()[:-1])

    def test_fresh_cache_keeps_original_timestamp_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = field.normalize(
                cells(), dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
            )
            field.save(root / field.NAME, doc)
            before = (root / field.NAME).read_bytes()
            with patch.object(
                field, "urlopen", side_effect=OSError("temporary outage")
            ):
                self.assertEqual(field.refresh(root), 0)
            self.assertEqual((root / field.NAME).read_bytes(), before)

    def test_expired_cache_cannot_render_a_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            field.save(
                root / field.NAME,
                field.normalize(
                    cells(), dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=19)
                ),
            )
            with patch.object(
                field, "urlopen", side_effect=OSError("temporary outage")
            ):
                field.refresh(root)
            self.assertEqual(
                json.loads((root / field.NAME).read_text())["status"], "unavailable"
            )

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_interpolation_contracts(self):
        result = subprocess.run(
            ["node", "--test", "tests/weather_field.test.js"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
