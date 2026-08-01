"""Tests for the Flask app factory (create_app), wsgi entry point, and
background service startup behavior.

Importing the ``app`` module must be side-effect free (no background
threads, no MQTT subscriber); production entry points start services
explicitly via :func:`start_background_services`.
"""

import os
from contextlib import contextmanager

from unittest.mock import patch

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

    def test_accepts_config_override(self):
        custom = Config()
        app = app_mod.create_app(config=custom)
        assert app.config["SOLARA_CONFIG"] is custom

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
        with patch.object(app_mod, "_start_mqtt_subscriber") as mock_mqtt, \
             patch.object(app_mod, "_start_load_manager_thread") as mock_lm:
            app_mod.start_background_services()
        mock_mqtt.assert_not_called()
        mock_lm.assert_not_called()

    def test_start_background_services_starts_threads_when_enabled(self):
        with patch.object(app_mod, "_start_mqtt_subscriber") as mock_mqtt, \
             patch.object(app_mod, "_start_load_manager_thread") as mock_lm:
            Config().set("LOAD_TESLA_CONTROLLER", "real")
            Config().set("LOAD_MANAGE_ENABLED", "True")
            app_mod.start_background_services()
        mock_mqtt.assert_called_once_with()
        mock_lm.assert_called_once_with()


class TestWsgiEntrypoint:
    """wsgi.py exposes a fully constructed app for gunicorn."""

    def test_wsgi_exposes_flask_app(self):
        import wsgi

        assert isinstance(wsgi.app, Flask)
        resp = wsgi.app.test_client().get("/health")
        assert resp.status_code == 200
        assert resp.data == b"ok"
