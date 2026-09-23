"""add_plan_and_waiting_input_to_agent_sessions

Revision ID: b1c2d3e4f5a6
Revises: f6a7b8c9d0e1
Create Date: 2026-09-23 20:00:00.000000

Adds plan and waiting_input columns to agent_sessions table for
Interactive Planning & Clarification UI (Phase 6).
- plan: JSONB list of multi-step tasks tracking progress.
- waiting_input: JSONB storing active question and options when awaiting user input.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column(
            "plan",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "agent_sessions",
        sa.Column(
            "waiting_input",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "waiting_input")
    op.drop_column("agent_sessions", "plan")
