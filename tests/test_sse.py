"""Tests for SSE streaming support."""  # noqa: D01

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from sse_event import SSEBroadcaster, event_stream

pytest.importorskip("app")
from app import _enrich_metrics_for_sse, _state, _trim_output_device, app, camelize


class TestSSEBroadcaster:
    """Unit tests for the SSEBroadcaster thread-safe pub/sub."""

    def test_subscribe_and_publish(self) -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        count = b.publish("test_event", {"key": "val"})
        assert count == 1
        payload = q.get(timeout=1)
        assert payload["event"] == "test_event"
        assert payload["data"] == {"key": "val"}
        assert "timestamp" in payload

    def test_multiple_subscribers(self) -> None:
        b = SSEBroadcaster()
        q1 = b.subscribe()
        q2 = b.subscribe()
        count = b.publish("evt", {"n": 42})
        assert count == 2
        assert q1.get(timeout=1)["data"] == {"n": 42}
        assert q2.get(timeout=1)["data"] == {"n": 42}

    def test_unsubscribe(self) -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        assert b.subscriber_count() == 1
        b.unsubscribe(q)
        assert b.subscriber_count() == 0

    def test_slow_client_eviction(self) -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        # Fill queue to maxsize (64)
        for i in range(64):
            q.put_nowait({"i": i})
        # Next publish should find this subscriber's queue full and evict it
        count = b.publish("evt", {})
        assert count == 0  # subscriber was evicted

    def test_publish_event_naming(self) -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        b.publish("load_cycle", {"status": "ok"})
        b.publish("metrics_update", {"devices": []})
        evt1 = q.get(timeout=1)
        evt2 = q.get(timeout=1)
        assert evt1["event"] == "load_cycle"
        assert evt2["event"] == "metrics_update"

    def test_publish_after_all_unsubscribed(self) -> None:
        b = SSEBroadcaster()
        q = b.subscribe()
        b.unsubscribe(q)
        count = b.publish("evt", {})
        assert count == 0


class TestEventStream:
    """Tests for the event_stream generator function."""

    def test_initial_events_yielded_first(self) -> None:
        b = SSEBroadcaster()
        gen = event_stream(
            b,
            timeout=0.1,
            initial_events=[("init1", {"a": 1}), ("init2", {"b": 2})],
        )
        frame1 = next(gen)
        assert "event: init1" in frame1
        assert '"a": 1' in frame1
        frame2 = next(gen)
        assert "event: init2" in frame2
        assert '"b": 2' in frame2

    def test_heartbeat_on_idle(self) -> None:
        b = SSEBroadcaster()
        gen = event_stream(b, timeout=0.1)
        frame = next(gen)
        assert "event: heartbeat" in frame

    def test_default_heartbeat_timeout_is_30s(self) -> None:
        """Default heartbeat fires every 30s, below typical 60s proxy timeouts.

        The heartbeat is the only traffic on /stream/status when load
        management is disabled, so it must arrive well before proxy read
        timeouts (nginx default 60s) would kill an idle connection.
        """
        import inspect

        default = inspect.signature(event_stream).parameters["timeout"].default
        assert default == 30
        assert default < 60

    def test_subscribed_events_appear_in_stream(self) -> None:
        b = SSEBroadcaster()
        gen = event_stream(b, timeout=0.1)
        # Read past heartbeat, then publish from another thread
        next(gen)  # heartbeat or initial

        def publish() -> None:
            time.sleep(0.05)
            b.publish("load_cycle", {"status": "dry-run"})

        t = threading.Thread(target=publish, daemon=True)
        t.start()
        frame = next(gen)
        assert "event: load_cycle" in frame
        assert '"status": "dry-run"' in frame

    def test_unsubscribe_on_generator_exit(self) -> None:
        b = SSEBroadcaster()
        gen = event_stream(b, timeout=0.1)
        next(gen)  # heartbeat
        assert b.subscriber_count() == 1
        gen.close()
        # Give the finally block time to run
        time.sleep(0.05)
        assert b.subscriber_count() == 0

    def test_generator_exit_does_not_raise(self) -> None:
        b = SSEBroadcaster()
        gen = event_stream(b, timeout=0.1)
        next(gen)
        gen.close()  # should not raise


class TestShutdownWake:
    """close_all() must promptly end every streaming generator.

    Under gunicorn each SSE response occupies a worker-pool thread parked in
    ``queue.get``; at interpreter exit concurrent.futures joins those threads,
    so an open stream used to block process shutdown until a second Ctrl-C.
    """

    def test_close_all_terminates_blocked_generator(self) -> None:
        """A generator parked in queue.get ends within ~2s of close_all()."""
        b = SSEBroadcaster()
        frames: list[str] = []
        done = threading.Event()

        def consume() -> None:
            try:
                for frame in event_stream(b, timeout=30):
                    frames.append(frame)
            finally:
                done.set()

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        # Wait until the generator has subscribed and parked in queue.get.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b.subscriber_count() == 0:
            time.sleep(0.02)
        assert b.subscriber_count() == 1, "generator never subscribed"

        b.close_all()

        assert done.wait(timeout=2), (
            "SSE generator stayed blocked after close_all()"
        )
        assert all(frame.startswith("event:") for frame in frames), (
            f"non-SSE frames leaked to client: {frames!r}"
        )
        assert b.subscriber_count() == 0

    def test_close_all_is_terminal_for_heartbeat_loop(self) -> None:
        """After close_all(), an idle stream exits instead of heartbeating."""
        b = SSEBroadcaster()
        gen = event_stream(b, timeout=0.05)
        next(gen)  # heartbeat

        b.close_all()

        with pytest.raises(StopIteration):
            next(gen)

    def test_close_all_clears_subscribers(self) -> None:
        """close_all() drops every subscriber registration."""
        b = SSEBroadcaster()
        b.subscribe()
        b.subscribe()
        assert b.subscriber_count() == 2

        b.close_all()

        assert b.subscriber_count() == 0


class TestMetricsEnrichment:
    """Tests for _enrich_metrics_for_sse()."""

    def _make_device(
        self, name: str = "test", lag_secs: float = 5.0
    ) -> dict[str, Any]:
        return {
            "name": name,
            "gid": 12345,
            "lag": timedelta(seconds=lag_secs),
            "per_second_data": [1.0, 2.0, 3.0],
            "nbc": {},
        }

    def test_enrich_recalculates_lag(self) -> None:
        """Elapsed time since _fetched_at is added to cached lag."""
        fetched_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        metrics_data: dict[str, Any] = {
            "devices": [self._make_device(lag_secs=5.0)],
            "_fetched_at": fetched_at,
        }
        # Save original _data and restore after test
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = MagicMock(samples=[])
        try:
            now = fetched_at + timedelta(seconds=10)
            result = _enrich_metrics_for_sse(metrics_data, now=now)
        finally:
            _state.energy_cache._data = orig_data

        device = result["devices"][0]
        expected_lag = 5.0 + 10.0  # cached + elapsed
        assert device["lag"].total_seconds() == pytest.approx(expected_lag, abs=0.001)

    def test_enrich_does_not_mutate_source_device_lag(self) -> None:
        """The source metrics dict's devices are not mutated in place.

        get_or_fetch returns the same dict stored as full_metrics_dict; if
        enrich updated lag in place, repeated SSE publishes would inflate
        the cached lag cumulatively.  enrich must return an enriched copy
        of the device entries, leaving the source untouched.
        """
        fetched_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        source_device = self._make_device(lag_secs=5.0)
        metrics_data: dict[str, Any] = {
            "devices": [source_device],
            "_fetched_at": fetched_at,
        }
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = MagicMock(samples=[])
        try:
            now = fetched_at + timedelta(seconds=10)
            result = _enrich_metrics_for_sse(metrics_data, now=now)
        finally:
            _state.energy_cache._data = orig_data

        # Source device untouched...
        assert source_device["lag"].total_seconds() == pytest.approx(5.0, abs=0.001)
        # ...while the returned device carries the elapsed-time-adjusted lag.
        device = result["devices"][0]
        assert device is not source_device
        assert device["lag"].total_seconds() == pytest.approx(15.0, abs=0.001)

    def test_enrich_merges_samples(self) -> None:
        """Per-second samples from EnergyCache replace per_second_data."""
        fetched_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        metrics_data: dict[str, Any] = {
            "devices": [self._make_device(lag_secs=5.0)],
            "_fetched_at": fetched_at,
        }
        accumulated = [10.0, 20.0, 30.0, 40.0]
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = MagicMock(samples=accumulated)
        try:
            result = _enrich_metrics_for_sse(metrics_data, now=fetched_at)
        finally:
            _state.energy_cache._data = orig_data

        device = result["devices"][0]
        assert device["per_second_data"] == accumulated

    def test_enrich_trims_output(self) -> None:
        """_trim_output_device is called on each device."""
        fetched_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        long_data = list(range(500))
        metrics_data: dict[str, Any] = {
            "devices": [self._make_device(lag_secs=5.0)],
            "_fetched_at": fetched_at,
        }
        metrics_data["devices"][0]["per_second_data"] = long_data
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = MagicMock(samples=[])
        try:
            result = _enrich_metrics_for_sse(metrics_data, now=fetched_at)
        finally:
            _state.energy_cache._data = orig_data

        device = result["devices"][0]
        assert len(device["per_second_data"]) == 300  # trimmed to 300
        assert device["per_second_data"] == long_data[-300:]

    def test_enrich_handles_no_fetched_at(self) -> None:
        """No crash when _fetched_at is missing."""
        metrics_data: dict[str, Any] = {
            "devices": [self._make_device()],
        }
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = MagicMock(samples=[])
        try:
            result = _enrich_metrics_for_sse(metrics_data)
        finally:
            _state.energy_cache._data = orig_data

        assert "devices" in result
        assert len(result["devices"]) == 1

    def test_enrich_handles_no_devices(self) -> None:
        """No crash when devices list is empty."""
        metrics_data: dict[str, Any] = {"_fetched_at": datetime.now(timezone.utc)}
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = MagicMock(samples=[])
        try:
            result = _enrich_metrics_for_sse(metrics_data)
        finally:
            _state.energy_cache._data = orig_data

        assert result["devices"] == []


class TestSSEEndpoint:
    """Integration tests for the /stream/status SSE endpoint."""

    def _setup_mock_load_manager(self) -> MagicMock:
        """Set up a mock LoadManager and inject it into the app module."""
        import app as app_mod
        from config import Config

        Config().set("LOAD_MANAGE_ENABLED", "True")
        mock_lm = MagicMock()
        mock_lm.enabled = True
        mock_lm.dry_run = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state.to_dict.return_value = {"devices": {}}
        mock_lm.config_interval_secs = 30
        app_mod._state.load_manager = mock_lm
        app_mod._state.load_manager_init_failed = False
        app_mod._state.last_cycle_result = {
            "status": "ok",
            "sleep_hint": 30.0,
            "sleep_hint_at": "2025-01-15T12:00:00+00:00",
        }
        app_mod._state.recent_cycles.clear()
        return mock_lm

    def _cleanup_load_manager(self) -> None:
        import app as app_mod

        app_mod._state.load_manager = None
        app_mod._state.last_cycle_result = None
        app_mod._state.recent_cycles.clear()

    @pytest.fixture(autouse=True)
    def setup_app(self) -> Any:
        self.client = app.test_client()
        self.client.testing = True

    def test_stream_status_content_type(self) -> None:
        """Response has text/event-stream content type."""
        self._setup_mock_load_manager()
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = None
        try:
            resp = self.client.get("/stream/status")
            assert resp.status_code == 200
            assert resp.mimetype == "text/event-stream"
        finally:
            _state.energy_cache._data = orig_data
            self._cleanup_load_manager()

    def test_stream_status_headers(self) -> None:
        """Required SSE headers are present."""
        self._setup_mock_load_manager()
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = None
        try:
            resp = self.client.get("/stream/status")
            assert resp.headers.get("Cache-Control") == "no-cache"
            assert resp.headers.get("X-Accel-Buffering") == "no"
        finally:
            _state.energy_cache._data = orig_data
            self._cleanup_load_manager()

    def test_stream_status_initial_event(self) -> None:
        """First frame is an initial_load_state event with valid JSON."""
        self._setup_mock_load_manager()
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = None
        try:
            resp = self.client.get("/stream/status")
            iter_ = resp.response
            for chunk in iter_:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                assert "event: initial_load_state" in text
                assert "data: " in text
                # Extract and parse JSON data
                data_line = [l for l in text.split("\n") if l.startswith("data: ")]
                assert len(data_line) >= 1
                payload = json.loads(data_line[0][6:])
                assert payload.get("enabled") is True
                assert payload.get("dryRun") is True
                break  # only need first frame
        finally:
            _state.energy_cache._data = orig_data
            self._cleanup_load_manager()

    def test_stream_status_initial_metrics(self) -> None:
        """When cached metrics exist, initial_metrics event is emitted."""
        self._setup_mock_load_manager()
        orig_data = _state.energy_cache._data
        # Set up mock MetricsData with full_metrics_dict
        mock_cache = MagicMock()
        mock_cache.full_metrics_dict = {
            "devices": [{"name": "test", "gid": 1, "lag": timedelta(0)}],
            "_fetched_at": datetime.now(timezone.utc),
        }
        mock_cache.samples = [1.0, 2.0, 3.0]
        _state.energy_cache._data = mock_cache
        try:
            resp = self.client.get("/stream/status")
            frames_read = 0
            for chunk in resp.response:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                if "initial_metrics" in text:
                    data_line = [l for l in text.split("\n") if l.startswith("data: ")]
                    assert len(data_line) >= 1
                    payload = json.loads(data_line[0][6:])
                    assert "devices" in payload
                    assert payload["devices"][0]["name"] == "test"
                    break
                frames_read += 1
                if frames_read > 5:
                    pytest.fail("initial_metrics event not found in first 5 frames")
        finally:
            _state.energy_cache._data = orig_data
            self._cleanup_load_manager()

    def test_stream_status_event_format(self) -> None:
        """SSE frames follow the 'event: NAME\ndata: JSON\n\n' format."""
        self._setup_mock_load_manager()
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = None
        try:
            resp = self.client.get("/stream/status")
            for chunk in resp.response:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                # Must have event: and data: lines
                lines = text.strip().split("\n")
                assert any(l.startswith("event: ") for l in lines)
                assert any(l.startswith("data: ") for l in lines)
                # Must end with blank line (double newline)
                assert text.endswith("\n\n")
                break  # only check first frame
        finally:
            _state.energy_cache._data = orig_data
            self._cleanup_load_manager()

    def test_sse_payload_camelcase(self) -> None:
        """SSE payloads use camelCase keys matching the legacy index endpoint."""
        self._setup_mock_load_manager()
        orig_data = _state.energy_cache._data
        _state.energy_cache._data = None
        try:
            resp = self.client.get("/stream/status")
            for chunk in resp.response:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                if "initial_load_state" not in text:
                    continue
                data_line = [l for l in text.split("\n") if l.startswith("data: ")]
                assert len(data_line) >= 1
                payload = json.loads(data_line[0][6:])
                # Top-level camelCase keys
                assert "dryRun" in payload, f"Expected dryRun in {list(payload.keys())}"
                assert "targetWh" in payload, f"Expected targetWh in {list(payload.keys())}"
                assert "nbcDevice" in payload, f"Expected nbcDevice in {list(payload.keys())}"
                assert "lastCycleResult" in payload
                assert "sleepHint" in payload
                assert "sleepHintAt" in payload
                # Nested keys under lastCycleResult are also camelCased
                lcr = payload.get("lastCycleResult", {})
                if lcr:
                    assert "sleepHint" in lcr, f"Expected camelCase in {list(lcr.keys())}"
                    assert "sleep_hint" not in lcr
                break
        finally:
            _state.energy_cache._data = orig_data
            self._cleanup_load_manager()
