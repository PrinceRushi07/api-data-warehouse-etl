"""Logging setup: console + pipeline log + error log + per-run execution log."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-8s | run=%(run_id)s | %(name)s | %(message)s"
_MARK = "_etl_handler"


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def setup_logging(log_dir: Path, run_id: str, level: str = "INFO") -> dict[str, Path]:
    """Configure the root logger. Safe to call repeatedly (replaces own handlers).

    Returns the paths of the log files:
      pipeline   - rolling log of every run (INFO+)
      error      - rolling log with ERROR+ only
      execution  - log of this run only (uploaded to S3)
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "pipeline": log_dir / "pipeline.log",
        "error": log_dir / "error.log",
        "execution": log_dir / f"execution_{run_id}.log",
    }

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(_FORMAT)
    run_filter = _RunIdFilter(run_id)

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(paths["pipeline"], maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
        RotatingFileHandler(paths["error"], maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
        logging.FileHandler(paths["execution"], encoding="utf-8"),
    ]
    handlers[2].setLevel(logging.ERROR)

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(run_filter)
        setattr(handler, _MARK, True)
        root.addHandler(handler)

    # Quiet noisy libraries
    for noisy in ("urllib3", "botocore", "boto3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return paths


def flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        if getattr(handler, _MARK, False):
            handler.flush()
