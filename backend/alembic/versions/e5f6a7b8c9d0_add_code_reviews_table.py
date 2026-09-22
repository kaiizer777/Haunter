"""add_code_reviews_table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-22 13:00:00.000000

Creates code_reviews table to support autonomous push-level code reviews
and actionable remediation (Feature 1).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "code_reviews",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repo_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("risk_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "findings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="completed"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        op.f("ix_code_reviews_repo_id"),
        "code_reviews",
        ["repo_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_reviews_commit_sha"),
        "code_reviews",
        ["commit_sha"],
        unique=False,
    )
    op.create_index(
        op.f("ix_code_reviews_pr_number"),
        "code_reviews",
        ["pr_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_code_reviews_pr_number"), table_name="code_reviews")
    op.drop_index(op.f("ix_code_reviews_commit_sha"), table_name="code_reviews")
    op.drop_index(op.f("ix_code_reviews_repo_id"), table_name="code_reviews")
    op.drop_table("code_reviews")
