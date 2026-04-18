"""partial indexes on status columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-18

Status filters power both the new GET /sweeps/{id}/status endpoint and the
GET /sweeps?status= listing endpoint. Without these indexes both queries
sequential-scan the relevant table, which becomes painful at 50k+ task rows.

We use composite indexes (parent_id, status) instead of bare status indexes
because every status query is per-sweep / per-chunk; the leading parent_id
gives us O(log n) lookup before the status filter even kicks in. For
task_variants we additionally add a partial index on status='running' so the
"is anything still running?" check is sub-millisecond regardless of total row
count.

The CONCURRENTLY keyword is intentionally NOT used — Alembic wraps DDL in a
transaction by default, and CONCURRENTLY can't run inside one. For the size
ranges we operate at (<10M rows) a brief lock during the index build is fine.
If the table grows materially larger, run the indexes manually outside Alembic.
"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_chunks_sweep_id_status", "chunks", ["sweep_id", "status"])
    op.create_index("ix_jobs_chunk_id_status", "jobs", ["chunk_id", "status"])
    op.create_index("ix_task_variants_job_id_status", "task_variants", ["job_id", "status"])
    # Partial index: rows in 'running' state are always a tiny subset, so this
    # makes "any task still running for sweep X?" sub-millisecond. Postgres-only
    # syntax; tests run on sqlite via create_all() and don't see this migration.
    op.create_index(
        "ix_task_variants_running",
        "task_variants",
        ["job_id"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("ix_sweeps_status", "sweeps", ["status"])


def downgrade() -> None:
    op.drop_index("ix_sweeps_status", table_name="sweeps")
    op.drop_index("ix_task_variants_running", table_name="task_variants")
    op.drop_index("ix_task_variants_job_id_status", table_name="task_variants")
    op.drop_index("ix_jobs_chunk_id_status", table_name="jobs")
    op.drop_index("ix_chunks_sweep_id_status", table_name="chunks")
