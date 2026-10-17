"""add unique constraint to session_stream_events

Revision ID: e1f2a3b4c5d6
Revises: b8c9d0e1f2a3
Create Date: 2026-10-16 22:00:00.000000

Issue #112: Prevent duplicate SSE events on ambiguous commit retry.
- Adds uq_session_stream_events_session_turn_event unique constraint on (session_id, turn_id, event_id).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_session_stream_events_session_turn_event",
        "session_stream_events",
        ["session_id", "turn_id", "event_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_session_stream_events_session_turn_event",
        "session_stream_events",
        type_="unique",
    )
