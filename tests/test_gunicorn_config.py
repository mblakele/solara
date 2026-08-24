"""Tests for gunicorn shutdown hooks and app-level signal hook installation.

Shutdown contract: gunicorn's ``post_worker_init`` hook must chain a
cooperative-shutdown handler over the worker's installed signal handlers so
one Ctrl-C/SIGTERM stops background services promptly, and ``worker_int`` /
``worker_exit`` must funnel through ``app.request_shutdown``.
"""

from __future__ import annotations

import importlib.util
import signal
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as app_mod


def _load_gunicorn_conf():
    """Load gunicorn.conf.py (dotted filename needs explicit loading)."""
    path = Path(__file__).resolve().parent.parent / "gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("gunicorn_conf", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_HOOKED_SIGNALS = (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)


class TestSignalHookInstallation(unittest.TestCase):
    """install_shutdown_signal_hooks() chains over existing handlers."""

    def setUp(self):
        self._saved = {sig: signal.getsignal(sig) for sig in _HOOKED_SIGNALS}
        app_mod._stop_event.clear()
        app_mod._shutdown_hooks_installed = False
        self.conf = _load_gunicorn_conf()

    def tearDown(self):
        for sig, handler in self._saved.items():
            signal.signal(sig, handler)
        app_mod._stop_event.clear()
        app_mod._shutdown_hooks_installed = False

    def test_post_worker_init_installs_callable_handlers(self):
        # Give every hooked signal a recognizable callable predecessor
        # (under pytest some default to SIG_DFL, which must stay untouched).
        for sig in _HOOKED_SIGNALS:
            signal.signal(sig, self._saved[sig] if callable(self._saved[sig])
                          else signal.default_int_handler)
        app_mod._state.shutdown_hooks_installed = False

        self.conf.post_worker_init(MagicMock())

        self.assertTrue(app_mod._shutdown_hooks_installed)
        for sig in _HOOKED_SIGNALS:
            handler = signal.getsignal(sig)
            self.assertTrue(
                callable(handler),
                f"signal {sig} lost its handler after installation",
            )
            self.assertNotEqual(handler, self._saved[sig])

    def test_install_is_idempotent(self):
        self.conf.post_worker_init(MagicMock())
        first = {sig: signal.getsignal(sig) for sig in _HOOKED_SIGNALS}

        self.conf.post_worker_init(MagicMock())
        second = {sig: signal.getsignal(sig) for sig in _HOOKED_SIGNALS}

        self.assertEqual(first, second, "second install re-wrapped handlers")

    def test_chained_handler_requests_shutdown_then_delegates(self):
        delegated = []

        def fake_previous(signum, frame):
            delegated.append(signum)

        # Install a recognizable predecessor for SIGTERM, then hook.
        signal.signal(signal.SIGTERM, fake_previous)
        app_mod._shutdown_hooks_installed = False
        self.conf.post_worker_init(MagicMock())

        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)

        self.assertTrue(app_mod._stop_event.is_set())
        self.assertEqual(delegated, [signal.SIGTERM])

    def test_non_callable_disposition_left_untouched(self):
        signal.signal(signal.SIGQUIT, signal.SIG_DFL)
        app_mod._shutdown_hooks_installed = False

        self.conf.post_worker_init(MagicMock())

        self.assertIs(signal.getsignal(signal.SIGQUIT), signal.SIG_DFL)


class TestGunicornHooks(unittest.TestCase):
    """worker_int/worker_exit funnel through app.request_shutdown."""

    def setUp(self):
        self.conf = _load_gunicorn_conf()

    def test_worker_int_calls_request_shutdown(self):
        with patch.object(app_mod, "request_shutdown") as spy:
            self.conf.worker_int(MagicMock())
        spy.assert_called_once_with("gunicorn:worker_int")

    def test_worker_exit_calls_request_shutdown(self):
        with patch.object(app_mod, "request_shutdown") as spy:
            self.conf.worker_exit(MagicMock(), MagicMock())
        spy.assert_called_once_with("gunicorn:worker_exit")
