"""read-only role for text-to-SQL

Revision ID: 3558ad0690e3
Revises: acf7fb2d13a7
Create Date: 2026-11-04 18:10:08.522022

`fira_readonly` is a NOLOGIN role with column-level SELECT grants matching the screener
allowlist (agent/tools/sql_guard.py) at the time of this migration. The app's database
user is granted membership so a text-to-SQL transaction can `SET LOCAL ROLE fira_readonly`:
from then on Postgres enforces the role's privileges, even if the login user is a superuser.
Column lists are frozen here on purpose; a test checks they still match the allowlist.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3558ad0690e3"
down_revision: str | None = "acf7fb2d13a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "fira_readonly"
GRANTS: dict[str, tuple[str, ...]] = {
    "companies": (
        "cik", "ticker", "name", "sic", "sic_description", "sic_sector", "llm_sector",
        "llm_sub_industry", "sector_conflict", "fiscal_year_end",
    ),
    "filings": ("accession_no", "cik", "form_type", "filing_date", "fiscal_year", "url"),
    "risk_factors": (
        "id", "cik", "fiscal_year", "heading", "category", "summary", "needs_review",
    ),
    "eight_k_events": ("accession_no", "cik", "filing_date", "items"),
    "financial_facts": ("cik", "metric", "fiscal_year", "value", "unit", "period_end"),
    "news_articles": ("id", "cik", "title", "source_name", "published_at", "url"),
}  # fmt: skip


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN;
            END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    for table, cols in GRANTS.items():
        # Identifiers come from the constant GRANTS above, never from input.
        op.execute(f"GRANT SELECT ({', '.join(cols)}) ON {table} TO {ROLE}")
    op.execute(f"GRANT {ROLE} TO CURRENT_USER")


def downgrade() -> None:
    op.execute(f"REVOKE {ROLE} FROM CURRENT_USER")
    for table in GRANTS:
        op.execute(f"REVOKE ALL ON {table} FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {ROLE}")
