"""session stream events replay for multi-env lambda
 
Revision ID: b8c9d0e1f2a3
Revises: a5c6d7e8f9b0
Create Date: 2026-10-15 00:00:00.000000
 
Issue #96: DB-backed shared SSE replay for multi-env AWS Lambda environments.
- Adds active_turn_id column to agent_sessions.
- Creates session_stream_events table with composite index for fast resumption.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a5c6d7e8f9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column("active_turn_id", sa.String(length=36), nullable=True),
    )
    op.create_table(
        "session_stream_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("chunk", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            name="fk_session_stream_events_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_session_stream_events"),
    )
    op.create_index(
        "ix_session_stream_events_lookup",
        "session_stream_events",
        ["session_id", "turn_id", "event_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_session_stream_events_lookup", table_name="session_stream_events")
    op.drop_table("session_stream_events")
    op.drop_column("agent_sessions", "active_turn_id")
