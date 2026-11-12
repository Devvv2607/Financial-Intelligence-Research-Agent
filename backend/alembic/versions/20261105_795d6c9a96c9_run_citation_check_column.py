"""run citation check column

Revision ID: 795d6c9a96c9
Revises: d8c19346191f
Create Date: 2026-11-05 12:51:22.199000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "795d6c9a96c9"
down_revision: str | None = "d8c19346191f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("citation_check", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "citation_check")
