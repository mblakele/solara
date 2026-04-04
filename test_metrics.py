import unittest
from datetime import datetime, timezone, timedelta
from metrics import Metrics, MetricsMock, RetryableMetricsException


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
        # This should NOT raise AttributeError: 'PartialTOU' object has no attribute 'EnergyDataAggregator'
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
        self.assertEqual(len(devices), 1)
        device = devices[0]

        self.assertEqual(device["gid"], 12345)
        self.assertEqual(device["name"], "MOCK")
        self.assertEqual(device["timezone"], "America/Los_Angeles")
        self.assertIn("prediction", device)
        self.assertIn("scales", device)
        self.assertIn("smoothing", device)

    def test_mock_scales(self):
        device = self.metrics_data["devices"][0]
        scales = device["scales"]

        self.assertIn("1H", scales)
        self.assertIn("1MIN", scales)
        self.assertIn("10MIN", scales)

        hour_data = scales["1H"]
        self.assertEqual(hour_data["seconds"], 2552)
        self.assertAlmostEqual(hour_data["usage"], 415.91752700753847)
        self.assertIsInstance(hour_data["instant"], datetime)

    def test_mock_smoothing(self):
        device = self.metrics_data["devices"][0]
        smoothing = device["smoothing"]

        self.assertIn("1MIN", smoothing)
        self.assertIn("10MIN", smoothing)
        self.assertEqual(smoothing["1MIN"], -52.516668090260964)

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


if __name__ == "__main__":
    unittest.main()
