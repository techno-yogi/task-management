from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Sweep(TimestampMixin, Base):
    __tablename__ = "sweeps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finalized_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    chunks: Mapped[list[Chunk]] = relationship(back_populates="sweep", cascade="all, delete-orphan")


class Chunk(TimestampMixin, Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sweep_id: Mapped[int] = mapped_column(ForeignKey("sweeps.id", ondelete="CASCADE"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    total_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finalized_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    sweep: Mapped[Sweep] = relationship(back_populates="chunks")
    jobs: Mapped[list[Job]] = relationship(back_populates="chunk", cascade="all, delete-orphan")


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shared_context_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    finalized_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    chunk: Mapped[Chunk] = relationship(back_populates="jobs")
    tasks: Mapped[list[TaskVariant]] = relationship(back_populates="job", cascade="all, delete-orphan")


class TaskVariant(TimestampMixin, Base):
    __tablename__ = "task_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    point_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    expected_value: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validation_message: Mapped[str | None] = mapped_column(String(200), nullable=True)
    processed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    job: Mapped[Job] = relationship(back_populates="tasks")
