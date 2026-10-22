"""Regression tests for the hermetic audit FakeStore's ORDER BY handling.

SQLAlchemy 2.x represents ``ORDER BY created_at DESC`` as a ``UnaryExpression``
whose ``modifier`` is ``sqlalchemy.sql.operators.desc_op`` (a function), not the
string ``"desc"``. An equality check against the string silently downgraded
every DESC clause to ASC, so these tests pin the emitted SQL semantics for the
shapes the audit pipeline relies on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select

from app.models import Attempt, WebhookDelivery

# Fixtures only — importing registers them for this module.
from tests.fake_audit_db import (  # noqa: F401
    FakeAsyncSession,
    FakeStore,
    UnsupportedStatementError,
    audit_store,
)

pytestmark = pytest.mark.fast

_JAN_1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_JAN_2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
_JAN_3 = datetime(2026, 1, 3, tzinfo=timezone.utc)


def _delivery(
    *,
    row_id: uuid.UUID,
    created_at: datetime,
    status: str,
) -> WebhookDelivery:
    return WebhookDelivery(
        id=row_id,
        event="issue_comment",
        delivery_id="delivery-1",
        status=status,
        created_at=created_at,
    )


def _seed(session: FakeAsyncSession) -> None:
    session.add(_delivery(row_id=uuid.UUID(int=1), created_at=_JAN_1, status="zeta"))
    session.add(_delivery(row_id=uuid.UUID(int=2), created_at=_JAN_2, status="alpha"))
    session.add(_delivery(row_id=uuid.UUID(int=3), created_at=_JAN_3, status="mu"))


async def _ordered_ids(
    session: FakeAsyncSession, *ordering: Any
) -> list[uuid.UUID]:
    statement = select(WebhookDelivery).order_by(*ordering)
    rows = (await session.execute(statement)).scalars().all()
    return [row.id for row in rows]


@pytest.mark.asyncio
async def test_order_by_created_at_desc_returns_newest_first(
    audit_store: FakeStore,
) -> None:
    """``created_at.desc()`` is newest-to-oldest, as PostgreSQL sorts it."""
    session = FakeAsyncSession(audit_store)
    _seed(session)

    assert await _ordered_ids(session, WebhookDelivery.created_at.desc()) == [
        uuid.UUID(int=3),
        uuid.UUID(int=2),
        uuid.UUID(int=1),
    ]


@pytest.mark.asyncio
async def test_order_by_created_at_asc_returns_oldest_first(
    audit_store: FakeStore,
) -> None:
    """``created_at.asc()`` stays oldest-to-newest."""
    session = FakeAsyncSession(audit_store)
    _seed(session)

    assert await _ordered_ids(session, WebhookDelivery.created_at.asc()) == [
        uuid.UUID(int=1),
        uuid.UUID(int=2),
        uuid.UUID(int=3),
    ]


@pytest.mark.asyncio
async def test_order_by_plain_column_is_ascending(audit_store: FakeStore) -> None:
    """A bare column (no UnaryExpression) sorts ascending."""
    session = FakeAsyncSession(audit_store)
    _seed(session)

    assert await _ordered_ids(session, WebhookDelivery.status) == [
        uuid.UUID(int=2),
        uuid.UUID(int=3),
        uuid.UUID(int=1),
    ]


@pytest.mark.asyncio
async def test_order_by_created_at_desc_then_id_desc(
    audit_store: FakeStore,
) -> None:
    """Multi-column DESC: created_at groups first, id breaks ties descending.

    The store applies order clauses back-to-front with a stable sort, so the
    rightmost clause (``id``) must be the tie-breaker inside equal timestamps.
    """
    session = FakeAsyncSession(audit_store)
    session.add(
        _delivery(row_id=uuid.UUID(int=1), created_at=_JAN_1, status="first")
    )
    session.add(
        _delivery(row_id=uuid.UUID(int=4), created_at=_JAN_1, status="fourth")
    )
    session.add(
        _delivery(row_id=uuid.UUID(int=2), created_at=_JAN_2, status="second")
    )
    session.add(
        _delivery(row_id=uuid.UUID(int=3), created_at=_JAN_2, status="third")
    )

    assert await _ordered_ids(
        session,
        WebhookDelivery.created_at.desc(),
        WebhookDelivery.id.desc(),
    ) == [
        uuid.UUID(int=3),
        uuid.UUID(int=2),
        uuid.UUID(int=4),
        uuid.UUID(int=1),
    ]


@pytest.mark.asyncio
async def test_order_by_created_at_desc_then_id_asc(
    audit_store: FakeStore,
) -> None:
    """Mixed direction: created_at groups newest-first, id asc inside each group.

    The rightmost clause (``id``) breaks ties inside equal timestamps, so the
    DESC and ASC clauses must not be applied with one shared direction.
    """
    session = FakeAsyncSession(audit_store)
    session.add(
        _delivery(row_id=uuid.UUID(int=1), created_at=_JAN_1, status="first")
    )
    session.add(
        _delivery(row_id=uuid.UUID(int=4), created_at=_JAN_1, status="fourth")
    )
    session.add(
        _delivery(row_id=uuid.UUID(int=2), created_at=_JAN_2, status="second")
    )
    session.add(
        _delivery(row_id=uuid.UUID(int=3), created_at=_JAN_2, status="third")
    )

    assert await _ordered_ids(
        session,
        WebhookDelivery.created_at.desc(),
        WebhookDelivery.id.asc(),
    ) == [
        uuid.UUID(int=2),
        uuid.UUID(int=3),
        uuid.UUID(int=1),
        uuid.UUID(int=4),
    ]


_RUN_ID = uuid.UUID(int=99)


def _attempt(
    *,
    row_id: uuid.UUID,
    attempt_number: int,
    status: str | None,
    failure_reason: str | None = None,
) -> Attempt:
    """Build an attempt row; the fake store does not enforce foreign keys."""
    return Attempt(
        id=row_id,
        run_id=_RUN_ID,
        attempt_number=attempt_number,
        patch_text="",
        verification_status=status,
        failure_reason=failure_reason,
    )


async def _ordered_attempt_numbers(
    session: FakeAsyncSession, *ordering: Any
) -> list[int]:
    statement = select(Attempt).order_by(*ordering)
    rows = (await session.execute(statement)).scalars().all()
    return [row.attempt_number for row in rows]


@pytest.mark.asyncio
async def test_order_by_ascending_places_nulls_last(
    audit_store: FakeStore,
) -> None:
    """PostgreSQL sorts NULL above every value, so ASC ends with NULL rows."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.asc()
    ) == [3, 2, 1]


@pytest.mark.asyncio
async def test_order_by_descending_places_nulls_first(
    audit_store: FakeStore,
) -> None:
    """Reversing an ascending sort of "NULL largest" puts NULL rows first."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.desc()
    ) == [1, 2, 3]


@pytest.mark.asyncio
async def test_order_by_bare_nullable_column_places_nulls_last(
    audit_store: FakeStore,
) -> None:
    """A bare column is implicit ASC, so it inherits NULLS LAST."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status
    ) == [3, 2, 1]


@pytest.mark.asyncio
async def test_null_placement_applies_per_clause_in_multi_column_order(
    audit_store: FakeStore,
) -> None:
    """Each clause keeps its own NULL placement; later clauses break ties."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))
    session.add(_attempt(row_id=uuid.UUID(int=4), attempt_number=4, status="pass"))

    assert await _ordered_attempt_numbers(
        session,
        Attempt.verification_status.desc(),
        Attempt.attempt_number.asc(),
    ) == [1, 2, 4, 3]


@pytest.mark.asyncio
async def test_order_by_bare_nulls_first_places_nulls_first(
    audit_store: FakeStore,
) -> None:
    """A bare ``NULLS FIRST`` is implicit ASC with NULLs ahead of values."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.nulls_first()
    ) == [1, 3, 2]


@pytest.mark.asyncio
async def test_order_by_asc_nulls_first_places_nulls_first(
    audit_store: FakeStore,
) -> None:
    """``ASC NULLS FIRST`` overrides PostgreSQL's ASC default of NULLs last."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.asc().nulls_first()
    ) == [1, 3, 2]


@pytest.mark.asyncio
async def test_order_by_desc_nulls_last_places_nulls_last(
    audit_store: FakeStore,
) -> None:
    """``DESC NULLS LAST`` overrides PostgreSQL's DESC default of NULLs first."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.desc().nulls_last()
    ) == [2, 3, 1]


@pytest.mark.asyncio
async def test_explicit_restatements_match_direction_defaults(
    audit_store: FakeStore,
) -> None:
    """Agreeing modifiers restate the PostgreSQL direction defaults."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.desc().nulls_first()
    ) == [1, 2, 3]
    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.asc().nulls_last()
    ) == [3, 2, 1]


@pytest.mark.asyncio
async def test_order_by_nulls_last_before_desc_places_nulls_last(
    audit_store: FakeStore,
) -> None:
    """Modifier order is irrelevant: outer DESC over inner NULLS LAST."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.nulls_last().desc()
    ) == [2, 3, 1]


@pytest.mark.asyncio
async def test_explicit_null_modifiers_override_direction_defaults(
    audit_store: FakeStore,
) -> None:
    """Explicit modifiers flip both direction defaults to their opposite."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(_attempt(row_id=uuid.UUID(int=2), attempt_number=2, status="pass"))
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))

    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.nulls_first().asc()
    ) == [1, 3, 2]
    assert await _ordered_attempt_numbers(
        session, Attempt.verification_status.desc().nulls_last()
    ) == [2, 3, 1]


@pytest.mark.asyncio
async def test_multi_column_order_honours_explicit_nulls_modifier(
    audit_store: FakeStore,
) -> None:
    """A clause's explicit NULLS LAST must not leak into later clauses.

    The tiebreaker sorts the nullable failure_reason DESC, whose PostgreSQL
    default is NULLS FIRST, so a leak would flip the tied pair to [2, 4].
    """
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))
    session.add(
        _attempt(
            row_id=uuid.UUID(int=2),
            attempt_number=2,
            status="pass",
            failure_reason="b",
        )
    )
    session.add(_attempt(row_id=uuid.UUID(int=3), attempt_number=3, status="fail"))
    session.add(_attempt(row_id=uuid.UUID(int=4), attempt_number=4, status="pass"))

    assert await _ordered_attempt_numbers(
        session,
        Attempt.verification_status.desc().nulls_last(),
        Attempt.failure_reason.desc(),
    ) == [4, 2, 3, 1]


@pytest.mark.asyncio
async def test_unmodelled_order_by_modifier_fails_loud(
    audit_store: FakeStore,
) -> None:
    """A non-ordering modifier must raise, never silently default to ASC."""
    session = FakeAsyncSession(audit_store)
    session.add(_attempt(row_id=uuid.UUID(int=1), attempt_number=1, status=None))

    with pytest.raises(UnsupportedStatementError, match="ORDER BY modifier"):
        await session.execute(
            select(Attempt).order_by(Attempt.verification_status.distinct())
        )
