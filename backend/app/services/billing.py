"""Billing service.

Provides a minimal `charge(amount)` helper. Exists so the Phase 6 demo
canonical test (`backend/tests/demo_canonical/test_demo_canonical.py`) has
a real import target after its deliberate typo (`app.servies` -> `app.services`)
is corrected.
"""

from __future__ import annotations


def charge(amount: int) -> int:
    """Charge the given amount and return the amount charged.

    Minimal stub — Phase 6 does not introduce real billing logic. Real
    billing integration is out of scope (see fix.md "Out of scope").
    """
    if amount < 0:
        raise ValueError("amount must be non-negative")
    return amount
