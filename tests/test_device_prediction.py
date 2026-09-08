"""Tests for DevicePrediction dataclass.

Phase 3 of direction-b-dataclasses: replaces dict return from
_predict_device() with typed DevicePrediction dataclass.

Tests verify:
- DevicePrediction construction
- to_dict() output matches expected dict structure
- Roundtrip: to_dict() output can reconstruct DevicePrediction
"""

import unittest
from datetime import timedelta

from metrics import DevicePrediction


class TestDevicePrediction(unittest.TestCase):
    """Tests for DevicePrediction dataclass construction."""

    def test_to_dict_matches_original_structure(self):
        """to_dict() output matches the original _predict_device return dict."""
        pred = DevicePrediction(
            lag=timedelta(seconds=30),
            minute_predicted=1.5,
            prediction=75.0,
            prediction_min=70.0,
            prediction_max=80.0,
            seconds_remaining=600.0,
        )
        d = pred.to_dict()
        self.assertEqual(d["lag"], timedelta(seconds=30))
        self.assertEqual(d["minute_predicted"], 1.5)
        self.assertEqual(d["prediction"], 75.0)
        self.assertEqual(d["prediction_min"], 70.0)
        self.assertEqual(d["prediction_max"], 80.0)
        self.assertEqual(d["seconds_remaining"], 600.0)

    def test_to_dict_has_all_expected_keys(self):
        """to_dict() contains all 6 keys expected by callers."""
        pred = DevicePrediction(
            lag=timedelta(0),
            minute_predicted=0.0,
            prediction=0.0,
            prediction_min=0.0,
            prediction_max=0.0,
            seconds_remaining=0.0,
        )
        d = pred.to_dict()
        expected_keys = {
            "lag", "minute_predicted", "prediction",
            "prediction_min", "prediction_max",
            "seconds_remaining",
        }
        self.assertEqual(set(d.keys()), expected_keys)

    def test_roundtrip_to_dict(self):
        """DevicePrediction(**pred.to_dict()) roundtrips."""
        original = DevicePrediction(
            lag=timedelta(seconds=10),
            minute_predicted=2.0,
            prediction=90.0,
            prediction_min=85.0,
            prediction_max=95.0,
            seconds_remaining=1200.0,
        )
        d = original.to_dict()
        restored = DevicePrediction(**d)
        self.assertEqual(original, restored)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
