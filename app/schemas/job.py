from __future__ import annotations

from pydantic import BaseModel, Field, ConfigDict


class TaskVariantCreate(BaseModel):
    point_idx: int
    name: str
    expected_value: int


class JobCreate(BaseModel):
    name: str
    shared_context: dict[str, int | str | float] = Field(default_factory=dict)
    tasks: list[TaskVariantCreate]


class ChunkCreate(BaseModel):
    ordinal: int
    jobs: list[JobCreate]


class SweepCreate(BaseModel):
    name: str
    chunks: list[ChunkCreate]


class TaskVariantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    point_idx: int
    name: str
    status: str
    expected_value: int
    actual_value: int | None
    validation_message: str | None
    processed_by: str | None
    celery_task_id: str | None


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    attempts: int
    finalized_by: str | None
    tasks: list[TaskVariantRead]


class ChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ordinal: int
    status: str
    total_jobs: int
    total_tasks: int
    finalized_by: str | None
    jobs: list[JobRead]


class SweepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    total_chunks: int
    total_jobs: int
    total_tasks: int
    finalized_by: str | None
    chunks: list[ChunkRead]


class SweepLaunchResponse(BaseModel):
    sweep_id: int
    status: str
    root_task_id: str | None


class SweepLaunchStatusResponse(BaseModel):
    sweep_id: int
    root_task_id: str
    ready: bool
    state: str
    successful: bool | None = None
    result: dict | None = None
