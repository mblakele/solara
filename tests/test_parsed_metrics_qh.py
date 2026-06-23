"""Tests for ParsedMetricsQH dataclass.

Phase 5 of direction-b-dataclasses: replaces dict return from
_parse_metrics() with typed ParsedMetricsQH dataclass.
"""

import unittest
import dataclasses

from load_nbc import ParsedMetricsQH


class TestParsedMetricsQH(unittest.TestCase):
    """Tests for ParsedMetricsQH dataclass construction."""

    def test_construction_all_fields(self):
        """ParsedMetricsQH can be constructed with all fields."""
        qh = ParsedMetricsQH(
            qh_name="QH1",
            predicted_wh=-1500.0,
            seconds_remaining=600,
            data_lag_secs=2.5,
        )
        self.assertEqual(qh.qh_name, "QH1")
        self.assertEqual(qh.predicted_wh, -1500.0)
        self.assertEqual(qh.seconds_remaining, 600)
        self.assertEqual(qh.data_lag_secs, 2.5)

    def test_to_dict_matches_original_structure(self):
        """to_dict() output matches the original _parse_metrics return dict."""
        qh = ParsedMetricsQH(
            qh_name="QH1",
            predicted_wh=-2000.0,
            seconds_remaining=300,
            data_lag_secs=1.0,
        )
        d = qh.to_dict()
        self.assertEqual(d["qh_name"], "QH1")
        self.assertEqual(d["predicted_wh"], -2000.0)
        self.assertEqual(d["seconds_remaining"], 300)
        self.assertEqual(d["_data_lag_secs"], 1.0)

    def test_to_dict_has_all_expected_keys(self):
        """to_dict() contains all 4 keys."""
        qh = ParsedMetricsQH(
            qh_name="QH1",
            predicted_wh=0.0,
            seconds_remaining=0,
            data_lag_secs=0.0,
        )
        self.assertEqual(set(qh.to_dict().keys()), {
            "qh_name", "predicted_wh", "seconds_remaining", "_data_lag_secs",
        })

    def test_is_frozen(self):
        """ParsedMetricsQH is frozen (immutable)."""
        qh = ParsedMetricsQH(
            qh_name="QH1",
            predicted_wh=-1500.0,
            seconds_remaining=600,
            data_lag_secs=2.5,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            qh.predicted_wh = 0.0

    def test_equality(self):
        """Two ParsedMetricsQH instances with same values are equal."""
        a = ParsedMetricsQH(
            qh_name="QH1",
            predicted_wh=-1500.0,
            seconds_remaining=600,
            data_lag_secs=2.5,
        )
        b = ParsedMetricsQH(
            qh_name="QH1",
            predicted_wh=-1500.0,
            seconds_remaining=600,
            data_lag_secs=2.5,
        )
        self.assertEqual(a, b)

    def test_inequality(self):
        """Two ParsedMetricsQH instances with different values are not equal."""
        a = ParsedMetricsQH(
            qh_name="QH1",
            predicted_wh=-1500.0,
            seconds_remaining=600,
            data_lag_secs=2.5,
        )
        b = ParsedMetricsQH(
            qh_name="QH2",
            predicted_wh=-1000.0,
            seconds_remaining=400,
            data_lag_secs=1.0,
        )
        self.assertNotEqual(a, b)
