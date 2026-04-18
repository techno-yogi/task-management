"""create sweep graph tables

Revision ID: 0001
Revises:
Create Date: 2026-04-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sweeps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("total_chunks", sa.Integer(), nullable=False),
        sa.Column("total_jobs", sa.Integer(), nullable=False),
        sa.Column("total_tasks", sa.Integer(), nullable=False),
        sa.Column("finalized_by", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sweep_id", sa.Integer(), sa.ForeignKey("sweeps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("total_jobs", sa.Integer(), nullable=False),
        sa.Column("total_tasks", sa.Integer(), nullable=False),
        sa.Column("finalized_by", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chunks_sweep_id", "chunks", ["sweep_id"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chunk_id", sa.Integer(), sa.ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("shared_context_json", sa.Text(), nullable=False),
        sa.Column("finalized_by", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_jobs_chunk_id", "jobs", ["chunk_id"])
    op.create_table(
        "task_variants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("point_idx", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("expected_value", sa.Integer(), nullable=False),
        sa.Column("actual_value", sa.Integer(), nullable=True),
        sa.Column("validation_message", sa.String(length=200), nullable=True),
        sa.Column("processed_by", sa.String(length=200), nullable=True),
        sa.Column("celery_task_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_variants_job_id", "task_variants", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_task_variants_job_id", table_name="task_variants")
    op.drop_table("task_variants")
    op.drop_index("ix_jobs_chunk_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_chunks_sweep_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_table("sweeps")
