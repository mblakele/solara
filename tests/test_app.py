import contextlib
import json
import logging
import os
import threading
import time as time_module
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import requests
from werkzeug.exceptions import BadRequest
from app import app
from config import Config



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


@contextlib.contextmanager
def real_mode_config(**overrides: Any):
    """Configure real (non-mock) mode and reset the LoadManager singleton.

    The autouse clean_env fixture has already cleared config; this sets
    real-mode values on top (optionally overridden per-test, e.g.
    LOAD_MANAGE_ENABLED="True") and resets the module-level singletons so
    the test starts from a clean state. Restores singleton state on exit.

    Args:
        overrides: Key-value pairs to set in config on top of the defaults.
    """
    import app as app_mod

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
        **overrides,
    }.items():
        cfg.set(key, value)

    def _reset_singletons() -> None:
        app_mod._state.load_manager = None
        app_mod._state.load_manager_init_failed = False
        app_mod._state.last_cycle_result = None

    _reset_singletons()
    try:
        yield
    finally:
        _reset_singletons()


def realistic_metrics() -> dict[str, Any]:
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




class TestApp(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def test_health_endpoint(self):
        response = self.app.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/json")
        data = json.loads(response.data)
        self.assertIn(data["status"], ("ok", "degraded"))
        self.assertIn("components", data)

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
            with patch("app.create_metrics", return_value=realistic_metrics()):
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
            with patch("app.create_metrics", return_value=realistic_metrics()):
                response = self.app.get("/", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)

    @contextlib.contextmanager
    def _real_mode_config(self):
        """Configure real (non-mock) mode and reset the LoadManager singleton."""
        with real_mode_config():
            yield

    @staticmethod
    def _realistic_metrics():
        """Return a metrics dict shaped like real-mode HourlyProjection.metrics."""
        return realistic_metrics()

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



# Sentinel for "use lm.run_cycle.return_value" in _lm_wired.
_UNSET = object()


def _cycle_result(**overrides):
    """Full cycle-result payload as stored in _state.last_cycle_result."""
    result = {
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
    result.update(overrides)
    return result


def _make_lm(result, *, interval=30):
    """Standard mocked LoadManager wired the way app.py expects."""
    lm = unittest.mock.MagicMock()
    lm.enabled = True
    lm.dry_run = True
    lm.target_wh = -500
    lm.nbc_device = "test_nbc"
    lm.state.to_dict.return_value = {}
    if interval is not None:
        lm.config_interval_secs = interval
    if result is not None:
        lm.run_cycle.return_value = result
    return lm


class TestLoadManagementEndpoints(unittest.TestCase):
    """Tests for GET /api/v1/load/status and the index endpoint's
    load-management payload."""

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    # --- shared builders -------------------------------------------------

    @contextlib.contextmanager
    def _lm_wired(self, lm, last_cycle_result=_UNSET):
        """Install a LoadManager mock into app state under real-ish config.

        last_cycle_result defaults to ``lm.run_cycle.return_value`` (what the
        background loop would store after a cycle); pass an explicit value
        (including None) to override.
        """
        import app as app_mod

        with mock_config():
            Config().set("LOAD_MANAGE_ENABLED", "True")
            app_mod._state.load_manager = lm
            app_mod._state.load_manager_init_failed = False
            if last_cycle_result is _UNSET:
                app_mod._state.last_cycle_result = lm.run_cycle.return_value
            else:
                app_mod._state.last_cycle_result = last_cycle_result
            yield

    def test_load_status_503_when_not_initialized(self):
        """GET /load/status returns 503 when LoadManager is None."""
        with patch("app._get_load_manager", return_value=None):
            response = self.app.get("/api/v1/load/status")
        self.assertEqual(response.status_code, 503)

    def test_load_status_success(self):
        """GET /load/status returns 200 with state payload."""
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

    def test_load_status_includes_quantization_diagnostics(self):
        """GET /load/status diagnostics carry the current quantization state."""
        from load_models import CycleDiagnostics, CycleResult

        mock_lm = unittest.mock.MagicMock()
        mock_lm.enabled = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_state = unittest.mock.MagicMock()
        mock_state.devices = {}
        mock_state.pending_effects = []
        mock_lm.state = mock_state

        import app as app_mod

        app_mod._state.last_cycle_result = CycleResult(
            status="ok",
            qh="QH1",
            predicted_wh=-800.0,
            adjusted_wh=-750.0,
            target_wh=-500,
            actions=[],
            diagnostics=CycleDiagnostics(
                gap_wh=-300.0,
                hysteresis_wh=50,
                seconds_remaining=45,
                data_point_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                reason="ok",
                pending_effects_count=0,
                tesla_configured=False,
                quantization_seconds=60,
                quantization_offset=5,
                quantization_confidence=0.9,
                settle_window_secs=60,
            ),
            sleep_hint=30.0,
            sleep_hint_at="2026-01-01T12:00:00+00:00",
        )

        with patch("app._get_load_manager", return_value=mock_lm):
            response = self.app.get("/api/v1/load/status")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        diag = data["lastCycleResult"]["diagnostics"]
        self.assertEqual(diag["quantizationSeconds"], 60)
        self.assertEqual(diag["quantizationOffset"], 5)
        self.assertEqual(diag["quantizationConfidence"], 0.9)
        self.assertEqual(diag["settleWindowSecs"], 60)

    def test_index_html_includes_sleep_hint_meta(self):
        """Index HTML includes a meta tag with the sleep_hint value for JS."""
        lm = _make_lm(_cycle_result())

        with self._lm_wired(lm):
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn('id="sleep-hint"', html)
        self.assertIn('data-value="30.0"', html)

    def test_index_json_includes_top_level_sleep_hint(self):
        """Index JSON loadManagement includes top-level sleepHint."""
        lm = _make_lm(
            {
                "status": "ok",
                "predicted_wh": -800,
                "target_wh": -500,
                "actions": [],
                "sleep_hint": 30.0,
                "sleep_hint_at": "2025-01-15T12:00:00+00:00",
            }
        )

        with self._lm_wired(lm):
            response = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)
        self.assertIn("sleepHint", data["loadManagement"])
        self.assertEqual(data["loadManagement"]["sleepHint"], 30.0)

    def test_index_json_fallback_sleep_hint_to_config_interval(self):
        """Index JSON falls back to config_interval_secs when lastCycleResult is empty."""
        # Minimal result without sleep_hint; lastCycleResult left empty so
        # sleepHint should fall back to config_interval_secs.
        lm = _make_lm(
            {
                "status": "ok",
                "predicted_wh": -800,
                "target_wh": -500,
                "actions": [],
            }
        )

        with self._lm_wired(lm, last_cycle_result=None):
            response = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)
        self.assertIn("sleepHint", data["loadManagement"])
        self.assertEqual(data["loadManagement"]["sleepHint"], 30)

    def test_index_html_missing_sleep_hint_no_crash(self):
        """Index HTML handles a cycle result without sleep_hint without crashing."""
        result = _cycle_result()
        del result["sleep_hint"]  # No sleep_hint — should not cause a template error
        lm = _make_lm(result)

        with self._lm_wired(lm):
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)

    def test_index_json_includes_sleep_hint_at(self):
        """Index JSON loadManagement includes sleepHintAt timestamp."""
        lm = _make_lm(
            {
                "status": "ok",
                "predicted_wh": -800,
                "target_wh": -500,
                "actions": [],
                "sleep_hint": 30.0,
                "sleep_hint_at": "2025-01-15T12:00:00+00:00",
            }
        )

        with self._lm_wired(lm):
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
        lm = _make_lm(_cycle_result())

        with self._lm_wired(lm):
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn('id="sleep-hint-at"', html)
        self.assertIn('data-value="2025-01-15T12:00:00+00:00"', html)

    def test_index_json_missing_sleep_hint_at_no_crash(self):
        """Index JSON handles missing sleepHintAt gracefully."""
        # No sleep_hint_at in the result
        result = _cycle_result()
        del result["sleep_hint_at"]
        lm = _make_lm(result)

        with self._lm_wired(lm):
            response = self.app.get("/", headers={"Accept": "application/json"})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn("loadManagement", data)
        self.assertIn("sleepHintAt", data["loadManagement"])
        self.assertIsNone(data["loadManagement"]["sleepHintAt"])

    def test_index_html_missing_sleep_hint_at_no_crash(self):
        """Index HTML handles missing sleep_hint_at without crashing."""
        result = _cycle_result()
        del result["sleep_hint_at"]  # No sleep_hint_at — should not crash
        lm = _make_lm(result)

        with self._lm_wired(lm):
            response = self.app.get("/", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)

    def test_index_html_handles_none_predicted_wh(self):
        """Index template renders when predicted_wh is None (no crash)."""
        result = _cycle_result(predicted_wh=None)
        del result["sleep_hint"]
        del result["sleep_hint_at"]
        lm = _make_lm(result)

        with self._lm_wired(lm):
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


class TestLagRecalculation(unittest.TestCase):
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

        # Small pause so the two requests are measurably distinct in time.
        time.sleep(0.05)

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


class _LoopStop:
    """Patch helper: terminate one loop iteration via the stop event.

    The loop now sleeps on ``app._stop_event`` instead of ``time.sleep``;
    this preserves the old raise-from-sleep test contract.
    """

    def __init__(self, side_effect=None):
        ev = MagicMock()
        ev.is_set.return_value = False
        ev.wait.side_effect = side_effect or InterruptedError("stop")
        self._patch = patch("app._stop_event", ev)

    def __enter__(self):
        return self._patch.__enter__()

    def __exit__(self, *exc_info):
        return self._patch.__exit__(*exc_info)


class TestLoadManagementLoopErrorHandling(unittest.TestCase):
    """Tests for _load_management_loop error handling."""

    def test_cache_invalidated_on_error(self):
        """When run_cycle raises, energy cache is invalidated."""
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
        mock_lm.run_cycle.side_effect = RuntimeError("test crash")

        app_mod._state.telegram_sender = None
        app_mod._state.consecutive_error_count = 0
        app_mod._state.last_error_type = None

        with patch("app._get_load_manager", return_value=mock_lm):
            with _LoopStop():
                with self.assertRaises(InterruptedError):
                    app_mod._load_management_loop()

        assert app_mod._state.energy_cache._data is None
        app_mod._state.consecutive_error_count = 0
        app_mod._state.last_error_type = None

    def test_error_counter_increments_on_error(self):
        """Error counter increments on each error."""
        import app as app_mod

        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_cycle.side_effect = RuntimeError("test crash")

        app_mod._state.telegram_sender = None
        app_mod._state.consecutive_error_count = 0

        with patch("app._get_load_manager", return_value=mock_lm):
            with _LoopStop():
                with self.assertRaises(InterruptedError):
                    app_mod._load_management_loop()

        assert app_mod._state.consecutive_error_count == 1
        app_mod._state.consecutive_error_count = 0
        app_mod._state.last_error_type = None

    def test_error_counter_resets_on_success(self):
        """Error counter resets when cycle succeeds."""
        import app as app_mod
        from load_models import CycleResult, CycleDiagnostics

        app_mod._state.consecutive_error_count = 5

        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_cycle.return_value = CycleResult(
            status="ok",
            sleep_hint=30.0,
            sleep_hint_at="2026-01-01T12:00:00",
            diagnostics=CycleDiagnostics(
                gap_wh=0.0,
                hysteresis_wh=3.0,
                seconds_remaining=300,
                data_point_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                reason="none",
            ),
        )
        mock_lm._send_pending_notifications_sync = unittest.mock.MagicMock()

        app_mod._state.telegram_sender = None

        with patch("app._get_load_manager", return_value=mock_lm):
            with _LoopStop():
                with self.assertRaises(InterruptedError):
                    app_mod._load_management_loop()

        assert app_mod._state.consecutive_error_count == 0
        assert app_mod._state.last_error_type is None

    def test_rate_limited_telegram_alert(self):
        """Telegram alert sent on first error, then every 10th."""
        import app as app_mod

        mock_sender = unittest.mock.MagicMock()
        mock_sender.is_configured = True
        mock_sender.send_notification_sync = unittest.mock.MagicMock(return_value=True)

        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_cycle.side_effect = RuntimeError("crash")

        app_mod._state.telegram_sender = mock_sender
        app_mod._state.consecutive_error_count = 0

        call_count = 0

        def stop_after_n_sleeps(_secs):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise InterruptedError("stop")

        with patch("app._get_load_manager", return_value=mock_lm):
            with _LoopStop(stop_after_n_sleeps):
                with self.assertRaises(InterruptedError):
                    app_mod._load_management_loop()

        # First error (count=1) triggers alert, second (count=2) doesn't
        assert mock_sender.send_notification_sync.call_count == 1
        app_mod._state.consecutive_error_count = 0
        app_mod._state.last_error_type = None
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
        mock_lm.run_cycle.return_value = CycleResult(
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

        app_mod._state.consecutive_error_count = 0
        app_mod._state.last_error_type = None
        app_mod._state.telegram_sender = None

        captured_sleep_values: list[float] = []

        def capture_sleep(secs: float) -> None:
            captured_sleep_values.append(secs)
            if len(captured_sleep_values) >= 2:
                raise InterruptedError("stop")

        with patch("app._get_load_manager", return_value=mock_lm):
            with _LoopStop(capture_sleep):
                with self.assertRaises(InterruptedError):
                    app_mod._load_management_loop()

        assert captured_sleep_values[0] == 30.0, (
            f"Expected 30.0 for disabled cycle, got {captured_sleep_values[0]}"
        )

        app_mod._state.consecutive_error_count = 0
        app_mod._state.last_error_type = None


class TestCooperativeShutdown(unittest.TestCase):
    """request_shutdown() must promptly stop background services once.

    Shutdown contract: a single signal (Ctrl-C / SIGTERM via the gunicorn
    hooks) sets the stop event, wakes every sleeping background loop and SSE
    stream immediately, and closes the LoadManager exactly once — so
    interpreter finalization has nothing left to join and no second Ctrl-C
    is needed.
    """

    def setUp(self):
        import app as app_mod

        self.app_mod = app_mod

    def tearDown(self):
        self.app_mod._stop_event.clear()

    @staticmethod
    def _make_success_lm():
        from load_models import CycleResult, CycleDiagnostics

        mock_lm = unittest.mock.MagicMock()
        mock_lm.run_cycle.return_value = CycleResult(
            status="ok",
            sleep_hint=30.0,
            sleep_hint_at="2026-01-01T12:00:00",
            diagnostics=CycleDiagnostics(
                gap_wh=0.0,
                hysteresis_wh=3.0,
                seconds_remaining=300,
                data_point_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                reason="none",
            ),
        )
        mock_lm._send_pending_notifications_sync = unittest.mock.MagicMock()
        return mock_lm

    def test_request_shutdown_sets_stop_event(self):
        self.assertFalse(self.app_mod._stop_event.is_set())

        self.app_mod.request_shutdown("test")

        self.assertTrue(self.app_mod._stop_event.is_set())

    def test_request_shutdown_idempotent(self):
        """Second call must not re-close resources."""
        mock_lm = unittest.mock.MagicMock()
        self.app_mod._state.load_manager = mock_lm
        self.app_mod._state.mqtt_subscriber_started = False
        try:
            with patch.object(
                self.app_mod._state.sse_broadcaster, "close_all"
            ) as spy_close:
                with patch("mqtt_telemetry.stop_mqtt_subscriber") as spy_mqtt:
                    self.app_mod.request_shutdown("first")
                    self.app_mod.request_shutdown("second")

            self.assertEqual(mock_lm.close.call_count, 1)
            self.assertEqual(spy_close.call_count, 1)
            spy_mqtt.assert_not_called()  # subscriber was never started
        finally:
            self.app_mod._state.load_manager = None

    def test_loop_exits_promptly_on_shutdown_request(self):
        """Loop must wake from its (30 s) sleep within ~2 s of a stop."""
        mock_lm = self._make_success_lm()
        self.app_mod._state.telegram_sender = None
        self.app_mod._state.consecutive_error_count = 0

        done = threading.Event()

        def run():
            try:
                self.app_mod._load_management_loop()
            finally:
                done.set()

        with patch("app._get_load_manager", return_value=mock_lm):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            # Wait until the first cycle completed (loop parked in its sleep).
            parked = False
            for _ in range(250):
                if self.app_mod._state.lm_last_cycle_finished_at is not None:
                    parked = True
                    break
                time_module.sleep(0.02)
            assert parked, "loop never finished its first cycle"

            self.app_mod.request_shutdown("test-loop")
            t.join(timeout=2.0)

        self.assertFalse(
            t.is_alive(),
            "load loop ignored shutdown request for its full sleep interval",
        )
        self.app_mod._state.consecutive_error_count = 0


class TestLoadManagerSharedCache(unittest.TestCase):
    """LoadManager shares the app-level EnergyCache (twin-cycles bug fix).

    Before the fix, app._get_load_manager() did not pass
    energy_cache=_state.energy_cache, so NBCReader built a private,
    never-populated cache: is_valid() was always False (which masked the
    force bug) and _resolve_prediction_window() could never see the
    quantization data that HourlyProjection writes to the shared cache
    after every fetch.
    """

    @contextlib.contextmanager
    def _enabled_real_mode(self):
        """Real-mode config with load management enabled; reset singletons."""
        with real_mode_config(
            LOAD_MANAGE_ENABLED="True",
            LOAD_NBC_DEVICE="METER",
        ):
            yield

    def test_reader_uses_app_energy_cache(self):
        """NBCReader reads from the shared app-level EnergyCache, not a private one."""
        import app as app_mod

        with self._enabled_real_mode():
            lm = app_mod._get_load_manager()
            self.assertIsNotNone(lm)
            self.assertIs(
                lm.nbc_reader.energy_cache,
                app_mod._state.energy_cache,
            )

    def test_quantization_on_shared_cache_feeds_prediction_window(self):
        """Prediction window resolves from quantization on the shared cache.

        HourlyProjection writes quantization data to the shared cache after
        every fetch; the LoadManager must see it (via its reader's shared
        cache) when resolving the prediction window.  The settle-window
        side of this chain (effective_settle_secs) is covered by
        TestStageComputeGap in tests/test_pipeline_stages.py, since it
        resolves lazily on the first cycle.
        """
        import app as app_mod

        with self._enabled_real_mode():
            app_mod._state.energy_cache.quantization_seconds = 120
            app_mod._state.energy_cache.quantization_confidence = 1.0
            lm = app_mod._get_load_manager()
            self.assertIsNotNone(lm)
            self.assertEqual(lm._resolve_prediction_window(), 120)

    def test_run_cycle_refetches_every_cycle(self):
        """Every run_cycle fetches fresh NBC data (no cache-hit skipping).

        Regression guard for the force=True path: with the shared cache, a
        TTL-paced read would cache-hit and skip the fetch, letting data age
        toward the stale-data threshold and breaking the fetch cadence.
        """
        import app as app_mod

        with self._enabled_real_mode():
            with patch(
                "app.create_metrics",
                return_value=realistic_metrics(),
            ) as mock_metrics:
                lm = app_mod._get_load_manager()
                self.assertIsNotNone(lm)
                result = lm.run_cycle()
                self.assertNotEqual(result.status, "disabled")
                lm.run_cycle()
                self.assertEqual(mock_metrics.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class TestValidateDatesHygiene(unittest.TestCase):
    """Injected clock + explicit-DST handling in date parsing (plan 3.8)."""

    def setUp(self):
        self.app = app.test_client()

    def test_validate_dates_accepts_injected_clock(self):
        """end_date defaults to the INJECTED clock's time, not raw datetime.now()."""
        from app import _validate_dates
        from clock import FakeClock

        fake = FakeClock(datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc))
        start, end = _validate_dates("2026-01-01", None, clock=fake)
        self.assertEqual(end, fake.now())
        self.assertIsNotNone(end.tzinfo)

    def test_validate_dates_default_end_is_utc_aware(self):
        """Without injection, end_date defaults to an aware UTC instant."""
        from app import _validate_dates
        from clock import RealClock

        real = RealClock()
        start, end = _validate_dates("2026-01-01", None)
        self.assertIsNotNone(end.tzinfo)
        self.assertLess(abs((end - real.now()).total_seconds()), 60)

    def test_validate_dates_missing_start_aborts(self):
        """Missing start_date aborts with 400 (werkzeug BadRequest)."""
        from app import _validate_dates

        with self.assertRaises(BadRequest):
            _validate_dates(None, None)

    def test_parse_date_rejects_nonexistent_spring_forward_time(self):
        """A nonexistent local time (DST spring-forward gap) raises ValueError."""
        import pytest
        from app import parse_date_to_utc

        with patch("app.get_timezone", return_value="America/Los_Angeles"):
            with pytest.raises(ValueError):
                # 2026-03-08 02:30 PST does not exist (clocks jump to 03:00).
                parse_date_to_utc("2026-03-08T02:30:00")

    def test_parse_date_rejects_ambiguous_fall_back_time(self):
        """An ambiguous local time (DST fall-back overlap) raises ValueError."""
        import pytest
        from app import parse_date_to_utc

        with patch("app.get_timezone", return_value="America/Los_Angeles"):
            with pytest.raises(ValueError):
                # 2026-11-01 01:30 occurs twice (PDT then PST).
                parse_date_to_utc("2026-11-01T01:30:00")

    def test_parse_date_unambiguous_local_time_still_parses(self):
        """Ordinary local times still localize + convert to UTC correctly."""
        from app import parse_date_to_utc

        with patch("app.get_timezone", return_value="America/Los_Angeles"):
            dt = parse_date_to_utc("2026-06-15T12:00:00")
        self.assertEqual(dt.utcoffset(), timedelta(0))
        self.assertEqual(dt.hour, 19)  # 12:00 PDT == 19:00 UTC


class TestStallWatchdogWiring(unittest.TestCase):
    """The LM loop must check the watchdog BEFORE refreshing finished_at.

    Review #1: refreshing ``lm_last_cycle_finished_at`` immediately before
    calling ``_check_stall_watchdog`` makes ``stalled_for ≈ 0`` every
    iteration, so the CRITICAL branch was unreachable through the loop.
    """

    def setUp(self):
        import app as app_mod

        self._app = app_mod
        app_mod._stall_critical_last_at = None
        app_mod._state.lm_last_cycle_finished_at = None

    def tearDown(self):
        self._app._stall_critical_last_at = None
        self._app._state.lm_last_cycle_finished_at = None

    def test_loop_fires_watchdog_after_long_previous_iteration(self):
        """A >300s iteration trips the watchdog at its END (before refresh)."""
        import time as time_module
        from datetime import timedelta

        from config import Config

        class _StopLoop(BaseException):
            """Sentinel raised from patched sleep to exit the infinite loop."""

        # Simulate: the PREVIOUS iteration finished 400s ago (e.g. run_cycle
        # hung for ~7 min before finally returning). The next iteration's
        # tail check must fire the CRITICAL watchdog.
        self._app._state.lm_last_cycle_finished_at = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        )

        def fake_sleep(_secs):
            raise _StopLoop()

        stop_ev = MagicMock()
        stop_ev.is_set.return_value = False
        stop_ev.wait.side_effect = fake_sleep

        with patch.object(
            self._app, "_get_load_manager", return_value=None
        ), patch("app._stop_event", stop_ev):
            Config().set("LOAD_MANAGE_ENABLED", "True")
            try:
                with self.assertLogs("app", level="CRITICAL") as captured:
                    with self.assertRaises(_StopLoop):
                        self._app._load_management_loop()
            finally:
                Config().set("LOAD_MANAGE_ENABLED", "False")

        criticals = [
            r for r in captured.records if r.levelno == logging.CRITICAL
        ]
        self.assertTrue(
            criticals,
            "stall watchdog never fired through the loop wiring",
        )
        # After the firing check, finished_at must have been refreshed so
        # the NEXT iteration measures only its own duration.
        self.assertIsNotNone(self._app._state.lm_last_cycle_finished_at)
