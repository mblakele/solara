# Solara - Hourly Predicted Usage for Emporia VUE Utility Connect

## Overview

Solara is a Python/Flask web application that connects to the Emporia VUE Utility Connect API to predict hourly solar energy usage. It helps homeowners with rooftop solar and net energy metering (NEM) maximize self-consumption by predicting total energy produced or consumed in the coming hour, based on per-second energy data from smart meters over the past ten minutes.

Key capabilities:
- Fetches real-time energy metrics from the Emporia VUE API
- Predicts hourly energy usage/generation
- Provides a simple web UI showing current and predicted metrics
- Exposes a JSON HTTP endpoint for home automation integrations

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Backend (Flask)

- **Framework**: Python with Flask as the web framework
- **Entry point**: `app.py` sets up the Flask app, JSON serialization, error handling, and Jinja2 template filters
- **Metrics logic**: `metrics.py` contains the `Metrics` class that handles Emporia VUE API authentication and data fetching via the `pyemvue` library
- **Configuration**: Uses `python-decouple` to manage environment variables (e.g., `DEBUG` flag), keeping secrets out of source code
- **Error handling**: A custom `RetryableMetricsException` triggers an auto-refreshing error page (5-second refresh) when the Emporia API returns server errors

### Authentication

- **Emporia VUE API**: Authentication tokens (access, id, refresh) are stored in `.vue-keys.json` at the project root. This file is read by the `Metrics` class to authenticate with the Emporia VUE API using the `pyemvue` library.
- **Note**: The `.vue-keys.json` file contains sensitive credentials and should not be committed to source control in production use.

### Frontend (Templates)

- **Templating**: Jinja2 HTML templates in the `templates/` directory
- **Pages**: `index.html` displays energy metrics in a large-font table layout; `error_retryable.html` shows server errors with an auto-retry link
- **Timezone handling**: A custom Jinja2 filter (`astimezonestr`) converts UTC timestamps to device-local time for display

### JSON Serialization

- A custom `CustomJSONProvider` extends Flask's default JSON provider to handle Python `datetime` (ISO 8601) and `timedelta` (ISO 8601 duration) types, and iterables — making the JSON API endpoint robust for these data types.

### Data Flow

1. App receives HTTP request
2. `Metrics` class authenticates with Emporia VUE API using stored tokens
3. Real-time per-second energy data is fetched for the past 10 minutes
4. Data is processed to predict current-hour totals
5. Results are rendered as HTML or returned as JSON

### Deployment

- Designed for deployment on [Render](https://render.com) (free tier compatible) using Python/Flask
- A `render.yaml` blueprint may be expected for one-click deployment
- Can also run locally or on any Python-compatible hosting environment

## External Dependencies

| Dependency | Purpose |
|---|---|
| `pyemvue` | Python client library for the Emporia VUE API — fetches real-time smart meter data |
| `flask` | Web framework for routing, templating, and JSON responses |
| `python-decouple` | Reads configuration from environment variables or `.env` files |
| `pytz` | Timezone conversion for displaying timestamps in device-local time |
| `isodate` | Serializes Python `timedelta` objects to ISO 8601 duration strings |
| `humps` | Case conversion utilities (camelCase ↔ snake_case) for API data |
| `requests` | HTTP client used internally by `pyemvue` for API calls |
| **Emporia VUE API** | External cloud API that receives data from the VUE Utility Connect hardware device |
| **Render.com** | Recommended cloud hosting platform for deployment |

### Key Environment Variables

- `DEBUG` — Enables debug logging when set to `True`
- Emporia VUE credentials are stored in `.vue-keys.json` rather than environment variables (tokens are obtained via Emporia's Cognito-based authentication)