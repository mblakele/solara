"""Tests for the Flask app factory (create_app), wsgi entry point, and
background service startup behavior.

Importing the ``app`` module must be side-effect free (no background
threads, no MQTT subscriber); production entry points start services
explicitly via :func:`start_background_services`.
"""

import fcntl
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone

from unittest.mock import patch

import pytest
from flask import Flask

import app as app_mod
from config import Config
from energy_cache import EnergyCache, EnergyCacheData
from sse_event import SSEBroadcaster


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


@pytest.fixture(autouse=True)
def _release_controller_slot():
    """Release the cross-process controller lock acquired by the app."""
    yield
    app_mod._release_controller_slot()


def _seeded_cache(device_name: str) -> EnergyCache:
    """Return an EnergyCache with one warm full-metrics snapshot."""
    now = datetime.now(timezone.utc)
    cache = EnergyCache(ttl_seconds=60)
    cache._data = EnergyCacheData(
        samples=[0.0] * 10,
        data_start=now,
        last_sample_at=now,
        last_fetch_at=now,
        sample_count=10,
        quantization_seconds=None,
        quantization_offset=None,
        quantization_confidence=None,
        full_metrics_dict={
            "devices": [{"name": device_name}],
            "api_response": {},
            "instant": now,
        },
    )
    return cache


class TestCreateApp:
    """create_app() builds a configured, route-bearing Flask application."""

    def test_returns_flask_app_with_all_routes(self):
        app = app_mod.create_app()
        assert isinstance(app, Flask)
        rules = {str(rule) for rule in app.url_map.iter_rules()}
        for route in ("/", "/health", "/api/v1/tou", "/api/v1/load/status", "/stream/status"):
            assert route in rules

    def test_create_app_accepts_injected_config(self) -> None:
        """A.1: create_app accepts an injected Config (module singleton default)."""
        app = app_mod.create_app(
            config=Config(overrides={"TIMEZONE": "Europe/London"})
        )
        assert isinstance(app, Flask)
        assert app.config["SOLARA_CONFIG"].timezone == "Europe/London"

    def test_default_config_is_module_singleton(self):
        app = app_mod.create_app()
        assert app.config["SOLARA_CONFIG"] is app_mod._config

    def test_create_app_injected_state_is_per_app(self) -> None:
        """A.1: injected cache/broadcaster produce a per-app state, not the module default."""
        cache_a = EnergyCache(ttl_seconds=60)
        cache_b = EnergyCache(ttl_seconds=60)
        app_a = app_mod.create_app(
            config=Config(), energy_cache=cache_a, broadcaster=SSEBroadcaster()
        )
        app_b = app_mod.create_app(
            config=Config(), energy_cache=cache_b, broadcaster=SSEBroadcaster()
        )
        assert app_a.extensions["solara_state"].energy_cache is cache_a
        assert app_b.extensions["solara_state"].energy_cache is cache_b

    def test_injected_cache_is_used_by_the_request_path(self, monkeypatch):
        """A.1: a route serves the injected cache, not the module singleton."""
        monkeypatch.setattr(
            app_mod._state, "energy_cache", EnergyCache(ttl_seconds=60)
        )
        app = app_mod.create_app(
            config=Config(overrides={"MOCK": "False"}),
            energy_cache=_seeded_cache("injected"),
        )
        resp = app.test_client().get(
            "/", headers={"Accept": "application/json"}
        )
        assert resp.status_code == 200
        assert b"injected" in resp.data

    def test_two_apps_serve_their_own_cache(self, monkeypatch):
        """A.1: two apps in one process must not share request-path state."""
        monkeypatch.setattr(
            app_mod._state, "energy_cache", EnergyCache(ttl_seconds=60)
        )
        cfg = Config(overrides={"MOCK": "False"})
        app_a = app_mod.create_app(
            config=cfg, energy_cache=_seeded_cache("alpha")
        )
        app_b = app_mod.create_app(
            config=cfg, energy_cache=_seeded_cache("beta")
        )
        data_a = app_a.test_client().get(
            "/", headers={"Accept": "application/json"}
        ).data
        data_b = app_b.test_client().get(
            "/", headers={"Accept": "application/json"}
        ).data
        assert b"alpha" in data_a and b"beta" not in data_a
        assert b"beta" in data_b and b"alpha" not in data_b

    def test_injected_config_controls_mock_mode(self, monkeypatch):
        """A.1: views read the injected Config, not the module singleton."""
        monkeypatch.setattr(
            app_mod, "_config", Config(overrides={"MOCK": "False"})
        )
        app = app_mod.create_app(
            config=Config(overrides={"MOCK": "True", "MOCK_ERROR": "False"})
        )
        resp = app.test_client().get(
            "/", headers={"Accept": "application/json"}
        )
        assert resp.status_code == 200

    def test_health_serves_json_status(self):
        app = app_mod.create_app()
        resp = app.test_client().get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

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

    def test_start_background_services_twice_starts_mqtt_once(self):
        """Repeated calls must not double-start MQTT (mirror of the lm guard).

        Spies ``mqtt_telemetry.start_mqtt_subscriber`` (the actual session
        creator, also self-guarded) rather than app's thin wrapper, so the
        assertion reflects real subscriber creation. Load management is left
        disabled (its guard predates this one). Saves/restores the startup
        flags so module-level state does not leak into other tests.
        """
        saved = (
            app_mod._state.mqtt_subscriber_started,
            app_mod._state.lm_thread_started,
        )
        app_mod._state.mqtt_subscriber_started = False
        app_mod._state.lm_thread_started = False
        try:
            with patch("mqtt_telemetry.start_mqtt_subscriber") as mock_mqtt:
                Config().set("LOAD_TESLA_CONTROLLER", "real")

                app_mod.start_background_services()
                app_mod.start_background_services()

            mock_mqtt.assert_called_once()
        finally:
            app_mod._state.mqtt_subscriber_started = saved[0]
            app_mod._state.lm_thread_started = saved[1]


class TestCacheRefresher:
    """F.1: with load management disabled a read-only loop warms the cache."""

    def test_refill_cache_once_populates_shared_cache(self, monkeypatch):
        cache = EnergyCache(ttl_seconds=60)
        monkeypatch.setattr(app_mod._state, "energy_cache", cache)
        payload = {
            "devices": [{"name": "meter", "per_second_data": [0.0] * 10}],
            "api_response": {},
            "instant": None,
            "data_start": datetime.now(timezone.utc),
        }
        with patch("app.create_metrics", return_value=payload):
            app_mod._refill_cache_once()
        assert cache.full_metrics_dict is payload

    def test_start_background_services_starts_refiller_when_lm_disabled(self):
        with patch.object(app_mod, "_claim_controller_slot", return_value=True), \
             patch.object(app_mod, "_start_cache_refiller") as mock_refiller, \
             patch.object(app_mod, "_start_load_manager_thread") as mock_lm:
            Config().set("LOAD_MANAGE_ENABLED", "False")
            app_mod.start_background_services()
        mock_refiller.assert_called_once_with()
        mock_lm.assert_not_called()

    def test_start_background_services_skips_refiller_when_lm_enabled(self):
        with patch.object(app_mod, "_claim_controller_slot", return_value=True), \
             patch.object(app_mod, "_start_cache_refiller") as mock_refiller, \
             patch.object(app_mod, "_start_load_manager_thread") as mock_lm:
            Config().set("LOAD_MANAGE_ENABLED", "True")
            app_mod.start_background_services()
        mock_refiller.assert_not_called()
        mock_lm.assert_called_once_with()


class TestControllerClaim:
    """B0.1: one live controller per deployment; second claimant defers."""

    def test_lock_path_default_is_scoped_to_the_deployment(self, monkeypatch):
        """Two checkouts on one host must not share the default lock file."""
        monkeypatch.delenv("SOLARA_CONTROLLER_LOCK", raising=False)
        path = app_mod._controller_lock_path()
        assert os.path.basename(path).startswith("solara-controller-")
        assert path.endswith(".lock")

    def test_lock_path_env_override_wins(self, monkeypatch, tmp_path):
        override = tmp_path / "shared.lock"
        monkeypatch.setenv("SOLARA_CONTROLLER_LOCK", str(override))
        assert app_mod._controller_lock_path() == str(override)

    def test_start_background_services_skips_when_controller_slot_held(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        """A live controller holding the controller lock must defer startup."""
        monkeypatch.setenv("SOLARA_CONTROLLER_LOCK", str(tmp_path / "controller.lock"))
        saved = (
            app_mod._state.lm_thread_started,
            app_mod._state.mqtt_subscriber_started,
            app_mod._state.background_services_started_at,
        )
        app_mod._state.lm_thread_started = False
        app_mod._state.mqtt_subscriber_started = False
        app_mod._state.background_services_started_at = None
        lock_fd = os.open(str(tmp_path / "controller.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(app_mod, "_start_mqtt_subscriber") as mock_mqtt, \
                 patch.object(app_mod, "_start_load_manager_thread") as mock_lm, \
                 caplog.at_level(logging.CRITICAL, logger="app"):
                Config().set("LOAD_TESLA_CONTROLLER", "real")
                Config().set("LOAD_MANAGE_ENABLED", "True")
                app_mod.start_background_services()
            mock_mqtt.assert_not_called()
            mock_lm.assert_not_called()
            assert app_mod._state.background_services_started_at is None
            assert "controller" in caplog.text
        finally:
            os.close(lock_fd)
            app_mod._state.lm_thread_started = saved[0]
            app_mod._state.mqtt_subscriber_started = saved[1]
            app_mod._state.background_services_started_at = saved[2]

    def test_second_start_in_same_process_is_idempotent(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        """A repeat start in the same process must not look like a rival controller."""
        monkeypatch.setenv("SOLARA_CONTROLLER_LOCK", str(tmp_path / "controller.lock"))
        saved = (
            app_mod._state.lm_thread_started,
            app_mod._state.mqtt_subscriber_started,
            app_mod._state.background_services_started_at,
        )
        app_mod._state.lm_thread_started = False
        app_mod._state.mqtt_subscriber_started = False
        app_mod._state.background_services_started_at = None
        try:
            with patch.object(app_mod, "_start_mqtt_subscriber") as mock_mqtt, \
                 patch.object(app_mod, "_start_load_manager_thread") as mock_lm, \
                 caplog.at_level(logging.INFO, logger="app"):
                Config().set("LOAD_TESLA_CONTROLLER", "real")
                Config().set("LOAD_MANAGE_ENABLED", "True")
                app_mod.start_background_services()
                app_mod.start_background_services()
            # The gate is reentrant in one process; the thread starters keep
            # their own idempotency guards.
            mock_mqtt.assert_called()
            mock_lm.assert_called()
            assert "already holds the controller slot" not in caplog.text
        finally:
            app_mod._state.lm_thread_started = saved[0]
            app_mod._state.mqtt_subscriber_started = saved[1]
            app_mod._state.background_services_started_at = saved[2]


class TestWsgiEntrypoint:
    """wsgi.py exposes a fully constructed app for gunicorn."""

    def test_wsgi_exposes_flask_app(self):
        import wsgi

        assert isinstance(wsgi.app, Flask)
        resp = wsgi.app.test_client().get("/health")
        assert resp.status_code == 200
        assert "status" in resp.get_json()
