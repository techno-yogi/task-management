from __future__ import annotations

import pytest

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from app.models import Chunk, Job, Sweep, TaskVariant
from app.services.job_service import (
    StaleTaskStateError,
    complete_task,
    count_statuses,
    start_task,
)


def test_create_and_get_sweep_graph(client, eager_celery, make_payload):
    response = client.post("/sweeps", json=make_payload(chunks=2, jobs_per_chunk=2, tasks_per_job=3))
    assert response.status_code == 201
    payload = response.json()

    assert payload["status"] == "pending"
    assert payload["total_chunks"] == 2
    assert payload["total_jobs"] == 4
    assert payload["total_tasks"] == 12
    assert len(payload["chunks"]) == 2
    assert payload["chunks"][0]["total_jobs"] == 2
    assert payload["chunks"][0]["total_tasks"] == 6
    assert len(payload["chunks"][0]["jobs"][0]["tasks"]) == 3

    fetched = client.get(f"/sweeps/{payload['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == payload["id"]


def test_full_sweep_execution_with_eager_celery(client, sync_session_factory, eager_celery, make_payload):
    sweep = client.post("/sweeps", json=make_payload(chunks=2, jobs_per_chunk=2, tasks_per_job=3)).json()

    launch_response = client.post(f"/sweeps/{sweep['id']}/launch")
    assert launch_response.status_code == 200
    assert launch_response.json()["status"] == "done"

    with sync_session_factory() as session:
        persisted_sweep = session.execute(select(Sweep).where(Sweep.id == sweep["id"])).scalar_one()
        assert persisted_sweep.status == "done"
        assert persisted_sweep.finalized_by is not None

        chunks = session.execute(select(Chunk).where(Chunk.sweep_id == sweep["id"])).scalars().all()
        assert len(chunks) == 2
        assert all(chunk.status == "done" for chunk in chunks)
        assert all(chunk.finalized_by is not None for chunk in chunks)

        jobs = session.execute(select(Job)).scalars().all()
        assert len(jobs) == 4
        assert all(job.status == "done" for job in jobs)
        assert all(job.attempts == 1 for job in jobs)
        assert all(job.finalized_by is not None for job in jobs)

        tasks = session.execute(select(TaskVariant)).scalars().all()
        assert len(tasks) == 12
        assert all(task.status == "done" for task in tasks)
        assert all(task.validation_message == "validated" for task in tasks)
        assert all(task.processed_by is not None for task in tasks)
        assert all(task.celery_task_id is not None for task in tasks)

        counts = count_statuses(session)
        assert counts == {
            "sweeps_done": 1,
            "chunks_done": 2,
            "jobs_done": 4,
            "tasks_done": 12,
        }




def test_sweep_status_endpoint_initial_counts_are_all_pending(client, eager_celery, make_payload):
    sweep = client.post(
        "/sweeps", json=make_payload(chunks=2, jobs_per_chunk=2, tasks_per_job=3)
    ).json()

    response = client.get(f"/sweeps/{sweep['id']}/status")
    assert response.status_code == 200
    body = response.json()

    assert body["id"] == sweep["id"]
    assert body["status"] == "pending"
    assert body["total_chunks"] == 2
    assert body["total_jobs"] == 4
    assert body["total_tasks"] == 12
    # Every level starts entirely pending; no rows in any other status bucket.
    assert body["chunks"] == {"pending": 2, "running": 0, "done": 0, "failed": 0}
    assert body["jobs"] == {"pending": 4, "running": 0, "done": 0, "failed": 0}
    assert body["tasks"] == {"pending": 12, "running": 0, "done": 0, "failed": 0}


def test_sweep_status_endpoint_after_eager_run_is_all_done(client, eager_celery, make_payload):
    sweep = client.post(
        "/sweeps", json=make_payload(chunks=2, jobs_per_chunk=2, tasks_per_job=3)
    ).json()

    launch = client.post(f"/sweeps/{sweep['id']}/launch")
    assert launch.status_code == 200

    body = client.get(f"/sweeps/{sweep['id']}/status").json()
    assert body["status"] == "done"
    assert body["finalized_by"] is not None
    assert body["chunks"]["done"] == 2 and body["chunks"]["pending"] == 0
    assert body["jobs"]["done"] == 4 and body["jobs"]["pending"] == 0
    assert body["tasks"]["done"] == 12 and body["tasks"]["pending"] == 0


def test_sweep_status_endpoint_returns_404_for_unknown_sweep(client):
    response = client.get("/sweeps/999999/status")
    assert response.status_code == 404


def _seed_one_task(client, make_payload) -> int:
    """Create a 1-chunk/1-job/1-task sweep and return the lone task id."""
    sweep = client.post(
        "/sweeps", json=make_payload(chunks=1, jobs_per_chunk=1, tasks_per_job=1)
    ).json()
    return sweep["chunks"][0]["jobs"][0]["tasks"][0]["id"]


def test_complete_task_is_atomic_on_running(client, sync_session_factory, make_payload):
    """Happy path: row was 'running' -> single UPDATE flips it to 'done', returns it."""
    task_id = _seed_one_task(client, make_payload)
    with sync_session_factory() as session:
        start_task(session, task_id)

    with sync_session_factory() as session:
        updated = complete_task(
            session,
            task_id=task_id,
            actual_value=42,
            expected_value=42,
            validation_message="validated",
            processed_by="test-worker",
            celery_task_id="ctid-1",
        )
        assert updated.status == "done"
        assert updated.actual_value == 42
        assert updated.processed_by == "test-worker"


def test_complete_task_raises_stale_when_row_not_running(client, sync_session_factory, make_payload):
    """If the row isn't 'running' (e.g. reset to pending mid-flight), raise so the
    Celery autoretry path can re-acquire and re-complete the task cleanly."""
    task_id = _seed_one_task(client, make_payload)

    with sync_session_factory() as session, pytest.raises(StaleTaskStateError):
        complete_task(
            session,
            task_id=task_id,
            actual_value=1,
            expected_value=1,
            validation_message="validated",
            processed_by="w",
            celery_task_id="c",
        )


def test_complete_task_double_call_only_one_winner(client, sync_session_factory, make_payload):
    """Second `complete_task` after the first one already flipped the row to terminal
    state must raise — the row is no longer 'running'. This is the duplicate-delivery
    safety property from acks_late + worker death."""
    task_id = _seed_one_task(client, make_payload)
    with sync_session_factory() as session:
        start_task(session, task_id)

    with sync_session_factory() as session:
        complete_task(
            session,
            task_id=task_id,
            actual_value=1,
            expected_value=1,
            validation_message="validated",
            processed_by="w1",
            celery_task_id="c1",
        )

    with sync_session_factory() as session, pytest.raises(StaleTaskStateError):
        complete_task(
            session,
            task_id=task_id,
            actual_value=1,
            expected_value=1,
            validation_message="validated",
            processed_by="w2",
            celery_task_id="c2",
        )

    with sync_session_factory() as session:
        row = session.get(TaskVariant, task_id)
        assert row.processed_by == "w1"
        assert row.celery_task_id == "c1"
        assert row.status == "done"


def test_metrics_and_db_diagnostics_work_in_eager_mode(client, eager_celery, make_payload):
    sweep = client.post('/sweeps', json=make_payload(chunks=1, jobs_per_chunk=1, tasks_per_job=2)).json()
    launch = client.post(f"/sweeps/{sweep['id']}/launch")
    assert launch.status_code == 200

    metrics = client.get('/metrics')
    assert metrics.status_code == 200
    assert 'celery_task_started_total' in metrics.text
    assert 'app.tasks.execute_task_variant' in metrics.text

    diagnostics = client.get('/diagnostics/db')
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload['pool']['pool_class']
    assert isinstance(payload['pg_stat_activity'], list)

@pytest.mark.live_worker
def test_live_multiworker_distribution_and_queue_routing(live_multiworker_harness, make_payload):
    client = live_multiworker_harness["client"]
    sync_session_factory = live_multiworker_harness["sync_session_factory"]
    exec_workers = live_multiworker_harness["exec_workers"]
    job_finalize_workers = live_multiworker_harness["job_finalize_workers"]
    chunk_finalize_workers = live_multiworker_harness["chunk_finalize_workers"]
    sweep_finalize_workers = live_multiworker_harness["sweep_finalize_workers"]

    response = client.post(
        "/sweeps",
        json=make_payload(chunks=5, jobs_per_chunk=5, tasks_per_job=4, sleep_ms=75),
    )
    assert response.status_code == 201
    sweep = response.json()
    assert sweep["total_tasks"] == 100

    launch_response = client.post(f"/sweeps/{sweep['id']}/launch")
    assert launch_response.status_code == 200, launch_response.text
    assert launch_response.json()["status"] == "done"

    fetched = client.get(f"/sweeps/{sweep['id']}")
    assert fetched.status_code == 200
    payload = fetched.json()
    assert payload["status"] == "done"

    with sync_session_factory() as session:
        sweep_row = session.execute(select(Sweep).where(Sweep.id == sweep["id"])).scalar_one()
        assert sweep_row.status == "done"
        assert sweep_row.finalized_by in sweep_finalize_workers

        chunks = session.execute(select(Chunk).where(Chunk.sweep_id == sweep["id"])).scalars().all()
        assert len(chunks) == 5
        assert all(chunk.status == "done" for chunk in chunks)
        assert {chunk.finalized_by for chunk in chunks}.issubset(chunk_finalize_workers)

        jobs = session.execute(select(Job)).scalars().all()
        assert len(jobs) == 25
        assert all(job.status == "done" for job in jobs)
        assert all(job.attempts == 1 for job in jobs)
        assert {job.finalized_by for job in jobs}.issubset(job_finalize_workers)

        tasks = session.execute(select(TaskVariant).order_by(TaskVariant.id)).scalars().all()
        assert len(tasks) == 100
        assert all(task.status == "done" for task in tasks)
        assert all(task.validation_message == "validated" for task in tasks)
        assert all(task.actual_value == task.expected_value for task in tasks)
        workers = {task.processed_by for task in tasks if task.processed_by}
        assert len(workers) >= 2
        assert workers.issubset(exec_workers)
        assert all(task.celery_task_id for task in tasks)

        counts = count_statuses(session)
        assert counts == {
            "sweeps_done": 1,
            "chunks_done": 5,
            "jobs_done": 25,
            "tasks_done": 100,
        }


@pytest.mark.live_worker
def test_concurrent_sweeps_keep_db_state_consistent(live_multiworker_harness, make_payload):
    client = live_multiworker_harness["client"]
    sync_session_factory = live_multiworker_harness["sync_session_factory"]

    sweep_ids = []
    for idx in range(5):
        response = client.post(
            "/sweeps",
            json=make_payload(chunks=2, jobs_per_chunk=3, tasks_per_job=5, sleep_ms=40, base_seed=100 + idx * 1000),
        )
        assert response.status_code == 201
        sweep_ids.append(response.json()["id"])

    def launch_one(sweep_id: int) -> dict:
        response = client.post(f"/sweeps/{sweep_id}/launch")
        assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(launch_one, sweep_ids))

    assert all(result["status"] == "done" for result in results)

    for sweep_id in sweep_ids:
        fetched = client.get(f"/sweeps/{sweep_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "done"

    with sync_session_factory() as session:
        sweeps = session.execute(select(Sweep).order_by(Sweep.id)).scalars().all()
        assert len(sweeps) == 5
        assert all(s.status == "done" for s in sweeps)

        chunks = session.execute(select(Chunk)).scalars().all()
        assert len(chunks) == 10
        assert all(c.status == "done" for c in chunks)

        jobs = session.execute(select(Job)).scalars().all()
        assert len(jobs) == 30
        assert all(j.status == "done" for j in jobs)
        assert all(j.attempts == 1 for j in jobs)

        tasks = session.execute(select(TaskVariant)).scalars().all()
        assert len(tasks) == 150
        assert all(t.status == "done" for t in tasks)
        assert all(t.actual_value == t.expected_value for t in tasks)
        assert all(t.processed_by for t in tasks)
        assert all(t.celery_task_id for t in tasks)


@pytest.mark.live_worker
def test_metrics_and_db_diagnostics_expose_runtime_state(live_multiworker_harness, make_payload):
    client = live_multiworker_harness["client"]

    sweep = client.post("/sweeps", json=make_payload(chunks=2, jobs_per_chunk=2, tasks_per_job=3, sleep_ms=25)).json()
    launch = client.post(f"/sweeps/{sweep['id']}/launch")
    assert launch.status_code == 200

    metrics = client.get('/metrics')
    assert metrics.status_code == 200
    body = metrics.text
    assert 'celery_task_started_total' in body
    assert 'celery_task_succeeded_total' in body
    assert 'app.tasks.execute_task_variant' in body

    diagnostics = client.get('/diagnostics/db')
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert 'pool' in payload
    assert payload['pool']['pool_class']
    assert 'checkedout' in payload['pool']
    assert isinstance(payload['pg_stat_activity'], list)


@pytest.mark.live_worker
def test_high_concurrency_runtime_handles_50_operations_without_session_breakage(live_multiworker_harness, make_payload):
    client = live_multiworker_harness['client']
    sync_session_factory = live_multiworker_harness['sync_session_factory']

    sweep_ids = []
    for idx in range(10):
        response = client.post('/sweeps', json=make_payload(chunks=1, jobs_per_chunk=2, tasks_per_job=4, sleep_ms=20, base_seed=2000 + idx * 100))
        assert response.status_code == 201
        sweep_ids.append(response.json()['id'])

    def op(i: int) -> tuple[str, int]:
        sweep_id = sweep_ids[i % len(sweep_ids)]
        if i < len(sweep_ids):
            r = client.post(f'/sweeps/{sweep_id}/launch')
            assert r.status_code == 200, r.text
            return ('launch', sweep_id)
        r = client.get(f'/sweeps/{sweep_id}')
        assert r.status_code == 200, r.text
        return ('get', sweep_id)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as executor:
        ops = list(executor.map(op, range(50)))

    assert len(ops) == 50

    with sync_session_factory() as session:
        from sqlalchemy import select
        from app.models import Sweep, Chunk, Job, TaskVariant
        sweeps = session.execute(select(Sweep).order_by(Sweep.id)).scalars().all()
        assert len(sweeps) == 10
        assert all(s.status == 'done' for s in sweeps)
        assert all(s.finalized_by for s in sweeps)

        chunks = session.execute(select(Chunk)).scalars().all()
        assert len(chunks) == 10
        assert all(c.status == 'done' for c in chunks)

        jobs = session.execute(select(Job)).scalars().all()
        assert len(jobs) == 20
        assert all(j.status == 'done' for j in jobs)
        assert all(j.attempts == 1 for j in jobs)

        tasks = session.execute(select(TaskVariant)).scalars().all()
        assert len(tasks) == 80
        assert all(t.status == 'done' for t in tasks)
        assert all(t.celery_task_id for t in tasks)
        assert all(t.processed_by for t in tasks)

    diagnostics = client.get('/diagnostics/db').json()
    pool = diagnostics['pool']
    if pool['checkedout'] is not None:
        assert pool['checkedout'] >= 0
