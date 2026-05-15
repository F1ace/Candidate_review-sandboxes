"""pgvector document chunk embeddings

Revision ID: 3b0d72e2a9f1
Revises: 1d9f3b6a2f4c
Create Date: 2026-04-27 13:35:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "3b0d72e2a9f1"
down_revision: Union[str, None] = "1d9f3b6a2f4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector")


def downgrade() -> None:
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding")
