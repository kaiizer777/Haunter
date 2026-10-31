"""Tests for Issue #96: DB-backed shared SSE replay across multi-env AWS Lambda environments.

Session router/orchestrator/streamer modules deleted; session-dependent cases removed.
Remaining coverage: PostgreSQL locking semantics (FOR NO KEY UPDATE NOWAIT).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import Run


def test_lock_compiles_for_no_key_update() -> None:
    """PostgreSQL lock compiles to FOR NO KEY UPDATE NOWAIT (permitting child FK inserts)."""
    stmt = (
        select(Run)
        .where(Run.id == uuid.uuid4())
        .with_for_update(nowait=True, key_share=True)
    )
    compiled_sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "FOR NO KEY UPDATE NOWAIT" in compiled_sql
