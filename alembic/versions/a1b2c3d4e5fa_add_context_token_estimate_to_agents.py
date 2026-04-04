"""add context_token_estimate to agents

Revision ID: a1b2c3d4e5fa
Revises: a1b2c3d4e5f9
Create Date: 2026-02-21 23:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from letta.settings import settings

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5fa"
down_revision: Union[str, None] = "a1b2c3d4e5f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Skip this migration for SQLite
    if not settings.letta_pg_uri_no_default:
        return

    op.add_column("agents", sa.Column("context_token_estimate", sa.Integer(), nullable=True))


def downgrade() -> None:
    # Skip this migration for SQLite
    if not settings.letta_pg_uri_no_default:
        return

    op.drop_column("agents", "context_token_estimate")
