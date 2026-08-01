"""Gunicorn entry point for the Solara Flask application.

Creates the Flask app via :func:`app.create_app` and starts background
services (MQTT subscriber, load-management thread) so each gunicorn
worker serves a fully initialized application. The module import is
side-effect free; only this entry point starts background threads.

The app object is named ``app`` so gunicorn's ``wsgi:app`` target works.
"""

from app import create_app, start_background_services

app = create_app()
start_background_services()
