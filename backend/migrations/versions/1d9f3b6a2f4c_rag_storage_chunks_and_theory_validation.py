"""rag storage, chunks and theory validation

Revision ID: 1d9f3b6a2f4c
Revises: 7c17cf4264ee
Create Date: 2026-03-30 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1d9f3b6a2f4c"
down_revision: Union[str, None] = "7c17cf4264ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("content_type", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("storage_bucket", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("object_key", sa.String(length=512), nullable=True))
    op.add_column("documents", sa.Column("size_bytes", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("checksum_sha256", sa.String(length=64), nullable=True))
    op.add_column(
        "documents",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ready"),
    )
    op.add_column(
        "documents",
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.add_column("documents", sa.Column("ingested_at", sa.DateTime(), nullable=True))

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_length", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk"),
    )
    op.create_index(op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"], unique=False)

    op.create_table(
        "theory_fact_validations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("question_index", sa.Integer(), nullable=False),
        sa.Column("candidate_message_id", sa.Integer(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="completed"),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["candidate_message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_theory_fact_validations_candidate_message_id"),
        "theory_fact_validations",
        ["candidate_message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_theory_fact_validations_session_id"),
        "theory_fact_validations",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_theory_fact_validations_session_id"), table_name="theory_fact_validations")
    op.drop_index(op.f("ix_theory_fact_validations_candidate_message_id"), table_name="theory_fact_validations")
    op.drop_table("theory_fact_validations")

    op.drop_index(op.f("ix_document_chunks_document_id"), table_name="document_chunks")
    op.drop_table("document_chunks")

    op.drop_column("documents", "ingested_at")
    op.drop_column("documents", "created_at")
    op.drop_column("documents", "status")
    op.drop_column("documents", "checksum_sha256")
    op.drop_column("documents", "size_bytes")
    op.drop_column("documents", "object_key")
    op.drop_column("documents", "storage_bucket")
    op.drop_column("documents", "content_type")
