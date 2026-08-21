"""Tests for the component-aware /health endpoint (plan subtask 1.5).

The endpoint must move beyond a static text "ok": deploy tooling needs to
distinguish a healthy instance from one whose load-management thread died,
whose Emporia feed went stale, or whose MQTT telemetry went dark — without
false-positive restart storms during boot or when load management is off.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import app as app_mod
import mqtt_telemetry
from config import Config
from energy_cache import EnergyCacheData
from load_models import CycleResult


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def reset_health_state():
    """Reset app state read by the health builder."""
    app_mod._state.lm_heartbeat_at = None
    app_mod._state.mqtt_subscriber_started = False
    app_mod._state.background_services_started_at = None
    app_mod._state.consecutive_error_count = 0
    app_mod._state.last_error_type = None
    app_mod._state.energy_cache._data = None
    yield
    app_mod._state.lm_heartbeat_at = None
    app_mod._state.mqtt_subscriber_started = False
    app_mod._state.background_services_started_at = None
    app_mod._state.consecutive_error_count = 0
    app_mod._state.last_error_type = None
    app_mod._state.energy_cache._data = None


def _fresh_cache(now: datetime) -> EnergyCacheData:
    return EnergyCacheData(
        samples=[0.1] * 100,
        data_start=now - timedelta(seconds=50),
        last_sample_at=now - timedelta(seconds=1),
        last_fetch_at=now - timedelta(seconds=1),
        sample_count=100,
    )


class TestHealthPayload:
    """_build_health_payload(now) gating rules."""

    def test_ok_in_default_test_state(self):
        """Disabled LM + unstarted MQTT is a healthy (idle) instance."""
        payload = app_mod._build_health_payload(_now())
        assert payload["status"] == "ok"
        components = payload["components"]
        assert components["load_manager_thread"]["state"] == "disabled"
        assert components["mqtt_telemetry"]["state"] == "not_started"
        assert components["energy_cache"]["state"] == "empty"

    def test_stale_heartbeat_degrades(self):
        """A dead LM thread (enabled but heartbeat too old) degrades."""
        Config().set("LOAD_MANAGE_ENABLED", "True")
        app_mod._state.lm_heartbeat_at = _now() - timedelta(seconds=9999)
        payload = app_mod._build_health_payload(_now())
        assert payload["components"]["load_manager_thread"]["state"] == "stale"
        assert payload["status"] == "degraded"

    def test_never_run_heartbeat_is_reported_not_gated(self):
        """Enabled LM that hasn't ticked yet (boot grace) is informational."""
        Config().set("LOAD_MANAGE_ENABLED", "True")
        payload = app_mod._build_health_payload(_now())
        assert (
            payload["components"]["load_manager_thread"]["state"]
            == "never_run"
        )
        assert payload["status"] == "ok"

    def test_recent_heartbeat_alive(self):
        """Recent heartbeat with LM enabled reports alive."""
        Config().set("LOAD_MANAGE_ENABLED", "True")
        app_mod._state.lm_heartbeat_at = _now() - timedelta(seconds=5)
        payload = app_mod._build_health_payload(_now())
        assert payload["components"]["load_manager_thread"]["state"] == "alive"
        assert payload["status"] == "ok"

    def test_sustained_errors_degrade(self):
        """Three or more consecutive cycle errors degrade health."""
        app_mod._state.consecutive_error_count = 3
        app_mod._state.last_error_type = "RuntimeError"
        payload = app_mod._build_health_payload(_now())
        assert payload["components"]["errors"]["consecutive_error_count"] == 3
        assert payload["components"]["errors"]["last_error_type"] == "RuntimeError"
        assert payload["status"] == "degraded"

    def test_single_error_does_not_degrade(self):
        """A single transient error must not flip overall health."""
        app_mod._state.consecutive_error_count = 1
        payload = app_mod._build_health_payload(_now())
        assert payload["status"] == "ok"

    def test_mqtt_dark_after_receiving_degrades(self, monkeypatch):
        """Started MQTT whose updates stopped arriving degrades."""
        app_mod._state.mqtt_subscriber_started = True
        monkeypatch.setattr(
            mqtt_telemetry,
            "_field_update_at",
            {"ChargeAmps": _now() - timedelta(seconds=900)},
        )
        payload = app_mod._build_health_payload(_now())
        assert payload["components"]["mqtt_telemetry"]["state"] == "dark"
        assert payload["status"] == "degraded"

    def test_mqtt_receiving_is_ok(self, monkeypatch):
        """Fresh MQTT updates report receiving."""
        app_mod._state.mqtt_subscriber_started = True
        monkeypatch.setattr(
            mqtt_telemetry,
            "_field_update_at",
            {"ChargeAmps": _now() - timedelta(seconds=30)},
        )
        payload = app_mod._build_health_payload(_now())
        assert (
            payload["components"]["mqtt_telemetry"]["state"] == "receiving"
        )
        assert payload["status"] == "ok"

    def test_mqtt_waiting_beyond_boot_grace_degrades(
        self, monkeypatch
    ):
        """Started MQTT that never received anything degrades after grace."""
        app_mod._state.mqtt_subscriber_started = True
        app_mod._state.background_services_started_at = _now() - timedelta(
            seconds=400
        )
        monkeypatch.setattr(mqtt_telemetry, "_field_update_at", {})
        payload = app_mod._build_health_payload(_now())
        assert payload["components"]["mqtt_telemetry"]["state"] == "waiting"
        assert payload["status"] == "degraded"

    def test_mqtt_waiting_within_grace_not_gated(self, monkeypatch):
        """Started MQTT still inside boot grace is informational."""
        app_mod._state.mqtt_subscriber_started = True
        app_mod._state.background_services_started_at = _now() - timedelta(
            seconds=10
        )
        monkeypatch.setattr(mqtt_telemetry, "_field_update_at", {})
        payload = app_mod._build_health_payload(_now())
        assert payload["components"]["mqtt_telemetry"]["state"] == "waiting"
        assert payload["status"] == "ok"

    def test_cache_stale_gates_only_when_lm_enabled(self):
        """Stale cache degrades only when the LM thread depends on it."""
        now = _now()
        app_mod._state.energy_cache._data = EnergyCacheData(
            samples=[0.1] * 100,
            data_start=now - timedelta(minutes=10),
            last_sample_at=now - timedelta(minutes=8, seconds=41),
            last_fetch_at=now - timedelta(minutes=9),
            sample_count=100,
            quantization_seconds=30,
            quantization_offset=0,
            quantization_confidence=1.0,
        )

        # LM disabled (default test env): informational only.
        payload = app_mod._build_health_payload(now)
        assert payload["components"]["energy_cache"]["state"] == "stale"
        assert payload["status"] == "ok"

        # LM enabled: stale feed breaks the control loop's inputs.
        Config().set("LOAD_MANAGE_ENABLED", "True")
        payload = app_mod._build_health_payload(now)
        assert payload["components"]["energy_cache"]["state"] == "stale"
        assert payload["status"] == "degraded"


class TestHealthEndpoint:
    """/health serves the JSON payload over HTTP."""

    def test_route_returns_json_with_components(self):
        client = app_mod.app.test_client()
        response = client.get("/health")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ok"
        assert "components" in data
        assert "loadManagerThread" in data["components"]
        assert "energyCache" in data["components"]
        assert "mqttTelemetry" in data["components"]
        assert "errors" in data["components"]


class TestHeartbeatWiring:
    """The LM loop records its liveness heartbeat each iteration."""

    def test_loop_updates_heartbeat(self):
        mock_lm = MagicMock()
        mock_lm.run_cycle.return_value = CycleResult(
            status="disabled", sleep_hint=30.0
        )
        mock_lm._send_pending_notifications_sync = MagicMock()

        app_mod._state.telegram_sender = None
        try:
            with patch("app._get_load_manager", return_value=mock_lm):
                with patch(
                    "app.time.sleep", side_effect=InterruptedError("stop")
                ):
                    with pytest.raises(InterruptedError):
                        app_mod._load_management_loop()

            assert app_mod._state.lm_heartbeat_at is not None
        finally:
            app_mod._state.consecutive_error_count = 0
            app_mod._state.last_error_type = None
