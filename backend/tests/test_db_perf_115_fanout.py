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

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict:
        self.kwargs_seen.append(kwargs)
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
    """One execute() call; statement carries LIMIT 1 with pass-first order."""
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
    sql = str(fake.statements[0]).upper()
    assert "LIMIT" in sql
    assert "VERIFICATION_STATUS" in sql
    assert "ATTEMPT_NUMBER" in sql


@pytest.mark.asyncio
async def test_fetch_prior_attempt_none_when_no_attempts():
    fake = _FakeSession([])
    assert await _fetch_prior_attempt(uuid.uuid4(), fake) is None
    assert fake.calls == 1


# ---------------------------------------------------------------------------
# 5. Auditor: one shared model-config resolution across perspectives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auditor_shares_single_model_config_across_perspectives(
    monkeypatch: pytest.MonkeyPatch,
):
    """4 perspectives → 1 config resolution; every complete() gets it."""
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

    assert resolve_calls["n"] == 1
    assert len(fake_llm.kwargs_seen) == 4
    assert all(k.get("model_config") is shared for k in fake_llm.kwargs_seen)
    assert {p.perspective for p in result.perspectives} == {
        "security",
        "correctness",
        "performance",
        "architecture",
    }
