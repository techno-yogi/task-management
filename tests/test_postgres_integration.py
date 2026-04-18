from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text


pytestmark = pytest.mark.postgres_integration


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Set TEST_POSTGRES_URL for live PostgreSQL integration checks.")
def test_pg_stat_activity_query_executes_against_live_postgres() -> None:
    engine = create_engine(os.environ["TEST_POSTGRES_URL"], pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    select application_name, state, wait_event_type, count(*) as connections
                    from pg_stat_activity
                    where datname = current_database()
                    group by application_name, state, wait_event_type
                    order by application_name nulls last, state nulls last
                    """
                )
            ).mappings().all()
            assert isinstance(rows, list)
    finally:
        engine.dispose()
