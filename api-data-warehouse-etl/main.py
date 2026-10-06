"""CLI entry point:  python main.py [--full-refresh] [--skip-s3] [--init-db-only]"""
from __future__ import annotations

import argparse
import logging
import sys

from src.config import load_settings
from src.pipeline import run_pipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="API -> PostgreSQL data warehouse ETL pipeline")
    parser.add_argument("--full-refresh", action="store_true", help="Ignore the watermark and re-extract all carts")
    parser.add_argument("--skip-s3", action="store_true", help="Do not upload anything to S3")
    parser.add_argument("--init-db-only", action="store_true", help="Create/verify the schema and exit")
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL (DEBUG, INFO, ...)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    if args.log_level:
        from dataclasses import replace

        settings = replace(settings, log_level=args.log_level.upper())

    if args.init_db_only:
        from src import database
        from src.logger import setup_logging

        setup_logging(settings.log_dir, "init", settings.log_level)
        engine = database.get_engine(settings)
        database.wait_for_database(engine)
        database.init_schema(engine, settings.sql_dir)
        return 0

    try:
        summary = run_pipeline(settings, full_refresh=args.full_refresh or None, skip_s3=args.skip_s3)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("main").error("Pipeline failed: %s", exc)
        return 1

    print(
        f"\nRun {summary.run_id}: {summary.status} in {summary.duration_seconds}s\n"
        f"  extracted={summary.extracted}\n  rejected={summary.rejected}\n"
        f"  loaded={summary.loaded}\n  quality={summary.quality}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
