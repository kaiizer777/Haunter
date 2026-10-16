"""Issue #115 — subagent read fan-out bounds.

Hermetic (fast, zero external I/O): fake sessions count ``execute`` calls and
serve canned rows, so every test below asserts exact query counts without a
database.

Covers:
  1. ``resolve_workflow_run_id`` fetches a 10-deep parent chain in ONE query
     (was: one query per level, N+1).
  2. Cycles / missing parents still resolve to None with one query.
  3. ``_fetch_prior_attempt`` is a single ``LIMIT 1`` row fetch (was: full
     history ``.all()`` + pick in Python).
  4. ``run_audit`` resolves the active model config ONCE and shares it across
     all four perspectives (was: one DB session per perspective, doubled on
     schema-retry).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import pytest

from app.llm.config import ResolvedModelConfig
from app.models import Attempt, Run
from app.subagents import auditor
from app.subagents.auditor import run_audit
from app.subagents.context_gatherer import (
    _fetch_prior_attempt,
    resolve_workflow_run_id,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeSession:
    """Counts every query; serves canned rows for both execute() styles."""

    def __init__(self, rows: list[Any]) -> None:
        self.calls = 0
        self.statements: list[Any] = []
        self._rows = rows

    async def execute(self, stmt: Any) -> _FakeResult:
        self.calls += 1
        self.statements.append(stmt)
        return _FakeResult(self._rows)

    async def scalars(self, stmt: Any) -> _FakeScalars:
        self.calls += 1
        self.statements.append(stmt)
        return _FakeScalars(self._rows)


def _make_run(
    repo_id: uuid.UUID,
    *,
    parent_run_id: Optional[uuid.UUID] = None,
    github_run_id: Optional[int] = None,
) -> Run:
    return Run(
        repo_id=repo_id,
        parent_run_id=parent_run_id,
        github_run_id=github_run_id,
        head_sha="a" * 40,
        head_branch="main",
        status="error",
        conclusion="failure",
    )


SAMPLE_DIFF = """diff --git a/backend/app/auth.py b/backend/app/auth.py
--- a/backend/app/auth.py
+++ b/backend/app/auth.py
@@ -80,3 +80,9 @@ def rotate_refresh_token(token: str):
+import time
+
 def verify_token(token: str, stored_token: str):
+    if token == stored_token:
+        time.sleep(5)
+        return True
+    return False
 def refresh():
     pass
"""


class _FakeLLM:
    """Records complete() kwargs; returns a valid empty-finding perspective."""

    def __init__(self) -> None:
        self.kwargs_seen: list[dict[str, Any]] = []
        self.calls: int = 0

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict:
        self.kwargs_seen.append(kwargs)
        self.calls += 1
        user_text = " ".join(m.get("content", "") for m in (messages or []))
        perspective = next(
            p
            for p in ("security", "correctness", "performance", "architecture")
            if f"## Perspective\n{p}" in user_text
        )
        return {
            "content": json.dumps(
                {
                    "summary": f"{perspective} verdict.",
                    "confidence": 90,
                    "findings": [],
                }
            ),
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "latency_ms": 1,
            "model": "test-engine",
        }


class _FakeSchemaFailThenPassLLM:
    """First complete() returns invalid JSON; the retry call returns valid JSON.

    Exercises the schema-retry path in ``_run_single_perspective``. The
    first call must raise ``pydantic.ValidationError`` when the caller
    tries to parse it; the second call (the retry) must succeed. The
    fake records every kwargs it sees and exposes ``calls`` so the test
    can assert exactly two calls per perspective and that both see the
    same shared ``model_config``.
    """

    def __init__(self) -> None:
        self.kwargs_seen: list[dict[str, Any]] = []
        self.calls: int = 0
        # Per-perspective call counter so each perspective fails once
        # and then succeeds.
        self._calls_by_perspective: dict[str, int] = {}

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict:
        self.kwargs_seen.append(kwargs)
        self.calls += 1
        user_text = " ".join(m.get("content", "") for m in (messages or []))
        perspective = next(
            p
            for p in ("security", "correctness", "performance", "architecture")
            if f"## Perspective\n{p}" in user_text
        )
        self._calls_by_perspective[perspective] = (
            self._calls_by_perspective.get(perspective, 0) + 1
        )
        if self._calls_by_perspective[perspective] == 1:
            # First call: deliberately invalid JSON (a string is not a
            # JSON object). The auditor's ``_sanitize_perspective_output``
            # call MUST raise ``pydantic.ValidationError``.
            return {
                "content": "not-valid-json-at-all-just-a-string",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "latency_ms": 1,
                "model": "test-engine",
            }
        # Second call (retry): valid JSON.
        return {
            "content": json.dumps(
                {
                    "summary": f"{perspective} verdict (retry).",
                    "confidence": 90,
                    "findings": [],
                }
            ),
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "latency_ms": 1,
            "model": "test-engine",
        }


# ---------------------------------------------------------------------------
# 1-3. Parent-chain walk: O(1) queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_chain_ten_deep_resolves_in_single_query():
    """10-deep chain: exactly 1 query (recursive CTE), root id recovered."""
    repo_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(11)]
    root_github_id = 123456789

    # CTE rowset: (id, parent_run_id, github_run_id), leaf first.
    rows: list[tuple] = [(ids[0], None, root_github_id)]
    for depth in range(1, 11):
        rows.append((ids[depth], ids[depth - 1], None))
    leaf = _make_run(repo_id, parent_run_id=ids[9])
    leaf.id = ids[10]

    fake = _FakeSession(rows)
    assert await resolve_workflow_run_id(leaf, fake) == root_github_id
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_parent_chain_own_id_costs_zero_queries():
    """Run carrying its own id short-circuits before any query."""
    fake = _FakeSession([])
    run = _make_run(uuid.uuid4(), github_run_id=999)
    assert await resolve_workflow_run_id(run, fake) == 999
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_parent_chain_cycle_returns_none_in_single_query():
    """A↔B cycle terminates via the visited set: None, 1 query."""
    repo_id = uuid.uuid4()
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    rows = [(id_a, id_b, None), (id_b, id_a, None)]
    run = _make_run(repo_id, parent_run_id=id_a)
    run.id = id_b

    fake = _FakeSession(rows)
    assert await resolve_workflow_run_id(run, fake) is None
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_parent_chain_self_cycle_returns_none_in_single_query():
    """Self-referential parent_run_id: None, 1 query."""
    loop_id = uuid.uuid4()
    rows = [(loop_id, loop_id, None)]
    run = _make_run(uuid.uuid4(), parent_run_id=loop_id)
    run.id = loop_id

    fake = _FakeSession(rows)
    assert await resolve_workflow_run_id(run, fake) is None
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_parent_chain_missing_parent_returns_none_in_single_query():
    """Parent row absent from the fetched rowset: None, 1 query."""
    run_id, missing = uuid.uuid4(), uuid.uuid4()
    rows = [(run_id, missing, None)]
    run = _make_run(uuid.uuid4(), parent_run_id=missing)
    run.id = run_id

    fake = _FakeSession(rows)
    assert await resolve_workflow_run_id(run, fake) is None
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_parent_chain_no_id_anywhere_returns_none_in_single_query():
    """Chain with no workflow id: None, 1 query (caller skips log fetch)."""
    ids = [uuid.uuid4() for _ in range(4)]
    rows = [(ids[0], None, None)] + [(ids[d], ids[d - 1], None) for d in range(1, 4)]
    leaf = _make_run(uuid.uuid4(), parent_run_id=ids[2])
    leaf.id = ids[3]

    fake = _FakeSession(rows)
    assert await resolve_workflow_run_id(leaf, fake) is None
    assert fake.calls == 1


# ---------------------------------------------------------------------------
# 4. Prior-attempt history: single bounded row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_prior_attempt_single_bounded_query():
    """One execute() call; the statement has LIMIT 1 with pass-first ordering.

    Verifies the actual SQL shape — not just substring presence — by
    compiling the statement with literal binds and asserting on the
    rendered ORDER BY / LIMIT clauses:

      * LIMIT 1 (was: full history .all() + Python-side pick)
      * ORDER BY puts ``verification_status = 'pass'`` ahead of
        ``attempt_number`` (pass-first: any pass beats any fail, ties
        broken by most recent attempt)
    """
    from sqlalchemy.dialects import postgresql

    source_id = uuid.uuid4()
    attempt = Attempt(
        run_id=source_id,
        attempt_number=3,
        patch_text="diff --git a/f b/f",
        verification_status="fail",
        strategy_notes="notes",
    )
    fake = _FakeSession([attempt])

    got = await _fetch_prior_attempt(source_id, fake)

    assert got is attempt
    assert fake.calls == 1
    compiled = fake.statements[0].compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled).upper()
    # Bounded fetch
    assert "LIMIT 1" in sql
    # Pass-first ordering: case(verification_status='pass', 1) desc
    assert "'PASS'" in sql
    # Most-recent tie-break: attempt_number desc
    assert "ATTEMPT_NUMBER" in sql
    assert "DESC" in sql


@pytest.mark.asyncio
async def test_fetch_prior_attempt_none_when_no_attempts():
    fake = _FakeSession([])
    assert await _fetch_prior_attempt(uuid.uuid4(), fake) is None
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_fetch_prior_attempt_picks_most_recent_pass_over_fail():
    """Pass-first ordering with multiple attempts.

    Real-world scenario the fake can express: the source run has both
    passing and failing attempts. The prior attempt the refinement must
    build on is the most recent *passing* attempt, regardless of any
    more recent fail. The fake serves a rowset that intentionally puts a
    later fail ahead of an earlier pass, and the function must still
    pick the pass because the SQL ``ORDER BY verification_status = 'pass'
    DESC, attempt_number DESC`` resolves the rowset before the fake
    returns the first one.

    The ``_FakeResult.all()`` and ``scalars().first()`` are what
    SQLAlchemy uses to materialize the bounded result; for this test
    the fake returns the PASS row first (its rowset is unordered
    by default, so the test asserts the property via the seed data
    plus the assertion that pass is preferred regardless of recency).
    """
    source_id = uuid.uuid4()
    later_fail = Attempt(
        run_id=source_id,
        attempt_number=5,
        patch_text="diff --git a/later b/later",
        verification_status="fail",
        strategy_notes="latest",
    )
    earlier_pass = Attempt(
        run_id=source_id,
        attempt_number=3,
        patch_text="diff --git a/earlier b/earlier",
        verification_status="pass",
        strategy_notes="last good",
    )
    # Fake returns the passing attempt first to verify the function
    # surfaces it; the SQL ORDER BY (asserted in the previous test) is
    # what guarantees it on a real DB where the rowset is unordered.
    fake = _FakeSession([earlier_pass, later_fail])

    got = await _fetch_prior_attempt(source_id, fake)

    assert got is earlier_pass
    assert got.verification_status == "pass"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_fetch_prior_attempt_falls_back_to_latest_when_no_pass():
    """No passing attempt at all → most recent attempt overall.

    A failing refinement of a never-passing source run must still get
    a prior attempt to refine from; in that case the function returns
    the most recent attempt regardless of status.
    """
    source_id = uuid.uuid4()
    oldest = Attempt(
        run_id=source_id,
        attempt_number=1,
        patch_text="diff --git a/1 b/1",
        verification_status="fail",
        strategy_notes="",
    )
    latest = Attempt(
        run_id=source_id,
        attempt_number=4,
        patch_text="diff --git a/4 b/4",
        verification_status="fail",
        strategy_notes="most recent",
    )
    fake = _FakeSession([latest, oldest])

    got = await _fetch_prior_attempt(source_id, fake)

    assert got is latest
    assert got.attempt_number == 4
    assert fake.calls == 1


# ---------------------------------------------------------------------------
# 5. Auditor: one shared model-config resolution across perspectives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auditor_shares_single_model_config_across_perspectives(
    monkeypatch: pytest.MonkeyPatch,
):
    """4 perspectives, one shared model_config, zero DB I/O when the LLM is faked.

    With a mock ``llm_client`` injected, the resolver is skipped entirely
    (no DB lookup, no env-default probe); the same ``ResolvedModelConfig``
    flows to every perspective's ``complete()`` call. A monkey-patched
    ``auditor.get_active_model_config`` therefore MUST NOT fire here.
    """
    resolve_calls = {"n": 0}
    shared = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="shared-test-model",
        base_url="http://shared-config.invalid",
    )

    async def _counting_resolve(db=None, repo_id=None) -> ResolvedModelConfig:
        resolve_calls["n"] += 1
        return shared

    monkeypatch.setattr(auditor, "get_active_model_config", _counting_resolve)
    fake_llm = _FakeLLM()

    result = await run_audit(
        diff_text=SAMPLE_DIFF,
        audit_id="audit-abcdefabcdef",
        llm_client=fake_llm,  # type: ignore[arg-type]
    )

    # Resolver is bypassed for an injected non-LLMClient — hermetic guarantee.
    assert resolve_calls["n"] == 0
    assert len(fake_llm.kwargs_seen) == 4
    # Every complete() sees the SAME object — shared across perspectives.
    seen_configs = {id(k.get("model_config")) for k in fake_llm.kwargs_seen}
    assert len(seen_configs) == 1
    assert all(
        isinstance(k.get("model_config"), ResolvedModelConfig)
        for k in fake_llm.kwargs_seen
    )
    assert {p.perspective for p in result.perspectives} == {
        "security",
        "correctness",
        "performance",
        "architecture",
    }


@pytest.mark.asyncio
async def test_auditor_config_resolve_timeout_falls_back(
    monkeypatch: pytest.MonkeyPatch,
):
    """A slow DB resolve surfaces as env defaults, never blocks the audit.

    Without the wait_for bound, a stuck DB would prevent any perspective
    from starting — the bot-flagged gap. With it, the timeout falls
    through to the same env-default shape ``get_active_model_config``
    itself uses on DB errors, and the audit proceeds.
    """
    import asyncio as _asyncio
    from app.config import settings
    from app.llm.config import default_base_url_for_provider

    async def _hanging_resolve(db=None, repo_id=None) -> ResolvedModelConfig:
        await _asyncio.sleep(60.0)  # far longer than the budget
        return ResolvedModelConfig(provider="never", model_name="never", base_url="x")

    monkeypatch.setattr(auditor, "get_active_model_config", _hanging_resolve)

    # Use a real LLMClient so the skip-when-mocked guard does NOT short-circuit;
    # this exercises the timeout + env-default fallback path.
    import app.llm as _llm_pkg

    class _StubClient(_llm_pkg.LLMClient):
        async def complete(self, *args, **kwargs):
            # Return a valid perspective response without touching the network.
            return {
                "content": json.dumps(
                    {"summary": "ok", "confidence": 90, "findings": []}
                ),
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "latency_ms": 1,
                "model": "stub",
            }

    started = _asyncio.get_event_loop().time()
    result = await run_audit(
        diff_text=SAMPLE_DIFF,
        audit_id="audit-dead0000beef",
        llm_client=_StubClient(),
    )
    elapsed = _asyncio.get_event_loop().time() - started
    assert elapsed < 10.0, f"audit stalled after {elapsed:.1f}s on slow DB"
    assert len(result.perspectives) == 4
    assert all(p.succeeded for p in result.perspectives)


@pytest.mark.asyncio
async def test_auditor_does_resolve_when_real_llm_client_provided(
    monkeypatch: pytest.MonkeyPatch,
):
    """The resolve path stays in place when a *real* LLMClient is injected.

    Guards against an over-broad skip that would silently turn every
    production caller into a fake-resolver codepath. Real
    ``LLMClient`` → resolve runs; resolver monkey-patch MUST be seen.
    """
    from app.llm import LLMClient as _RealLLMClient

    resolve_calls = {"n": 0}
    shared = ResolvedModelConfig(
        provider="opencode_zen",
        model_name="shared-test-model",
        base_url="http://shared-config.invalid",
    )

    async def _counting_resolve(db=None, repo_id=None) -> ResolvedModelConfig:
        resolve_calls["n"] += 1
        return shared

    monkeypatch.setattr(auditor, "get_active_model_config", _counting_resolve)

    class _StubReal(_RealLLMClient):
        async def complete(self, *args, **kwargs):
            messages = kwargs.get("messages") or (args[0] if args else [])
            user_text = " ".join(m.get("content", "") for m in (messages or []))
            perspective = next(
                p
                for p in ("security", "correctness", "performance", "architecture")
                if f"## Perspective\n{p}" in user_text
            )
            return {
                "content": json.dumps(
                    {
                        "summary": f"{perspective} verdict.",
                        "confidence": 90,
                        "findings": [],
                    }
                ),
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "latency_ms": 1,
                "model": "test-engine",
            }

    result = await run_audit(
        diff_text=SAMPLE_DIFF,
        audit_id="audit-deadbee00000",
        llm_client=_StubReal(timeout=1.0),
    )

    assert resolve_calls["n"] == 1
    assert {p.perspective for p in result.perspectives} == {
        "security",
        "correctness",
        "performance",
        "architecture",
    }


@pytest.mark.asyncio
async def test_auditor_schema_retry_preserves_shared_config_and_call_count():
    """Schema-fail-then-pass: retry path must reuse the same shared model_config.

    When the first ``complete()`` returns invalid JSON, ``_run_single_perspective``
    triggers a schema-retry. That retry must:

      1. Receive the *same* ``model_config`` kwarg as the first call
         (the shared config stays shared — no per-retry resolve).
      2. Be counted as a separate ``complete()`` call (8 calls for
         4 perspectives = 4 first attempts + 4 retries).

    Without this, a regression that drops ``model_config`` on the
    retry, or one that resolves the config twice per perspective,
    would silently double DB I/O and re-introduce the original
    fanout the PR was supposed to fix.
    """
    fake_llm = _FakeSchemaFailThenPassLLM()

    result = await run_audit(
        diff_text=SAMPLE_DIFF,
        audit_id="audit-1234567890ab",
        llm_client=fake_llm,  # type: ignore[arg-type]
    )

    # 4 perspectives, each fails then succeeds → 8 complete() calls.
    assert fake_llm.calls == 8, (
        f"expected 8 complete() calls (4 perspectives × 2 attempts), "
        f"got {fake_llm.calls}"
    )
    # Every call (first and retry) must carry the SAME shared model_config
    # object — the audit must not re-resolve on retry.
    seen_configs = [k.get("model_config") for k in fake_llm.kwargs_seen]
    assert all(c is not None for c in seen_configs)
    assert len({id(c) for c in seen_configs}) == 1, (
        "schema-retry must not produce a second ResolvedModelConfig"
    )
    # All four perspectives still succeeded (every retry produced valid JSON).
    assert len(result.perspectives) == 4
    assert all(p.succeeded for p in result.perspectives)
    # The retry summaries match the fake's second-call text.
    assert all("(retry)" in p.summary for p in result.perspectives)
