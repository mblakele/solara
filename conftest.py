"""Pytest configuration for Solara project."""

import gc

import pytest

from config import Config
import device_config

def _close_all_aiohttp_sessions():
    """Find and close all lingering aiohttp ClientSessions."""
    import aiohttp.client

    for obj in gc.get_objects():
        try:
            if isinstance(obj, aiohttp.client.ClientSession) and not obj.closed:
                # Close synchronously by running the async close in a new event loop
                import asyncio

                loop = asyncio.new_event_loop()
                loop.run_until_complete(obj.close())
                loop.close()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Prevent .env from polluting tests.

    Clears config keys from os.environ and the .env cache, then sets
    deterministic defaults. Also clears the device_config cache so each
    test starts with a fresh read of devices.json (or empty defaults if
    the file doesn't exist).
    """
    cfg = Config()
    cfg.clear_all()
    # Override load management defaults so tests control them explicitly
    cfg.set("LOAD_MANAGE_ENABLED", "False")
    cfg.set("LOAD_MANAGE_DRY_RUN", "True")
    cfg.set("LOAD_PLUG_CONTROLLER", "stub")
    cfg.set("LOAD_TESLA_CONTROLLER", "stub")

    # Clear Tesla secrets so tests control them
    cfg.set("TESLA_CLIENT_ID", "")
    cfg.set("TESLA_CLIENT_SECRET", "")
    cfg.set("TESLA_REGION", "na")

    # Clear VOCOlinc credentials
    cfg.set("VOCOLINC_USERNAME", "")
    cfg.set("VOCOLINC_PASSWORD", "")

    # Clear VUE credentials so mock mode is used by default
    cfg.set("VUE_USERNAME", "")
    cfg.set("VUE_PASSWORD", "")

    # Clear Tesla telemetry config
    cfg.set("TESLA_TELEMETRY_CALLBACK_URL", "")
    cfg.set("TESLA_TELEMETRY_LOCATION_INTERVAL_SEC", "")
    cfg.set("TESLA_TELEMETRY_CHARGESTATE_INTERVAL_SEC", "")
    cfg.set("TESLA_TELEMETRY_DETAILEDCHARGESTATE_INTERVAL_SEC", "")
    monkeypatch.setenv("PUBLIC_URL", "")

    # Ensure mock mode is off by default (tests enable it explicitly)
    cfg.set("MOCK", "False")
    cfg.set("MOCK_ERROR", "False")
    cfg.set("DEBUG", "False")

    # Clear device_config cache so tests get fresh defaults
    device_config.reload()


def pytest_unconfigure(config):  # pylint: disable=unused-argument
    """Called at the very end of the test session."""
    _close_all_aiohttp_sessions()
    gc.collect()
