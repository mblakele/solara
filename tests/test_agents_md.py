"""Tests that keep AGENTS.md small, accurate, and in step with the tree.

AGENTS.md is loaded into every agent session. These tests stop three drifts:
a module missing from the map, a map entry for a file that does not exist, and
unchecked growth of the file itself.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"

MAX_LINES = 250
MAX_LINE_LENGTH = 100
MAP_ENTRY_PATTERN = re.compile(r"^((?:tools/)?[\w.]+\.py)\s*(?:#.*)?$")


def _agents_text() -> str:
    return AGENTS_MD.read_text(encoding="utf-8")


def _module_map_entries(text: str) -> list[str]:
    """Return the module paths listed in the fenced module-map block."""

    lines = text.splitlines()
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if line.strip().lstrip("#").strip().lower() == "module map"
        )
    except StopIteration as exc:  # pragma: no cover - guarded by callers
        raise AssertionError(
            "AGENTS.md has no 'Module map' heading. The map must stay discoverable."
        ) from exc

    fenced = [line for line in lines[start + 1 :] if line.startswith("```")]
    assert fenced, "The module map must be a fenced block."

    entries: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() == "```" and entries:
            break
        match = MAP_ENTRY_PATTERN.match(line.strip())
        if match:
            entries.append(match.group(1))
    return entries


def _expected_modules() -> list[str]:
    """Return every production module that the map must name."""

    modules = sorted(p.name for p in REPO_ROOT.glob("*.py"))
    modules += sorted(f"tools/{p.name}" for p in (REPO_ROOT / "tools").glob("*.py"))
    return modules


def test_module_map_is_complete() -> None:
    """Every root and tools module must appear in the map."""

    entries = set(_module_map_entries(_agents_text()))
    missing = sorted(set(_expected_modules()) - entries)
    assert not missing, (
        f"AGENTS.md module map omits: {missing}. Add one line per module."
    )


def test_module_map_has_no_extra_entries() -> None:
    """The map must not name a file that does not exist."""

    entries = set(_module_map_entries(_agents_text()))
    stale = sorted(entries - set(_expected_modules()))
    assert not stale, (
        f"AGENTS.md module map names files that do not exist: {stale}. "
        "Remove the stale line."
    )


def test_module_map_has_no_duplicates() -> None:
    """Each module gets exactly one map line."""

    entries = _module_map_entries(_agents_text())
    duplicates = sorted({entry for entry in entries if entries.count(entry) > 1})
    assert not duplicates, f"AGENTS.md module map repeats: {duplicates}."


def test_length_budget() -> None:
    """AGENTS.md must stay small enough to load every session."""

    lines = _agents_text().splitlines()
    assert len(lines) <= MAX_LINES, (
        f"AGENTS.md is {len(lines)} lines, over the {MAX_LINES}-line budget. "
        "Move reference material to docs/ and keep rules here."
    )


def test_line_length() -> None:
    """AGENTS.md must obey the line length it sets for the code."""

    overlong = [
        (number, len(line))
        for number, line in enumerate(_agents_text().splitlines(), start=1)
        if len(line) > MAX_LINE_LENGTH
    ]
    assert not overlong, (
        f"AGENTS.md has {len(overlong)} lines over {MAX_LINE_LENGTH} characters: "
        f"{overlong[:10]}"
    )
