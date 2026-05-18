"""Tests for util.compute_nbc_quarter prediction window behavior.

Verifies that incomplete quarter-hour predictions use exactly the last 60
per-second samples (or fewer if fewer are available), matching the docstring
promise in compute_nbc_quarters: "computes a rate from the last 60 seconds of
data within the current quarter".
"""

import unittest

from util import compute_nbc_quarter


class TestComputeNBCQuarterPredictionWindow(unittest.TestCase):
    """Tests that compute_nbc_quarter uses exactly the right number of samples for prediction."""

    def test_prediction_capped_at_60_samples_when_more_available(self):
        """With 360 samples available, prediction must use only the last 60 — not all 360.

        Uses constant values so we can compute expected prediction deterministically.

        Layout: 300 samples of 0.001 kWh/s (1 W) followed by 60 samples of 0.003 kWh/s (3 W).

        raw_wh = (300 * 0.001 + 60 * 0.003) * 1000 = 480 Wh
        remaining_seconds = 900 - 360 = 540

        Expected prediction_w when using exactly last 60:
            prediction_w = 1000 * (60 * 0.003) / 60 = 3.0 W

        Expected predicted_wh:
            predicted_wh = 480 + 540 * 3.0 = 2100 Wh

        If the code used more than 60 samples (e.g. last 90), prediction_w would be
        diluted by the earlier 1 W samples and predicted_wh would fall below 2100.
        """
        values = [0.001] * 300 + [0.003] * 60
        result = compute_nbc_quarter(values)

        self.assertIsNotNone(result)
        self.assertFalse(result["complete"])
        self.assertEqual(result["samples_used"], 360)

        expected_prediction_w = 3.0  # 1000 * 0.003
        expected_predicted_wh = 2100.0  # 480 + 540 * 3.0

        self.assertAlmostEqual(result["prediction_w"], expected_prediction_w, places=6)
        self.assertAlmostEqual(result["predicted_wh"], expected_predicted_wh, places=6)

    def test_prediction_uses_all_60_when_60_available(self):
        """With exactly 60 samples available, prediction must use all 60 — not fewer.

        Uses the last 30 samples to have a higher rate than the first 30 so that
        using fewer than 60 samples would inflate the prediction.

        Layout: 30 samples of 0.001 kWh/s (1 W) followed by 30 samples of 0.003 kWh/s (3 W).

        Expected prediction_w when using all 60:
            prediction_w = 1000 * (30 * 0.001 + 30 * 0.003) / 60 = 2.0 W

        Expected predicted_wh:
            raw_wh = 60 * 0.002 * 1000 = 120 Wh
            predicted_wh = 120 + 840 * 2.0 = 1800 Wh

        If the code used only the last 30 samples, prediction_w would be 3.0
        and predicted_wh would be 120 + 840 * 3.0 = 2640 Wh — clearly wrong.
        """
        values = [0.001] * 30 + [0.003] * 30
        result = compute_nbc_quarter(values)

        self.assertIsNotNone(result)
        self.assertFalse(result["complete"])
        self.assertEqual(result["samples_used"], 60)

        expected_prediction_w = 2.0  # 1000 * (30*0.001 + 30*0.003) / 60
        expected_predicted_wh = 1800.0  # 120 + 840 * 2.0

        self.assertAlmostEqual(result["prediction_w"], expected_prediction_w, places=6)
        self.assertAlmostEqual(result["predicted_wh"], expected_predicted_wh, places=6)

    def test_prediction_uses_all_samples_when_fewer_than_60(self):
        """With only 30 samples available, prediction must use all 30 — not try to use 60.

        Layout: 30 samples of 0.002 kWh/s (2 W).

        raw_wh = 30 * 0.002 * 1000 = 60 Wh
        remaining_seconds = 900 - 30 = 870

        Expected prediction_w = 1000 * 0.002 = 2.0 W
        Expected predicted_wh = 60 + 870 * 2.0 = 1800 Wh
        """
        values = [0.002] * 30
        result = compute_nbc_quarter(values)

        self.assertIsNotNone(result)
        self.assertFalse(result["complete"])
        self.assertEqual(result["samples_used"], 30)

        expected_prediction_w = 2.0
        expected_predicted_wh = 1800.0  # 60 + 870 * 2.0

        self.assertAlmostEqual(result["prediction_w"], expected_prediction_w, places=6)
        self.assertAlmostEqual(result["predicted_wh"], expected_predicted_wh, places=6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
