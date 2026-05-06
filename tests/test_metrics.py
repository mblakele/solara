import time
import unittest
from datetime import datetime, timedelta, timezone
from metrics import Metrics, MetricsCache, RetryableMetricsException
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


if __name__ == "__main__":
    unittest.main()
