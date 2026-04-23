# 🛠️ AI Agent Guidelines for Solara Codebase

This document serves as a style guide and command reference for AI coding agents
operating within the `solara` repository. Adhering to these guidelines ensures
code consistency, maintainability, and adherence to project standards.

---

## General Advice

Take time to think things through before acting.

Don't worry about whether a bug or error is "your fault" or pre-existing. Just fix it.

When something is ambiguous or two consecutive fix attempts have not resolved a
failing test, **stop and ask** rather than continuing to iterate blindly.

---

## Project Structure

```
solara/
├── app.py              # Flask application entrypoint
├── tests/              # All pytest tests
├── utils/              # Shared utility modules
├── .opencode/plans/    # Agent planning scratch space (only writable dir during planning)
└── .env                # Local secrets — never commit
```

---

## Planning

**During planning, operate in strict read-only mode.** This means:

- No file writes anywhere in the repo except `.opencode/plans/`
- No shell commands that mutate state: no `pip install`, no `git commit`,
  no `git add`, no file edits, no database migrations
- Allowed read operations: `cat`, `ls`, `grep`, `git log`, `git diff`, `git status`

When asked to plan changes, break tasks into subtasks that each fit within a
**32k–64k token budget per subtask**. If a task requires touching more than 3
files or ~200 lines of code, split it into sequential subtasks and plan them
separately. Document each subtask as its own file in `.opencode/plans/`.

For changes larger than ~20 lines, summarize what will change (files affected,
functions modified, any data migrations or schema changes) before writing any code.

---

## 🚀 Build, Lint, & Test Commands

### Mandatory Post-Edit Verification Gate

After **any** code change, always run these commands in order. Do not proceed
to the next step if a prior step fails.

```bash
uv run pylint       # 1. Style and bug checks
uv run mypy         # 2. Type correctness
uv run pytest       # 3. Full test suite
```

### Individual Commands

| Purpose | Command |
|---|---|
| Run full test suite | `uv run pytest` |
| Run a single test | `uv run pytest tests/test_app.py::test_function_name` |
| Lint | `uv run pylint` |
| Type check | `uv run mypy` |
| Dev server | `uv run python app.py` |
| Production-like server | `gunicorn --reload --bind 0.0.0.0:8000 app:app` |

The dev server reads credentials from `.env` (`VUE_USERNAME`, `VUE_PASSWORD`).
Ensure that file is present and sourced before running.

---

## 📐 Code Style Guidelines

### 1. General Formatting (PEP 8)

- **Indentation:** 4 spaces — no tabs
- **Line length:** 100 characters maximum
- **Imports:** Grouped in this order, each on its own line:
  1. Standard library (`os`, `json`, `datetime`)
  2. Third-party packages (`flask`, `requests`, `pytz`)
  3. Local project imports (`.utils`, `.models`)
- All code must pass `pylint` clean with no suppressions unless explicitly justified
  in a comment

### 2. Naming Conventions

| Construct | Convention | Example |
|---|---|---|
| Modules / files | `snake_case` | `energy_utils.py` |
| Classes | `PascalCase` | `EmporiaClient` |
| Functions / methods | `snake_case` | `fetch_daily_usage()` |
| Constants | `ALL_CAPS` | `DEFAULT_TIMEOUT_SECS` |
| Variables | `snake_case` | `kwh_total` |

### 3. Documentation & Typing

- **Docstrings:** Required on all modules, classes, public methods, and functions.
  Use Google-style format:

  ```python
  def fetch_usage(start: datetime, end: datetime) -> list[float]:
      """Fetch energy usage between two timestamps.

      Args:
          start: Start of the query window, timezone-aware.
          end: End of the query window, timezone-aware.

      Returns:
          List of kWh readings, one per hour.

      Raises:
          EmporiaAPIError: If the upstream API returns a non-200 status.
      """
  ```

- **Type hints:** Mandatory on all function arguments, return values, and instance
  attributes. Use `from __future__ import annotations` at the top of modules to
  support forward references. Prefer built-in generics (`list[str]`, `dict[str, int]`)
  over `typing.List`, `typing.Dict` in Python 3.9+.

### 4. Error Handling

- Never use bare `except:` — always catch specific exceptions
- Use `with` statements for file handles, DB connections, and any resource
  requiring cleanup
- Wrap all Emporia API calls in `try/except` blocks handling at minimum:
  `requests.RequestException`, `requests.Timeout`, and any custom `APIError`
- On auth failures (HTTP 401/403), log the error and raise — do not silently retry

### 5. Security

- **No hardcoded secrets.** Read all credentials and API keys from environment
  variables via `python-decouple` or `os.environ`. If you find hardcoded secrets,
  fix them immediately.
- **Validate all user input** (URL params, form fields, query strings) before
  use or storage.

---

## 🧩 Specific Guidelines

### Date / Time

- Always use timezone-aware `datetime` objects
- Use `pytz` for timezone handling; default to local system timezone unless
  storing to a database, in which case use UTC
- Never compare naive and aware datetimes — this will raise a `TypeError` at runtime

### HTTP Requests

- Use the `requests` library
- Parse JSON responses with `.json()` — never `json.loads(response.text)`
- Set explicit timeouts on all outbound requests (e.g., `timeout=30`)

### Emporia API

- Rate limits: respect any `Retry-After` headers
- Auth tokens expire; implement token refresh before retrying a failed request
- Wrap all calls in the standard error handling pattern described above

---

## 🧪 Testing Guidelines

- **Write tests for all new functionality.** A PR with new behavior but no new
  tests is incomplete.
- **Never add special-case code solely to make tests pass.** For example, do not
  add `if os.getenv("TESTING"):` branches in production code paths.
- **Updating test data is allowed and expected** when modernizing hardcoded dates
  or stale fixture values. Example of what's allowed:
  ```python
  # Before (stale fixture date causes false failure)
  SAMPLE_DATE = datetime(2021, 1, 1)
  # After (updated to a current reference date)
  SAMPLE_DATE = datetime(2025, 1, 1)
  ```
  Example of what's **not** allowed:
  ```python
  # Not allowed — production logic changed to accommodate a test
  if date.year < 2022:
      return []  # silence legacy test failure
  ```

---

## ⛔ Stop and Ask Policy

Pause and explicitly ask the user before proceeding when:

- Requirements are ambiguous and the choice between interpretations would affect
  more than one file
- A change involves destructive operations: file deletion, schema migration,
  bulk data modification
- Two consecutive attempts to fix a failing test have not resolved it
- A dependency needs to be added or upgraded (`pyproject.toml` / `requirements`)
- You are about to make a change that touches the auth flow or secrets handling