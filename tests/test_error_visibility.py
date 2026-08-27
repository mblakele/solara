"""Error-visibility tests: stack traces on swallowed errors (plan subtask 1.1a).

Red-phase tests for subtask 1.1a of .opencode/plans/architectural-review.md:
error paths that currently log without ``exc_info`` (losing the stack trace)
or swallow exceptions silently must become observable:

- app._load_management_loop catch-all must log the crash traceback.
- app._get_load_manager init failure must log the init traceback.
- app._send_error_alert failure must be visible at WARNING (not DEBUG).
- LoadManager action-execution error logs must carry exc_info.
- LoadManager._cleanup_sessions must not swallow close() errors silently.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from load_controllers import RealTeslaController, TeslaController
from load_manager import LoadManager, LoadManagerConfig
from load_models import PendingEffect, TeslaConfig


@pytest.fixture
def lm() -> LoadManager:
    """Default LoadManager with minimal config, no real controllers."""
    return LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))


def _effect(device_name: str = "plug_a", action: str = "turn_on") -> PendingEffect:
    """Build a PendingEffect for direct _execute_action calls."""
    return PendingEffect(
        device_name=device_name,
        action=action,
        timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
        data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        power_watts=100.0,
    )


class TestLoopErrorVisibility:
    """app._load_management_loop must preserve crash tracebacks."""

    def test_loop_crash_logs_stack_trace(self, caplog):
        """The catch-all ERROR record for a loop crash carries exc_info."""
        import app as app_mod

        mock_lm = MagicMock()
        mock_lm.run_cycle.side_effect = RuntimeError("test crash")

        app_mod._state.telegram_sender = None
        app_mod._state.consecutive_error_count = 0
        try:
            with caplog.at_level(logging.DEBUG, logger="app"):
                with patch("app._get_load_manager", return_value=mock_lm):
                    stop_ev = MagicMock()
                    stop_ev.is_set.return_value = False
                    stop_ev.wait.side_effect = InterruptedError("stop")
                    with patch("app._stop_event", stop_ev):
                        with pytest.raises(InterruptedError):
                            app_mod._load_management_loop()

            errors = [
                r
                for r in caplog.records
                if r.levelno == logging.ERROR
                and "Error in load management loop" in r.getMessage()
            ]
            assert errors, (
                f"loop crash must be logged at ERROR; captured="
                f"{[(r.levelno, r.name, r.getMessage()[:40]) for r in caplog.records]}"
            )
            assert errors[0].exc_info is not None, (
                "loop crash log must include the stack trace (exc_info)"
            )
            assert errors[0].exc_info[0] is RuntimeError
        finally:
            app_mod._state.consecutive_error_count = 0
            app_mod._state.last_error_type = None

    def test_init_failure_logs_stack_trace(self, caplog):
        """LoadManager init failure WARNING carries exc_info."""
        import app as app_mod

        app_mod._state.load_manager = None
        app_mod._state.load_manager_init_failed = False
        try:
            with caplog.at_level(logging.DEBUG, logger="app"):
                with patch(
                    "load_manager.LoadManager",
                    side_effect=RuntimeError("boom"),
                ):
                    result = app_mod._get_load_manager()

            assert result is None
            warnings = [
                r
                for r in caplog.records
                if "Failed to initialize LoadManager" in r.getMessage()
            ]
            assert warnings, "init failure must be logged"
            assert warnings[0].exc_info is not None, (
                "init failure log must include the stack trace (exc_info)"
            )
        finally:
            app_mod._state.load_manager = None
            app_mod._state.load_manager_init_failed = False

    def test_error_alert_failure_logged_at_warning(self, caplog):
        """A failed error alert must surface at WARNING, not DEBUG."""
        import app as app_mod

        mock_sender = MagicMock()
        mock_sender.is_configured = True
        mock_sender.send_notification_sync.side_effect = RuntimeError("tg down")

        app_mod._state.telegram_sender = mock_sender
        try:
            with caplog.at_level(logging.DEBUG, logger="app"):
                app_mod._send_error_alert(ValueError("boom"))

            visible = [
                r
                for r in caplog.records
                if r.levelno >= logging.WARNING
                and "Failed to send error alert" in r.getMessage()
            ]
            assert visible, (
                "alert-send failure must be logged at WARNING or above "
                "(currently DEBUG-only, invisible in production)"
            )
            assert visible[0].exc_info is not None
        finally:
            app_mod._state.telegram_sender = None


class TestActionExecutionErrorVisibility:
    """LoadManager action-execution error logs must carry exc_info."""

    def test_plug_action_failure_logs_stack_trace(self, lm, caplog):
        """_execute_action logs the plug-command traceback."""
        lm.plug_ctrl = MagicMock()
        lm.plug_ctrl.set_state = AsyncMock(side_effect=RuntimeError("hw fail"))

        with caplog.at_level(logging.ERROR, logger="load_manager"):
            ok = asyncio.run(lm._execute_action(_effect()))

        assert ok is False
        errors = [
            r
            for r in caplog.records
            if "Failed to execute action" in r.getMessage()
        ]
        assert errors, "plug action failure must be logged at ERROR"
        assert errors[0].exc_info is not None, (
            "action failure log must include the stack trace (exc_info)"
        )

    def _lm_with_tesla(self) -> LoadManager:
        """LoadManager with stub Tesla config/controller wired."""
        mgr = LoadManager(LoadManagerConfig(dry_run=True, config_interval_secs=30))
        mgr.tesla_config = TeslaConfig(
            client_id="test",
            client_secret="test",
            redirect_uri="http://localhost/callback",
            vehicle_id="v1",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=24,
        )
        mgr.tesla_ctrl = TeslaController(mgr.tesla_config)
        return mgr

    def test_tesla_stop_failure_logs_stack_trace(self, caplog):
        """_execute_tesla_stop logs the stop_charging traceback."""
        mgr = self._lm_with_tesla()
        mgr.tesla_ctrl.stop_charging = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("fleet 500")
        )

        with caplog.at_level(logging.ERROR, logger="load_manager"):
            ok = asyncio.run(mgr._execute_tesla_stop())

        assert ok is False
        errors = [
            r
            for r in caplog.records
            if "Failed to stop Tesla charging" in r.getMessage()
        ]
        assert errors, "tesla stop failure must be logged at ERROR"
        assert errors[0].exc_info is not None

    def test_tesla_set_amps_failure_logs_stack_trace(self, caplog):
        """set_charge_amps failures log the traceback."""
        mgr = self._lm_with_tesla()
        mgr.tesla_ctrl.set_charge_amps = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("fleet timeout")
        )
        action = PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=datetime(2025, 6, 1, 12, 0, 5, tzinfo=timezone.utc),
            data_point_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            power_watts=0.0,
            target_amps=12,
        )

        with caplog.at_level(logging.ERROR, logger="load_manager"):
            ok = asyncio.run(mgr._execute_tesla_action(action))

        assert ok is False
        errors = [
            r
            for r in caplog.records
            if "Failed to set Tesla charge amps" in r.getMessage()
        ]
        assert errors, "set_amps failure must be logged at ERROR"
        assert errors[0].exc_info is not None


class TestCleanupSessionsVisibility:
    """_cleanup_sessions must not swallow close() errors silently."""

    def test_tesla_close_failure_is_logged(self, lm, caplog):
        """A raising tesla_ctrl.close() produces a log record."""
        lm.tesla_ctrl = TeslaController(None)  # type: ignore[arg-type]
        with patch.object(
            lm.tesla_ctrl,
            "close",
            new_callable=AsyncMock,
            side_effect=RuntimeError("close fail"),
        ):
            with caplog.at_level(logging.DEBUG, logger="load_manager"):
                asyncio.run(lm._cleanup_sessions())

        records = [
            r
            for r in caplog.records
            if "close" in r.getMessage().lower()
            and r.name == "load_manager"
        ]
        assert records, (
            "_cleanup_sessions must log controller-close failures "
            "(currently silently swallowed)"
        )

    def test_telegram_session_close_failure_is_logged(self, lm, caplog):
        """A raising telegram session.close() produces a log record."""
        failing_session = SimpleNamespace(
            close=AsyncMock(side_effect=RuntimeError("session close fail"))
        )
        client = SimpleNamespace(_session=failing_session)
        sender = SimpleNamespace(_telegram_client=client)

        lm.tesla_ctrl = None
        lm.telegram_sender = sender  # type: ignore[assignment]
        with caplog.at_level(logging.DEBUG, logger="load_manager"):
            asyncio.run(lm._cleanup_sessions())

        records = [
            r
            for r in caplog.records
            if "close" in r.getMessage().lower() and r.name == "load_manager"
        ]
        assert records, (
            "_cleanup_sessions must log session-close failures "
            "(currently silently swallowed)"
        )


class TestRootLoggingGuard:
    """Importing app must not destroy pre-existing root logging handlers.

    app.py rewires root handlers to gunicorn's at import time. Under
    gunicorn those handlers exist, but in any other embedding context
    (tests, scripts, notebooks) gunicorn's logger is empty, so the
    assignment silently EMPTIES root logging — discarding whatever
    configuration the embedder installed (including pytest caplog).
    """

    def test_noop_when_gunicorn_has_no_handlers(self):
        """With no gunicorn handlers, existing root handlers are kept."""
        import app as app_mod

        root = logging.getLogger()
        sentinel = logging.NullHandler()
        root.addHandler(sentinel)
        gunicorn_logger = logging.getLogger("gunicorn.error")
        saved = gunicorn_logger.handlers[:]
        try:
            gunicorn_logger.handlers = []
            app_mod._route_root_logging_through_gunicorn()
            assert sentinel in root.handlers, (
                "import-time routing wiped pre-existing root handlers "
                "when gunicorn has none"
            )
        finally:
            gunicorn_logger.handlers = saved
            root.removeHandler(sentinel)

    def test_rewires_when_gunicorn_has_handlers(self):
        """With gunicorn handlers present, root routes through them."""
        import app as app_mod

        root = logging.getLogger()
        saved_root = root.handlers[:]
        gunicorn_logger = logging.getLogger("gunicorn.error")
        saved_gunicorn = gunicorn_logger.handlers[:]
        gunicorn_handler = logging.NullHandler()
        try:
            gunicorn_logger.handlers = [gunicorn_handler]
            app_mod._route_root_logging_through_gunicorn()
            assert root.handlers == [gunicorn_handler]
        finally:
            root.handlers = saved_root
            gunicorn_logger.handlers = saved_gunicorn


class TestControllerCommandErrorVisibility:
    """RealTeslaController command failures must log stack traces."""

    @pytest.fixture()
    def tesla_config(self):
        return TeslaConfig(
            client_id="test-id",
            client_secret="test-secret",
            redirect_uri="http://localhost/callback",
            vehicle_id="vehicle-123",
            home_lat=37.0,
            home_lon=-122.0,
            home_radius_m=500,
            charge_amps_min=5,
            charge_amps_max=48,
        )

    def _make_ctrl(self, tesla_config):
        """RealTeslaController with mocked API ready for command tests."""
        ctrl = RealTeslaController(tesla_config)
        ctrl._ensure_api = AsyncMock()  # type: ignore[assignment]
        mock_api = MagicMock()
        mock_api.has_private_key = True
        ctrl._api = mock_api  # type: ignore[assignment]
        ctrl.close = AsyncMock()  # type: ignore[assignment]
        return ctrl

    @pytest.mark.asyncio
    async def test_stop_charging_failure_logs_stack_trace(
        self, tesla_config, caplog
    ):
        """Generic stop_charging failure logs the traceback."""
        ctrl = self._make_ctrl(tesla_config)
        ctrl._get_vehicle = AsyncMock(  # type: ignore[assignment]
            side_effect=RuntimeError("fleet 500")
        )

        with caplog.at_level(logging.ERROR, logger="load_controllers"):
            ok = await ctrl.stop_charging()

        assert ok is False
        errors = [
            r
            for r in caplog.records
            if r.name == "load_controllers"
            and "Failed to stop Tesla charging" in r.getMessage()
        ]
        assert errors, "controller stop failure must be logged at ERROR"
        assert errors[0].exc_info is not None, (
            "controller stop failure log must include the stack trace"
        )

    @pytest.mark.asyncio
    async def test_set_charge_amps_failure_logs_stack_trace(
        self, tesla_config, caplog
    ):
        """Generic set_charge_amps failure logs the traceback."""
        ctrl = self._make_ctrl(tesla_config)
        vehicle_mock = AsyncMock()
        vehicle_mock.set_charging_amps = AsyncMock(
            side_effect=RuntimeError("fleet timeout")
        )
        ctrl._get_vehicle = AsyncMock(return_value=vehicle_mock)  # type: ignore[assignment]

        with caplog.at_level(logging.ERROR, logger="load_controllers"):
            ok = await ctrl.set_charge_amps(16)

        assert ok is False
        errors = [
            r
            for r in caplog.records
            if r.name == "load_controllers"
            and "Failed to set Tesla charge amps" in r.getMessage()
        ]
        assert errors, "controller set_amps failure must be logged at ERROR"
        assert errors[0].exc_info is not None


class TestConfigWatcherErrorVisibility:
    """ConfigWatcher.check() must not swallow stat() errors silently."""

    def test_env_stat_error_is_logged(self, caplog):
        """OSError while polling .env mtime produces a visible record."""
        from config import ConfigWatcher

        def _raise_stat():
            raise PermissionError("stat denied")

        failing_env = SimpleNamespace(exists=lambda: True, stat=_raise_stat)
        absent_devices = SimpleNamespace(exists=lambda: False)
        watcher = ConfigWatcher(
            env_path=failing_env, devices_path=absent_devices
        )

        with caplog.at_level(logging.WARNING, logger="config"):
            changes = watcher.check()

        assert changes is not None
        records = [
            r
            for r in caplog.records
            if r.name == "config" and r.levelno >= logging.WARNING
        ]
        assert records, (
            "ConfigWatcher must log stat() failures instead of passing "
            "(hot-reload breakage is currently invisible)"
        )


class TestDeviceConfigErrorVisibility:
    """device_config must surface unreadable devices.json instead of
    silently treating it as missing."""

    def test_unreadable_devices_json_logs_error(self, caplog, monkeypatch):
        """OSError (not FileNotFoundError) reading devices.json -> ERROR."""
        import device_config as dc

        dc.reload()

        def _raise_read_text(*args, **kwargs):
            raise PermissionError("permission denied")

        monkeypatch.setattr(
            dc,
            "_DEVICES_FILE",
            SimpleNamespace(read_text=_raise_read_text),
        )

        with caplog.at_level(logging.ERROR, logger="device_config"):
            result = dc._load()

        assert result == {}
        records = [
            r
            for r in caplog.records
            if r.name == "device_config" and r.levelno >= logging.ERROR
        ]
        assert records, (
            "unreadable devices.json must be logged at ERROR "
            "(currently indistinguishable from a missing file)"
        )
        monkeypatch.setattr(dc, "_cache", None)

    def test_missing_file_stays_silent(self, caplog, monkeypatch):
        """FileNotFoundError remains the documented silent default."""
        import device_config as dc

        dc.reload()

        def _raise_missing(*args, **kwargs):
            raise FileNotFoundError("no such file")

        monkeypatch.setattr(
            dc,
            "_DEVICES_FILE",
            SimpleNamespace(read_text=_raise_missing),
        )

        with caplog.at_level(logging.DEBUG, logger="device_config"):
            result = dc._load()

        assert result == {}
        records = [
            r
            for r in caplog.records
            if r.name == "device_config" and r.levelno >= logging.ERROR
        ]
        assert not records, (
            "a missing devices.json is the documented default and must "
            "not be logged as an error"
        )
        monkeypatch.setattr(dc, "_cache", None)
