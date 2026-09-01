"""
Conftest for the Phase 6 demo canonical test case.

The test file in this directory (``test_demo_canonical.py``) has a deliberate
one-character typo in an import statement
(``from app.servies.billing import charge`` — the ``c`` in ``services`` is
missing). The typo causes ``ModuleNotFoundError`` to fire at module-import
time.

Two audiences consume this file:

1. **The sandbox (production / demo path).** The sandbox clones the test
   mirror and runs ``pytest`` without this conftest's safety net — the
   import error propagates as a real test failure, the orchestrator
   diagnoses it, and the deterministic ``ModuleNotFoundError`` fast-path
   (Phase 3) applies a ``conftest.py`` fix. The PR then passes.

2. **Our own CI / local pytest run.** This conftest pre-checks the demo
   file via ``pytest_ignore_collect`` (the earliest hook that fires before
   pytest attempts to import the module). We pre-load the file with
   ``importlib``; if the deliberate ``ModuleNotFoundError`` fires, we
   return ``True`` and pytest skips the file. The session continues with
   the rest of the suite, exits cleanly (no collection error), and the
   demo file simply does not appear in the collected items.

   When the typo is corrected, the pre-load succeeds, we return ``False``,
   and pytest collects and runs the test normally — it passes.

The hook is scoped to this directory only (it lives next to the test file,
not in the parent ``backend/tests/conftest.py``) so it cannot affect any
other test module.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_DEMO_TEST_FILENAME = "test_demo_canonical.py"


def _basename(path) -> str:
    """Return the basename of a path-like object (str, pathlib.Path, py.path.local)."""
    name = getattr(path, "name", None)
    if name is not None:
        return name
    return os.path.basename(str(path))


def _has_deliberate_import_error(file_path) -> bool:
    """Return True if loading ``file_path`` raises ModuleNotFoundError."""
    spec = importlib.util.spec_from_file_location("_haunter_demo_check", file_path)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError:
        return True
    return False


def pytest_ignore_collect(collection_path, config):
    """
    Skip the demo canonical test file when its deliberate import error is
    present.

    In pytest 9.x the module is imported before any per-file collection
    hooks (``pytest_collect_file`` / ``pytest_collectstart`` /
    ``pytest_pycollect_makemodule``) fire. ``pytest_ignore_collect`` is the
    earliest hook and is called *before* pytest attempts the import, so we
    pre-load the file ourselves. If ``ModuleNotFoundError`` fires (the
    deliberate typo), we return ``True`` and pytest skips the file. The
    session continues with the rest of the suite and exits cleanly.

    When the typo is corrected, the pre-load succeeds, we return
    ``False``, and pytest collects and runs the test normally.
    """
    if _basename(collection_path) != _DEMO_TEST_FILENAME:
        return False

    if not _has_deliberate_import_error(collection_path):
        return False  # Typo is fixed (or never present) — collect normally.

    return True
