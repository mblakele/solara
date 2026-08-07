"""Tests for the Flask app factory (create_app), wsgi entry point, and
background service startup behavior.

Importing the ``app`` module must be side-effect free (no background
threads, no MQTT subscriber); production entry points start services
explicitly via :func:`start_background_services`.
"""

import os
from contextlib import contextmanager

from unittest.mock import patch

import pytest
from flask import Flask

import app as app_mod
from config import Config


@contextmanager
def mock_env():
    """Force mock mode for the duration of the context."""
    saved = {key: os.environ.get(key) for key in ("MOCK", "MOCK_ERROR", "VUE_USERNAME")}
    try:
        os.environ["MOCK"] = "True"
        os.environ["MOCK_ERROR"] = "False"
        os.environ["VUE_USERNAME"] = ""
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class TestCreateApp:
    """create_app() builds a configured, route-bearing Flask application."""

    def test_returns_flask_app_with_all_routes(self):
        app = app_mod.create_app()
        assert isinstance(app, Flask)
        rules = {str(rule) for rule in app.url_map.iter_rules()}
        for route in ("/", "/health", "/api/v1/tou", "/api/v1/load/status", "/stream/status"):
            assert route in rules

    def test_rejects_config_override(self):
        # The config parameter was removed: create_app() always binds the
        # module-level _config singleton. Views and background services read
        # that singleton, so a per-app override was inert and misleading.
        with pytest.raises(TypeError):
            app_mod.create_app(config=Config())

    def test_default_config_is_module_singleton(self):
        app = app_mod.create_app()
        assert app.config["SOLARA_CONFIG"] is app_mod._config

    def test_health_serves_ok(self):
        app = app_mod.create_app()
        resp = app.test_client().get("/health")
        assert resp.status_code == 200
        assert resp.data == b"ok"

    def test_index_serves_html_in_mock_mode(self):
        app = app_mod.create_app()
        with mock_env():
            resp = app.test_client().get("/", headers={"Accept": "text/html"})
        assert resp.status_code == 200


class TestBackgroundServices:
    """Importing the module must not start threads; start_background_services() gates them."""

    def test_import_does_not_start_background_threads(self):
        assert app_mod._state.lm_thread_started is False

    def test_start_background_services_noop_when_disabled(self):
        with patch.object(app_mod, "_acquire_instance_lock", return_value=-1), \
             patch.object(app_mod, "_start_mqtt_subscriber") as mock_mqtt, \
             patch.object(app_mod, "_start_load_manager_thread") as mock_lm:
            app_mod.start_background_services()
        mock_mqtt.assert_not_called()
        mock_lm.assert_not_called()

    def test_start_background_services_starts_threads_when_enabled(self):
        with patch.object(app_mod, "_acquire_instance_lock", return_value=-1), \
             patch.object(app_mod, "_start_mqtt_subscriber") as mock_mqtt, \
             patch.object(app_mod, "_start_load_manager_thread") as mock_lm:
            Config().set("LOAD_TESLA_CONTROLLER", "real")
            Config().set("LOAD_MANAGE_ENABLED", "True")
            app_mod.start_background_services()
        mock_mqtt.assert_called_once_with()
        mock_lm.assert_called_once_with()


class TestInstanceLock:
    """Single-instance advisory lock guarding background services.

    Duplicate gunicorn workers (or duplicate app processes) must not each
    run their own metrics + decision loops against the same physical
    plugs: the loops would fight each other via plug-state reconciliation.
    The flock guard makes the second process fail loud (ERROR log) and
    serve HTTP without background services instead.
    """

    def test_lock_excludes_second_acquire(self, tmp_path):
        """Second acquire returns None while the first holder lives."""
        lock_path = str(tmp_path / ".load-manager.lock")
        first = app_mod._acquire_instance_lock(lock_path)
        assert first is not None, "first acquire should succeed"
        try:
            second = app_mod._acquire_instance_lock(lock_path)
            assert second is None, "second acquire must be refused"
        finally:
            os.close(first)
        # Lock released with the fd — re-acquire succeeds.
        third = app_mod._acquire_instance_lock(lock_path)
        assert third is not None, "re-acquire after release should succeed"
        os.close(third)

    def test_start_background_services_skips_when_lock_held(self, tmp_path):
        """start_background_services starts nothing when another instance holds the lock."""
        lock_path = str(tmp_path / ".load-manager.lock")
        held = app_mod._acquire_instance_lock(lock_path)
        assert held is not None
        try:
            with patch.object(app_mod, "_instance_lock_path", return_value=lock_path), \
                 patch.object(app_mod, "_start_mqtt_subscriber") as mock_mqtt, \
                 patch.object(app_mod, "_start_load_manager_thread") as mock_lm:
                Config().set("LOAD_TESLA_CONTROLLER", "real")
                Config().set("LOAD_MANAGE_ENABLED", "True")
                app_mod.start_background_services()
            mock_mqtt.assert_not_called()
            mock_lm.assert_not_called()
        finally:
            os.close(held)


class TestFastDecideThreading:
    """_start_load_manager_thread starts metrics + decision loops per config."""

    @staticmethod
    def _capture_thread_starts(app_mod):
        """Replace threading.Thread with a recorder that never starts threads.

        Returns:
            (patcher, started) where started is the list of targets passed
            to each Thread(...).start() call.
        """
        started: list = []

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kwargs):
                self.target = target

            def start(self):
                started.append(self.target)

        return patch.object(app_mod.threading, "Thread", FakeThread), started

    def test_fast_decide_enabled_starts_both_threads(self):
        """With fast decide on, both metrics and decision loops start."""
        app_mod._state.lm_thread_started = False
        patcher, started = self._capture_thread_starts(app_mod)
        with patcher, patch.object(app_mod, "_config") as mock_config:
            mock_config.load_fast_decide_enabled = True
            app_mod._start_load_manager_thread()
        assert len(started) == 2, f"expected 2 threads, started {started}"
        assert app_mod._metrics_loop in started
        assert app_mod._decision_loop in started

    def test_fast_decide_disabled_starts_only_metrics_thread(self):
        """With fast decide off, only the metrics loop starts."""
        app_mod._state.lm_thread_started = False
        patcher, started = self._capture_thread_starts(app_mod)
        with patcher, patch.object(app_mod, "_config") as mock_config:
            mock_config.load_fast_decide_enabled = False
            app_mod._start_load_manager_thread()
        assert len(started) == 1, f"expected 1 thread, started {started}"
        assert app_mod._metrics_loop in started
        assert app_mod._decision_loop not in started

    def test_start_guard_prevents_duplicate_threads(self):
        """lm_thread_started flag prevents starting threads twice."""
        app_mod._state.lm_thread_started = True
        patcher, started = self._capture_thread_starts(app_mod)
        with patcher:
            app_mod._start_load_manager_thread()
        assert started == []


class TestWsgiEntrypoint:
    """wsgi.py exposes a fully constructed app for gunicorn."""

    def test_wsgi_exposes_flask_app(self):
        import wsgi

        assert isinstance(wsgi.app, Flask)
        resp = wsgi.app.test_client().get("/health")
        assert resp.status_code == 200
        assert resp.data == b"ok"
