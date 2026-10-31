"""Issue #147: _stream_from_db timeout anchored to Lambda remaining time.

The 900s guard inside ``_stream_from_db`` was unreachable under Lambda: the
hard kill counts from invoke start while the generator's clock started after
auth/queries/replay. The budget now comes from
``context.get_remaining_time_in_millis()`` (stashed by ``lambda_handler``),
with 900s kept only as the non-Lambda fallback.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

from app.lambda_runtime import (
    LAMBDA_TIMEOUT_FALLBACK_S,
    LAMBDA_TIMEOUT_SAFETY_MARGIN_S,
    reset_lambda_context,
    resolve_lambda_timeout_s,
    set_lambda_context,
)


class _FakeLambdaContext:
    """Minimal Lambda invoke context: only the remaining-time method."""

    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


@contextmanager
def _stashed_context(context: Any) -> Iterator[None]:
    token = set_lambda_context(context)
    try:
        yield
    finally:
        reset_lambda_context(token)


def test_resolve_timeout_falls_back_without_lambda_context() -> None:
    """Local dev / no invoke context keeps the 900s fallback."""
    assert resolve_lambda_timeout_s() == LAMBDA_TIMEOUT_FALLBACK_S == 900.0


def test_resolve_timeout_anchors_to_lambda_remaining_time() -> None:
    """Under Lambda the budget is remaining time minus the safety margin."""
    with _stashed_context(_FakeLambdaContext(600_000)):
        assert resolve_lambda_timeout_s() == 600.0 - LAMBDA_TIMEOUT_SAFETY_MARGIN_S


def test_resolve_timeout_falls_back_on_broken_context() -> None:
    """A context that raises, a non-numeric budget, or no time left → 900s."""

    class _Raising:
        def get_remaining_time_in_millis(self) -> int:
            raise RuntimeError("sandboxed")

    class _NonNumeric:
        def get_remaining_time_in_millis(self) -> str:
            return "soon"

    for broken in (_Raising(), _NonNumeric(), object(), None):
        with _stashed_context(broken):
            assert resolve_lambda_timeout_s() == LAMBDA_TIMEOUT_FALLBACK_S

    with _stashed_context(_FakeLambdaContext(0)):
        assert resolve_lambda_timeout_s() == LAMBDA_TIMEOUT_FALLBACK_S


def test_resolve_timeout_floors_tiny_remaining_at_zero() -> None:
    """Remaining time below the margin trips the guard immediately, not never."""
    with _stashed_context(_FakeLambdaContext(10_000)):
        assert resolve_lambda_timeout_s() == 0.0


def test_handler_stashes_context_and_resets_it() -> None:
    """lambda_handler.handler exposes the invoke context during dispatch only."""
    import lambda_handler

    seen: list[float] = []

    def _fake_dispatch(event: dict, context: Any) -> dict:
        seen.append(resolve_lambda_timeout_s())
        assert context is not None
        return {"status": "completed"}

    with patch.object(lambda_handler, "_handler", side_effect=_fake_dispatch) as dispatched:
        result = lambda_handler.handler({"ping": True}, _FakeLambdaContext(600_000))

    assert result == {"status": "completed"}
    dispatched.assert_called_once()
    assert seen == [600.0 - LAMBDA_TIMEOUT_SAFETY_MARGIN_S]
    # The invoke context must not leak into the next invocation.
    assert resolve_lambda_timeout_s() == LAMBDA_TIMEOUT_FALLBACK_S



import pytest


@pytest.mark.skip(reason="session removed, #207")
def test_placeholder():
    pass
