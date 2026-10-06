"""Data quality checks: record counts, duplicates, schema and referential integrity.

Pre-load checks run on in-memory DataFrames (before anything is written).
Post-load checks run inside the load transaction, so a failure rolls the load back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.transform import EXPECTED_COLUMNS, KEYS
from src.validate import ValidationResult

logger = logging.getLogger(__name__)

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"

# Columns that must exist in the warehouse (subset check - extra columns are fine)
EXPECTED_DB_COLUMNS: dict[str, list[str]] = {
    "dim_date": EXPECTED_COLUMNS["dim_date"],
    "dim_customer": ["customer_key", *EXPECTED_COLUMNS["dim_customer"]],
    "dim_product": ["product_key", *EXPECTED_COLUMNS["dim_product"]],
    "fact_sales": [
        "sales_key", "cart_id", "customer_key", "product_key", "date_key",
        "quantity", "unit_price", "line_total", "etl_run_id",
    ],
    "agg_daily_sales": ["date_key", "orders", "units_sold", "revenue"],
    "agg_category_sales": ["category", "units_sold", "revenue", "revenue_share"],
    "agg_customer_summary": ["customer_key", "orders", "revenue", "customer_segment"],
}


@dataclass
class CheckResult:
    check_name: str
    entity: str
    status: str
    expected: str | None = None
    actual: str | None = None
    details: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


def _result(name: str, entity: str, ok: bool, expected, actual, details: str = "", severity: str = "error") -> CheckResult:
    status = PASS if ok else (FAIL if severity == "error" else WARN)
    res = CheckResult(name, entity, status, str(expected), str(actual), details)
    log = logger.info if ok else (logger.error if status == FAIL else logger.warning)
    log("DQ [%s] %s / %s expected=%s actual=%s %s", status, name, entity, expected, actual, details)
    return res


def has_failures(results: list[CheckResult]) -> bool:
    return any(r.failed for r in results)


# ------------------------------------------------------------------ pre-load (in memory)
def check_record_counts(validation: dict[str, ValidationResult]) -> list[CheckResult]:
    """input == valid + rejected for every dataset (nothing silently dropped)."""
    out = []
    for entity, res in validation.items():
        total = res.valid_count + res.rejected_count
        out.append(
            _result("record_count_reconciliation", entity, total == res.input_count, res.input_count, total,
                    f"valid={res.valid_count} rejected={res.rejected_count}")
        )
    return out


def check_reject_threshold(validation: dict[str, ValidationResult], max_ratio: float) -> list[CheckResult]:
    return [
        _result("reject_ratio_threshold", entity, res.reject_ratio <= max_ratio, f"<= {max_ratio:.0%}",
                f"{res.reject_ratio:.1%}", f"{res.rejected_count}/{res.input_count} rejected")
        for entity, res in validation.items()
    ]


def check_frame_schema(frames: dict[str, pd.DataFrame]) -> list[CheckResult]:
    out = []
    for name, frame in frames.items():
        expected = EXPECTED_COLUMNS[name]
        actual = list(frame.columns)
        out.append(_result("processed_schema", name, actual == expected, expected, actual))
    return out


def check_frame_keys(frames: dict[str, pd.DataFrame]) -> list[CheckResult]:
    out = []
    for name, frame in frames.items():
        key = KEYS[name]
        nulls = int(frame[key].isna().any(axis=1).sum()) if len(frame) else 0
        dups = int(frame.duplicated(subset=key).sum()) if len(frame) else 0
        out.append(_result("null_keys", name, nulls == 0, 0, nulls, f"key={key}"))
        out.append(_result("duplicate_keys", name, dups == 0, 0, dups, f"key={key}"))
    return out


def run_pre_load_checks(
    validation: dict[str, ValidationResult], frames: dict[str, pd.DataFrame], max_reject_ratio: float
) -> list[CheckResult]:
    return [
        *check_record_counts(validation),
        *check_reject_threshold(validation, max_reject_ratio),
        *check_frame_schema(frames),
        *check_frame_keys(frames),
    ]


# ------------------------------------------------------------------ post-load (database)
def _scalar(conn: Connection, sql: str, params: dict | None = None) -> int:
    return int(conn.execute(text(sql), params or {}).scalar() or 0)


def check_db_schema(conn: Connection) -> list[CheckResult]:
    out = []
    for table, expected in EXPECTED_DB_COLUMNS.items():
        rows = conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t"),
            {"t": table},
        ).fetchall()
        present = {r[0] for r in rows}
        missing = [c for c in expected if c not in present]
        out.append(_result("warehouse_schema", table, not missing, "all expected columns", f"missing={missing}"))
    return out


def check_db_duplicates(conn: Connection) -> list[CheckResult]:
    checks = {
        "dim_customer": "SELECT COUNT(*) - COUNT(DISTINCT customer_id) FROM dim_customer",
        "dim_product": "SELECT COUNT(*) - COUNT(DISTINCT product_id) FROM dim_product",
        "dim_date": "SELECT COUNT(*) - COUNT(DISTINCT date_key) FROM dim_date",
        "fact_sales": "SELECT COUNT(*) FROM (SELECT 1 FROM fact_sales GROUP BY cart_id, product_key HAVING COUNT(*) > 1) d",
    }
    return [_result("duplicate_business_keys", t, (n := _scalar(conn, sql)) == 0, 0, n) for t, sql in checks.items()]


def check_db_integrity(conn: Connection) -> list[CheckResult]:
    checks = {
        "orphan_customer_fk": ("fact_sales", "SELECT COUNT(*) FROM fact_sales f LEFT JOIN dim_customer c ON c.customer_key = f.customer_key WHERE c.customer_key IS NULL"),
        "orphan_product_fk": ("fact_sales", "SELECT COUNT(*) FROM fact_sales f LEFT JOIN dim_product p ON p.product_key = f.product_key WHERE p.product_key IS NULL"),
        "orphan_date_fk": ("fact_sales", "SELECT COUNT(*) FROM fact_sales f LEFT JOIN dim_date d ON d.date_key = f.date_key WHERE d.date_key IS NULL"),
        "non_positive_measures": ("fact_sales", "SELECT COUNT(*) FROM fact_sales WHERE quantity <= 0 OR unit_price < 0 OR line_total < 0"),
        "line_total_equals_qty_x_price": ("fact_sales", "SELECT COUNT(*) FROM fact_sales WHERE ABS(line_total - quantity * unit_price) > 0.005"),
        "agg_daily_revenue_reconciles": (
            "agg_daily_sales",
            "SELECT CASE WHEN ABS(COALESCE((SELECT SUM(revenue) FROM agg_daily_sales), 0) - COALESCE((SELECT SUM(line_total) FROM fact_sales), 0)) < 0.01 THEN 0 ELSE 1 END",
        ),
        "agg_category_revenue_reconciles": (
            "agg_category_sales",
            "SELECT CASE WHEN ABS(COALESCE((SELECT SUM(revenue) FROM agg_category_sales), 0) - COALESCE((SELECT SUM(line_total) FROM fact_sales), 0)) < 0.01 THEN 0 ELSE 1 END",
        ),
    }
    return [_result(name, entity, (n := _scalar(conn, sql)) == 0, 0, n) for name, (entity, sql) in checks.items()]


def check_loaded_counts(conn: Connection, frames: dict[str, pd.DataFrame]) -> list[CheckResult]:
    """Every row we meant to load must now exist in the warehouse."""
    out = []
    specs = {
        "dim_customer": ("dim_customer", "customer_id"),
        "dim_product": ("dim_product", "product_id"),
        "dim_date": ("dim_date", "date_key"),
    }
    for name, (table, key) in specs.items():
        frame = frames[name]
        if frame.empty:
            out.append(_result("loaded_row_count", name, True, 0, 0))
            continue
        found = _scalar(
            conn,
            f"SELECT COUNT(*) FROM {table} WHERE {key} = ANY(CAST(:ids AS bigint[]))",
            {"ids": [int(v) for v in frame[key].tolist()]},
        )
        out.append(_result("loaded_row_count", name, found == len(frame), len(frame), found))

    fact = frames["fact_sales"]
    if fact.empty:
        out.append(_result("loaded_row_count", "fact_sales", True, 0, 0))
    else:
        found = _scalar(
            conn,
            "SELECT COUNT(*) FROM unnest(CAST(:carts AS bigint[]), CAST(:prods AS bigint[])) AS t(cart_id, product_id) "
            "JOIN dim_product p ON p.product_id = t.product_id "
            "JOIN fact_sales f ON f.cart_id = t.cart_id AND f.product_key = p.product_key",
            {"carts": [int(v) for v in fact["cart_id"].tolist()], "prods": [int(v) for v in fact["product_id"].tolist()]},
        )
        out.append(_result("loaded_row_count", "fact_sales", found == len(fact), len(fact), found))
    return out


def run_post_load_checks(conn: Connection, frames: dict[str, pd.DataFrame]) -> list[CheckResult]:
    return [
        *check_db_schema(conn),
        *check_loaded_counts(conn, frames),
        *check_db_duplicates(conn),
        *check_db_integrity(conn),
    ]


def summarize(results: list[CheckResult]) -> dict[str, int]:
    return {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL)}
