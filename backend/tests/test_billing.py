"""
Tests for billing service (backend/app/services/billing.py).

Covers:
- charge(0) returns 0
- charge(100) returns 100
- charge(-1) raises ValueError("amount must be non-negative")
- charge(10**9) returns 10**9 unchanged
- charge with large negative value raises ValueError
"""

from __future__ import annotations

import pytest

from app.services.billing import charge


def test_charge_zero():
    """charge(0) returns 0."""
    assert charge(0) == 0


def test_charge_positive():
    """charge(100) returns 100."""
    assert charge(100) == 100


def test_charge_negative_raises_value_error():
    """charge(-1) raises ValueError with 'amount must be non-negative'."""
    with pytest.raises(ValueError) as exc_info:
        charge(-1)
    assert "amount must be non-negative" in str(exc_info.value)


def test_charge_large_value():
    """charge(10**9) returns 10**9 unchanged."""
    large_amount = 10**9
    assert charge(large_amount) == large_amount


def test_charge_large_negative_raises_value_error():
    """charge with large negative value raises ValueError."""
    with pytest.raises(ValueError) as exc_info:
        charge(-10**9)
    assert "amount must be non-negative" in str(exc_info.value)
