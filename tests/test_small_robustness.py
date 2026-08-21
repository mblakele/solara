"""Small robustness fixes (plan subtask 2.7): R8, R9, R10 remainder.

- HomeKit connect() must close AsyncZeroconf on every failure path (R9).
- Tesla OAuth state tokens must be pruned when expired (R8).
- The LM loop's sleep computation must be exception-safe and clamped >=0
  so a bad hint cannot kill the thread (R10).
- A rate-limited CRITICAL watchdog fires when cycles stop completing.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app as app_mod
from config import Config


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def reset_watchdog_state():
    """Reset watchdog bookkeeping around each test."""
    app_mod._state.lm_last_cycle_finished_at = None
    app_mod._stall_critical_last_at = None
    yield
    app_mod._state.lm_last_cycle_finished_at = None
    app_mod._stall_critical_last_at = None


# ── R9: AsyncZeroconf lifecycle ──────────────────────────────────────


class _FakePairing:
    def __init__(self, accessories):
        self._accessories = accessories

    def list_accessories_and_characteristics(self):
        return self._accessories

    async def async_populate_accessories_state(self):
        pass


class TestZeroconfLifecycle:
    """connect() failures must not leak the AsyncZeroconf instance."""

    def _controller(self, tmp_path):
        from load_controllers import RealPlugController
        from load_models import PlugConfig

        pairings = tmp_path / ".homekit-pairings.json"
        pairings.write_text(
            json.dumps({"1.2.3.4": {"AccessoryIP": "1.2.3.4"}}),
            encoding="utf-8",
        )
        plugs = {
            "lamp": PlugConfig(
                name="lamp", accessory_id="1.2.3.4", power_watts=60.0
            )
        }
        return RealPlugController(plugs=plugs, pairings_path=pairings)

    @pytest.fixture()
    def fake_azc(self, monkeypatch):
        import zeroconf.asyncio as zca

        closed = {"n": 0}

        class FakeAzc:
            def __init__(self):
                pass

            async def async_wait_for_start(self):
                pass

            async def async_close(self):
                closed["n"] += 1

        monkeypatch.setattr(zca, "AsyncZeroconf", FakeAzc)
        return closed

    @pytest.mark.asyncio
    async def test_ipcontroller_failure_closes_zeroconf(
        self, tmp_path, fake_azc, monkeypatch
    ):
        """IpController construction failure closes azc before returning."""
        import aiohomekit.controller.ip as ip_mod

        def boom(*args, **kwargs):
            raise RuntimeError("no controller")

        monkeypatch.setattr(ip_mod, "IpController", boom)
        ctrl = self._controller(tmp_path)
        assert await ctrl.connect() is False
        assert fake_azc["n"] == 1, "AsyncZeroconf leaked on IpController failure"

    @pytest.mark.asyncio
    async def test_load_pairing_failure_closes_zeroconf(
        self, tmp_path, fake_azc, monkeypatch
    ):
        """load_pairing failure closes azc before returning."""
        import aiohomekit.controller.ip as ip_mod

        class FailingController:
            def __init__(self, *_args, **_kwargs):
                pass

            def load_pairing(self, *_args, **_kwargs):
                raise RuntimeError("bad pairing")

        monkeypatch.setattr(ip_mod, "IpController", FailingController)
        ctrl = self._controller(tmp_path)
        assert await ctrl.connect() is False
        assert fake_azc["n"] == 1, "AsyncZeroconf leaked on load_pairing failure"

    @pytest.mark.asyncio
    async def test_success_keeps_zeroconf_alive(
        self, tmp_path, fake_azc, monkeypatch
    ):
        """On success azc stays alive for subsequent commands."""
        from aiohomekit.model.characteristics import CharacteristicsTypes
        from aiohomekit.model.services import ServicesTypes

        import aiohomekit.controller.ip as ip_mod

        accessories = [
            {
                "aid": 1,
                "services": [
                    {
                        "sid": ServicesTypes.SWITCH,
                        "chars": [{"cid": CharacteristicsTypes.ON, "iid": 8}],
                    }
                ],
            }
        ]

        class WorkingController:
            def __init__(self, *_args, **_kwargs):
                pass

            def load_pairing(self, *_args, **_kwargs):
                return _FakePairing(accessories)

        monkeypatch.setattr(ip_mod, "IpController", WorkingController)
        ctrl = self._controller(tmp_path)
        assert await ctrl.connect() is True
        assert fake_azc["n"] == 0, "success path must keep azc alive"


# ── R8: OAuth state pruning ──────────────────────────────────────────


class TestOAuthStatePruning:
    def test_prune_removes_only_expired(self, monkeypatch):
        import tesla_oauth

        monkeypatch.setattr(
            tesla_oauth,
            "_oauth_states",
            {"old": 900.0, "live": 2000.0},
        )
        removed = tesla_oauth.prune_expired_oauth_states(now=1000.0)
        assert removed == 1
        assert tesla_oauth._oauth_states == {"live": 2000.0}

    def test_initiate_prunes_before_inserting(self, monkeypatch):
        """The initiate route sweeps expired states on every call."""
        import app as app_mod_inner
        import tesla_oauth

        monkeypatch.setenv("TESLA_CLIENT_ID", "id")
        monkeypatch.setenv("TESLA_CLIENT_SECRET", "secret")
        monkeypatch.setenv("TESLA_VEHICLE_ID", "v1")
        monkeypatch.setenv("TESLA_REDIRECT_URI", "http://localhost/callback")

        calls = {"n": 0}

        def spy(now=None):
            calls["n"] += 1

        monkeypatch.setattr(
            tesla_oauth, "prune_expired_oauth_states", spy
        )
        monkeypatch.setattr(
            tesla_oauth,
            "_controllers",
            lambda: (MagicMock(), lambda: None, lambda *a, **k: None),
        )

        client = app_mod_inner.app.test_client()
        response = client.get("/api/v1/tesla/auth/initiate")
        assert response.status_code in (200, 500)  # config-dependent tail
        assert calls["n"] == 1, "initiate must sweep expired oauth states"


# ── R10: loop sleep safety + watchdog ────────────────────────────────


class TestLoopSleepSafety:
    def test_negative_sleep_hint_clamped_to_zero(self):
        result = SimpleNamespace(status="disabled")
        assert app_mod._compute_loop_sleep(result, -5.0) == 0.0

    def test_adjust_failure_falls_back_to_interval(self, monkeypatch):
        result = SimpleNamespace(status="ok")
        monkeypatch.setattr(
            app_mod._state.energy_cache,
            "sleep_interval_adjust",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("tz!")),
        )
        assert app_mod._compute_loop_sleep(result, 30.0) == 30.0

    def test_passthrough_when_adjust_ok(self, monkeypatch):
        result = None
        monkeypatch.setattr(
            app_mod._state.energy_cache,
            "sleep_interval_adjust",
            lambda secs, _now: 7.5,
        )
        assert app_mod._compute_loop_sleep(result, 30.0) == 7.5

class TestStallWatchdog:
    def test_critical_logged_when_cycles_stall(self, caplog):
        app_mod._state.lm_last_cycle_finished_at = _now() - timedelta(
            seconds=100000
        )
        Config().set("LOAD_MANAGE_ENABLED", "True")
        try:
            with caplog.at_level("CRITICAL", logger="app"):
                app_mod._check_stall_watchdog(_now())
        finally:
            Config().set("LOAD_MANAGE_ENABLED", "False")
        criticals = [r for r in caplog.records if r.levelno == 50]
        assert criticals, "stall watchdog did not fire"

    def test_watchdog_rate_limited(self, caplog):
        app_mod._state.lm_last_cycle_finished_at = _now() - timedelta(
            seconds=100000
        )
        Config().set("LOAD_MANAGE_ENABLED", "True")
        try:
            with caplog.at_level("CRITICAL", logger="app"):
                app_mod._check_stall_watchdog(_now())
                app_mod._check_stall_watchdog(_now())
        finally:
            Config().set("LOAD_MANAGE_ENABLED", "False")
        criticals = [r for r in caplog.records if r.levelno == 50]
        assert len(criticals) == 1, "watchdog must be rate-limited"

    def test_watchdog_silent_when_disabled_or_fresh(self, caplog):
        app_mod._state.lm_last_cycle_finished_at = _now() - timedelta(
            seconds=100000
        )
        # Disabled (clean_env default): no gate.
        with caplog.at_level("CRITICAL", logger="app"):
            app_mod._check_stall_watchdog(_now())
        # Enabled but fresh completion.
        Config().set("LOAD_MANAGE_ENABLED", "True")
        try:
            app_mod._state.lm_last_cycle_finished_at = (
                _now() - timedelta(seconds=30)
            )
            with caplog.at_level("CRITICAL", logger="app"):
                app_mod._check_stall_watchdog(_now())
        finally:
            Config().set("LOAD_MANAGE_ENABLED", "False")
        assert not [r for r in caplog.records if r.levelno == 50]
