"""Central configuration, loaded from environment variables / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUE = {"1", "true", "yes", "y", "on"}


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else raw.strip().lower() in _TRUE


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else float(raw)


@dataclass(frozen=True)
class Settings:
    # API
    api_base_url: str
    api_timeout: float
    api_max_retries: int
    api_backoff_seconds: float
    # Database
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    database_url_override: str
    # S3
    s3_enabled: bool
    s3_bucket: str
    s3_prefix: str
    s3_sse: str
    s3_endpoint_url: str
    s3_fail_on_error: bool
    aws_region: str
    # Behaviour
    log_level: str
    full_refresh: bool
    lookback_days: int
    max_reject_ratio: float
    fail_on_quality_error: bool
    # Paths
    data_dir: Path
    log_dir: Path
    sql_dir: Path

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def rejected_dir(self) -> Path:
        return self.data_dir / "rejected"

    @property
    def database_url(self) -> URL | str:
        """SQLAlchemy URL (credentials safely escaped when built from parts)."""
        if self.database_url_override:
            return self.database_url_override
        return URL.create(
            drivername="postgresql+psycopg2",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    def ensure_directories(self) -> None:
        for path in (self.raw_dir, self.processed_dir, self.rejected_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """Build Settings from the environment (and .env if present)."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    settings = Settings(
        api_base_url=_str("API_BASE_URL", "https://fakestoreapi.com").rstrip("/"),
        api_timeout=_float("API_TIMEOUT_SECONDS", 15.0),
        api_max_retries=_int("API_MAX_RETRIES", 4),
        api_backoff_seconds=_float("API_BACKOFF_SECONDS", 1.5),
        db_host=_str("POSTGRES_HOST", "localhost"),
        db_port=_int("POSTGRES_PORT", 5432),
        db_name=_str("POSTGRES_DB", "sales_dwh"),
        db_user=_str("POSTGRES_USER", "etl_user"),
        db_password=_str("POSTGRES_PASSWORD", ""),
        database_url_override=_str("DATABASE_URL"),
        s3_enabled=_bool("S3_ENABLED", False),
        s3_bucket=_str("S3_BUCKET"),
        s3_prefix=_str("S3_PREFIX", "api-dwh").strip("/"),
        s3_sse=_str("S3_SSE", "AES256"),
        s3_endpoint_url=_str("S3_ENDPOINT_URL"),
        s3_fail_on_error=_bool("S3_FAIL_ON_ERROR", False),
        aws_region=_str("AWS_REGION", "ap-south-1"),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        full_refresh=_bool("FULL_REFRESH", False),
        lookback_days=_int("LOOKBACK_DAYS", 1),
        max_reject_ratio=_float("MAX_REJECT_RATIO", 0.30),
        fail_on_quality_error=_bool("FAIL_ON_QUALITY_ERROR", True),
        data_dir=Path(_str("DATA_DIR") or PROJECT_ROOT / "data"),
        log_dir=Path(_str("LOG_DIR") or PROJECT_ROOT / "logs"),
        sql_dir=Path(_str("SQL_DIR") or PROJECT_ROOT / "sql"),
    )
    settings.ensure_directories()
    return settings
