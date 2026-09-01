"""
Phase 6 demo canonical test case.

This file has a deliberate one-character typo in its import statement:

    from app.servies.billing import charge   # <-- typo: 'servies' should be 'services'

The typo causes ``ModuleNotFoundError: No module named 'app.servies'`` to
fire at module-import time. The orchestrator's deterministic
``ModuleNotFoundError`` fast-path (Phase 3) catches this and applies a
``conftest.py`` fix (or, with the typo corrected directly, simply imports
the right module).

When the typo is corrected — either by Haunter's PR or by a developer — the
test below runs and passes. The test is intentionally trivial: the point is
the import path, not the test logic.

Sandbox path: the test mirror's pytest run will fail at collection. The
failure surfaces as a real CI error. Haunter diagnoses it, the fast-path
applies the fix, the PR passes.

Local pytest path: ``backend/tests/demo_canonical/conftest.py`` intercepts
the ``ModuleNotFoundError`` during collection and reports this test as
skipped, so the suite stays green while the typo is in place.
"""

from app.servies.billing import charge  # noqa: F401  -- deliberate typo for Phase 6 demo


def test_charge_returns_amount() -> None:
    """When the typo is corrected, ``charge(100)`` returns 100."""
    assert charge(100) == 100


def test_charge_zero() -> None:
    """Zero is a valid charge amount."""
    assert charge(0) == 0
