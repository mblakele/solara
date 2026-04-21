# 🛠️ AI Agent Guidelines for Solara Codebase

This document serves as a style guide and command reference for AI coding agents operating within the `solara` repository. Adhering to these guidelines ensures code consistency, maintainability, and adherence to project standards.

## General Advice

Take time to think things through.

Don't worry about whether a bug or any error is "your fault" or pre-existing. Just fix it.

## Planning

When planning, run in read-only mode. The sole exception is that you may create and edit files in `.opencode/plans/` as requested.

When asked to plan changes, try to break up tasks to fit within a budget 32k-64k tokens.

## 🚀 Build, Lint, & Test Commands

Use these commands for routine maintenance and validation.

### Testing
- **Write tests for new functionality** Any new functionality must be accompanied by new tests.
- **Never add special-case code that only exists to let tests pass** However it may be necessary to update test data to modernize the dates.
- **Run all tests:** `uv run pytest`
  - *Purpose:* Executes the full test suite to ensure all features work as expected.
- **Run a single specific test:** `uv run pytest tests/test_app.py::test_function_name`
  - *Purpose:* Executes one specific test case for isolated debugging.

### Linting & Type Checking
- **Run Linter:** `uv run pylint`
  - *Purpose:* Checks for general code style issues, potential bugs, and bad practices according to Python standards.
- **Type Checking:** (Specify command if found, e.g., `mypy src/`)
  - *Recommendation:* Add explicit type-checking commands here once the linter/type checker is confirmed.

### Running the Application
- **Local Development (Development Server):** `uv run python app.py`
  - *Note:* Use `gunicorn` for production-like local testing: `gunicorn --reload --bind 0.0.0.0:8000 app:app`
  - *Environment:* Ensure the `.env` file containing `VUE_USERNAME` and `VUE_PASSWORD` is sourced/read by the process.

## 📐 Code Style Guidelines

### 1. General Style & Formatting (PEP 8 Adherence)
*   **Indentation:** Use 4 spaces.
*   **Line Length:** Maximum line length should not exceed 100 characters.
*   **Imports:** Group imports in the following order, each on its own line:
    1.  Standard library imports (e.g., `os`, `json`, `datetime`).
    2.  Third-party/External package imports (e.g., `flask`, `requests`, `pytz`).
    3.  Local application/project imports (e.g., `.utils`, `.models`).
*   **Flake8/Pylint/Black:** All code must pass `pylint` and conform to formatting expected by the development tools.

### 2. Naming Conventions
*   **Modules/Files:** Use `snake_case` (lowercase\_with\_underscores).
*   **Classes:** Use `CapWords` (PascalCase).
*   **Functions/Methods:** Use `snake_case`.
*   **Constants:** Use `ALL_CAPS_WITH_UNDERSCORES`.
*   **Variables:** Use `snake_case`.

### 3. Documentation & Typing
*   **Docstrings:** All modules, classes, public methods, and functions *must* have clear docstrings. Use Google or NumPy style format.
*   **Type Hinting:** Mandatory for all function arguments, return values, and instance attributes. Prefer the `typing` module for complex types.

### 4. Error Handling
*   **Exceptions:** Instead of bare `except:` blocks, always catch specific exceptions (`except SpecificError:`) to ensure robustness.
*   **Context Managers:** Use `with` statements (context managers) for resources that need explicit cleanup (e.g., file handles, database connections).
*   **API Calls:** Wrap external API calls (like Emporia API calls) in robust `try...except` blocks that catch potential `requests` errors, authentication failures, or `APIError` (if a custom one exists).

### 5. Security Considerations
*   **Secrets Management:** Never hardcode credentials, API keys, or passwords. Always retrieve them from environment variables (read via `python-decouple` or similar mechanisms).
*   **Input Validation:** Always validate and sanitize all user-controllable input (URLs, form data, query parameters) before use or database interaction.

## 🧩 Specific Guidelines
*   **Date/Time:** Use `datetime` objects and leverage `pytz` for timezone-aware operations. Timezones should default to the local system timezone unless absolute UTC is required for storage.
*   **HTTP Requests:** Use the `requests` library. When dealing with JSON, use the `.json()` method instead of manual parsing.

## 📜 Included Rules (From External Sources)
*   **Cursor Rules:** None found in `.cursor/rules/`.
*   **Copilot Rules:** None found in `.github/copilot-instructions.md`.

---
*This document is automatically generated and should be kept up-to-date with project evolution.*
