"""Gunicorn entry point for the Solara Flask application.

Imports the Flask app constructed at module level in :mod:`app` and
starts background services (MQTT subscriber, load-management threads) so
each gunicorn worker serves a fully initialized application.

The app object is named ``app`` so gunicorn's ``wsgi:app`` target works.

Deployment constraints (see ``app.start_background_services``):
- Load management must run in exactly ONE process. The single-instance
  lock (``.load-manager.lock``, advisory flock) makes a second process
  fail loud and skip background services instead of running duplicate
  decision loops against the same physical devices.
- Keep gunicorn at one worker: do NOT add ``-w/--workers`` and do not
  scale Render ``numInstances`` above 1.
- Do NOT add ``--preload``: the app import (and therefore the background
  services) would run in the master process before forking, and threads
  do not survive fork — every worker would silently run without load
  management.
"""

from app import app, start_background_services  # noqa: F401  # pylint: disable=unused-import
# `app` is intentionally unused here: it is re-exported as the `wsgi:app`
# target gunicorn resolves at startup (see module docstring).

start_background_services()
