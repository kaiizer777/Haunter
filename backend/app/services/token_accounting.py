"""
Canonical token/cost accounting + JSONB partial-update builders (issue #106).

Two problems, one module:

1. Token/cost drift — ``context_gatherer``, ``fix_generator`` and
   ``pr_writer`` each carried their own ``COST_PER_*`` constants and their own
   inline ``usage.get(...)`` parsing. Any pricing change — or any provider
   response that spells keys ``prompt_tokens``/``completion_tokens``, returns
   ``None``, or returns numeric strings — silently diverged the stored
   ``RunStep.cost_estimate`` from the tokens actually recorded, so dashboard
   totals drifted from reality. Every writer now goes through
   :func:`normalize_usage` + :func:`estimate_cost` (+ :func:`make_run_step`),
   so there is exactly one formula and one parsing rule.

2. Whole-document JSONB rewrites — ``SessionOrchestrator._persist`` assigned
   the full ``conversation_history`` list and ``staged_patches`` dict on every
   turn, so each keystroke-sized update paid a full TOAST rewrite and shipped
   the whole document over the wire. The statement builders below express
   small updates as server-side deltas instead:
   history appends via the ``||`` concat operator, single staged-patch writes
   via ``jsonb_set``, patch merges via ``||``, and patch deletes via ``-``.
   The UPDATE ships only the delta; the full document is never round-tripped
   through the client.

Dashboard impact: nil by construction — same rates, same ``round(..., 8)``,
same non-negative-int coercion — so cost totals are unchanged-or-corrected
(only previously-silent-zero / crash-on-None rows change, toward the true
value).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import Text, cast, func, literal, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, array

from app.models import RunStep

# ---------------------------------------------------------------------------
# Canonical pricing — single source of truth for every RunStep writer.
# Placeholder rates ($0.001 / 1k input, $0.002 / 1k output); replace with real
# provider pricing once known. Import these names — do not re-define them.
# ---------------------------------------------------------------------------

COST_PER_INPUT_TOKEN: float = 0.001 / 1_000
COST_PER_OUTPUT_TOKEN: float = 0.002 / 1_000

#: Rounding applied to every persisted ``cost_estimate`` (matches the
#: historical ``round(cost, 8)`` the dashboards aggregate with SUM).
COST_ROUND_DIGITS: int = 8


# ---------------------------------------------------------------------------
# Usage normalization + cost estimation
# ---------------------------------------------------------------------------


def _coerce_tokens(value: Any) -> int:
    """Coerce a token count to a non-negative int.

    Providers (and test doubles) hand back ``None``, numeric strings, floats
    or negatives; each used to either crash ``int(None)`` or poison the cost
    math. Anything unparseable counts as 0 — tokens unreported are tokens
    unbilled, which is the safe direction for a cost ceiling. ``OverflowError``
    is caught too because ``int(float("inf"))`` (or a sufficiently large
    float) raises it, and an overflow must not abort response processing
    (see cubic#4297090262).
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    try:
        coerced = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return coerced if coerced > 0 else 0


def normalize_usage(usage: Mapping[str, Any] | None) -> tuple[int, int]:
    """Extract ``(input_tokens, output_tokens)`` from an LLM usage payload.

    Accepts both spellings the adapters emit — canonical
    ``input_tokens``/``output_tokens`` and wire-level
    ``prompt_tokens``/``completion_tokens`` — with the canonical spelling
    winning when both are present. ``None``/non-dict payloads yield ``(0, 0)``
    instead of raising.
    """
    if not isinstance(usage, Mapping):
        return (0, 0)
    raw_in = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    raw_out = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return (_coerce_tokens(raw_in), _coerce_tokens(raw_out))


def estimate_cost(input_tokens: Any, output_tokens: Any) -> float:
    """Canonical cost estimate, rounded exactly as persisted.

    Inputs are coerced with the same rule as :func:`normalize_usage`, so
    recomputing from stored columns always reproduces the stored estimate.
    """
    total = (_coerce_tokens(input_tokens) * COST_PER_INPUT_TOKEN) + (
        _coerce_tokens(output_tokens) * COST_PER_OUTPUT_TOKEN
    )
    return round(total, COST_ROUND_DIGITS)


def cost_matches_stored(
    input_tokens: Any, output_tokens: Any, cost_estimate: Any
) -> bool:
    """True when a stored ``cost_estimate`` equals recomputation from tokens.

    Round-trip invariant asserted by tests: the number on the dashboard must
    be exactly the number the stored payload implies.
    """
    try:
        stored = float(cost_estimate or 0.0)
    except (TypeError, ValueError):
        return False
    return abs(estimate_cost(input_tokens, output_tokens) - stored) <= 1e-9


def make_run_step(
    *,
    run_id: uuid.UUID,
    step_name: str,
    usage: Mapping[str, Any] | None = None,
    input_tokens: Any = None,
    output_tokens: Any = None,
    latency_ms: Any = 0,
) -> RunStep:
    """Build a ``RunStep`` from the same payload that will be persisted.

    Pass either the raw LLM ``usage`` dict (normalized here) or explicit
    token counts (coerced here); explicit counts win when both are given.
    Cost is always :func:`estimate_cost` of the final counts, so the row is
    self-consistent by construction. The caller adds + commits in its own
    transaction — this helper performs no I/O.
    """
    if input_tokens is None or output_tokens is None:
        norm_in, norm_out = normalize_usage(usage)
        if input_tokens is None:
            input_tokens = norm_in
        if output_tokens is None:
            output_tokens = norm_out
    final_in = _coerce_tokens(input_tokens)
    final_out = _coerce_tokens(output_tokens)
    return RunStep(
        run_id=run_id,
        step_name=step_name,
        input_tokens=final_in,
        output_tokens=final_out,
        latency_ms=_coerce_tokens(latency_ms),
        cost_estimate=estimate_cost(final_in, final_out),
    )

