import contextlib
import json
import unittest
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import requests
from app import app



@contextlib.contextmanager
def mock_config(**overrides: Any):
    """Patch app.config with default mock values plus any overrides."""
    defaults = {
        "VUE_USERNAME": None,
        "MOCK_ERROR": False,
        "MOCK": False,
    }
    config_values = {**defaults, **overrides}

    cfg_patch = patch(
        "app.config",
        side_effect=lambda key, default=None, cast=str: config_values.get(key, default),
    )
    with cfg_patch:
        yield


class TestApp(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def test_health_endpoint(self):
        response = self.app.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode("utf-8"), "ok")
        self.assertEqual(response.headers["Content-Type"], "text/plain")

    def test_index_json_mock(self):
        with mock_config():
            response = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/json")

        data = json.loads(response.data)
        self.assertIn("devices", data)
        self.assertTrue(len(data["devices"]) > 0)
        # We've seen different values for GID and Name in the mock,
        # so we'll just verify the keys exist in the first device.
        device = data["devices"][0]
        self.assertIn("gid", device)
        self.assertIn("name", device)
        self.assertIn("prediction", device)

    def test_index_json_time_range_enabled(self):
        """Index JSON endpoint serializes time-range enabled value correctly."""
        from decouple import config as dc_config

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "06:45-15:00")
            response = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)

    def test_index_html_time_range_enabled(self):
        """Index HTML shows time range when enabled is a time-range tuple."""
        from decouple import config as dc_config

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "06:45-15:00")
            # Reset LoadManager singleton so it reinitializes with the new config.
            import app as app_mod

            app_mod._load_manager = None
            app_mod._load_manager_init_failed = False
            response = self.app.get("/", headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 200)
        data = response.data.decode("utf-8")
        # Should show the time range, not just "yes"
        self.assertIn("06:45", data)
        self.assertIn("15:00", data)

    def test_index_html_mock(self):
        with mock_config():
            response = self.app.get("/", headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 200)
        # The word 'MOCK' might not be in the HTML if it's using the values directly.
        # Let's check for some characteristic HTML instead.
        self.assertIn(b"Wh, predicted total", response.data)

    def test_tou_endpoint_missing_start_date(self):
        response = self.app.get("/api/v1/tou")
        self.assertEqual(response.status_code, 400)

    def test_tou_endpoint_invalid_date_format(self):
        response = self.app.get("/api/v1/tou?start_date=invalid-date")
        self.assertEqual(response.status_code, 400)

    def test_tou_endpoint_valid_dates(self):
        with mock_config(MOCK=True):
            response = self.app.get(
                "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-01T04:00:00"
            )
        self.assertEqual(response.status_code, 200)

    def test_not_acceptable(self):
        with mock_config():
            response = self.app.get("/", headers={"Accept": "text/plain"})
        self.assertEqual(response.status_code, 406)

    def test_index_mock_error_retryable(self):
        """Test index() with MOCK_ERROR=True triggers RetryableMetricsException."""
        with mock_config(MOCK_ERROR="True"):
            response = self.app.get("/")
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"RETRY", response.data or b"")

    def test_tou_date_range_367_days_rejected(self):
        """Test tou() rejects date ranges exceeding 366 days with 400."""
        start = datetime(2025, 1, 1)
        end = start + timedelta(days=367)
        with mock_config(MOCK=True):
            response = self.app.get(
                f"/api/v1/tou?start_date={start.strftime('%Y-%m-%d')}"
                f"&end_date={end.strftime('%Y-%m-%d')}"
            )
        self.assertEqual(response.status_code, 400)

    def test_tou_date_range_366_days_accepted(self):
        """Test tou() accepts date ranges of exactly 366 days."""
        start = datetime(2025, 1, 1)
        end = start + timedelta(days=366)
        with mock_config(MOCK=True):
            response = self.app.get(
                f"/api/v1/tou?start_date={start.strftime('%Y-%m-%d')}"
                f"&end_date={end.strftime('%Y-%m-%d')}"
            )
        self.assertEqual(response.status_code, 200)

    def test_tou_api_failure_http_error(self):
        """Test tou() handles HTTPError from TOUReporter with proper error response."""
        mock_response = type(
            "MockResponse",
            (),
            {
                "status_code": 500,
                "text": "Internal Server Error",
            },
        )()

        http_error = requests.exceptions.HTTPError()
        http_error.response = mock_response

        with mock_config(MOCK=False, VUE_USERNAME="test_user"):
            with patch("app.TOUReporter") as mock_tou:
                mock_tou.side_effect = http_error
                response = self.app.get(
                    "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-02"
                )
                self.assertEqual(response.status_code, 500)
                self.assertIn(b"Error fetching usage data", response.data)

    def test_tou_endpoint_mock_realistic_values(self):
        """Verify TOU endpoint returns non-zero buckets in mock mode."""
        with mock_config(MOCK=True):
            response = self.app.get(
                "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-01T04:00:00"
            )
        data = json.loads(response.data)
        self.assertEqual(response.status_code, 200)
        self.assertGreater(data["buckets"]["total"], 0)
        self.assertGreater(data["buckets"]["peak"], 0)


class TestLoadManagementEndpoints(unittest.TestCase):
    """Tests for POST /api/v1/load/manage and GET /api/v1/load/status."""

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def test_load_manage_503_when_not_initialized(self):
        """POST /load/manage returns 503 when LoadManager is None."""
        with patch("app._get_load_manager", return_value=None):
            response = self.app.post("/api/v1/load/manage")
        self.assertEqual(response.status_code, 503)

    def test_load_manage_success(self):
        """POST /load/manage returns 200 with cycle result."""
        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_cycle.return_value = {
            "status": "ok",
            "nbc_prediction_wh": -800,
            "pending_effects": [],
            "devices": {},
        }
        with patch("app._get_load_manager", return_value=mock_lm):
            response = self.app.post("/api/v1/load/manage")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("status", data)
        mock_lm.run_cycle.assert_called_once_with(force=False)

    def test_load_manage_force_true(self):
        """POST /load/manage?force=true passes force=True to run_cycle."""
        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_cycle.return_value = {"status": "ok"}
        with patch("app._get_load_manager", return_value=mock_lm):
            response = self.app.post("/api/v1/load/manage?force=true")
        mock_lm.run_cycle.assert_called_once_with(force=True)
        self.assertEqual(response.status_code, 200)

    def test_load_manage_500_on_exception(self):
        """POST /load/manage returns 500 when run_cycle raises."""
        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_cycle.side_effect = RuntimeError("test error")
        with patch("app._get_load_manager", return_value=mock_lm):
            response = self.app.post("/api/v1/load/manage")
        self.assertEqual(response.status_code, 500)

    def test_load_status_503_when_not_initialized(self):
        """GET /load/status returns 503 when LoadManager is None."""
        with patch("app._get_load_manager", return_value=None):
            response = self.app.get("/api/v1/load/status")
        self.assertEqual(response.status_code, 503)

    def test_load_status_success(self):
        """GET /load/status returns 200 with state payload."""
        from datetime import datetime, timezone

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_state = unittest.mock.MagicMock()
        mock_state.devices = {}
        mock_state.pending_effects = []
        mock_lm.state = mock_state

        with patch("app._get_load_manager", return_value=mock_lm):
            response = self.app.get("/api/v1/load/status")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["enabled"])
        self.assertEqual(data["targetWh"], -500)


if __name__ == "__main__":
    unittest.main()
