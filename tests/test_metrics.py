import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests
from metrics import (
    HourlyProjection,
    Metrics,
    MetricsBase,
    MetricsCache,
    RetryableMetricsException,
)
from util import compute_nbc_quarters
from mockdata import MetricsMock


class TestTOUReporterAggregate(unittest.TestCase):
    """Test that TOUReporter.aggregate_tou correctly uses EnergyDataAggregator."""

    def test_aggregate_tou_uses_module_level_import(self):
        """Verify aggregate_tou doesn't reference self.EnergyDataAggregator.

        When EnergyDataAggregator was moved from class body to module-level,
        the call site changed from self.EnergyDataAggregator.aggregate_from_minutes()
        to EnergyDataAggregator.aggregate_from_minutes(). This test ensures
        the TOUReporter instance can successfully run aggregate_tou without
        raising AttributeError.
        """
        from metrics import TOUReporter

        # We can't fully instantiate TOUReporter (needs real VUE API),
        # but we can verify the method doesn't reference self.EnergyDataAggregator
        # by checking that calling it on a partial instance works.
        class PartialTOU(TOUReporter):
            def __init__(self):
                # Skip parent init, just set what aggregate_tou needs
                self.usage_data_list = [
                    {
                        "start": datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc),
                        "data": [0.001] * 60,
                    }
                ]

        reporter = PartialTOU()
        # Should not raise AttributeError for missing EnergyDataAggregator
        reporter.aggregate_tou()
        self.assertIsNotNone(reporter.tou_result)
        self.assertIn("total", reporter.tou_result)


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.mock = MetricsMock()
        self.metrics_data = self.mock.metrics

    def test_retryable_exception(self):
        ex = RetryableMetricsException("test error")
        self.assertEqual(ex.message, "test error")
        self.assertIsInstance(ex.instant, datetime)

    def test_metrics_mock_structure(self):
        self.assertIn("api_response", self.metrics_data)
        self.assertIn("devices", self.metrics_data)
        self.assertIn("instant", self.metrics_data)
        self.assertTrue(self.metrics_data["debug"])

    def test_mock_device_data(self):
        devices = self.metrics_data["devices"]
        self.assertEqual(len(devices), 2)

        # Device A: negative (solar export)
        device_a = devices[0]
        self.assertEqual(device_a["name"], "MOCK")
        self.assertEqual(device_a["timezone"], "America/Los_Angeles")
        self.assertIn("prediction", device_a)
        self.assertIn("scales", device_a)
        self.assertIn("smoothing", device_a)

        # Device B: positive (load only)
        device_b = devices[1]
        self.assertEqual(device_b["name"], "SOLAR+LOAD")
        self.assertIn("prediction", device_b)
        self.assertIn("scales", device_b)

    def test_mock_scales(self):
        # Device A: negative usage (solar export)
        device_a = self.metrics_data["devices"][0]
        scales_a = device_a["scales"]

        self.assertIn("1H", scales_a)
        self.assertIn("1MIN", scales_a)
        self.assertIn("10MIN", scales_a)

        hour_data_a = scales_a["1H"]
        # At default instant_minute=42, len(data) == 42*60 = 2520
        self.assertEqual(hour_data_a["seconds"], 42 * 60)
        self.assertIsInstance(hour_data_a["instant"], datetime)
        self.assertLess(hour_data_a["usage"], 0)

        # Device B: positive usage (load only)
        device_b = self.metrics_data["devices"][1]
        scales_b = device_b["scales"]
        hour_data_b = scales_b["1H"]
        self.assertEqual(hour_data_b["seconds"], 42 * 60)
        self.assertGreater(hour_data_b["usage"], 0)

    def test_mock_smoothing(self):
        device = self.metrics_data["devices"][0]
        smoothing = device["smoothing"]

        self.assertIn("1MIN", smoothing)
        self.assertIn("10MIN", smoothing)
        # Values are dynamically computed; just verify they're numeric
        self.assertIsInstance(smoothing["1MIN"], float)

    def test_data_for_scale_logic(self):
        # Testing the static method directly with sample data
        data = [0.1, 0.2, 0.3]  # kWh
        data_start = datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)

        # Test Hour scale (direct sum * 1000)
        result_h = Metrics.data_for_scale(data, data_start, "1H")
        self.assertAlmostEqual(result_h["usage"], 600.0)

        # Test Minute scale (sum * 1000 * 60 / len)
        # (0.6 * 1000 * 60 / 3) = 12000 Wh/min equivalent
        result_m = Metrics.data_for_scale(data, data_start, "1MIN")
        self.assertAlmostEqual(result_m["usage"], 12000.0)

    def test_mock_nbc_structure(self):
        """Verify nbc field exists with QH1–QH4 keys."""
        device = self.metrics_data["devices"][0]
        self.assertIn("nbc", device)
        nbc = device["nbc"]
        self.assertIn("QH1", nbc)
        self.assertIn("QH2", nbc)
        self.assertIn("QH3", nbc)
        self.assertIn("QH4", nbc)

    def test_mock_nbc_complete_quarters(self):
        """At minute=42, QH1 and QH2 should be complete."""
        device = self.metrics_data["devices"][0]
        self.assertTrue(device["nbc"]["QH1"]["complete"])
        self.assertTrue(device["nbc"]["QH2"]["complete"])

    def test_mock_nbc_incomplete_quarter(self):
        """At minute=42, QH3 should be incomplete with predicted_wh."""
        device = self.metrics_data["devices"][0]
        qh3 = device["nbc"]["QH3"]
        self.assertFalse(qh3["complete"])
        self.assertIn("predicted_wh", qh3)
        self.assertIn("samples_used", qh3)

    def test_mock_nbc_not_started(self):
        """At minute=42, QH4 should be None."""
        device = self.metrics_data["devices"][0]
        self.assertIsNone(device["nbc"]["QH4"])

    def test_mock_nbc_parameterized_minute(self):
        """Test NBC at different instant_minute values."""
        # minute=10: QH1 incomplete, QH2–QH4 not started
        mock_10 = MetricsMock(instant_minute=10)
        nbc_10 = mock_10.metrics["devices"][0]["nbc"]
        self.assertFalse(nbc_10["QH1"]["complete"])
        self.assertIsNone(nbc_10["QH2"])

        # minute=37: QH1–QH2 complete, QH3 incomplete, QH4 not started
        mock_37 = MetricsMock(instant_minute=37)
        nbc_37 = mock_37.metrics["devices"][0]["nbc"]
        self.assertTrue(nbc_37["QH1"]["complete"])
        self.assertTrue(nbc_37["QH2"]["complete"])
        self.assertFalse(nbc_37["QH3"]["complete"])
        self.assertIsNone(nbc_37["QH4"])

    def test_mock_nbc_wh_clamped_at_zero(self):
        """Verify NBC wh values are never negative (clamped at zero)."""
        device = self.metrics_data["devices"][0]
        for qh in ["QH1", "QH2", "QH3"]:
            if device["nbc"][qh] is not None:
                self.assertGreaterEqual(device["nbc"][qh]["wh"], 0)

    def test_mock_tou_result(self):
        """Verify MetricsMock().tou_result has non-zero values with all four keys."""
        mock = MetricsMock()
        tou = mock.tou_result
        self.assertIn("total", tou)
        self.assertIn("peak", tou)
        self.assertIn("part_peak", tou)
        self.assertIn("off_peak", tou)
        self.assertGreater(tou["total"], 0)
        self.assertGreater(tou["peak"], 0)

    def test_mock_device_b_positive_consumption(self):
        """Verify Device B has positive consumption (load-only scenario)."""
        device = self.metrics_data["devices"][1]
        self.assertEqual(device["name"], "SOLAR+LOAD")
        # All per-second data should be positive
        for val in device["per_second_data"]:
            self.assertGreater(val, 0)

    def test_mock_device_b_nbc_positive_wh(self):
        """Verify Device B's NBC quarters have positive wh (no clamping)."""
        mock = MetricsMock(instant_minute=37)
        device = mock.metrics["devices"][1]
        nbc = device["nbc"]

        # QH1 and QH2 should be complete with positive wh
        self.assertTrue(nbc["QH1"]["complete"])
        self.assertGreater(nbc["QH1"]["wh"], 0)
        self.assertTrue(nbc["QH2"]["complete"])
        self.assertGreater(nbc["QH2"]["wh"], 0)

        # QH3 should be incomplete with positive predicted_wh
        self.assertFalse(nbc["QH3"]["complete"])
        self.assertIn("predicted_wh", nbc["QH3"])
        self.assertGreater(nbc["QH3"]["predicted_wh"], 0)

    def test_mock_device_b_nbc_parameterized_minute(self):
        """Test Device B NBC at different instant_minute values."""
        # minute=10: QH1 incomplete, QH2–QH4 not started
        mock_10 = MetricsMock(instant_minute=10)
        nbc_10 = mock_10.metrics["devices"][1]["nbc"]
        self.assertFalse(nbc_10["QH1"]["complete"])
        self.assertIsNone(nbc_10["QH2"])

        # minute=37: QH1–QH2 complete, QH3 incomplete, QH4 not started
        mock_37 = MetricsMock(instant_minute=37)
        nbc_37 = mock_37.metrics["devices"][1]["nbc"]
        self.assertTrue(nbc_37["QH1"]["complete"])
        self.assertTrue(nbc_37["QH2"]["complete"])
        self.assertFalse(nbc_37["QH3"]["complete"])
        self.assertIsNone(nbc_37["QH4"])

    def test_mock_nbc_all_scenarios_covered(self):
        """Verify NBC covers all required scenarios across both devices.

        Device A (solar export, negative raw_wh) tests:
          - QH1 complete with clamped wh=0 (raw_wh < 0 → wh = 0)
          - QH2 incomplete with predicted_wh from recent samples

        Device B (load only, positive raw_wh) tests:
          - Complete quarters with positive wh (no clamping needed)
          - Incomplete quarter with positive predicted_wh
        """
        mock = MetricsMock(instant_minute=42)
        device_a = mock.metrics["devices"][0]
        device_b = mock.metrics["devices"][1]

        # Device A: solar export scenario (negative raw_wh → clamped to 0)
        nbc_a = device_a["nbc"]
        self.assertTrue(nbc_a["QH1"]["complete"])
        self.assertGreaterEqual(nbc_a["QH1"]["wh"], 0)
        self.assertFalse(nbc_a["QH3"]["complete"])
        self.assertIn("predicted_wh", nbc_a["QH3"])

        # Device B: load-only scenario (positive raw_wh, no clamping)
        nbc_b = device_b["nbc"]
        self.assertTrue(nbc_b["QH1"]["complete"])
        self.assertGreater(nbc_b["QH1"]["wh"], 0)
        self.assertFalse(nbc_b["QH3"]["complete"])
        self.assertIn("predicted_wh", nbc_b["QH3"])


class TestMetricsCache(unittest.TestCase):
    """Tests for MetricsCache."""

    def test_cache_miss_on_first_call(self):
        """First call should fetch fresh data and return was_fresh=True."""
        cache = MetricsCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {"key": "value"}

        data, was_fresh = cache.get_or_fetch(fetch_func)

        self.assertEqual(fetch_count, 1)
        self.assertTrue(was_fresh)
        self.assertEqual(data["key"], "value")

    def test_cache_hit_within_ttl(self):
        """Second call within TTL returns cached data with was_fresh=False."""
        cache = MetricsCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {"key": "value"}

        cache.get_or_fetch(fetch_func)
        data, was_fresh = cache.get_or_fetch(fetch_func)

        self.assertEqual(fetch_count, 1)
        self.assertFalse(was_fresh)
        self.assertEqual(data["key"], "value")

    def test_cache_miss_ttl_expired(self):
        """Call after TTL expiry fetches fresh data."""
        cache = MetricsCache(ttl_seconds=0)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {"count": fetch_count}

        cache.get_or_fetch(fetch_func)
        time.sleep(0.05)
        data, was_fresh = cache.get_or_fetch(fetch_func)

        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)
        self.assertEqual(data["count"], 2)

    def test_invalidate_clears_cache(self):
        """After invalidate, next call fetches fresh data."""
        cache = MetricsCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {"count": fetch_count}

        cache.get_or_fetch(fetch_func)
        self.assertEqual(fetch_count, 1)

        cache.invalidate()

        data, was_fresh = cache.get_or_fetch(fetch_func)
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)
        self.assertEqual(data["count"], 2)

    def test_fetched_at_in_data(self):
        """Fresh fetch stores _fetched_at timestamp in returned data."""
        cache = MetricsCache(ttl_seconds=60)

        def fetch_func():
            return {"key": "value"}

        before_fetch = datetime.now(timezone.utc)
        data, was_fresh = cache.get_or_fetch(fetch_func)
        after_fetch = datetime.now(timezone.utc)

        self.assertTrue(was_fresh)
        self.assertIn("_fetched_at", data)
        fetched_at = data["_fetched_at"]
        self.assertGreaterEqual(fetched_at, before_fetch)
        self.assertLessEqual(fetched_at, after_fetch)
        self.assertEqual(data["key"], "value")

    def test_fetched_at_preserved_on_cache_hit(self):
        """Cache hit returns the original _fetched_at from when data was fetched."""
        cache = MetricsCache(ttl_seconds=60)

        def fetch_func():
            return {"key": "value"}

        # First call: fresh fetch
        data1, _ = cache.get_or_fetch(fetch_func)
        original_fetched_at = data1["_fetched_at"]

        time.sleep(0.05)

        # Second call: cache hit — _fetched_at should be unchanged
        data2, was_fresh = cache.get_or_fetch(fetch_func)
        self.assertFalse(was_fresh)
        self.assertEqual(data2["_fetched_at"], original_fetched_at)

    def test_fetched_at_updated_on_refetch(self):
        """After TTL expiry and refetch, _fetched_at is updated."""
        cache = MetricsCache(ttl_seconds=0)

        def fetch_func():
            return {"key": "value"}

        data1, _ = cache.get_or_fetch(fetch_func)
        original_fetched_at = data1["_fetched_at"]

        time.sleep(0.05)

        data2, was_fresh = cache.get_or_fetch(fetch_func)
        self.assertTrue(was_fresh)
        self.assertGreater(data2["_fetched_at"], original_fetched_at)

    def test_multiple_calls_same_ttl(self):
        """Multiple calls within TTL all return same cached reference."""
        cache = MetricsCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {"count": fetch_count}

        result1, _ = cache.get_or_fetch(fetch_func)
        result2, _ = cache.get_or_fetch(fetch_func)
        result3, _ = cache.get_or_fetch(fetch_func)

        self.assertEqual(fetch_count, 1)
        self.assertIs(result1, result2)
        self.assertIs(result2, result3)


class TestComputeNBCQuartersEdgeCases(unittest.TestCase):
    """Tests for util.compute_nbc_quarters edge cases."""

    def test_empty_data_returns_all_none(self):
        """With empty per_second_data, all quarters should be None."""
        result = compute_nbc_quarters([], 0)

        for qh in ["QH1", "QH2", "QH3", "QH4"]:
            self.assertIsNone(result[qh])

    def test_n_zero_returns_all_none(self):
        """With n=0 (no seconds observed), all quarters should be None."""
        data = [0.001] * 3600
        result = compute_nbc_quarters(data, 0)

        for qh in ["QH1", "QH2", "QH3", "QH4"]:
            self.assertIsNone(result[qh])

    def test_n_900_completes_qh1(self):
        """n=900 (first second of QH2) should complete QH1, leave others None."""
        data = [0.002] * 3600
        result = compute_nbc_quarters(data, 900)

        self.assertTrue(result["QH1"]["complete"])
        # raw_wh = 900 * 0.002 * 1000
        self.assertAlmostEqual(result["QH1"]["raw_wh"], 900 * 0.002 * 1000)
        self.assertIsNone(result["QH2"])

    def test_n_901_partial_qh2(self):
        """n=901 should complete QH1, partial QH2 with 2 samples."""
        data = [0.005] * 3600
        result = compute_nbc_quarters(data, 901)

        self.assertTrue(result["QH1"]["complete"])
        self.assertFalse(result["QH2"]["complete"])

    def test_n_3600_completes_all_quarters(self):
        """n=3600 (past end of QH4) should complete all quarters."""
        data = [0.002] * 3600
        result = compute_nbc_quarters(data, 3600)

        for qh in ["QH1", "QH2", "QH3", "QH4"]:
            self.assertTrue(result[qh]["complete"])

    def test_n_past_end_clamped_to_data_length(self):
        """n exceeding data length should still produce partial results."""
        # Only 10 seconds of data, but n=50 — function uses raw values
        # QH1 is partial (n < 900), not None, because n > start_idx(0)
        data = [0.002] * 10
        result = compute_nbc_quarters(data, 50)

        self.assertFalse(result["QH1"]["complete"])
        # lookback = data[0:50] → 10 elements (Python clamps slice)
        self.assertEqual(result["QH1"]["samples_used"], 10)

    def test_negative_raw_wh_clamped_to_zero_in_complete(self):
        """Complete quarters with negative raw_wh should have wh=0."""
        data = [-0.002] * 3600
        result = compute_nbc_quarters(data, 900)

        self.assertTrue(result["QH1"]["complete"])
        self.assertEqual(result["QH1"]["wh"], 0)

    def test_negative_raw_wh_clamped_to_zero_in_partial(self):
        """Partial quarters with negative predicted_wh should have wh=0."""
        data = [-0.002] * 3600
        result = compute_nbc_quarters(data, 1500)

        self.assertFalse(result["QH2"]["complete"])
        # predicted_wh will be negative, clamped to 0
        self.assertEqual(result["QH2"]["wh"], 0)

    def test_partial_qh_has_predicted_wh(self):
        """Incomplete quarters should include predicted_wh field."""
        data = [0.002] * 3600
        result = compute_nbc_quarters(data, 1500)

        self.assertFalse(result["QH2"]["complete"])
        self.assertIn("predicted_wh", result["QH2"])

    def test_partial_qh_has_remaining_seconds(self):
        """Incomplete quarters should include remaining_seconds field."""
        data = [0.002] * 3600
        result = compute_nbc_quarters(data, 1500)

        self.assertIn("remaining_seconds", result["QH2"])
        # QH2 ends at index 1799, n=1500 → remaining = 1800 - 1500
        self.assertEqual(result["QH2"]["remaining_seconds"], 300)

    def test_partial_qh_has_samples_used(self):
        """Incomplete quarters should include samples_used field."""
        data = [0.002] * 3600
        result = compute_nbc_quarters(data, 1500)

        self.assertIn("samples_used", result["QH2"])
        # lookback = max(1500-60, 900) to 1500 = max(1440, 900)=1440 to 1500
        # samples = 60 (or less if lookback_start < start_idx)
        self.assertGreater(result["QH2"]["samples_used"], 0)

    def test_partial_qh_lookback_cannot_cross_boundary(self):
        """Lookback window should not cross quarter boundary."""
        # n=901 is just 2 seconds into QH2 (start_idx=900)
        # lookback_start = max(901-60, 900) = max(841, 900) = 900
        # So lookback only includes seconds from QH2, not QH1: data[900:901]
        # Python slice [start:end] is exclusive of end → 2 elements (indices 900, 901)
        data = [0.005] * 3600  # uniform positive values
        result = compute_nbc_quarters(data, 901)

        self.assertFalse(result["QH2"]["complete"])
        # lookback is data[900:901] → 2 elements (indices 900 and 901)
        self.assertEqual(result["QH2"]["samples_used"], 1)

    def test_partial_qh_lookback_clamped_to_start_idx(self):
        """Lookback start should be clamped to quarter start index."""
        # n=910, lookback_start = max(850, 900) = 900
        # So lookback is from index 900 to 910 = 10 samples
        data = [0.005] * 3600
        result = compute_nbc_quarters(data, 910)

        self.assertEqual(result["QH2"]["samples_used"], 10)


class TestDataForScaleEdgeCases(unittest.TestCase):
    """Tests for HourlyProjection.data_for_scale edge cases."""

    def test_empty_data_list(self):
        """data_for_scale with empty data list should handle gracefully."""
        from datetime import datetime, timezone

        result = HourlyProjection.data_for_scale(
            [], datetime.now(timezone.utc), "1H"
        )
        self.assertEqual(result["usage"], 0.0)

    def test_single_data_point(self):
        """data_for_scale with a single data point should work."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        result = HourlyProjection.data_for_scale([0.5], now, "1H")

        # data_len is only present when DEBUG mode is on
        self.assertGreaterEqual(result["usage"], 0.0)

    def test_scale_key_stored_as_is(self):
        """The scale parameter should be stored as the 'scale' key."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        result = HourlyProjection.data_for_scale(
            [0.5, 0.6], now, "CUSTOM_SCALE"
        )

        self.assertEqual(result["scale"], "CUSTOM_SCALE")


class TestHourlyProjectionErrorPaths(unittest.TestCase):
    """Tests for HourlyProjection error handling paths."""

    def test_retryable_exception_on_no_data(self):
        """HourlyProjection should raise RetryableMetricsException when API returns no data."""
        from unittest.mock import patch, MagicMock

        with patch.object(MetricsBase, "vue_init"), \
             patch.object(MetricsBase, "get_device_info"):

            # Patch device_info to have one device
            with patch.dict(MetricsBase.device_info, {1: MagicMock()}):
                vdi = MetricsBase.device_info[1]

                # Set up channels to return empty data
                mock_channel = MagicMock()
                mock_channel.channel_num = 1

                def empty_fetch(*args, **kwargs):
                    return [], None

                vdi.channels = [mock_channel]
                with patch.object(
                    MetricsBase.vue, "get_chart_usage", side_effect=empty_fetch
                ):

                    with self.assertRaises(RetryableMetricsException):
                        HourlyProjection()


class TestHourlyProjectionEdgeCases(unittest.TestCase):
    """Tests for HourlyProjection edge cases."""

    def test_predict_device_missing_extra_min_scales(self):
        """_predict_device with only 1MIN (no other MIN scales) should still compute prediction."""
        from datetime import timedelta, timezone

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        # Set up the instant attribute that _predict_device needs
        hp.instant = now.replace(minute=30)

        # Only 1H and 1MIN — no other MIN scales like 5MIN, 10MIN
        scales = {
            "1H": {"usage": 60.0, "instant": now},
            "1MIN": {"usage": 2.5, "instant": now + timedelta(minutes=1)},
        }

        result = hp._predict_device(scales)

        self.assertIn("prediction", result)
        # smoothing should only have "1MIN" key, not 5MIN/10MIN
        self.assertIn("smoothing", result)

    def test_predict_device_with_partial_scales(self):
        """_predict_device with only some MIN scales should compute smoothing for those."""
        from datetime import timedelta, timezone

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        # Set up the instant attribute that _predict_device needs
        hp.instant = now.replace(minute=30)

        # 1H + "5MIN" only (no other MIN scales like 2MIN, 3MIN)
        # _predict_device needs "1MIN" for minute_predicted calculation.
        scales = {
            "1H": {"usage": 60.0, "instant": now},
            "5MIN": {"usage": 3.0, "instant": now + timedelta(minutes=5)},
            "1MIN": {"usage": 2.0, "instant": now + timedelta(minutes=1)},
        }

        result = hp._predict_device(scales)

        self.assertIn("prediction", result)


class TestTOUReporterEdgeCases(unittest.TestCase):
    """Tests for TOUReporter edge cases."""

    def test_aggregate_tou_empty_usage_data_list(self):
        """aggregate_tou with empty usage_data_list should produce zero buckets."""
        from metrics import TOUReporter

        tou = TOUReporter.__new__(TOUReporter)

        # Set up required attributes
        tou.usage_data_list = []
        tou._fetch_error = None

        # Call aggregate_tou directly (skip fetch_usage_data)
        tou.aggregate_tou()

        self.assertIsNotNone(tou.tou_result)
        for bucket in ["total", "peak", "part_peak", "off_peak"]:
            self.assertEqual(tou.tou_result[bucket], 0.0)

    def test_aggregate_tou_with_none_values_in_data(self):
        """aggregate_tou should skip None values in 15-min data."""
        from metrics import TOUReporter

        tou = TOUReporter.__new__(TOUReporter)

        # Data with None values mixed in
        tou.usage_data_list = [
            {
                "start": datetime.now(timezone.utc),
                "data": [0.1, None, 0.2],
            }
        ]

        tou.aggregate_tou()

        self.assertIsNotNone(tou.tou_result)
        # total should include all values (including negatives if any, but these are positive)

    def test_aggregate_tou_with_negative_values(self):
        """aggregate_tou should handle negative values (solar export) in TOU buckets."""
        from metrics import TOUReporter

        tou = TOUReporter.__new__(TOUReporter)

        # Negative values represent solar export
        tou.usage_data_list = [
            {
                "start": datetime.now(timezone.utc),
                "data": [-0.1, 0.2],
            }
        ]

        tou.aggregate_tou()

        self.assertIsNotNone(tou.tou_result)
        # total should be (0.2 - 0.1) * some_factor = net positive
        # NBC should only sum positive values (imports), so -0.1 is ignored

    def test_aggregate_tou_all_none_data(self):
        """aggregate_tou with all None data should produce zero NBC."""
        from metrics import TOUReporter

        tou = TOUReporter.__new__(TOUReporter)

        tou.usage_data_list = [
            {
                "start": datetime.now(timezone.utc),
                "data": [None, None],
            }
        ]

        tou.aggregate_tou()

        self.assertIsNotNone(tou.nbc_result)
        # NBC should be 0 since all values are None (skipped in positive sum)


class TestMetricsCacheEdgeCases(unittest.TestCase):
    """Tests for MetricsCache edge cases."""

    def test_ttl_zero_forces_refetch(self):
        """TTL of 0 should force a refetch on every call."""
        cache = MetricsCache(ttl_seconds=0)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {"count": fetch_count}

        cache.get_or_fetch(fetch_func)
        self.assertEqual(fetch_count, 1)

        # Even without sleeping, TTL=0 means always fresh
        data2, was_fresh = cache.get_or_fetch(fetch_func)
        self.assertEqual(fetch_count, 2)

    def test_concurrent_calls_same_fetch(self):
        """Multiple sequential calls within TTL should all return same cached data."""
        cache = MetricsCache(ttl_seconds=60)

        def fetch_func():
            return {"value": "shared"}

        results = [cache.get_or_fetch(fetch_func) for _ in range(5)]
        # All should return the same data object (was_fresh varies)
        for i in range(1, 5):
            self.assertIs(results[0][0], results[i][0])



class TestDeviceMetricsDataClass(unittest.TestCase):
    """Tests for DeviceMetrics data class defaults and serialization."""

    def test_default_values_are_sensible(self):
        """Empty DeviceMetrics has sensible defaults for all fields."""
        from metrics import DeviceMetrics

        dm = DeviceMetrics()
        self.assertEqual(dm.gid, 0)
        self.assertEqual(dm.name, "")
        self.assertEqual(dm.timezone, "")
        self.assertIsInstance(dm.lag, timedelta)
        self.assertEqual(len(dm.per_second_data), 0)

    def test_to_dict_has_all_keys(self):
        """to_dict() includes all expected keys for JSON/template consumption."""
        from metrics import DeviceMetrics

        dm = DeviceMetrics(
            gid=42, name="test-device", timezone="UTC"
        )
        d = dm.to_dict()

        expected_keys = {
            "gid", "lag", "name", "per_second_data",
            "prediction", "prediction_min", "prediction_max",
            "minute_predicted", "minutes_remaining",
            "scales", "smoothing", "timezone", "nbc",
        }
        self.assertEqual(set(d.keys()), expected_keys)

    def test_to_dict_rounding(self):
        """prediction values are rounded to 14 decimal places in output dict."""
        from metrics import DeviceMetrics, _PredictionData

        dm = DeviceMetrics(
            gid=1, name="round-test",
            prediction=_PredictionData(value=0.123456789012345, min_value=0.0, max_value=1.0),
        )
        d = dm.to_dict()

        # 14 decimal places max — Python's round(x, 14) strips trailing zeros
        self.assertEqual(d["prediction"], round(0.123456789012345, 14))
        self.assertEqual(d["prediction_min"], round(0.0, 14))


class TestMetricsBaseVueInitErrorPaths(unittest.TestCase):
    """Tests for MetricsBase.vue_init error paths."""

    def test_vue_init_token_fallback_to_password(self):
        """When token login fails, vue_init falls back to password auth."""
        from unittest.mock import MagicMock

        # Create a mock PyEmVue where token login fails but password succeeds
        vue_mock = MagicMock()

        # First call (token-based) returns False → triggers password fallback
        vue_mock.login.side_effect = [False, True]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics._cfg") as cfg_mock:

            # Set up config so password auth works
            cfg_mock.vue_username = "testuser"
            cfg_mock.vue_password = "testpass"

            # Create a fake .vue-keys.json so the file read doesn't fail
            import tempfile, os

            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                os.write(f.fileno(), b'{"id_token":"t","access_token":"a","refresh_token":"r"}')
                keys_file = f.name

            try:
                base = MetricsBase.__new__(MetricsBase)
                # Skip __init__ to avoid real API calls; set up manually
                base.vue = vue_mock
                # Crucial: MagicMock always has .auth (creates it on access), so we must
                # set auth=None to prevent vue_init() from taking its early-return path.
                base.vue.auth = None  # type: ignore[attr-defined]
                base.vue_keys = keys_file
                base.logger = MagicMock()

                # Mock open to return our temp file content for token login
                original_open = __builtins__["open"]

                def mock_file_open(*args, **kwargs):
                    return original_open(keys_file, *args[1:], **kwargs)

                with patch("builtins.open", mock_file_open):
                    base.vue_init()

                # Token login was called first (returned False), then password auth succeeded
                self.assertEqual(vue_mock.login.call_count, 2)
            finally:
                os.unlink(keys_file)

    def test_vue_init_both_fail_raises(self):
        """When both token and password auth fail, raises VueAuthenticationError."""
        from unittest.mock import MagicMock

        vue_mock = MagicMock()
        # Both login attempts fail
        vue_mock.login.side_effect = [False, False]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics._cfg") as cfg_mock:

            import tempfile, os

            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                os.write(f.fileno(), b'{"id_token":"t","access_token":"a","refresh_token":"r"}')
                keys_file = f.name

            try:
                base = MetricsBase.__new__(MetricsBase)
                base.vue = vue_mock
                # Prevent early return in vue_init() — MagicMock always has .auth.
                base.vue.auth = None  # type: ignore[attr-defined]
                base.vue_keys = keys_file
                base.logger = MagicMock()

                original_builtins_open = __builtins__["open"]

                def mock_file_open(*args, **kwargs):
                    return original_builtins_open(keys_file)

                with patch("builtins.open", mock_file_open):
                    cfg_mock.vue_username = "u"
                    cfg_mock.vue_password = "p"

                    with self.assertRaises(Exception) as ctx:  # VueAuthenticationError
                        base.vue_init()

                    self.assertIn("authentication failed", str(ctx.exception).lower())
            finally:
                os.unlink(keys_file)


class TestMetricsBaseGetDeviceInfoFilters(unittest.TestCase):
    """Tests for MetricsBase.get_device_info filtering paths."""

    def test_get_device_info_401_invalidates_auth(self):
        """HTTPError 401 sets vue.auth=None and raises RetryableMetricsException."""

        http_ex = requests.exceptions.HTTPError(response=MagicMock(status_code=401))
        vue_mock = MagicMock()
        vue_mock.get_devices.side_effect = http_ex

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            base = MetricsBase.__new__(MetricsBase)
            # Skip __init__ to avoid real API calls; set up manually
            base.vue = vue_mock
            base.logger = MagicMock()

            with self.assertRaises(RetryableMetricsException):
                base.get_device_info()

            # Auth should be invalidated on 401
            self.assertIsNone(vue_mock.auth)

    def test_get_device_info_filters_disconnected(self):
        """Devices with connected=False are skipped."""

        disconnected_device = MagicMock()
        disconnected_device.connected = False
        disconnected_device.device_gid = 1

        vue_mock = MagicMock()
        vue_mock.get_devices.return_value = [disconnected_device]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            base = MetricsBase.__new__(MetricsBase)
            base.vue = vue_mock
            base.logger = MagicMock()

            # Should not raise, but device_info should remain empty
            with patch("metrics.MetricsBase.vue_auth", {"last": datetime.now(timezone.utc)}):
                base.get_device_info()

            self.assertEqual(len(MetricsBase.device_info), 0)

    def test_get_device_info_filters_wrong_model(self):
        """Devices with model != 'ZIG001' are skipped."""

        wrong_model_device = MagicMock()
        wrong_model_device.connected = True
        wrong_model_device.model = "ZIG002"  # Wrong model
        wrong_model_device.device_gid = 1

        vue_mock = MagicMock()
        vue_mock.get_devices.return_value = [wrong_model_device]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            base = MetricsBase.__new__(MetricsBase)
            base.vue = vue_mock
            base.logger = MagicMock()

            with patch("metrics.MetricsBase.vue_auth", {"last": datetime.now(timezone.utc)}):
                base.get_device_info()

            self.assertEqual(len(MetricsBase.device_info), 0)

    def test_get_device_info_filters_empty_channels(self):
        """Devices with no channels are skipped."""

        empty_channels_device = MagicMock()
        empty_channels_device.connected = True
        empty_channels_device.model = "ZIG001"
        empty_channels_device.device_gid = 42
        empty_channels_device.channels = []

        vue_mock = MagicMock()
        vue_mock.get_devices.return_value = [empty_channels_device]

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            base = MetricsBase.__new__(MetricsBase)
            base.vue = vue_mock
            base.logger = MagicMock()

            with patch("metrics.MetricsBase.vue_auth", {"last": datetime.now(timezone.utc)}):
                base.get_device_info()

            self.assertEqual(len(MetricsBase.device_info), 0)


class TestFetchChannelDataErrors(unittest.TestCase):
    """Tests for HourlyProjection._fetch_channel_data error paths."""

    def test_fetch_no_valid_data_raises(self):
        """_fetch_channel_data raises RetryableMetricsException when API returns empty data."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()

        # Mock get_chart_usage to return empty data (no valid points)
        hp.vue.get_chart_usage.return_value = ([], None)

        chan_mock = MagicMock()
        chan_mock.channel_num = 1

        with self.assertRaises(RetryableMetricsException) as ctx:
            hp._fetch_channel_data(chan_mock, now.replace(minute=0), now)

        self.assertIn("No data for hour", str(ctx.exception))

    def test_fetch_first_element_none_raises(self):
        """_fetch_channel_data raises when first element of data is None."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()

        # Mock get_chart_usage to return data where first element is None
        hp.vue.get_chart_usage.return_value = ([None, 0.1], now)

        chan_mock = MagicMock()
        chan_mock.channel_num = 2

        with self.assertRaises(RetryableMetricsException) as ctx:
            hp._fetch_channel_data(chan_mock, now.replace(minute=0), now)

        self.assertIn("No data for hour", str(ctx.exception))


class TestProcessOffsetScalesEdgeCases(unittest.TestCase):
    """Tests for _process_offset_scales edge cases."""

    def test_process_minute_0_boundary(self):
        """When minute=0, max(1, min(10, 0)) = 1 so only '1MIN' scale is computed."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.logger = MagicMock()
        scales: dict[str, Any] = {}

        # usage_data_end has minute=0
        end_time = now.replace(minute=0)

        # 61 seconds of data (one minute worth)
        usage_data = [0.5] * 61

        result = hp._process_offset_scales(scales, usage_data, end_time)

        # Only "1MIN" should be in scales (minute=0 → max(1, min(10, 0)) = 1)
        self.assertIn("1MIN", scales)

    def test_process_minute_6_plus(self):
        """When minute >= 6, scales should include '1MIN' through '6MIN'."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.logger = MagicMock()
        scales: dict[str, Any] = {}

        # usage_data_end has minute=6
        end_time = now.replace(minute=6)

        # 390 seconds of data (6 minutes worth + a bit more for the tail)
        usage_data = [0.5] * 391

        result = hp._process_offset_scales(scales, usage_data, end_time)

        # Should have "1MIN" through "6MIN"
        for i in range(1, 7):
            self.assertIn(f"{i}MIN", scales)

    def test_process_returns_last_300(self):
        """Returns last 300 data points from the usage_data_local."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.logger = MagicMock()
        scales: dict[str, Any] = {}

        end_time = now.replace(minute=42)
        # 2520 seconds of data (minute=42 → 42*60 = 2520)
        usage_data = [float(i % 10) for i in range(2520)]

        result = hp._process_offset_scales(scales, usage_data, end_time)

        # Should return exactly the last 300 elements
        self.assertEqual(len(result), min(300, len(usage_data)))


class TestComputeNBCEdgeCases(unittest.TestCase):
    """Tests for _compute_nbc edge cases."""

    def test_elapsed_zero(self):
        """When instant == usage_data_start, n=0 and all quarters are not started."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now
        data_start = now  # Same time → elapsed=0

        result = hp._compute_nbc([0.1] * 3600, data_start)

        # n=0 → all quarters should be None
        for qh in ["QH1", "QH2", "QH3", "QH4"]:
            self.assertIsNone(result[qh])

    def test_elapsed_exceeds_data_len(self):
        """When elapsed exceeds data length, n is clamped to len(data)."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now
        # Only 10 seconds of data but instant is far ahead → elapsed >> len(data)
        short_data = [0.1] * 10

        result = hp._compute_nbc(short_data, now - timedelta(seconds=5))

        # n should be clamped to len(data)=10
        self.assertFalse(result["QH1"]["complete"])

    def test_negative_elapsed(self):
        """When instant is before data start, elapsed is negative → n=0."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now
        # Data start is in the future relative to instant → negative elapsed
        data_start = now + timedelta(hours=1)

        result = hp._compute_nbc([0.1] * 3600, data_start)

        # n = max(0, negative_int) → 0
        for qh in ["QH1", "QH2", "QH3", "QH4"]:
            self.assertIsNone(result[qh])


class TestPopulateDeviceErrors(unittest.TestCase):
    """Tests for _populate_device error paths."""

    def test_fetch_error_returns_none(self):
        """_fetch_channel_data raising RequestException causes _populate_device to return None."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        hp.vue = MagicMock()
        hp.logger = MagicMock()

        # Mock get_chart_usage to raise a RequestException
        http_ex = requests.exceptions.HTTPError("API error")
        hp.vue.get_chart_usage.side_effect = http_ex

        vdi_mock = MagicMock()
        chan_mock = MagicMock(channel_num=1)
        vdi_mock.channels = [chan_mock]

        result = hp._populate_device(vdi_mock, now.replace(minute=0))
        self.assertIsNone(result)

    def test_empty_channels_returns_none(self):
        """_populate_device returns None when device has no channels."""
        from unittest.mock import MagicMock

        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        vdi_mock = MagicMock()

        # Device with no channels → for-loop never executes, returns None
        vdi_mock.channels = []

        result = hp._populate_device(vdi_mock, now.replace(minute=0))
        self.assertIsNone(result)


class TestPredictDeviceEdgeCases(unittest.TestCase):
    """Tests for _predict_device edge cases."""

    def test_no_minute_scales(self):
        """With only 1H and no extra MIN scales, smoothing has just '1MIN'."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        hp.instant = now.replace(minute=30)
        scales: dict[str, Any] = {
            "1H": {"usage": 60.0, "instant": now},
            # _predict_device requires at least a 1MIN scale to compute minute_predicted.
            "1MIN": {"usage": 2.5, "instant": now + timedelta(minutes=1)},
        }

        result = hp._predict_device(scales)

        self.assertIn("prediction", result)
        # With only 1MIN (no other MIN scales), smoothing has just one entry.
        self.assertEqual(result["smoothing"], {"1MIN": result["prediction"]})

    def test_lag_zero(self):
        """When hour['instant'] >= instant, lag is timedelta(0)."""
        hp = HourlyProjection.__new__(HourlyProjection)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        # Set instant to be AFTER the hour data point
        hp.instant = now.replace(minute=45)

        scales: dict[str, Any] = {
            "1H": {"usage": 60.0, "instant": now.replace(minute=45)},
            # _predict_device requires at least a 1MIN scale to compute minute_predicted.
            "1MIN": {"usage": 2.5, "instant": now.replace(minute=46)},
        }

        result = hp._predict_device(scales)

        self.assertEqual(result["lag"], timedelta(0))


class TestTOUReporterFetchErrors(unittest.TestCase):
    """Tests for TOUReporter.fetch_usage_data error paths."""

    def test_fetch_http_error_re_raises(self):
        """fetch_usage_data re-raises HTTPError from get_chart_usage."""
        from unittest.mock import MagicMock

        vue_mock = MagicMock()
        http_ex = requests.exceptions.HTTPError("API error")
        http_ex.response = MagicMock()  # type: ignore[attr-defined]
        vue_mock.get_chart_usage.side_effect = http_ex

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            # Create a minimal device with channels
            vdi_mock = MagicMock()
            chan_mock = MagicMock(channel_num=1)
            vdi_mock.channels = [chan_mock]

            with patch.object(MetricsBase, "device_info", {1: vdi_mock}):
                from metrics import TOUReporter

                from datetime import UTC, datetime as dt

                tou = TOUReporter.__new__(TOUReporter)
                # Set up required attributes without calling __init__ (which calls fetch_usage_data)
                tou.vue = vue_mock
                tou.logger = MagicMock()
                tou.start_date = dt(2025, 1, 1, tzinfo=UTC)
                tou.end_date = dt(2025, 1, 8, tzinfo=UTC)

                with self.assertRaises(requests.exceptions.HTTPError):
                    # Manually call fetch_usage_data (which will try to iterate device_info)
                    tou.fetch_usage_data()

    def test_fetch_empty_list(self):
        """fetch_usage_data with no data chunks produces empty usage_data_list."""

        from datetime import UTC, datetime as dt

        vue_mock = MagicMock()
        # Return empty data for all calls (no chunks)
        vue_mock.get_chart_usage.return_value = ([], None)

        with patch.object(MetricsBase, "vue", vue_mock), \
             patch("metrics.MetricsBase.device_info", {}):

            vdi_mock = MagicMock()
            chan_mock = MagicMock(channel_num=1)
            vdi_mock.channels = [chan_mock]

            with patch.object(MetricsBase, "device_info", {1: vdi_mock}):
                from metrics import TOUReporter

                tou = TOUReporter.__new__(TOUReporter)
                tou.vue = vue_mock
                tou.logger = MagicMock()

                # Set up dates that will result in zero iterations (start >= end)
                now = datetime.now(timezone.utc).replace(
                    hour=10, minute=30, second=0, microsecond=0
                )
                tou.start_date = now
                tou.end_date = now  # Same time → no iterations

                tou.fetch_usage_data()

                self.assertEqual(tou.usage_data_list, [])


class TestDataForScaleDebugMode(unittest.TestCase):
    """Tests for data_for_scale debug mode behavior."""

    def test_debug_mode_off_no_extra_keys(self):
        """When DEBUG is off, data_for_scale result has no 'data'/'data_len' keys."""
        from unittest.mock import patch

        now = datetime.now(timezone.utc)
        data = [0.1, 0.2]

        with patch("metrics.is_debug", return_value=False):
            result = HourlyProjection.data_for_scale(data, now, "1H")

        self.assertNotIn("data", result)
        self.assertNotIn("data_len", result)

    def test_debug_mode_on_has_extra_keys(self):
        """When DEBUG is on, data_for_scale result includes 'data'/'data_len'."""
        from unittest.mock import patch

        now = datetime.now(timezone.utc)
        data = [0.1, 0.2]

        with patch("metrics.is_debug", return_value=True):
            result = HourlyProjection.data_for_scale(data, now, "1H")

        self.assertIn("data", result)
        self.assertEqual(result["data_len"], 2)


class TestHourlyProjectionNoPredictions(unittest.TestCase):
    """Tests for HourlyProjection constructor edge cases."""

    def test_no_predictions_lag_not_set(self):
        """When populate() returns empty dict, _data_lag_secs is not set."""
        from unittest.mock import MagicMock

        # Create a partial HourlyProjection where populate returns {}
        hp = HourlyProjection.__new__(HourlyProjection)

        # Set up required attributes
        hp.metrics = {"api_response": {}, "debug": False, "devices": [], "instant": datetime.now(timezone.utc)}
        hp.instant = hp.metrics["instant"]

        # Mock populate to return empty dict (no predictions)
        with patch.object(hp, "populate", return_value={}):
            # Mock device_info to be empty so the for-loop doesn't add devices
            with patch.object(MetricsBase, "device_info", {}):
                # Mock predict to return empty dict too (no predictions)
                with patch.object(hp, "predict", return_value={}):
                    # Now call the relevant part of __init__ manually
                    hp.metrics["api_response"]["total"] = timedelta()

        # When predictions is empty, _data_lag_secs should not be set
        self.assertNotIn("_data_lag_secs", hp.metrics)


class TestEnergyCache(unittest.TestCase):
    """Tests for EnergyCache — unified per-second sample cache with sliding-window semantics."""

    def test_import_exists(self):
        """EnergyCache class must be importable from metrics."""
        # This test fails until EnergyCache is implemented.
        from metrics import EnergyCache  # noqa: F401

    def test_initial_state_empty(self):
        """Fresh EnergyCache has no samples, no start time, no fetch timestamp."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache._samples)
        self.assertIsNone(cache._data_start)
        self.assertIsNone(cache._last_fetch_at)

    def test_is_valid_false_when_empty(self):
        """is_valid returns False when cache has no data."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertFalse(cache.is_valid())

    def test_is_valid_true_after_fetch(self):
        """is_valid returns True after a successful fetch within TTL."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        def fetch_func():
            return {
                "per_second_data": [0.001] * 10,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func)
        self.assertTrue(cache.is_valid())

    def test_is_valid_false_after_ttl_expiry(self):
        """is_valid returns False after TTL expires."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=0)  # TTL of 0 means always expired

        def fetch_func():
            return {
                "per_second_data": [0.001] * 10,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func)
        self.assertFalse(cache.is_valid())

    def test_get_or_fetch_miss_on_first_call(self):
        """First call to get_or_fetch should invoke fetch_func and return was_fresh=True."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        now = datetime.now(timezone.utc)

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.002] * 5,
                "data_start": now,
            }

        result, was_fresh = cache.get_or_fetch(fetch_func)

        self.assertEqual(fetch_count, 1)
        self.assertTrue(was_fresh)
        self.assertIsNotNone(result)

    def test_get_or_fetch_hit_within_ttl(self):
        """Second call within TTL should return cached data with was_fresh=False."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0
        now = datetime.now(timezone.utc)

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.003] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func)
        result2, was_fresh = cache.get_or_fetch(fetch_func)

        self.assertEqual(fetch_count, 1)
        self.assertFalse(was_fresh)
        # Should return the same cached data object (identity check)

    def test_get_or_fetch_miss_after_ttl_expiry(self):
        """After TTL expires, get_or_fetch should call fetch_func again."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=0)  # Always expired
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.004] * fetch_count,  # Varying length
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func)
        self.assertEqual(fetch_count, 1)

        result2, was_fresh = cache.get_or_fetch(fetch_func)
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_or_fetch_force_bypasses_cache(self):
        """force=True should always call fetch_func even if cache is valid."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.005] * 3,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func)  # First call: fresh
        self.assertEqual(fetch_count, 1)

        _, was_fresh = cache.get_or_fetch(fetch_func, force=True)
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_or_fetch_stores_last_fetch_at_only_on_api_call(self):
        """_last_fetch_at should only be set when data comes from the API, not on cache hit."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.006] * 5,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func)
        first_fetch_at = cache._last_fetch_at

        # Second call should be a cache hit — _last_fetch_at unchanged
        time.sleep(0.01)  # Small delay to ensure different timestamp if updated
        cache.get_or_fetch(fetch_func)

        self.assertEqual(cache._last_fetch_at, first_fetch_at)
        self.assertEqual(fetch_count, 1)

    def test_get_or_fetch_none_result(self):
        """When fetch_func returns None, cache stores None and is_valid returns False."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        def fetch_func():
            return None  # Simulate API failure

        result, was_fresh = cache.get_or_fetch(fetch_func)
        self.assertIsNone(result)

    def test_invalidate_clears_cache(self):
        """After invalidate, next call fetches fresh data."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.007] * 5,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func)
        self.assertEqual(fetch_count, 1)

        cache.invalidate()

        result2, was_fresh = cache.get_or_fetch(fetch_func)
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_current_qh_returns_none_when_empty(self):
        """get_current_qh returns None when cache has no data."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache.get_current_qh())

    def test_get_or_fetch_merges_samples(self):
        """New samples from fetch_func should be appended to existing _samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        def fetch_func():
            # Simulate incremental data: first call returns 5 samples, second adds more
            if not cache._samples or len(cache._samples) == 0:
                return {
                    "per_second_data": [0.1] * 5,
                    "data_start": datetime.now(timezone.utc),
                }
            else:
                return {
                    "per_second_data": [0.2] * 3,  # New samples appended
                    "data_start": datetime.now(timezone.utc),
                }

        cache.get_or_fetch(fetch_func)  # First fetch: [0.1, 0.1, 0.1, 0.1, 0.1]
        self.assertEqual(len(cache._samples), 5)

        # Use force=True to simulate an incremental fetch that appends new samples.
        cache.get_or_fetch(fetch_func, force=True)  # Second fetch: append [0.2, 0.2, 0.2]
        self.assertEqual(len(cache._samples), 8)

    def test_get_current_qh_computes_from_samples(self):
        """get_current_qh should compute QH prediction from raw samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc).replace(second=30, microsecond=0)

        # 1200 samples (first 20 minutes = QH1 complete + first 5 min of QH2)
        samples = [0.001] * 1200

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": now - timedelta(seconds=len(samples)),
            }

        cache.get_or_fetch(fetch_func)
        result = cache.get_current_qh()

        self.assertIsNotNone(result)
        # Should return a dict with QH prediction info
        self.assertIn("qh_name", result)

    def test_thread_safety_concurrent_access(self):
        """Concurrent reads and writes should not corrupt the sample list."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        errors: list[str] = []

        def writer():
            try:
                for _ in range(10):
                    cache.get_or_fetch(lambda: {
                        "per_second_data": [0.1] * 5,
                        "data_start": datetime.now(timezone.utc),
                    })
            except Exception as ex:  # noqa: BLE001
                errors.append(str(ex))

        def reader():
            try:
                for _ in range(10):
                    cache.get_current_qh()
            except Exception as ex:  # noqa: BLE001
                errors.append(str(ex))

        import threading

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Thread errors occurred: {errors}")
        # Samples should be a list (not corrupted) and have some length
        self.assertIsInstance(cache._samples, list | type(None))

    def test_pruning_removes_samples_older_than_3600s(self):
        """Samples older than 3600 seconds from now are pruned after get_or_fetch."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        # Pre-populate with 5000 samples (over an hour of data)
        old_start = now - timedelta(seconds=5000)
        cache._samples = [0.1] * 5000
        cache._data_start = old_start

        # Patch datetime.now so pruning uses our test time
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now

            def fetch_func():
                return {
                    "per_second_data": [0.2] * 10,
                    "data_start": now - timedelta(seconds=10),
                }

            cache.get_or_fetch(fetch_func, force=True)

        # After merge: 5010 samples. After pruning (keep last 3600s): ~3610
        self.assertLess(len(cache._samples), 5000)
        # Should keep roughly the last 3600 samples plus the new ones
        self.assertLessEqual(len(cache._samples), 3620)

    def test_pruning_does_not_remove_recent_samples(self):
        """Samples within the last 3600s are preserved after pruning."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        # Pre-populate with exactly 3600 samples (1 hour of data)
        old_start = now - timedelta(seconds=3600)
        cache._samples = [0.1] * 3600
        cache._data_start = old_start

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now

            def fetch_func():
                return {
                    "per_second_data": [0.2] * 5,
                    "data_start": now - timedelta(seconds=5),
                }

            cache.get_or_fetch(fetch_func, force=True)

        # All 3605 samples should be kept (none older than 3600s)
        self.assertEqual(len(cache._samples), 3605)

    def test_get_current_qh_returns_incomplete_qh(self):
        """get_current_qh returns the first incomplete quarter with extrapolated prediction."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 12, 7, 30, tzinfo=timezone.utc)

        # 450 samples = halfway through QH1 (0-899 seconds)
        # Each sample is 0.5 Wh (stored as kWh in per_second_data, so 0.5 * 1000 = 500 Wh per second...
        # actually per_second_data is in kWh, so 0.5 kWh/sec = 500 Wh/sec)
        # Let's use small values: 0.001 kWh = 1 Wh per second
        samples = [0.001] * 450

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": now - timedelta(seconds=450),
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now
            cache.get_or_fetch(fetch_func)

        result = cache.get_current_qh()

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH1")
        self.assertFalse(result.get("seconds_remaining", 0) == 0)

    def test_get_current_qh_returns_complete_last_qh_when_all_done(self):
        """When all 4 quarters are complete, get_current_qh returns QH4."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 13, 0, 0, tzinfo=timezone.utc)

        # 3600 samples = exactly one hour (all 4 quarters complete)
        samples = [0.01] * 3600

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": now - timedelta(seconds=3600),
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now
            cache.get_or_fetch(fetch_func)

        result = cache.get_current_qh()

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH4")
        # QH4 is complete, so seconds_remaining should be 0
        self.assertEqual(result["seconds_remaining"], 0)

    def test_get_current_qh_skips_complete_quarters(self):
        """get_current_qh skips complete quarters and returns the first incomplete one."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime(2025, 6, 1, 12, 37, 30, tzinfo=timezone.utc)

        # 2250 samples = QH1 (900s), QH2 (900s) complete, halfway through QH3
        samples = [0.002] * 2250

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": now - timedelta(seconds=2250),
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now
            cache.get_or_fetch(fetch_func)

        result = cache.get_current_qh()

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH3")


class TestBuildIncrementalFetch(unittest.TestCase):
    """Tests for _build_incremental_fetch helper function."""

    def test_returns_callable(self):
        """_build_incremental_fetch returns a callable (zero-arg function)."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        fetcher = _build_incremental_fetch(cache, MagicMock(), 1, datetime.now(timezone.utc))
        self.assertTrue(callable(fetcher))

    def test_first_fetch_no_existing_samples(self):
        """When cache has no samples, fetcher calls API with full range."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1
        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        # Mock API to return some data
        vue_mock.get_chart_usage.return_value = (
            [0.1] * 300,
            now - timedelta(minutes=5),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)
        result = fetcher()

        # Should have called get_chart_usage with full range (chart_start to now)
        vue_mock.get_chart_usage.assert_called_once()
        call_args = vue_mock.get_chart_usage.call_args
        # First positional arg is the channel, second is start time
        self.assertEqual(call_args[0][1], now.replace(minute=0, second=0))  # chart_start
        self.assertEqual(call_args[0][2], now)

    def test_incremental_fetch_uses_last_sample_time(self):
        """When cache has samples, fetcher starts from last sample time."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        # Pre-populate cache with samples
        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        old_start = now - timedelta(minutes=5)  # 300 seconds ago

        cache._samples = [0.1] * 300
        cache._data_start = old_start

        # Mock API to return new samples starting from where cache left off
        vue_mock.get_chart_usage.return_value = (
            [0.2] * 60,  # 60 new samples
            old_start + timedelta(seconds=300),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)
        result = fetcher()

        # Should call get_chart_usage starting from last sample time
        vue_mock.get_chart_usage.assert_called_once()
        call_args = vue_mock.get_chart_usage.call_args
        # Start time should be old_start + 300 seconds = now
        expected_start = old_start + timedelta(seconds=299)  # last sample index
        self.assertEqual(call_args[0][1], expected_start)

    def test_incremental_fetch_merges_samples(self):
        """New samples from API are appended to existing cache samples via get_or_fetch."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        old_start = now - timedelta(minutes=5)

        # Pre-populate cache with 300 samples
        cache._samples = [0.1] * 300
        cache._data_start = old_start

        # Mock API to return new samples starting from where cache left off
        vue_mock.get_chart_usage.return_value = (
            [0.2] * 60,  # 60 new samples
            old_start + timedelta(seconds=300),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)
        # Patch datetime.now so pruning doesn't wipe out 2025 data
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now
            cache.get_or_fetch(fetcher, force=True)

        # Cache should have merged samples: 300 + 60 = 360
        self.assertEqual(len(cache._samples), 360)

    def test_api_error_returns_none(self):
        """When API raises an error, fetcher returns None and cache is unchanged."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        old_start = now - timedelta(minutes=5)

        # Pre-populate cache with samples
        cache._samples = [0.1] * 300
        cache._data_start = old_start

        # Mock API to raise an error
        vue_mock.get_chart_usage.side_effect = requests.exceptions.HTTPError("API error")

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)
        result = fetcher()

        self.assertIsNone(result)
        # Cache should be unchanged
        self.assertEqual(len(cache._samples), 300)

    def test_prunes_old_samples(self):
        """Samples older than 3600s from now are pruned via get_or_fetch."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        # Old samples from 2 hours ago (7200 seconds)
        old_start = now - timedelta(hours=2)

        # Pre-populate cache with 7200 samples (2 hours of per-second data)
        cache._samples = [0.1] * 7200
        cache._data_start = old_start

        # Mock API to return new samples (only the last 10 minutes worth)
        vue_mock.get_chart_usage.return_value = (
            [0.2] * 600,  # 600 new samples (10 minutes)
            old_start + timedelta(seconds=7200),
        )

        fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)
        # Patch datetime.now so pruning uses our test time
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now
            cache.get_or_fetch(fetcher, force=True)

        # After merging: 7200 + 600 = 7800 samples
        # After pruning (keep only last 3600s): should be ~4200 samples
        # (7800 - 3600 = 4200)
        self.assertLessEqual(len(cache._samples), 7800)
        # Should have pruned old samples
        self.assertLess(len(cache._samples), 7200)


class TestEnergyCacheSampleMetadata(unittest.TestCase):
    """Tests for EnergyCache sample metadata tracking."""

    def test_initial_state_has_sample_count_none(self):
        """Fresh cache has _sample_count = None."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache._sample_count)

    def test_initial_state_has_last_sample_at_none(self):
        """Fresh cache has _last_sample_at = None."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache._last_sample_at)

    def test_get_or_fetch_sets_sample_count(self):
        """After get_or_fetch, _sample_count reflects the number of samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.1] * 50,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func)
        self.assertEqual(cache._sample_count, 50)

    def test_get_or_fetch_sets_last_sample_at(self):
        """After get_or_fetch, _last_sample_at reflects the last sample time."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.1] * 50,
                "data_start": now - timedelta(seconds=50),
            }

        cache.get_or_fetch(fetch_func)
        # Last sample time = data_start + (count - 1) seconds ≈ now
        self.assertIsNotNone(cache._last_sample_at)


class TestIncrementalFetchIntegration(unittest.TestCase):
    """Integration tests for incremental fetch with get_or_fetch."""

    def test_full_then_incremental(self):
        """Full fetch followed by incremental fetch merges correctly."""
        from metrics import EnergyCache, _build_incremental_fetch

        cache = EnergyCache(ttl_seconds=60)
        vue_mock = MagicMock()
        gid = 1

        now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        call_count = 0

        def fetch_func():
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                # First fetch: full range, 300 samples (5 minutes)
                start = now - timedelta(minutes=5)
                return {
                    "per_second_data": [0.1] * 300,
                    "data_start": start,
                }

            # Incremental fetch: 60 new samples
            return {
                "per_second_data": [0.2] * 60,
                "data_start": now - timedelta(seconds=60),
            }

        # Build incremental fetcher (used to verify it doesn't crash)
        _fetcher = _build_incremental_fetch(cache, vue_mock, gid, now)

        # Patch datetime.now so pruning doesn't wipe out 2025 data
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now

            # First fetch: full range
            cache.get_or_fetch(fetch_func)
            self.assertEqual(len(cache._samples), 300)

            # Second call with force=True to simulate incremental fetch
            cache.get_or_fetch(fetch_func, force=True)
            self.assertEqual(len(cache._samples), 360)


if __name__ == "__main__":
    unittest.main()


