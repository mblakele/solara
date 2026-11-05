"""G.2: GET /api/v1/metrics exposes cycle and failure counters as JSON."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import get_args
from unittest.mock import patch

import pytest

import app as app_mod
from energy_cache import EnergyCache
from load_models import CycleDiagnostics, CycleResult, CycleStatus
from sse_event import SSEBroadcaster


def _result(status: str = "ok", reason: str = "ok") -> CycleResult:
    """Build a CycleResult with a minimal diagnostics snapshot."""
    return CycleResult(
        status=status,
        diagnostics=CycleDiagnostics(
            reason=reason,
            data_point_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    """Give every test isolated counters so cross-test state cannot leak."""
    monkeypatch.setattr(app_mod._state, "cycle_status_counts", Counter())
    monkeypatch.setattr(app_mod._state, "early_exit_counts", Counter())
    monkeypatch.setattr(app_mod._state, "arbitration_counts", Counter())


class TestMetricsEndpoint:
    def test_endpoint_lists_every_cycle_status(self):
        client = app_mod.app.test_client()
        response = client.get("/api/v1/metrics")

        assert response.status_code == 200
        data = response.get_json()
        assert set(data["cycles"]) == set(get_args(CycleStatus))
        assert data["cycles"]["ok"] == 0
        assert data["earlyExits"] == {}
        assert data["arbitration"] == {}
        assert data["driftEscalations"] == 0
        assert data["sseEvictions"] == 0
        assert data["fetchTimeouts"] == 0

    def test_record_cycle_increments_status_and_reason(self):
        app_mod._record_cycle_metrics(_result("stale_data", "no fresh data"))
        app_mod._record_cycle_metrics(_result("stale_data", "no fresh data"))
        app_mod._record_cycle_metrics(_result("ok", "ok"))

        data = app_mod.app.test_client().get("/api/v1/metrics").get_json()
        assert data["cycles"]["stale_data"] == 2
        assert data["cycles"]["ok"] == 1
        assert data["earlyExits"] == {"stale_data:no fresh data": 2}

    def test_record_cycle_counts_arbitration_verdict(self):
        from types import SimpleNamespace

        lm = SimpleNamespace(
            tesla_arbitration=SimpleNamespace(state="charging")
        )
        app_mod._record_cycle_metrics(_result("ok"), lm)

        data = app_mod.app.test_client().get("/api/v1/metrics").get_json()
        assert data["arbitration"] == {"charging": 1}

    def test_record_cycle_uses_the_state_lock(self, monkeypatch):
        """Counter updates must be serialized against endpoint reads."""
        entered: list[bool] = []

        class _RecordingLock:
            def __enter__(self) -> None:
                entered.append(True)

            def __exit__(self, *exc: object) -> bool:
                return False

        monkeypatch.setattr(app_mod._state, "load_manager_lock", _RecordingLock())
        app_mod._record_cycle_metrics(_result("ok"))
        assert entered, "counter update did not take the state lock"


class TestSourceCounters:
    def test_slow_subscriber_eviction_is_counted(self):
        broadcaster = SSEBroadcaster(maxsize=1)
        queue = broadcaster.subscribe()
        queue.put_nowait("occupied")
        broadcaster.publish("load_cycle", {})
        assert broadcaster.eviction_count == 1
        broadcaster.unsubscribe(queue)

    def test_fetch_timeout_is_counted(self):
        cache = EnergyCache(ttl_seconds=60, fetch_timeout_secs=1)

        def slow_fetch():
            import time

            time.sleep(2)
            return None

        assert cache._run_fetch_with_timeout(slow_fetch) is None
        assert cache.fetch_timeout_count == 1

    def test_drift_escalation_is_counted(self):
        from load_manager import LoadManager, LoadManagerConfig
        from metrics import DriftAlert

        manager = LoadManager(LoadManagerConfig(dry_run=True))
        alert = DriftAlert(
            channel_num=1,
            chart_start=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            data_start=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
            count=5,
        )
        with patch("load_manager.drain_drift_alerts", return_value=[alert]):
            manager._drain_drift_alerts()

        assert manager.drift_escalation_count == 1
