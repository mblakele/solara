"""Tests for mqtt_telemetry module — MQTT subscriber and telemetry state."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestGetTelemetrySnapshot:
    """get_telemetry_snapshot() returns a thread-safe copy of _telemetry_state."""

    def setup_method(self):
        import mqtt_telemetry as mt
        mt._telemetry_state.clear()

    def test_returns_empty_dict_initially(self):
        from mqtt_telemetry import get_telemetry_snapshot
        assert get_telemetry_snapshot() == {}

    def test_returns_copy_not_reference(self):
        import mqtt_telemetry as mt
        from mqtt_telemetry import get_telemetry_snapshot
        mt._telemetry_state["DetailedChargeState"] = "DetailedChargeStateCharging"
        snap = get_telemetry_snapshot()
        snap["DetailedChargeState"] = "Disconnected"
        assert mt._telemetry_state["DetailedChargeState"] == "DetailedChargeStateCharging"

    def test_snapshot_reflects_current_state(self):
        import mqtt_telemetry as mt
        from mqtt_telemetry import get_telemetry_snapshot
        mt._telemetry_state["DetailedChargeState"] = "DetailedChargeStateComplete"
        mt._telemetry_state["ChargeAmps"] = 16.0
        snap = get_telemetry_snapshot()
        assert snap["DetailedChargeState"] == "DetailedChargeStateComplete"
        assert snap["ChargeAmps"] == 16.0


class TestHasTelemetry:
    """has_telemetry() returns True iff _telemetry_state is non-empty."""

    def setup_method(self):
        import mqtt_telemetry as mt
        mt._telemetry_state.clear()

    def test_false_when_empty(self):
        from mqtt_telemetry import has_telemetry
        assert has_telemetry() is False

    def test_true_after_update(self):
        import mqtt_telemetry as mt
        from mqtt_telemetry import has_telemetry
        mt._telemetry_state["DetailedChargeState"] = "DetailedChargeStateCharging"
        assert has_telemetry() is True

    def test_false_after_clear(self):
        import mqtt_telemetry as mt
        from mqtt_telemetry import has_telemetry
        mt._telemetry_state["DetailedChargeState"] = "DetailedChargeStateCharging"
        mt._telemetry_state.clear()
        assert has_telemetry() is False


class TestOnMessage:
    """on_message() parses MQTT payloads and updates _telemetry_state."""

    def setup_method(self):
        import mqtt_telemetry as mt
        mt._telemetry_state.clear()

    def _make_msg(self, topic: str, payload: bytes) -> MagicMock:
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload
        return msg

    def test_envelope_value_field(self):
        from mqtt_telemetry import on_message
        msg = self._make_msg("tesla/DetailedChargeState", json.dumps({"value": "DetailedChargeStateCharging"}).encode())
        on_message(None, None, msg)
        import mqtt_telemetry as mt
        assert mt._telemetry_state["DetailedChargeState"] == "DetailedChargeStateCharging"

    def test_raw_scalar_payload(self):
        from mqtt_telemetry import on_message
        msg = self._make_msg("tesla/ChargeAmps", json.dumps(16.0).encode())
        on_message(None, None, msg)
        import mqtt_telemetry as mt
        assert mt._telemetry_state["ChargeAmps"] == 16.0

    def test_location_object_payload(self):
        from mqtt_telemetry import on_message
        loc = {"latitude": 37.7749, "longitude": -122.4194}
        msg = self._make_msg("tesla/Location", json.dumps({"value": loc}).encode())
        on_message(None, None, msg)
        import mqtt_telemetry as mt
        assert mt._telemetry_state["Location"] == loc

    def test_invalid_json_ignored(self):
        from mqtt_telemetry import on_message
        msg = self._make_msg("tesla/DetailedChargeState", b"not-json")
        on_message(None, None, msg)  # must not raise
        import mqtt_telemetry as mt
        assert "DetailedChargeState" not in mt._telemetry_state

    def test_uses_last_topic_segment_as_key(self):
        from mqtt_telemetry import on_message
        msg = self._make_msg("vehicles/1/DetailedChargeState", json.dumps({"value": "DetailedChargeStateComplete"}).encode())
        on_message(None, None, msg)
        import mqtt_telemetry as mt
        assert mt._telemetry_state["DetailedChargeState"] == "DetailedChargeStateComplete"

    def test_multiple_fields_accumulate(self):
        from mqtt_telemetry import on_message
        on_message(None, None, self._make_msg("t/DetailedChargeState", json.dumps("DetailedChargeStateCharging").encode()))
        on_message(None, None, self._make_msg("t/ChargeAmps", json.dumps({"value": 32.0}).encode()))
        import mqtt_telemetry as mt
        assert mt._telemetry_state["DetailedChargeState"] == "DetailedChargeStateCharging"
        assert mt._telemetry_state["ChargeAmps"] == 32.0

    def test_overwrite_existing_key(self):
        import mqtt_telemetry as mt
        from mqtt_telemetry import on_message
        mt._telemetry_state["DetailedChargeState"] = "DetailedChargeStateCharging"
        msg = self._make_msg("t/DetailedChargeState", json.dumps({"value": "DetailedChargeStateComplete"}).encode())
        on_message(None, None, msg)
        assert mt._telemetry_state["DetailedChargeState"] == "DetailedChargeStateComplete"


class TestTeslaStateFromSnapshot:
    """tesla_state_from_snapshot() converts a snapshot dict to TeslaState."""

    def setup_method(self):
        import mqtt_telemetry as mt
        mt._telemetry_state.clear()

    def _state(self, **kwargs):
        from mqtt_telemetry import tesla_state_from_snapshot
        return tesla_state_from_snapshot(kwargs)

    def test_returns_none_on_empty_snapshot(self):
        from mqtt_telemetry import tesla_state_from_snapshot
        assert tesla_state_from_snapshot({}) is None

    def test_charging_state_is_charging(self):
        ts = self._state(DetailedChargeState="DetailedChargeStateCharging")
        assert ts is not None
        assert ts.is_charging is True

    def test_charging_state_disconnected(self):
        ts = self._state(DetailedChargeState="DetailedChargeStateDisconnected")
        assert ts is not None
        assert ts.is_charging is False
        assert ts.plugged_in is False

    def test_plugged_in_when_not_disconnected(self):
        ts = self._state(DetailedChargeState="DetailedChargeStateComplete")
        assert ts is not None
        assert ts.plugged_in is True

    def test_charge_amps_float(self):
        ts = self._state(DetailedChargeState="DetailedChargeStateCharging", ChargeAmps=16.0)
        assert ts is not None
        assert ts.current_amps == 16

    def test_charge_amps_none_when_absent(self):
        ts = self._state(DetailedChargeState="DetailedChargeStateCharging")
        assert ts is not None
        assert ts.current_amps is None

    def test_at_home_true_when_within_radius(self):
        loc = {"latitude": 37.7749, "longitude": -122.4194}
        with patch("config.Config") as mock_config_cls:
            mock_cfg = MagicMock()
            mock_cfg.tesla_home_lat = 37.7749
            mock_cfg.tesla_home_lon = -122.4194
            mock_config_cls.return_value = mock_cfg
            ts = self._state(DetailedChargeState="DetailedChargeStateCharging", Location=loc)
        assert ts is not None
        assert ts.at_home is True

    def test_at_home_false_when_outside_radius(self):
        """Car 5 km from home with 500 m default radius → at_home=False."""
        loc = {"latitude": 37.8, "longitude": -122.5}
        with patch("config.Config") as mock_config_cls:
            mock_cfg = MagicMock()
            mock_cfg.tesla_home_lat = 37.7749
            mock_cfg.tesla_home_lon = -122.4194
            mock_config_cls.return_value = mock_cfg
            ts = self._state(DetailedChargeState="DetailedChargeStateCharging", Location=loc)
        assert ts is not None
        assert ts.at_home is False

    def test_at_home_false_when_no_env_coords(self):
        """No home coords in .env → at_home=False regardless of Location."""
        loc = {"latitude": 37.7749, "longitude": -122.4194}
        with patch("config.Config") as mock_config_cls:
            mock_cfg = MagicMock()
            mock_cfg.tesla_home_lat = None
            mock_cfg.tesla_home_lon = None
            mock_config_cls.return_value = mock_cfg
            ts = self._state(DetailedChargeState="DetailedChargeStateCharging", Location=loc)
        assert ts is not None
        assert ts.at_home is False  # No coords → haversine skipped

    def test_at_home_with_env_coords_matching_telemetry(self):
        """Regression: exact production coords (37.55303, -122.25198) → at_home=True."""
        loc = {"latitude": 37.55303, "longitude": -122.25198}
        with patch("config.Config") as mock_config_cls:
            mock_cfg = MagicMock()
            mock_cfg.tesla_home_lat = 37.55303
            mock_cfg.tesla_home_lon = -122.25198
            mock_config_cls.return_value = mock_cfg
            ts = self._state(DetailedChargeState="DetailedChargeStateCharging", Location=loc)
        assert ts is not None
        assert ts.at_home is True  # Identical coords → distance=0 → within any radius

    # ── Partial snapshot (ChargeAmps without DetailedChargeState) ──────────

    def test_returns_state_when_charge_amps_without_detailed(self):
        """ChargeAmps > 0 without DetailedChargeState → inferred charging."""
        ts = self._state(ChargeAmps=6.0)
        assert ts is not None
        assert ts.is_charging is True
        assert ts.plugged_in is True
        assert ts.current_amps == 6

    def test_returns_none_when_charge_amps_zero_without_detailed(self):
        """ChargeAmps=0 without DetailedChargeState → can't infer, return None."""
        ts = self._state(ChargeAmps=0.0)
        assert ts is None

    def test_returns_none_when_only_location_without_detailed(self):
        """Only Location without DetailedChargeState or ChargeAmps → None."""
        ts = self._state(Location={"latitude": 37.7749, "longitude": -122.4194})
        assert ts is None

    def test_partial_snapshot_still_computes_at_home(self):
        """Partial snapshot with ChargeAmps and Location still computes at_home."""
        loc = {"latitude": 37.7749, "longitude": -122.4194}
        with patch("config.Config") as mock_config_cls:
            mock_cfg = MagicMock()
            mock_cfg.tesla_home_lat = 37.7749
            mock_cfg.tesla_home_lon = -122.4194
            mock_config_cls.return_value = mock_cfg
            ts = self._state(ChargeAmps=16.0, Location=loc)
        assert ts is not None
        assert ts.current_amps == 16
        assert ts.is_charging is True
        assert ts.at_home is True

    def test_partial_snapshot_with_envelope_charge_amps(self):
        """ChargeAmps in fleet-telemetry envelope format works without DetailedChargeState."""
        ts = self._state(ChargeAmps={"value": 6.0})
        assert ts is not None
        assert ts.is_charging is True
        assert ts.current_amps == 6

    def test_at_home_uses_devices_json_radius(self):
        """home_radius_m from devices.json overrides 500.0 default."""
        loc = {"latitude": 37.7749, "longitude": -122.4194}
        with patch("config.Config") as mock_config_cls:
            mock_cfg = MagicMock()
            mock_cfg.tesla_home_lat = 37.7749
            mock_cfg.tesla_home_lon = -122.4194
            mock_config_cls.return_value = mock_cfg
            with patch("device_config.get_tesla_config", return_value={"home_radius_m": 100}):
                ts = self._state(DetailedChargeState="DetailedChargeStateCharging", Location=loc)
        assert ts is not None
        assert ts.at_home is True  # 100 m radius, same coords


class TestStartMqttSubscriber:
    """start_mqtt_subscriber() starts the background MQTT thread."""

    def test_starts_daemon_thread(self):
        from mqtt_telemetry import start_mqtt_subscriber
        from config import Config

        cfg = Config(overrides={
            "MQTT_HOST": "localhost",
            "MQTT_PORT": "1883",
            "MQTT_TOPIC_BASE": "tesla",
        })

        threads_before = threading.active_count()
        with patch("mqtt_telemetry.mqtt.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.loop_forever.side_effect = Exception("stop")

            start_mqtt_subscriber(cfg)
            time.sleep(0.05)

        # A new daemon thread should have been spawned
        assert threading.active_count() >= threads_before
