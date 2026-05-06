"""Pytest configuration for Solara project."""

import gc

import pytest

from decouple import config
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
def clean_env():
    """Prevent .env from polluting tests via decouple.

    Clears device_config cache so each test starts with a fresh read of
    devices.json (or empty defaults if the file doesn't exist).
    """
    config.clear_all()
    # Override load management defaults so tests control them explicitly
    config.set("LOAD_MANAGE_ENABLED", "False")
    config.set("LOAD_MANAGE_DRY_RUN", "True")
    config.set("LOAD_PLUG_CONTROLLER", "stub")
    config.set("LOAD_TESLA_CONTROLLER", "stub")

    # Clear Tesla secrets so tests control them
    config.set("TESLA_CLIENT_ID", "")
    config.set("TESLA_CLIENT_SECRET", "")
    config.set("TESLA_REGION", "na")

    # Clear VOCOlinc credentials
    config.set("VOCOLINC_USERNAME", "")
    config.set("VOCOLINC_PASSWORD", "")

    # Clear VUE credentials so mock mode is used by default
    config.set("VUE_USERNAME", "")
    config.set("VUE_PASSWORD", "")

    # Ensure mock mode is off by default (tests enable it explicitly)
    config.set("MOCK", "False")
    config.set("MOCK_ERROR", "False")
    config.set("DEBUG", "False")

    # Clear device_config cache so tests get fresh defaults
    device_config.reload()


def pytest_unconfigure(config):  # pylint: disable=unused-argument
    """Called at the very end of the test session."""
    _close_all_aiohttp_sessions()
    gc.collect()
