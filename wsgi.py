"""Gunicorn entry point for the Solara Flask application.

Imports the Flask app constructed at module level in :mod:`app` and
starts background services (MQTT subscriber, load-management thread) so
each gunicorn worker serves a fully initialized application.

The app object is named ``app`` so gunicorn's ``wsgi:app`` target works.
"""

from app import app, start_background_services  # noqa: F401  (re-exported as wsgi:app)

start_background_services()
