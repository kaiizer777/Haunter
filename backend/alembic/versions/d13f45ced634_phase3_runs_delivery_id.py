"""phase3_runs_delivery_id

Revision ID: d13f45ced634
Revises: b4e2f1a9c3d7
Create Date: 2026-08-27 23:51:08.322032

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd13f45ced634'
down_revision: Union[str, Sequence[str], None] = 'b4e2f1a9c3d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('runs', 'github_run_id',
                    existing_type=sa.INTEGER(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)
    op.add_column('runs', sa.Column('github_delivery_id', sa.String(length=255), nullable=True))
    op.create_index(op.f('ix_runs_github_delivery_id'), 'runs', ['github_delivery_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_runs_github_delivery_id'), table_name='runs')
    op.drop_column('runs', 'github_delivery_id')
    op.alter_column('runs', 'github_run_id',
                    existing_type=sa.BigInteger(),
                    type_=sa.INTEGER(),
                    existing_nullable=False)

