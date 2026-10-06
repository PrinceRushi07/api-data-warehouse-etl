"""AWS S3 uploads with an organised, partitioned key layout.

s3://<bucket>/<prefix>/raw/<entity>/dt=YYYY-MM-DD/<file>
s3://<bucket>/<prefix>/processed/<table>/dt=YYYY-MM-DD/<file>
s3://<bucket>/<prefix>/rejected/<entity>/dt=YYYY-MM-DD/<file>
s3://<bucket>/<prefix>/logs/dt=YYYY-MM-DD/<file>
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path

import boto3
from boto3.exceptions import Boto3Error
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

CATEGORIES = {"raw", "processed", "rejected", "logs"}


class S3UploadError(RuntimeError):
    pass


class S3Uploader:
    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
        sse: str | None = "AES256",
        enabled: bool = True,
        client=None,
        run_date: date | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.sse = sse or None
        self.enabled = enabled
        self.run_date = run_date or datetime.now(timezone.utc).date()
        if enabled and not bucket:
            raise ValueError("S3_BUCKET must be set when S3_ENABLED=true")
        self.client = client
        if enabled and self.client is None:
            self.client = boto3.client(
                "s3",
                region_name=region or None,
                endpoint_url=endpoint_url or None,
                config=Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=10, read_timeout=60),
            )

    @classmethod
    def from_settings(cls, settings, enabled: bool | None = None) -> "S3Uploader":
        return cls(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            region=settings.aws_region,
            endpoint_url=settings.s3_endpoint_url,
            sse=settings.s3_sse,
            enabled=settings.s3_enabled if enabled is None else enabled,
        )

    # ------------------------------------------------------------------ keys
    def build_key(self, category: str, filename: str, entity: str | None = None) -> str:
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {sorted(CATEGORIES)}")
        parts = [self.prefix, category]
        if entity:
            parts.append(entity)
        parts.append(f"dt={self.run_date.isoformat()}")
        parts.append(filename)
        return "/".join(p for p in parts if p)

    # ------------------------------------------------------------------ uploads
    def verify_bucket(self) -> None:
        if not self.enabled:
            return
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (ClientError, BotoCoreError) as exc:
            raise S3UploadError(f"Cannot access bucket '{self.bucket}': {exc}") from exc

    def upload_file(self, path: Path, category: str, entity: str | None = None) -> str | None:
        """Upload one file; returns its s3:// URI (None when S3 is disabled)."""
        if not self.enabled:
            return None
        path = Path(path)
        if not path.is_file():
            raise S3UploadError(f"File not found: {path}")

        key = self.build_key(category, path.name, entity)
        extra = {"ServerSideEncryption": self.sse} if self.sse else None
        try:
            self.client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)
        except (Boto3Error, ClientError, BotoCoreError, OSError) as exc:
            raise S3UploadError(f"Upload of {path.name} to s3://{self.bucket}/{key} failed: {exc}") from exc
        uri = f"s3://{self.bucket}/{key}"
        logger.info("Uploaded %s -> %s", path.name, uri)
        return uri

    def upload_files(self, category: str, files: dict[str, Path] | list[tuple[str | None, Path]]) -> list[str]:
        """Upload many files. `files` is {entity: path} or [(entity, path), ...]."""
        items = list(files.items()) if isinstance(files, dict) else list(files)
        uris = []
        for entity, path in items:
            uri = self.upload_file(path, category, entity)
            if uri:
                uris.append(uri)
        return uris
