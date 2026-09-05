"""
Tests for app.limiter — shared rate limiter instance contract.

Covers:
- limiter is an instance of slowapi.Limiter
- key_func is slowapi.util.get_remote_address
- default_limits is an empty list (limits are per-decorator, not global)
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.limiter import limiter


def test_limiter_instance_exists() -> None:
    assert isinstance(limiter, Limiter)


def test_limiter_key_func_is_remote_address() -> None:
    assert limiter._key_func == get_remote_address


def test_limiter_default_limits_empty() -> None:
    assert limiter._default_limits == []
