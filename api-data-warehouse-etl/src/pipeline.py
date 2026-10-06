"""Pipeline orchestration: extract -> validate -> transform -> quality -> load -> S3."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import database, extract, load, quality_checks, transform, validate
from src.config import Settings
from src.logger import flush_logs, setup_logging
from src.s3_upload import S3Uploader, S3UploadError

logger = logging.getLogger(__name__)


class DataQualityError(RuntimeError):
    pass


@dataclass
class RunSummary:
    run_id: str
    status: str = "RUNNING"
    extracted: dict = field(default_factory=dict)
    valid: dict = field(default_factory=dict)
    rejected: dict = field(default_factory=dict)
    loaded: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    duration_seconds: float = 0.0
    s3_uris: list = field(default_factory=list)


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


def _s3(uploader: S3Uploader, settings: Settings, category: str, files, summary: RunSummary) -> None:
    """Best-effort S3 upload: honours S3_FAIL_ON_ERROR."""
    try:
        summary.s3_uris.extend(uploader.upload_files(category, files))
    except S3UploadError as exc:
        logger.error("S3 upload (%s) failed: %s", category, exc)
        if settings.s3_fail_on_error:
            raise


def run_pipeline(
    settings: Settings,
    *,
    full_refresh: bool | None = None,
    skip_s3: bool = False,
    api_client: extract.ApiClient | None = None,
    engine=None,
    uploader: S3Uploader | None = None,
    run_id: str | None = None,
) -> RunSummary:
    run_id = run_id or new_run_id()
    full_refresh = settings.full_refresh if full_refresh is None else full_refresh
    log_paths = setup_logging(settings.log_dir, run_id, settings.log_level)
    summary = RunSummary(run_id=run_id)
    t0 = time.monotonic()

    if uploader is None:
        uploader = S3Uploader.from_settings(settings, enabled=settings.s3_enabled and not skip_s3)

    engine = engine or database.get_engine(settings)
    database.wait_for_database(engine)
    database.init_schema(engine, settings.sql_dir)
    started = database.start_run(engine, run_id, full_refresh)
    logger.info("=== Pipeline run %s started (full_refresh=%s, s3=%s) ===", run_id, full_refresh, uploader.enabled)

    quality_results: list[quality_checks.CheckResult] = []
    raw_files: dict[str, Path] = {}
    processed_files: dict[str, Path] = {}
    rejected_files: dict[str, Path] = {}

    try:
        uploader.verify_bucket()

        # ---------------------------------------------------------------- extract
        since: date | None = None
        if not full_refresh:
            watermark = database.get_watermark(engine, "fact_sales")
            if watermark:
                since = watermark - timedelta(days=settings.lookback_days)
        extracted = extract.extract_all(settings, run_id, since=since, client=api_client)
        raw_files = {e: r.path for e, r in extracted.items()}
        summary.extracted = {e: r.record_count for e, r in extracted.items()}
        _s3(uploader, settings, "raw", raw_files, summary)  # keep raw even if a later stage fails

        # ---------------------------------------------------------------- validate
        validation = validate.validate_all({e: r.records for e, r in extracted.items()})
        rejected_files = validate.write_rejected(validation, settings.rejected_dir, run_id)
        summary.valid = {e: r.valid_count for e, r in validation.items()}
        summary.rejected = {e: r.rejected_count for e, r in validation.items()}

        # ---------------------------------------------------------------- transform
        frames = transform.transform_all(
            {
                "products": validation["products"].valid,
                "users": validation["users"].valid,
                "cart_items": validation["cart_items"].valid,
            }
        )
        processed_files = transform.write_processed(frames, settings.processed_dir)

        # ---------------------------------------------------------------- pre-load quality gate
        pre = quality_checks.run_pre_load_checks(validation, frames, settings.max_reject_ratio)
        quality_results.extend(pre)
        if settings.fail_on_quality_error and quality_checks.has_failures(pre):
            raise DataQualityError("Pre-load data quality checks failed; nothing was loaded")

        # ---------------------------------------------------------------- load (single transaction)
        with engine.begin() as conn:
            load_results = load.load_all(conn, frames, run_id)
            load.refresh_aggregates(conn, settings.sql_dir)

            post = quality_checks.run_post_load_checks(conn, frames)
            quality_results.extend(post)
            if settings.fail_on_quality_error and quality_checks.has_failures(post):
                raise DataQualityError("Post-load data quality checks failed; transaction rolled back")

            fact = frames["fact_sales"]
            if not fact.empty:
                database.set_watermark(conn, "fact_sales", max(fact["order_date"]), run_id)
        summary.loaded = {r.table: r.as_dict() for r in load_results}
        summary.quality = quality_checks.summarize(quality_results)
        summary.status = "SUCCESS"
        logger.info("Pipeline run %s succeeded. Quality: %s", run_id, summary.quality)

    except Exception as exc:  # noqa: BLE001 - we re-raise after recording the failure
        summary.status = "FAILED"
        summary.quality = quality_checks.summarize(quality_results)
        logger.exception("Pipeline run %s FAILED: %s", run_id, exc)
        _finalize(engine, settings, uploader, summary, started, run_id, quality_results,
                  processed_files, rejected_files, log_paths, t0, error=str(exc))
        raise

    _finalize(engine, settings, uploader, summary, started, run_id, quality_results,
              processed_files, rejected_files, log_paths, t0)
    return summary


def _finalize(engine, settings, uploader, summary, started, run_id, quality_results,
              processed_files, rejected_files, log_paths, t0, error: str | None = None) -> None:
    """Persist audit + DQ results, then ship artefacts and logs to S3 (never masks the original error)."""
    summary.duration_seconds = round(time.monotonic() - t0, 2)
    try:
        database.save_quality_results(engine, run_id, quality_results)
        database.finish_run(
            engine, run_id, started, summary.status,
            extracted=summary.extracted, valid=summary.valid, rejected=summary.rejected,
            loaded=summary.loaded, error=error,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist audit/quality records")

    try:
        _s3(uploader, settings, "processed", processed_files, summary)
        _s3(uploader, settings, "rejected", rejected_files, summary)
        logger.info("Run %s finished with status=%s in %.2fs", run_id, summary.status, summary.duration_seconds)
        flush_logs()
        _s3(uploader, settings, "logs", [(None, p) for p in log_paths.values() if Path(p).exists()], summary)
    except Exception:  # noqa: BLE001
        logger.exception("S3 artefact upload failed")
