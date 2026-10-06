"""PostgreSQL connectivity, schema bootstrap and ETL metadata helpers (SQLAlchemy)."""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)


def get_engine(settings) -> Engine:
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5, future=True)
    logger.info("Database engine created for %s", engine.url.render_as_string(hide_password=True))
    return engine


def wait_for_database(engine: Engine, retries: int = 30, delay: float = 2.0) -> None:
    """Block until PostgreSQL accepts connections (useful on container start-up)."""
    for attempt in range(1, retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Database is reachable")
            return
        except OperationalError as exc:
            logger.warning("Database not ready (%d/%d): %s", attempt, retries, str(exc).splitlines()[0])
            time.sleep(delay)
    raise ConnectionError("Database did not become available in time")


def run_sql_file(conn: Connection, path: Path) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    conn.exec_driver_sql(sql)


def init_schema(engine: Engine, sql_dir: Path) -> None:
    with engine.begin() as conn:
        run_sql_file(conn, Path(sql_dir) / "schema.sql")
    logger.info("Schema verified/created from %s", Path(sql_dir) / "schema.sql")


# ------------------------------------------------------------------ run audit
def start_run(engine: Engine, run_id: str, full_refresh: bool) -> datetime:
    started = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO etl_run_audit (run_id, status, started_at, full_refresh) "
                "VALUES (:run_id, 'RUNNING', :started, :full_refresh)"
            ),
            {"run_id": run_id, "started": started, "full_refresh": full_refresh},
        )
    return started


def finish_run(
    engine: Engine,
    run_id: str,
    started: datetime,
    status: str,
    extracted: dict[str, int] | None = None,
    valid: dict[str, int] | None = None,
    rejected: dict[str, int] | None = None,
    loaded: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    finished = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE etl_run_audit SET status = :status, finished_at = :finished, "
                "duration_seconds = :duration, extracted_counts = CAST(:extracted AS jsonb), "
                "valid_counts = CAST(:valid AS jsonb), rejected_counts = CAST(:rejected AS jsonb), "
                "loaded_counts = CAST(:loaded AS jsonb), error_message = :error WHERE run_id = :run_id"
            ),
            {
                "status": status,
                "finished": finished,
                "duration": round((finished - started).total_seconds(), 2),
                "extracted": json.dumps(extracted or {}),
                "valid": json.dumps(valid or {}),
                "rejected": json.dumps(rejected or {}),
                "loaded": json.dumps(loaded or {}, default=str),
                "error": (error or None) and error[:4000],
                "run_id": run_id,
            },
        )


# ------------------------------------------------------------------ watermark
def get_watermark(engine: Engine, entity: str = "fact_sales") -> date | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT watermark_value FROM etl_watermark WHERE entity = :e"), {"e": entity}
        ).scalar()


def set_watermark(conn: Connection, entity: str, value: date, run_id: str) -> None:
    conn.execute(
        text(
            "INSERT INTO etl_watermark (entity, watermark_value, run_id) VALUES (:e, :v, :r) "
            "ON CONFLICT (entity) DO UPDATE SET watermark_value = GREATEST(etl_watermark.watermark_value, EXCLUDED.watermark_value), "
            "run_id = EXCLUDED.run_id, updated_at = now()"
        ),
        {"e": entity, "v": value, "r": run_id},
    )


# ------------------------------------------------------------------ data quality persistence
def save_quality_results(engine: Engine, run_id: str, results: list) -> None:
    if not results:
        return
    rows = [
        {
            "run_id": run_id,
            "check_name": r.check_name,
            "entity": r.entity,
            "status": r.status,
            "expected": r.expected,
            "actual": r.actual,
            "details": r.details,
        }
        for r in results
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO data_quality_results (run_id, check_name, entity, status, expected, actual, details) "
                "VALUES (:run_id, :check_name, :entity, :status, :expected, :actual, :details)"
            ),
            rows,
        )
    logger.info("Persisted %d data quality results", len(rows))
