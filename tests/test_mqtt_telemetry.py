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
        from mqtt_telemetry import start_mqtt_subscriber, stop_mqtt_subscriber
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

        # Stop the subscriber before it can construct a real paho client:
        # once the patch above exits, the leaked daemon thread would wake
        # from its reconnect backoff, build a real mqtt.Client() (emitting
        # a DeprecationWarning that pytest attributes to whatever test is
        # running at the time), and keep retrying for the rest of the
        # session. stop_mqtt_subscriber() wakes the backoff wait and makes
        # the loop exit.
        stop_mqtt_subscriber()
        subscriber = next(
            t for t in threading.enumerate() if t.name == "mqtt-subscriber"
        )
        subscriber.join(timeout=5)
        assert not subscriber.is_alive(), (
            "mqtt-subscriber thread leaked past the test — it would keep "
            "reconnecting in the background for the rest of the session"
        )

    def test_double_start_is_idempotent(self):
        """A second start while the subscriber is alive must be a no-op.

        Re-starting clears _stop_event and the active-client registration,
        which would corrupt the running subscriber's shutdown path and
        health reporting — so a duplicate start must never spawn a second
        session.
        """
        from mqtt_telemetry import start_mqtt_subscriber, stop_mqtt_subscriber
        from config import Config

        cfg = Config(overrides={
            "MQTT_HOST": "localhost",
            "MQTT_PORT": "1883",
            "MQTT_TOPIC_BASE": "tesla",
        })

        def subscriber_threads():
            return [t for t in threading.enumerate() if t.name == "mqtt-subscriber"]

        with patch("mqtt_telemetry.mqtt.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.loop_forever.side_effect = Exception("stop")

            start_mqtt_subscriber(cfg)
            time.sleep(0.05)
            threads_after_first = subscriber_threads()

            start_mqtt_subscriber(cfg)  # must be a no-op
            threads_after_second = subscriber_threads()

            stop_mqtt_subscriber()
            for t in threads_after_second:
                t.join(timeout=5)

        assert len(threads_after_first) == 1, (
            f"expected one subscriber thread after first start, "
            f"got {len(threads_after_first)}"
        )
        assert len(threads_after_second) == 1, (
            f"second start spawned a duplicate subscriber "
            f"({len(threads_after_second)} threads)"
        )
        assert mock_client_cls.call_count == 1, (
            "second start constructed a second MQTT session"
        )
        assert all(not t.is_alive() for t in threads_after_second), (
            "subscriber thread leaked past stop"
        )

    def test_stop_clears_session_so_prompt_restart_is_allowed(self, monkeypatch):
        """After stop_mqtt_subscriber(), a new start must be permitted.

        Even if the previous subscriber thread is technically still alive
        (winding down its backoff wait), an explicit stop ends the session:
        the stored reference must be cleared so the next start is not
        swallowed by the duplicate-start guard.
        """
        from mqtt_telemetry import _subscriber_thread, stop_mqtt_subscriber
        import mqtt_telemetry

        blocker = threading.Event()
        zombie = threading.Thread(target=blocker.wait, daemon=True)
        zombie.start()
        monkeypatch.setattr(mqtt_telemetry, "_subscriber_thread", zombie)
        try:
            assert zombie.is_alive()  # precondition: reference looks live

            stop_mqtt_subscriber()

            assert mqtt_telemetry._subscriber_thread is None, (
                "stop must clear the stored subscriber reference so a "
                "fresh start is allowed"
            )
        finally:
            blocker.set()

    def test_restart_supersedes_lingering_old_session(self):
        """stop→start while the old thread is parked in loop_forever().

        The prompt-restart path must not revive the superseded session:
        when the old thread's disconnect finally lands, it must exit rather
        than reconnect — and its teardown must not clear the new session's
        active-client registration or health flag.
        """
        from mqtt_telemetry import (
            _get_active_client,
            start_mqtt_subscriber,
            stop_mqtt_subscriber,
        )
        import mqtt_telemetry
        from config import Config

        cfg = Config(overrides={
            "MQTT_HOST": "localhost",
            "MQTT_PORT": "1883",
            "MQTT_TOPIC_BASE": "tesla",
        })

        clients: list[MagicMock] = []

        def make_client():
            client = MagicMock()
            release = threading.Event()

            def _park() -> None:
                # Simulate a live network loop: parked until disconnected.
                release.wait(timeout=10)

            client.loop_forever.side_effect = _park
            client._test_release = release
            clients.append(client)
            return client

        def subscriber_threads():
            return [
                t for t in threading.enumerate()
                if t.name == "mqtt-subscriber"
            ]

        with patch("mqtt_telemetry.mqtt.Client", side_effect=make_client), \
             patch.object(mqtt_telemetry, "_MQTT_RECONNECT_MIN_SECS", 0.05):
            start_mqtt_subscriber(cfg)
            time.sleep(0.05)  # let S1 park inside loop_forever
            threads_after_first = subscriber_threads()
            assert len(threads_after_first) == 1
            old_thread = threads_after_first[0]

            stop_mqtt_subscriber()
            start_mqtt_subscriber(cfg)  # prompt restart while S1 unwinds
            time.sleep(0.05)

            assert len(clients) == 2, "restart did not create a new session"
            new_client = clients[1]
            assert _get_active_client() is new_client

            clients[0]._test_release.set()  # S1's disconnect finally lands
            old_thread.join(timeout=5)

            assert not old_thread.is_alive(), (
                "superseded session kept running after restart"
            )
            assert len(clients) == 2, (
                f"superseded session reconnected "
                f"({len(clients)} sessions constructed)"
            )
            assert _get_active_client() is new_client, (
                "superseded session's teardown clobbered the new session's "
                "active-client registration"
            )

            stop_mqtt_subscriber()
            for t in subscriber_threads():
                t.join(timeout=5)
