import contextlib
import json
import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import requests
from app import app



@contextlib.contextmanager
def mock_config(**overrides: Any):
    """Patch config with default mock values plus any overrides.

    Writes the values into os.environ for the duration of the context,
    restoring the previous values (or removing them) on exit. Since the
    Config lookup chain checks os.environ before the .env file, these
    values win over conftest's clean_env defaults.

    Args:
        overrides: Key-value pairs to set in config (e.g., MOCK=True).
    """
    defaults = {
        "VUE_USERNAME": None,
        "MOCK_ERROR": False,
        "MOCK": True,
    }
    config_values = {**defaults, **overrides}

    saved = {key: os.environ.get(key) for key in config_values}
    try:
        for key, value in config_values.items():
            os.environ[key] = "" if value is None else str(value)
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old




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
        from config import Config
        dc_config = Config()

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "06:45-15:00")
            response = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)

    def test_index_html_time_range_enabled(self):
        """Index HTML shows time range when enabled is a time-range tuple."""
        from config import Config
        dc_config = Config()

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "06:45-15:00")
            # Reset LoadManager singleton so it reinitializes with the new config.
            import app as app_mod

            app_mod._state.load_manager = None
            app_mod._state.load_manager_init_failed = False
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
        self.assertIn(b"response time", response.data)

    def test_index_real_mode_lm_disabled(self):
        """Index returns 200 in real mode when load management is disabled.

        Regression for the production 500 where a real-mode fetch through
        EnergyCache._run_fetch_with_timeout crashed with
        ``AttributeError: 'DaemonThreadPoolExecutor' object has no attribute
        '_initializer'`` on Python 3.14 (ThreadPoolExecutor internals changed).
        """
        import app as app_mod

        with self._real_mode_config():
            with patch("app.create_metrics", return_value=self._realistic_metrics()):
                response = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)

    def test_index_real_mode_lm_out_of_time_range(self):
        """Index returns 200 when LM is configured but outside its time range.

        Same regression as test_index_real_mode_lm_disabled, but with the
        load-manager background loop having run a disabled cycle first (the
        exact production scenario from the outage logs).
        """
        import app as app_mod
        import pytz
        from config import Config

        with self._real_mode_config():
            now_utc = app_mod.datetime.now(app_mod.timezone.utc)
            now_la = now_utc.astimezone(pytz.timezone("America/Los_Angeles"))
            start_h = (now_la.hour + 1) % 24
            end_h = (now_la.hour + 2) % 24
            # A time range that excludes the current time so the cycle is
            # "disabled" while load management is still configured.
            Config().set("LOAD_MANAGE_ENABLED", f"{start_h:02d}:00-{end_h:02d}:00")
            Config().set("LOAD_PLUG_SENTINEL", "1234:5:1")
            Config().set("LOAD_PLUG_JACKERY", "5678:10:1")
            Config().set("TESLA_CLIENT_ID", "client-id")
            Config().set("TESLA_CLIENT_SECRET", "client-secret")
            Config().set("TESLA_REGION", "na")
            # Reinitialize the LoadManager singleton with the new config.
            app_mod._state.load_manager = None
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = None
            lm = app_mod._get_load_manager()
            self.assertIsNotNone(lm)
            result = lm.run_cycle()
            self.assertEqual(result.status, "disabled")
            with app_mod._state.load_manager_lock:
                app_mod._state.last_cycle_result = result
            with patch("app.create_metrics", return_value=self._realistic_metrics()):
                response = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)

    @contextlib.contextmanager
    def _real_mode_config(self):
        """Configure real (non-mock) mode and reset the LoadManager singleton.

        The autouse clean_env fixture has already cleared config; this sets
        real-mode values on top and resets the module-level singletons so the
        test starts from a clean state. Restores on exit.
        """
        import app as app_mod
        from config import Config

        cfg = Config()
        for key, value in {
            "VUE_USERNAME": "",
            "VUE_PASSWORD": "",
            "MOCK": "False",
            "MOCK_ERROR": "False",
            "DEBUG": "False",
            "LOAD_MANAGE_ENABLED": "False",
            "LOAD_MANAGE_DRY_RUN": "True",
            "LOAD_PLUG_CONTROLLER": "stub",
            "LOAD_TESLA_CONTROLLER": "stub",
        }.items():
            cfg.set(key, value)
        app_mod._state.load_manager = None
        app_mod._state.load_manager_init_failed = False
        app_mod._state.last_cycle_result = None
        try:
            yield
        finally:
            app_mod._state.load_manager = None
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = None

    @staticmethod
    def _realistic_metrics():
        """Return a metrics dict shaped like real-mode HourlyProjection.metrics."""
        from mockdata import _generate_hour_seconds
        from util import compute_nbc_quarters

        now = datetime.now(timezone.utc)
        per_second = _generate_hour_seconds(12345, 42, sign=-1.0)
        return {
            "api_response": {"total": timedelta(microseconds=750072)},
            "debug": True,
            "devices": [
                {
                    "gid": 12345,
                    "lag": timedelta(seconds=2),
                    "name": "METER",
                    "minute_predicted": -100.0,
                    "minutes_remaining": 18.0,
                    "per_second_data": per_second,
                    "prediction": -1000.0,
                    "prediction_min": -1100.0,
                    "prediction_max": -900.0,
                    "timezone": "America/Los_Angeles",
                    "nbc": compute_nbc_quarters(per_second).to_dict(),
                }
            ],
            "instant": now,
            "data_start": now.replace(second=0, microsecond=0) - timedelta(minutes=42),
            "_fetched_at": now,
            "_data_lag_secs": 2.0,
        }

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
    """Tests for GET /api/v1/load/status."""

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

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
        mock_state.snapshot_devices.return_value = {}
        mock_state.snapshot_pending_effects.return_value = []
        mock_lm.state = mock_state

        with patch("app._get_load_manager", return_value=mock_lm):
            response = self.app.get("/api/v1/load/status")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["enabled"])
        self.assertEqual(data["targetWh"], -500)

    def test_load_status_snapshot_safe_under_concurrent_mutation(self):
        """load_status stays consistent while the background thread mutates state.

        Regression guard for ``RuntimeError: dictionary changed size during
        iteration``: the background load-management thread inserts/deletes
        device keys (e.g. a new "tesla" entry) while the Flask read path
        iterates device state. The read path must use locked snapshots.
        """
        import threading

        import app as app_mod
        from load_models import DeviceState
        from load_nbc import StateTracker

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        tracker = StateTracker()
        mock_lm.state = tracker

        app_mod._state.load_manager = mock_lm
        app_mod._state.load_manager_init_failed = False
        app_mod._state.last_cycle_result = None

        errors: list[str] = []

        def writer() -> None:
            try:
                for i in range(200):
                    name = f"plug_{i % 5}"
                    tracker.set_device(
                        name,
                        DeviceState(name=name, desired_state=(i % 2 == 0)),
                    )
                    if i % 4 == 0:
                        tracker.pop_device(name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"writer: {exc!r}")

        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        try:
            for _ in range(50):
                response = self.app.get("/api/v1/load/status")
                self.assertEqual(response.status_code, 200)
                data = json.loads(response.data)
                self.assertIn("devices", data)
                self.assertIn("pendingEffects", data)
        finally:
            writer_thread.join(timeout=10)

        self.assertEqual(errors, [], f"Thread errors: {errors}")

    def test_index_html_includes_sleep_hint_meta(self):
        """Index HTML includes a meta tag with the sleep_hint value for JS."""
        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_lm.run_cycle.return_value = {
            "status": "ok",
            "qh": "QH1",
            "predicted_wh": -800,
            "adjusted_wh": -750,
            "target_wh": -500,
            "actions": [],
            "diagnostics": {
                "gap_wh": -300,
                "hysteresis_wh": 50,
                "seconds_remaining": 45,
                "reason": "ok",
                "pending_effects_count": 0,
                "candidates": [],
                "tesla_configured": False,
                "tesla_state": None,
                "tesla_error": None,
                "tesla_login_url": None,
                "plugs_configured": 0,
            },
            "sleep_hint": 30.0,
            "sleep_hint_at": "2025-01-15T12:00:00+00:00",
        }

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = mock_lm.run_cycle.return_value
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn('id="sleep-hint"', html)
        self.assertIn('data-value="30.0"', html)

    def test_index_json_includes_top_level_sleep_hint(self):
        """Index JSON loadManagement includes top-level sleepHint."""
        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_lm.config_interval_secs = 30
        mock_lm.run_cycle.return_value = {
            "status": "ok",
            "predicted_wh": -800,
            "target_wh": -500,
            "actions": [],
            "sleep_hint": 30.0,
            "sleep_hint_at": "2025-01-15T12:00:00+00:00",
        }

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = mock_lm.run_cycle.return_value
            response = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)
        self.assertIn("sleepHint", data["loadManagement"])
        self.assertEqual(data["loadManagement"]["sleepHint"], 30.0)

    def test_index_json_fallback_sleep_hint_to_config_interval(self):
        """Index JSON falls back to config_interval_secs when lastCycleResult is empty."""
        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_lm.config_interval_secs = 30
        # lastCycleResult is empty — sleep_hint should fall back to config_interval_secs
        mock_lm.run_cycle.return_value = {
            "status": "ok",
            "predicted_wh": -800,
            "target_wh": -500,
            "actions": [],
        }

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = None
            response = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)
        self.assertIn("sleepHint", data["loadManagement"])
        self.assertEqual(data["loadManagement"]["sleepHint"], 30)

    def test_index_html_missing_sleep_hint_no_crash(self):
        """Index HTML handles a cycle result without sleep_hint without crashing."""
        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_result = {
            "status": "ok",
            "qh": "QH1",
            "predicted_wh": -800,
            "adjusted_wh": -750,
            "target_wh": -500,
            "actions": [],
            "diagnostics": {
                "gap_wh": -300,
                "hysteresis_wh": 50,
                "seconds_remaining": 45,
                "reason": "ok",
                "pending_effects_count": 0,
                "candidates": [],
                "tesla_configured": False,
                "tesla_state": None,
                "tesla_error": None,
                "tesla_login_url": None,
                "plugs_configured": 0,
            },
            # No sleep_hint — should not cause a template error
        }
        mock_lm.run_cycle.return_value = mock_result

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = mock_result
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)

    def test_index_json_includes_sleep_hint_at(self):
        """Index JSON loadManagement includes sleepHintAt timestamp."""
        from datetime import datetime

        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_lm.config_interval_secs = 30
        mock_lm.run_cycle.return_value = {
            "status": "ok",
            "predicted_wh": -800,
            "target_wh": -500,
            "actions": [],
            "sleep_hint": 30.0,
            "sleep_hint_at": "2025-01-15T12:00:00+00:00",
        }

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = mock_lm.run_cycle.return_value
            response = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)
        self.assertIn("sleepHintAt", data["loadManagement"])
        self.assertEqual(data["loadManagement"]["sleepHintAt"], "2025-01-15T12:00:00+00:00")
        # Verify it parses as a valid datetime
        parsed = datetime.fromisoformat(data["loadManagement"]["sleepHintAt"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_index_html_includes_sleep_hint_at_meta(self):
        """Index HTML includes a meta tag with the sleep_hint_at value for JS."""
        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_lm.config_interval_secs = 30
        mock_lm.run_cycle.return_value = {
            "status": "ok",
            "predicted_wh": -800,
            "target_wh": -500,
            "actions": [],
            "sleep_hint": 30.0,
            "sleep_hint_at": "2025-01-15T12:00:00+00:00",
        }

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = mock_lm.run_cycle.return_value
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn('id="sleep-hint-at"', html)
        self.assertIn('data-value="2025-01-15T12:00:00+00:00"', html)

    def test_index_json_missing_sleep_hint_at_no_crash(self):
        """Index JSON handles missing sleepHintAt gracefully."""
        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_lm.config_interval_secs = 30
        # No sleep_hint_at in the result
        mock_lm.run_cycle.return_value = {
            "status": "ok",
            "predicted_wh": -800,
            "target_wh": -500,
            "actions": [],
            "sleep_hint": 30.0,
        }

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = mock_lm.run_cycle.return_value
            response = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)
        self.assertIn("sleepHintAt", data["loadManagement"])
        self.assertIsNone(data["loadManagement"]["sleepHintAt"])

    def test_index_html_missing_sleep_hint_at_no_crash(self):
        """Index HTML handles missing sleep_hint_at without crashing."""
        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_lm.config_interval_secs = 30
        mock_result = {
            "status": "ok",
            "qh": "QH1",
            "predicted_wh": -800,
            "adjusted_wh": -750,
            "target_wh": -500,
            "actions": [],
            "diagnostics": {
                "gap_wh": -300,
                "hysteresis_wh": 50,
                "seconds_remaining": 45,
                "reason": "ok",
                "pending_effects_count": 0,
                "candidates": [],
                "tesla_configured": False,
                "tesla_state": None,
                "tesla_error": None,
                "tesla_login_url": None,
                "plugs_configured": 0,
            },
            "sleep_hint": 30.0,
            # No sleep_hint_at — should not cause a template error
        }
        mock_lm.run_cycle.return_value = mock_result

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = mock_result
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)

    def test_index_html_handles_none_predicted_wh(self):
        """Index template renders when predicted_wh is None (no crash)."""
        from config import Config
        dc_config = Config()

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {}
        mock_lm.run_cycle.return_value = {
            "status": "ok",
            "predicted_wh": None,
            "target_wh": -500,
            "actions": [],
            "diagnostics": {
                "gap_wh": -300,
                "hysteresis_wh": 50,
                "seconds_remaining": 45,
                "reason": "ok",
                "pending_effects_count": 0,
                "candidates": [],
                "tesla_configured": False,
                "tesla_state": None,
                "tesla_error": None,
                "tesla_login_url": None,
                "plugs_configured": 0,
            },
        }

        with mock_config():
            dc_config.set("LOAD_MANAGE_ENABLED", "True")
            import app as app_mod

            app_mod._state.load_manager = mock_lm
            app_mod._state.load_manager_init_failed = False
            app_mod._state.last_cycle_result = mock_lm.run_cycle.return_value
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)


class TestTrimOutputDevice(unittest.TestCase):
    """Tests for the _trim_output_device helper in app.py."""

    def test_truncates_to_300_samples(self):
        """_trim_output_device truncates per_second_data to 300 samples."""
        import app as app_mod

        device = {
            "gid": 1,
            "name": "test-device",
            "per_second_data": list(range(1000)),
            "prediction": 42.0,
        }
        result = app_mod._trim_output_device(device)

        self.assertEqual(len(result["per_second_data"]), 300)
        self.assertEqual(result["per_second_data"][0], 700)
        self.assertEqual(result["per_second_data"][-1], 999)

    def test_keeps_short_arrays_unchanged(self):
        """_trim_output_device keeps arrays shorter than 300 unchanged."""
        import app as app_mod

        device = {
            "gid": 1,
            "name": "short-device",
            "per_second_data": list(range(50)),
            "prediction": 42.0,
        }
        result = app_mod._trim_output_device(device)

        self.assertEqual(len(result["per_second_data"]), 50)
        self.assertEqual(result["per_second_data"], list(range(50)))

    def test_moves_per_second_data_to_end(self):
        """_trim_output_device places per_second_data as the last key."""
        import app as app_mod

        device = {
            "gid": 1,
            "name": "order-device",
            "per_second_data": [1, 2, 3],
            "prediction": 42.0,
            "nbc": {},
        }
        result = app_mod._trim_output_device(device)

        keys = list(result.keys())
        self.assertEqual(keys[-1], "per_second_data")
        # Verify other keys are in their original relative order.
        self.assertEqual(keys[:-1], ["gid", "name", "prediction", "nbc"])

    def test_empty_per_second_data(self):
        """_trim_output_device handles empty per_second_data gracefully."""
        import app as app_mod

        device = {
            "gid": 1,
            "name": "empty-device",
            "per_second_data": [],
            "prediction": 42.0,
        }
        result = app_mod._trim_output_device(device)

        self.assertEqual(len(result["per_second_data"]), 0)


class TestCamelizeFunction(unittest.TestCase):
    """Tests for the camelize() function used to convert JSON responses."""

    def test_simple_snake_to_camel(self):
        """Top-level snake_case keys are converted to camelCase."""
        import app as app_mod

        data = {"prediction_min": 10.0, "prediction_max": 20.0}
        result = app_mod.camelize(data)

        self.assertEqual(result["predictionMin"], 10.0)
        self.assertEqual(result["predictionMax"], 20.0)
        # Original keys should not exist.
        self.assertNotIn("prediction_min", result)
        self.assertNotIn("prediction_max", result)

    def test_no_underscore_keys_unchanged(self):
        """Keys without underscores are left as-is."""
        import app as app_mod

        data = {"abc": "value", "nested": {"key": 42}}
        result = app_mod.camelize(data)

        self.assertEqual(result["abc"], "value")
        self.assertEqual(result["nested"]["key"], 42)

    def test_nested_dicts_recursively(self):
        """CamelCase conversion recurses into nested dicts."""
        import app as app_mod

        data = {"outer_key": {"inner_key": {"deep_key": "value"}}}
        result = app_mod.camelize(data)

        self.assertIn("outerKey", result)
        self.assertIn("innerKey", result["outerKey"])
        self.assertIn("deepKey", result["outerKey"]["innerKey"])
        self.assertEqual(result["outerKey"]["innerKey"]["deepKey"], "value")

    def test_lists_are_traversed(self):
        """Items in lists are camelize'd individually."""
        import app as app_mod

        data = {"items": [{"key_a": 1}, {"key_b": 2}]}
        result = app_mod.camelize(data)

        self.assertEqual(result["items"][0]["keyA"], 1)
        self.assertEqual(result["items"][1]["keyB"], 2)

    def test_multiple_underscores(self):
        """Keys with multiple underscores: first segment stays lowercase, rest are camelCased."""
        import app as app_mod

        data = {"sleep_hint_at": "2025-01-15T12:00:00+00:00"}
        result = app_mod.camelize(data)

        self.assertIn("sleepHintAt", result)
        self.assertEqual(
            result["sleepHintAt"], "2025-01-15T12:00:00+00:00"
        )

    def test_non_dict_values_pass_through(self):
        """Scalars and non-container types are returned unchanged."""
        import app as app_mod

        self.assertEqual(app_mod.camelize(42), 42)
        self.assertEqual(app_mod.camelize("hello"), "hello")
        self.assertIsNone(app_mod.camelize(None))
        self.assertEqual(app_mod.camelize(True), True)
        self.assertEqual(app_mod.camelize([1, 2, 3]), [1, 2, 3])


class TestCamelizeEndToEnd(unittest.TestCase):
    """Tests that camelize produces JSON output matching the template's data expectations."""

    def test_json_camel_keys_match_template_snake_keys(self):
        """Every camelCase key in the JSON response corresponds to a snake_case key
        the template accesses, verifying the camelize transformation is correct
        for the full index endpoint payload structure."""
        import app as app_mod

        # Representative payload shape matching what the index route produces.
        payload = {
            "devices": [
                {
                    "gid": 42,
                    "lag": "PT2S",
                    "name": "test-device",
                    "prediction": 100.0,
                    "prediction_min": 90.0,
                    "prediction_max": 110.0,
                    "minute_predicted": 50.0,
                    "minutes_remaining": 18.0,
                    "timezone": "America/Los_Angeles",
                    "nbc": {
                        "QH1": {
                            "complete": False,
                            "raw_wh": -100.0,
                            "wh": 0,
                            "predicted_wh": 50.0,
                            "samples_used": 600,
                        },
                        "QH2": {
                            "complete": True,
                            "raw_wh": 500.0,
                            "wh": 500.0,
                        },
                        "QH3": None,
                        "QH4": None,
                    },
                    "per_second_data": [0.001, 0.002],
                }
            ],
            "instant": "2025-01-01T12:00:00+00:00",
            "api_response": {"total": "PT0.00075S"},
            "load_management": {
                "enabled": True,
                "dry_run": True,
                "target_wh": -500,
                "nbc_device": "test_nbc",
                "sleep_hint": 30.0,
                "sleep_hint_at": "2025-01-15T12:00:00+00:00",
            },
        }

        camel = app_mod.camelize(payload)

        # Top-level keys
        self.assertIn("devices", camel)
        self.assertIn("instant", camel)
        self.assertIn("apiResponse", camel)
        self.assertIn("loadManagement", camel)

        # Device-level keys
        device = camel["devices"][0]
        self.assertIn("gid", device)
        self.assertIn("lag", device)
        self.assertIn("prediction", device)
        self.assertIn("predictionMin", device)
        self.assertIn("predictionMax", device)
        self.assertIn("minutePredicted", device)
        self.assertIn("minutesRemaining", device)
        self.assertIn("perSecondData", device)

        # NBC — underscore keys in incomplete quarter
        nbc = device["nbc"]
        self.assertIn("QH1", nbc)
        self.assertTrue("predictedWh" in nbc["QH1"])
        self.assertTrue("samplesUsed" in nbc["QH1"])

        # Load management
        lm = camel["loadManagement"]
        self.assertIn("sleepHint", lm)
        self.assertIn("sleepHintAt", lm)

    def test_camelize_preserves_numeric_types(self):
        """Numeric values (int, float) pass through camelize without rounding or conversion."""
        import app as app_mod

        data = {"prediction_min": 0.123456789012345, "prediction_max": 1}
        result = app_mod.camelize(data)

        # Float should stay a float.
        self.assertIsInstance(result["predictionMin"], float)
        self.assertAlmostEqual(result["predictionMin"], 0.123456789012345)
        # Int should stay an int.
        self.assertIsInstance(result["predictionMax"], int)
        self.assertEqual(result["predictionMax"], 1)


class TestEndToEndMetricsPipeline(unittest.TestCase):
    """Structured end-to-end tests that validate the full device dict shape
    through the index route, exercising the dataclass-to-dict serialization
    chain (DeviceMetrics.to_dict → camelize → JSON provider)."""

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def _get_index_json(self, instant_minute=42):
        """Helper: get / with Accept: application/json in mock mode."""
        with mock_config():
            return self.app.get(
                f"/?instant_minute={instant_minute}",
                headers={"Accept": "application/json"},
            )

    def test_top_level_keys_present(self):
        """Response contains the expected top-level keys."""
        resp = self._get_index_json()
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)

        self.assertIn("devices", data)
        self.assertIn("instant", data)
        self.assertIn("apiResponse", data)
        self.assertIn("loadManagement", data)

    def test_device_has_all_required_camel_keys(self):
        """Every device in the JSON response has all the camelCase keys
        that the template and JS consumers expect."""
        resp = self._get_index_json()
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)

        required_keys = [
            "gid", "lag", "name", "prediction", "predictionMin",
            "predictionMax", "minutePredicted", "minutesRemaining",
            "timezone", "nbc", "perSecondData",
        ]

        for device in data["devices"]:
            for key in required_keys:
                self.assertIn(
                    key, device,
                    f"Device {device.get('name')} missing key '{key}'",
                )

    def test_nbc_camel_structure(self):
        """NBC quarters have correctly camelCased keys in the JSON."""
        resp = self._get_index_json()
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)

        for device in data["devices"]:
            nbc = device["nbc"]
            # QH1 incomplete → should have predictedWh and samplesUsed.
            qh1 = nbc["QH1"]
            self.assertIn("predictedWh", qh1)
            self.assertIn("samplesUsed", qh1)

            # QH2 complete → should have rawWh and wh.
            self.assertIn("rawWh", nbc["QH2"])
            self.assertIn("wh", nbc["QH2"])

            # QH3 complete, QH4 None.
            self.assertIsNotNone(nbc["QH3"])
            self.assertIsNone(nbc["QH4"])

    def test_index_with_different_instant_minute(self):
        """NBC endpoint with different instant_minute still produces valid structure."""
        resp = self._get_index_json(instant_minute=10)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)

        self.assertIn("devices", data)
        for device in data["devices"]:
            self.assertIn("nbc", device)
            nbc = device["nbc"]
            # At minute=10 only QH1 should be present (incomplete).
            self.assertIsNotNone(nbc["QH1"])
            self.assertIsNotNone(nbc["QH1"].get("predictedWh"))
            self.assertIsNone(nbc["QH2"])
            self.assertIsNone(nbc["QH3"])
            self.assertIsNone(nbc["QH4"])

    def test_lag_is_valid_iso_duration(self):
        """Lag value is a valid ISO 8601 duration string (serializable by JSON provider)."""
        import isodate

        resp = self._get_index_json()
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)

        lag_str = data["devices"][0]["lag"]
        self.assertIsInstance(lag_str, str)
        # Should parse as a valid ISO 8601 duration.
        delta = isodate.parse_duration(lag_str)
        self.assertGreaterEqual(delta.total_seconds(), 0)


class TestIndexEndpointPerSecondData(unittest.TestCase):
    """Tests that the index endpoint perSecondData contains the most recent
    300 samples after full and incremental fetches via the real-mode path."""

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def _make_metrics(self, samples, data_start, now=None):
        """Build a minimal metrics dict shaped like create_metrics() output."""
        if now is None:
            now = datetime.now(timezone.utc)
        return {
            "devices": [
                {
                    "gid": 1,
                    "name": "test-device",
                    "lag": timedelta(seconds=2),
                    "per_second_data": list(samples),
                }
            ],
            "instant": now,
            "api_response": {},
            "_fetched_at": now,
            "data_start": data_start,
        }

    def test_full_fetch_trims_to_300_samples(self):
        """After a full fetch with >300 samples, perSecondData is the last 300."""
        import app as app_mod
        from energy_cache import EnergyCache

        now = datetime.now(timezone.utc)
        data_start = now - timedelta(seconds=500)
        samples_500 = list(range(500))
        metrics_dict = self._make_metrics(samples_500, data_start, now)

        fresh_cache = EnergyCache(ttl_seconds=0)

        with mock_config(MOCK=False, VUE_USERNAME="test_user"):
            with patch.object(app_mod._state, "energy_cache", fresh_cache):
                with patch("app.create_metrics", return_value=metrics_dict):
                    resp = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        psd = data["devices"][0]["perSecondData"]

        self.assertEqual(len(psd), 300,
            f"Expected 300 samples from full fetch, got {len(psd)}")
        self.assertEqual(psd[0], 200,
            "First of the 300 should be sample index 200")
        self.assertEqual(psd[-1], 499,
            "Last sample should be 499 (most recent)")



class TestNetworkOutageGracefulDegradation(unittest.TestCase):
    """Tests for graceful handling when network is down and metrics are None."""

    def setUp(self):
        self.app = app.test_client()

    def test_index_returns_gracefully_when_metrics_none(self):
        """Index endpoint returns 200 (not 500) when get_or_fetch returns None.

        During a network outage, EnergyCache.get_or_fetch() returns (None, True)
        and _enrich_metrics_for_sse must not crash on None input.
        """
        import app as app_mod

        with mock_config(MOCK=False, VUE_USERNAME="test_user"):
            with patch.object(app_mod._state, "energy_cache") as mock_cache:
                mock_cache.get_or_fetch.return_value = (None, True)
                mock_cache._data = None
                resp = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("devices", data)

    def test_index_empty_dashboard_auto_refreshes(self):
        """An empty dashboard (no devices) on the real-data path auto-refreshes.

        On first boot during an API outage the fetch returns no devices and
        index() renders an empty dashboard with a 200 — the 500 retry page is
        dead for real-data paths. The empty dashboard must carry a meta
        refresh so the browser self-heals without a manual refresh.
        """
        import app as app_mod

        with mock_config(MOCK=False, VUE_USERNAME="test_user"):
            with patch.object(app_mod._state, "energy_cache") as mock_cache:
                mock_cache.get_or_fetch.return_value = (
                    {"devices": [], "api_response": {}, "instant": None},
                    True,
                )
                mock_cache.samples = []
                resp = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'http-equiv="refresh"', resp.data.lower())

    def test_index_enrich_metrics_for_sse_none_input(self):
        """_enrich_metrics_for_sse handles None input without crashing."""
        import app as app_mod
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        result = app_mod._enrich_metrics_for_sse(None, now=now)
        self.assertIsInstance(result, dict)
        self.assertIn("devices", result)


if __name__ == "__main__":
    unittest.main()
    """Tests that lag is recalculated per request, not frozen by cache."""

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def _lag_to_seconds(self, lag_value: str) -> float:
        """Convert an ISO 8601 duration string like 'PT3M13.983687S' to seconds."""
        delta = __import__("isodate").parse_duration(lag_value)
        return delta.total_seconds()

    def test_lag_increases_between_requests(self):
        """Lag recalculation adds elapsed time so cached data doesn't appear
        unnaturally fresh.

        In mock mode each request creates a fresh MetricsMock, so the lag
        stays deterministic (constant).  In real mode the EnergyCache persists
        across requests and the presentation-layer recalculation adds elapsed
        seconds, so lag grows.

        This test verifies the mock-mode behaviour (lag constant) since the
        test harness runs in mock_config.  The real-mode path is tested
        indirectly by the integration tests that hit the live API.
        """
        import time

        with mock_config():
            resp1 = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(resp1.status_code, 200)
        data1 = json.loads(resp1.data)
        lag1 = self._lag_to_seconds(data1["devices"][0]["lag"])

        # Small pause so elapsed time is measurable.
        time.sleep(0.5)

        with mock_config():
            resp2 = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(resp2.status_code, 200)
        data2 = json.loads(resp2.data)
        lag2 = self._lag_to_seconds(data2["devices"][0]["lag"])

        # Mock mode: lag stays the same (deterministic mock data).
        self.assertAlmostEqual(
            lag2, lag1, delta=0.1,
            msg="mock-mode lag should stay deterministic "
            f"(lag1={lag1:.1f}s, lag2={lag2:.1f}s)",
        )

    def test_lag_present_in_first_request(self):
        """Lag must be present even on the first request."""
        with mock_config():
            response = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("lag", data["devices"][0])
        lag = self._lag_to_seconds(data["devices"][0]["lag"])
        self.assertGreaterEqual(lag, 0)


class TestBuildLoadManagementPayloadLocked(unittest.TestCase):
    """Tests for _build_load_management_payload() when an lm is passed in.

    The background loop passes the LoadManager instance directly (it
    already holds _state.load_manager_lock).  The payload builder must NOT
    call _get_load_manager() because that function also tries to
    acquire _state.load_manager_lock, causing a non-reentrant Lock deadlock.
    """

    def setUp(self):
        import app as app_mod
        app_mod._state.load_manager = None
        app_mod._state.last_cycle_result = None

    def test_does_not_call_get_load_manager(self):
        """Passing lm builds the payload without calling _get_load_manager."""
        import app as app_mod
        from app import _build_load_management_payload

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = False
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {"devices": {}}
        mock_lm.config_interval_secs = 30

        with patch("app._get_load_manager", side_effect=Exception("would deadlock")):
            result = _build_load_management_payload(mock_lm)

        self.assertEqual(result["enabled"], True)
        self.assertEqual(result["target_wh"], -500)
        self.assertEqual(result["state"], {"devices": {}})


class TestBuildLoadManagementPayloadDisabled(unittest.TestCase):
    """Tests for _build_load_management_payload() when load management is disabled.

    When LOAD_MANAGE_ENABLED=False, the payload builder must return {}
    immediately without touching _get_load_manager() or _state.load_manager_lock.
    This avoids a lock contention crash where the background thread holds
    the lock during LoadManager init while the request handler blocks on it.
    """

    def setUp(self):
        import app as app_mod
        app_mod._state.load_manager = None

    def test_returns_empty_when_disabled(self):
        """Returns {} without calling _get_load_manager when disabled."""
        import app as app_mod
        from app import _build_load_management_payload
        from config import Config
        dc_config = Config()

        dc_config.set("LOAD_MANAGE_ENABLED", "False")
        with patch("app._get_load_manager", side_effect=Exception("should not be called")):
            result = _build_load_management_payload()

        self.assertEqual(result, {})


class TestSendErrorAlert(unittest.TestCase):
    """Tests for _send_error_alert helper."""

    def test_sends_when_sender_configured(self):
        """When telegram sender is configured, sends error notification."""
        import app as app_mod

        mock_sender = unittest.mock.MagicMock()
        mock_sender.is_configured = True
        mock_sender.send_notification_sync = unittest.mock.MagicMock(return_value=True)

        app_mod._state.telegram_sender = mock_sender
        try:
            exc = ValueError("test error")
            app_mod._send_error_alert(exc)
            mock_sender.send_notification_sync.assert_called_once()
            call_args = mock_sender.send_notification_sync.call_args[0][0]
            assert "test error" in call_args.description
        finally:
            app_mod._state.telegram_sender = None

    def test_noop_when_sender_none(self):
        """When telegram sender is None, no-op without error."""
        import app as app_mod

        app_mod._state.telegram_sender = None
        app_mod._send_error_alert(ValueError("ignored"))

    def test_noop_when_sender_not_configured(self):
        """When sender exists but is_configured is False, no-op."""
        import app as app_mod

        mock_sender = unittest.mock.MagicMock()
        mock_sender.is_configured = False

        app_mod._state.telegram_sender = mock_sender
        try:
            app_mod._send_error_alert(ValueError("ignored"))
            mock_sender.send_notification_sync.assert_not_called()
        finally:
            app_mod._state.telegram_sender = None


class TestMetricsLoopErrorHandling(unittest.TestCase):
    """Tests for _metrics_loop error handling.

    The metrics loop is fetch-only by default (fast decision loop on):
    it calls LoadManager.fetch_cycle() and owns the EnergyCache.
    """

    def test_cache_invalidated_on_error(self):
        """When fetch_cycle raises, energy cache is invalidated."""
        import app as app_mod
        from energy_cache import EnergyCacheData

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        app_mod._state.energy_cache._data = EnergyCacheData(
            samples=[0.1, 0.2],
            data_start=base,
            last_sample_at=base + timedelta(seconds=1),
            last_fetch_at=base,
            sample_count=2,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )

        mock_lm = unittest.mock.MagicMock()
        mock_lm.fetch_cycle.side_effect = RuntimeError("test crash")

        app_mod._state.telegram_sender = None
        app_mod._state.metrics_loop_error_count = 0
        app_mod._state.metrics_loop_last_error_type = None

        with patch("app._get_load_manager", return_value=mock_lm):
            with patch("app.time.sleep", side_effect=InterruptedError("stop")):
                with self.assertRaises(InterruptedError):
                    app_mod._metrics_loop()

        assert app_mod._state.energy_cache._data is None
        app_mod._state.metrics_loop_error_count = 0
        app_mod._state.metrics_loop_last_error_type = None

    def test_error_counter_increments_on_error(self):
        """Error counter increments on each error."""
        import app as app_mod

        mock_lm = unittest.mock.MagicMock()
        mock_lm.fetch_cycle.side_effect = RuntimeError("test crash")

        app_mod._state.telegram_sender = None
        app_mod._state.metrics_loop_error_count = 0

        with patch("app._get_load_manager", return_value=mock_lm):
            with patch("app.time.sleep", side_effect=InterruptedError("stop")):
                with self.assertRaises(InterruptedError):
                    app_mod._metrics_loop()

        assert app_mod._state.metrics_loop_error_count == 1
        assert app_mod._state.metrics_loop_last_error_type == "RuntimeError"
        app_mod._state.metrics_loop_error_count = 0
        app_mod._state.metrics_loop_last_error_type = None

    def test_error_counter_resets_on_success(self):
        """Error counter resets when fetch cycle succeeds."""
        import app as app_mod

        app_mod._state.metrics_loop_error_count = 5

        mock_lm = unittest.mock.MagicMock()
        mock_lm.fetch_cycle.return_value = None

        app_mod._state.telegram_sender = None
        app_mod._state.data_ready_event.clear()

        with patch("app._get_load_manager", return_value=mock_lm):
            with patch("app.time.sleep", side_effect=InterruptedError("stop")):
                with self.assertRaises(InterruptedError):
                    app_mod._metrics_loop()

        assert app_mod._state.metrics_loop_error_count == 0
        assert app_mod._state.metrics_loop_last_error_type is None
        assert app_mod._state.data_ready_event.is_set()

    def test_rate_limited_telegram_alert(self):
        """Telegram alert sent on first error, then every 10th."""
        import app as app_mod

        mock_sender = unittest.mock.MagicMock()
        mock_sender.is_configured = True
        mock_sender.send_notification_sync = unittest.mock.MagicMock(return_value=True)

        mock_lm = unittest.mock.MagicMock()
        mock_lm.fetch_cycle.side_effect = RuntimeError("crash")

        app_mod._state.telegram_sender = mock_sender
        app_mod._state.metrics_loop_error_count = 0

        call_count = 0

        def stop_after_n_sleeps(_secs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise InterruptedError("stop")

        with patch("app._get_load_manager", return_value=mock_lm):
            with patch("app.time.sleep", side_effect=stop_after_n_sleeps):
                with self.assertRaises(InterruptedError):
                    app_mod._metrics_loop()

        # First error (count=1) triggers alert, second (count=2) doesn't
        assert mock_sender.send_notification_sync.call_count == 1
        app_mod._state.metrics_loop_error_count = 0
        app_mod._state.metrics_loop_last_error_type = None
        app_mod._state.telegram_sender = None

    def test_disabled_cycle_skips_sleep_interval_adjust(self):
        """When cycle is disabled, sleep_interval_adjust is skipped.

        The loop should use result.sleep_hint directly (30s) instead of
        calling sleep_interval_adjust which would clamp to 5s when cache
        data is stale relative to the quantization period.
        """
        import app as app_mod
        from energy_cache import EnergyCacheData
        from load_models import CycleResult, CycleDiagnostics

        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Set up cache with stale data: last_sample_at is old enough that
        # sleep_interval_adjust would return MIN_SLEEP_SECS (5.0).
        app_mod._state.energy_cache._data = EnergyCacheData(
            samples=[0.1] * 100,
            data_start=base - timedelta(minutes=55),
            last_sample_at=base - timedelta(minutes=50),
            last_fetch_at=base - timedelta(minutes=50),
            sample_count=100,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=0.9,
        )

        mock_lm = unittest.mock.MagicMock()
        mock_lm.fetch_cycle.return_value = CycleResult(
            status="disabled",
            sleep_hint=30.0,
            sleep_hint_at=base.isoformat(),
            diagnostics=CycleDiagnostics(
                gap_wh=None,
                hysteresis_wh=3.0,
                seconds_remaining=None,
                data_point_at=None,
                reason="[run_cycle] outside_time_range(06:30-20:30)",
            ),
        )
        mock_lm._send_pending_notifications_sync = unittest.mock.MagicMock()

        app_mod._state.metrics_loop_error_count = 0
        app_mod._state.metrics_loop_last_error_type = None
        app_mod._state.telegram_sender = None

        captured_sleep_values: list[float] = []

        def capture_sleep(secs: float) -> None:
            captured_sleep_values.append(secs)
            if len(captured_sleep_values) >= 2:
                raise InterruptedError("stop")

        with patch("app._get_load_manager", return_value=mock_lm):
            with patch("app.time.sleep", side_effect=capture_sleep):
                with self.assertRaises(InterruptedError):
                    app_mod._metrics_loop()

        assert captured_sleep_values[0] == 30.0, (
            f"Expected 30.0 for disabled cycle, got {captured_sleep_values[0]}"
        )

        app_mod._state.metrics_loop_error_count = 0
        app_mod._state.metrics_loop_last_error_type = None


class TestDecisionLoopErrorHandling(unittest.TestCase):
    """Tests for _decision_loop error handling.

    The decision loop is cache-only: it calls run_decision_cycle() and
    never fetches. Its error counter must be independent of the metrics
    loop's counter so a fast success cadence cannot suppress alerts for
    the other loop.
    """

    def _fake_event_stop_after(self, iterations: int):
        """Return a fake data_ready_event that raises after N waits."""
        fake_event = unittest.mock.MagicMock()
        # The first (iterations - 1) waits return normally; the next raises
        # to stop the loop. MagicMock raises exception instances from a
        # side_effect list.
        fake_event.wait.side_effect = [None] * (iterations - 1) + [
            InterruptedError("stop")
        ]
        return fake_event

    def test_error_counter_increments_on_error(self):
        """Decision-loop error counter increments on each error."""
        import app as app_mod

        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_decision_cycle.side_effect = RuntimeError("test crash")

        app_mod._state.telegram_sender = None
        app_mod._state.decision_loop_error_count = 0
        app_mod._state.decision_loop_last_error_type = None
        app_mod._state.metrics_loop_error_count = 0
        real_event = app_mod._state.data_ready_event
        app_mod._state.data_ready_event = self._fake_event_stop_after(2)

        try:
            with patch("app._get_load_manager", return_value=mock_lm):
                with self.assertRaises(InterruptedError):
                    app_mod._decision_loop()
        finally:
            app_mod._state.data_ready_event = real_event

        assert app_mod._state.decision_loop_error_count == 1
        assert app_mod._state.decision_loop_last_error_type == "RuntimeError"
        app_mod._state.decision_loop_error_count = 0
        app_mod._state.decision_loop_last_error_type = None
        app_mod._state.metrics_loop_error_count = 0

    def test_error_counter_resets_on_success(self):
        """Decision-loop error counter resets when a decision succeeds."""
        import app as app_mod

        app_mod._state.decision_loop_error_count = 5
        app_mod._state.decision_loop_last_error_type = "RuntimeError"

        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_decision_cycle.return_value = None
        mock_lm._send_pending_notifications_sync = unittest.mock.MagicMock()

        app_mod._state.telegram_sender = None
        real_event = app_mod._state.data_ready_event
        app_mod._state.data_ready_event = self._fake_event_stop_after(2)

        try:
            with patch("app._get_load_manager", return_value=mock_lm):
                with self.assertRaises(InterruptedError):
                    app_mod._decision_loop()
        finally:
            app_mod._state.data_ready_event = real_event

        assert app_mod._state.decision_loop_error_count == 0
        assert app_mod._state.decision_loop_last_error_type is None
        app_mod._state.decision_loop_error_count = 0
        app_mod._state.decision_loop_last_error_type = None

    def test_rate_limited_telegram_alert(self):
        """Decision-loop alerts on first error, then every 10th."""
        import app as app_mod

        mock_sender = unittest.mock.MagicMock()
        mock_sender.is_configured = True
        mock_sender.send_notification_sync = unittest.mock.MagicMock(return_value=True)

        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_decision_cycle.side_effect = RuntimeError("crash")

        app_mod._state.telegram_sender = mock_sender
        app_mod._state.decision_loop_error_count = 0
        app_mod._state.metrics_loop_error_count = 0
        real_event = app_mod._state.data_ready_event
        app_mod._state.data_ready_event = self._fake_event_stop_after(4)

        try:
            with patch("app._get_load_manager", return_value=mock_lm):
                with self.assertRaises(InterruptedError):
                    app_mod._decision_loop()
        finally:
            app_mod._state.data_ready_event = real_event

        # First error (count=1) triggers alert, 2nd/3rd (count=2/3) don't.
        assert mock_sender.send_notification_sync.call_count == 1
        app_mod._state.decision_loop_error_count = 0
        app_mod._state.decision_loop_last_error_type = None
        app_mod._state.telegram_sender = None
        app_mod._state.metrics_loop_error_count = 0

    def test_error_does_not_touch_metrics_loop_counter(self):
        """A decision-loop error leaves the metrics-loop counter alone."""
        import app as app_mod

        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_decision_cycle.side_effect = RuntimeError("crash")

        app_mod._state.telegram_sender = None
        app_mod._state.decision_loop_error_count = 0
        app_mod._state.metrics_loop_error_count = 3
        real_event = app_mod._state.data_ready_event
        app_mod._state.data_ready_event = self._fake_event_stop_after(2)

        try:
            with patch("app._get_load_manager", return_value=mock_lm):
                with self.assertRaises(InterruptedError):
                    app_mod._decision_loop()
        finally:
            app_mod._state.data_ready_event = real_event

        assert app_mod._state.metrics_loop_error_count == 3
        assert app_mod._state.decision_loop_error_count == 1
        app_mod._state.decision_loop_error_count = 0
        app_mod._state.decision_loop_last_error_type = None
        app_mod._state.metrics_loop_error_count = 0


class TestErrorCountersIndependentAcrossLoops(unittest.TestCase):
    """The metrics loop's error counter must not be reset by decision-loop
    successes (the twin-cycles alert-spam bug).

    When the Emporia API is down, the metrics loop errors on every fetch
    while the decision loop keeps succeeding on cached data. The shared
    counter used to be reset by each decision-loop success, so every
    metrics error fired a Telegram alert instead of only the 1st + every
    10th.
    """

    def test_metrics_alerts_not_suppressed_by_decision_successes(self):
        """Interleaved metrics errors and decision successes alert once."""
        import app as app_mod

        mock_sender = unittest.mock.MagicMock()
        mock_sender.is_configured = True
        mock_sender.send_notification_sync = unittest.mock.MagicMock(return_value=True)

        mock_lm_fail = unittest.mock.MagicMock()
        mock_lm_fail.fetch_cycle.side_effect = RuntimeError("emporia down")

        mock_lm_ok = unittest.mock.MagicMock()
        mock_lm_ok.run_decision_cycle.return_value = None
        mock_lm_ok._send_pending_notifications_sync = unittest.mock.MagicMock()

        app_mod._state.telegram_sender = mock_sender
        app_mod._state.metrics_loop_error_count = 0
        app_mod._state.metrics_loop_last_error_type = None
        app_mod._state.decision_loop_error_count = 0
        app_mod._state.decision_loop_last_error_type = None

        try:
            # Iteration 1: metrics loop errors once -> alert, count 1.
            with patch("app._get_load_manager", return_value=mock_lm_fail):
                with patch("app.time.sleep", side_effect=InterruptedError("stop")):
                    with self.assertRaises(InterruptedError):
                        app_mod._metrics_loop()
            assert app_mod._state.metrics_loop_error_count == 1
            assert mock_sender.send_notification_sync.call_count == 1

            # Iteration 2: decision loop succeeds -> must NOT reset the
            # metrics-loop counter.
            real_event = app_mod._state.data_ready_event
            fake_event = unittest.mock.MagicMock()
            # One normal wait runs a decision cycle; the next raises to stop.
            fake_event.wait.side_effect = [None, InterruptedError("stop")]
            app_mod._state.data_ready_event = fake_event
            try:
                with patch("app._get_load_manager", return_value=mock_lm_ok):
                    with self.assertRaises(InterruptedError):
                        app_mod._decision_loop()
            finally:
                app_mod._state.data_ready_event = real_event

            assert app_mod._state.metrics_loop_error_count == 1, (
                "decision-loop success must not reset the metrics-loop counter"
            )
            assert app_mod._state.decision_loop_error_count == 0

            # Iteration 3: metrics loop errors again -> count 2, still only
            # one alert (alerts fire on 1st and every 10th).
            with patch("app._get_load_manager", return_value=mock_lm_fail):
                with patch("app.time.sleep", side_effect=InterruptedError("stop")):
                    with self.assertRaises(InterruptedError):
                        app_mod._metrics_loop()
            assert app_mod._state.metrics_loop_error_count == 2
            assert mock_sender.send_notification_sync.call_count == 1
        finally:
            app_mod._state.telegram_sender = None
            app_mod._state.metrics_loop_error_count = 0
            app_mod._state.metrics_loop_last_error_type = None
            app_mod._state.decision_loop_error_count = 0
            app_mod._state.decision_loop_last_error_type = None


class TestLoadCyclePublishOnChange(unittest.TestCase):
    """load_cycle SSE publishing dedupes steady-state decision cycles."""

    BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _make_result(self, status="ok", reason="none", actions=None, data_point_at=None):
        """Build a minimal CycleResult for publish-key tests."""
        from load_models import CycleDiagnostics, CycleResult

        return CycleResult(
            status=status,
            sleep_hint=5.0,
            sleep_hint_at=self.BASE.isoformat(),
            diagnostics=CycleDiagnostics(
                gap_wh=0.0,
                hysteresis_wh=3.0,
                seconds_remaining=300,
                data_point_at=data_point_at or self.BASE,
                reason=reason,
            ),
            actions=actions or [],
        )

    def test_stable_cycles_publish_once(self):
        """Identical steady-state cycles publish exactly one load_cycle event."""
        import app as app_mod

        app_mod._state.last_load_cycle_key = None
        result = self._make_result()
        with patch.object(app_mod._state.sse_broadcaster, "publish") as mock_publish:
            app_mod._publish_load_cycle_if_changed(result, {"payload": 1})
            app_mod._publish_load_cycle_if_changed(result, {"payload": 1})
            app_mod._publish_load_cycle_if_changed(result, {"payload": 1})
        mock_publish.assert_called_once()

    def test_action_transition_publishes(self):
        """A new action publishes; repeating it stays silent."""
        import app as app_mod
        from load_models import PendingEffect

        effect = PendingEffect(
            device_name="pool_pump",
            action="turn_on",
            timestamp=self.BASE,
            data_point_at=self.BASE,
            power_watts=1500.0,
        )
        app_mod._state.last_load_cycle_key = None
        with patch.object(app_mod._state.sse_broadcaster, "publish") as mock_publish:
            app_mod._publish_load_cycle_if_changed(self._make_result(), {"payload": 1})
            app_mod._publish_load_cycle_if_changed(
                self._make_result(actions=[effect]), {"payload": 2}
            )
            app_mod._publish_load_cycle_if_changed(
                self._make_result(actions=[effect]), {"payload": 3}
            )
            app_mod._publish_load_cycle_if_changed(
                self._make_result(actions=[effect], reason="no_action_needed"),
                {"payload": 4},
            )
        # 1st (initial), action, reason-change — but not the repeated action cycle.
        assert mock_publish.call_count == 3

    def test_qh_boundary_publishes(self):
        """A new data point (QH boundary) publishes even with same status."""
        import app as app_mod

        next_qh = self.BASE + timedelta(minutes=15)
        app_mod._state.last_load_cycle_key = None
        with patch.object(app_mod._state.sse_broadcaster, "publish") as mock_publish:
            app_mod._publish_load_cycle_if_changed(
                self._make_result(data_point_at=self.BASE), {"payload": 1}
            )
            app_mod._publish_load_cycle_if_changed(
                self._make_result(data_point_at=self.BASE), {"payload": 2}
            )
            app_mod._publish_load_cycle_if_changed(
                self._make_result(data_point_at=next_qh), {"payload": 3}
            )
        # base (initial) and next_qh — the repeated base stays silent.
        assert mock_publish.call_count == 2

    def test_decision_loop_publishes_once_for_stable_cycles(self):
        """The decision loop publishes load_cycle once for stable steady state."""
        import threading

        import app as app_mod

        result = self._make_result()
        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_decision_cycle.return_value = result
        mock_lm._send_pending_notifications_sync = unittest.mock.MagicMock()

        real_event = app_mod._state.data_ready_event
        fake_event = unittest.mock.MagicMock()
        waits = 0

        def wait_and_stop(timeout=None):
            nonlocal waits
            waits += 1
            if waits >= 3:
                raise InterruptedError("stop")

        fake_event.wait.side_effect = wait_and_stop
        app_mod._state.data_ready_event = fake_event
        app_mod._state.last_load_cycle_key = None

        try:
            with patch("app._get_load_manager", return_value=mock_lm), \
                 patch.object(app_mod._state.sse_broadcaster, "publish") as mock_publish:
                with self.assertRaises(InterruptedError):
                    app_mod._decision_loop()
        finally:
            app_mod._state.data_ready_event = real_event

        load_cycle_calls = [
            c for c in mock_publish.call_args_list if c.args[0] == "load_cycle"
        ]
        assert len(load_cycle_calls) == 1


if __name__ == "__main__":
    unittest.main()
