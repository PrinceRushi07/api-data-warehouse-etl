"""Extraction: call the REST API with retries/timeouts and persist raw JSON."""
from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
# PII / secrets that must never land in the raw zone (local or S3).
REDACT_FIELDS = {"password"}

ENDPOINTS: dict[str, dict[str, Any]] = {
    "products": {"path": "/products", "allow_empty": False},
    "users": {"path": "/users", "allow_empty": False},
    "carts": {"path": "/carts", "allow_empty": True},
}


class ExtractionError(RuntimeError):
    """Raised when the API cannot be read after all retries."""


@dataclass
class ExtractResult:
    entity: str
    records: list[dict]
    path: Path
    record_count: int
    duration_seconds: float


class ApiClient:
    """Thin HTTP client with exponential back-off, jitter and timeout handling."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        max_retries: int = 4,
        backoff_seconds: float = 1.5,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.backoff_seconds = backoff_seconds
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "api-dwh-etl/1.0"})
        self._sleep = sleep

    def _delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 60.0)
        base = self.backoff_seconds * (2 ** (attempt - 1))
        return min(base + random.uniform(0, base * 0.25), 60.0)

    def get_json(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        attempts = self.max_retries + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            retry_after: str | None = None
            try:
                logger.info("GET %s params=%s (attempt %d/%d)", url, params, attempt, attempts)
                response = self.session.get(url, params=params, timeout=(self.timeout, self.timeout))

                if response.status_code in RETRYABLE_STATUS:
                    retry_after = response.headers.get("Retry-After")
                    raise requests.HTTPError(f"retryable status {response.status_code}", response=response)
                if 400 <= response.status_code < 500:
                    raise ExtractionError(f"{url} returned non-retryable status {response.status_code}")
                response.raise_for_status()
                return response.json()

            except ExtractionError:
                raise
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError) as exc:
                # ValueError covers invalid JSON bodies (requests' JSONDecodeError subclasses it)
                last_error = exc
                logger.warning("Attempt %d/%d failed for %s: %s", attempt, attempts, url, exc)
                if attempt < attempts:
                    self._sleep(self._delay(attempt, retry_after))

        raise ExtractionError(f"{url} failed after {attempts} attempts: {last_error}") from last_error


def redact(records: list[Any]) -> list[Any]:
    """Drop sensitive fields before anything is written to disk."""
    return [
        {k: v for k, v in rec.items() if k not in REDACT_FIELDS} if isinstance(rec, dict) else rec
        for rec in records
    ]


def save_raw(entity: str, records: list[Any], raw_dir: Path, run_id: str, meta: dict) -> Path:
    """Write a timestamped raw JSON envelope atomically and return its path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target_dir = Path(raw_dir) / entity
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{entity}_{stamp}.json"
    envelope = {"_meta": {**meta, "entity": entity, "run_id": run_id, "record_count": len(records)}, "data": records}

    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def extract_entity(client: ApiClient, entity: str, raw_dir: Path, run_id: str, params: dict | None = None) -> ExtractResult:
    spec = ENDPOINTS[entity]
    started = time.monotonic()
    extracted_at = datetime.now(timezone.utc).isoformat()

    payload = client.get_json(spec["path"], params=params)
    if not isinstance(payload, list):
        raise ExtractionError(f"{entity}: expected a JSON array, got {type(payload).__name__}")
    if not payload and not spec["allow_empty"]:
        raise ExtractionError(f"{entity}: API returned an empty dataset")

    records = redact(payload)
    path = save_raw(
        entity,
        records,
        raw_dir,
        run_id,
        {"source_url": f"{client.base_url}{spec['path']}", "params": params or {}, "extracted_at": extracted_at},
    )
    duration = time.monotonic() - started
    logger.info("Extracted %d %s records -> %s (%.2fs)", len(records), entity, path, duration)
    return ExtractResult(entity, records, path, len(records), duration)


def extract_all(settings, run_id: str, since: date | None = None, client: ApiClient | None = None) -> dict[str, ExtractResult]:
    """Extract products, users and carts. Carts are filtered by `since` when given (incremental)."""
    client = client or ApiClient(
        settings.api_base_url, settings.api_timeout, settings.api_max_retries, settings.api_backoff_seconds
    )
    cart_params = None
    if since is not None:
        cart_params = {
            "startdate": since.isoformat(),
            "enddate": (date.today() + timedelta(days=1)).isoformat(),
        }
        logger.info("Incremental extract of carts since %s", since)
    else:
        logger.info("Full extract of carts")

    results: dict[str, ExtractResult] = {}
    for entity in ENDPOINTS:
        params = cart_params if entity == "carts" else None
        results[entity] = extract_entity(client, entity, settings.raw_dir, run_id, params)
    return results
