"""Load: set-based, idempotent, incremental upserts into the star schema.

Strategy
--------
1. Stage each DataFrame into a staging table *inside the load transaction*.
2. Upsert into the target with INSERT ... ON CONFLICT DO UPDATE ... WHERE <row changed>.
   - re-running the pipeline never creates duplicates
   - unchanged rows are not rewritten (no needless updated_at churn / bloat)
3. Surrogate keys for the fact table are resolved with joins against the dimensions.
Everything runs in the connection handed in by the caller (one transaction, all-or-nothing).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.database import run_sql_file

logger = logging.getLogger(__name__)

STAGING = {
    "dim_date": "stg_dim_date",
    "dim_customer": "stg_dim_customer",
    "dim_product": "stg_dim_product",
    "fact_sales": "stg_fact_sales",
}

CUSTOMER_ATTRS = [
    "email", "username", "first_name", "last_name", "full_name", "phone",
    "city", "street_address", "zipcode", "latitude", "longitude", "email_domain",
]
PRODUCT_ATTRS = [
    "title", "category", "category_display", "description", "image_url",
    "price", "price_tier", "rating_rate", "rating_count", "rating_band",
]
DATE_COLS = [
    "date_key", "full_date", "year", "quarter", "month", "month_name", "year_month",
    "week_of_year", "day", "day_of_week", "day_name", "is_weekend",
]


class LoadError(RuntimeError):
    pass


@dataclass
class LoadResult:
    table: str
    staged: int = 0
    inserted: int = 0
    updated: int = 0

    @property
    def unchanged(self) -> int:
        return self.staged - self.inserted - self.updated

    def as_dict(self) -> dict:
        return {"staged": self.staged, "inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged}


def _stage(conn: Connection, df: pd.DataFrame, table: str) -> None:
    df.to_sql(table, conn, if_exists="replace", index=False, method="multi", chunksize=1000)


def _drop_stage(conn: Connection, table: str) -> None:
    conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


def _upsert_dimension_sql(target: str, staging: str, key: str, attrs: list[str]) -> str:
    cols = [key, *attrs]
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in attrs)
    old = ", ".join(f"{target}.{c}" for c in attrs)
    new = ", ".join(f"EXCLUDED.{c}" for c in attrs)
    return (
        f"INSERT INTO {target} ({', '.join(cols)}) "
        f"SELECT {', '.join(cols)} FROM {staging} "
        f"ON CONFLICT ({key}) DO UPDATE SET {sets}, updated_at = now() "
        f"WHERE ({old}) IS DISTINCT FROM ({new}) "
        f"RETURNING (xmax = 0) AS inserted"
    )


def _count_upsert(rows: list) -> tuple[int, int]:
    inserted = sum(1 for r in rows if r[0])
    return inserted, len(rows) - inserted


def load_dim_date(conn: Connection, df: pd.DataFrame) -> LoadResult:
    result = LoadResult("dim_date", staged=len(df))
    if df.empty:
        return result
    _stage(conn, df, STAGING["dim_date"])
    res = conn.execute(
        text(
            f"INSERT INTO dim_date ({', '.join(DATE_COLS)}) "
            "SELECT date_key, CAST(full_date AS date), year, quarter, month, month_name, year_month, "
            "week_of_year, day, day_of_week, day_name, is_weekend "
            f"FROM {STAGING['dim_date']} ON CONFLICT (date_key) DO NOTHING"
        )
    )
    result.inserted = res.rowcount
    _drop_stage(conn, STAGING["dim_date"])
    return result


def load_dim_customer(conn: Connection, df: pd.DataFrame) -> LoadResult:
    result = LoadResult("dim_customer", staged=len(df))
    if df.empty:
        return result
    _stage(conn, df, STAGING["dim_customer"])
    rows = conn.execute(
        text(_upsert_dimension_sql("dim_customer", STAGING["dim_customer"], "customer_id", CUSTOMER_ATTRS))
    ).fetchall()
    result.inserted, result.updated = _count_upsert(rows)
    _drop_stage(conn, STAGING["dim_customer"])
    return result


def load_dim_product(conn: Connection, df: pd.DataFrame) -> LoadResult:
    result = LoadResult("dim_product", staged=len(df))
    if df.empty:
        return result
    _stage(conn, df, STAGING["dim_product"])
    rows = conn.execute(
        text(_upsert_dimension_sql("dim_product", STAGING["dim_product"], "product_id", PRODUCT_ATTRS))
    ).fetchall()
    result.inserted, result.updated = _count_upsert(rows)
    _drop_stage(conn, STAGING["dim_product"])
    return result


def load_fact_sales(conn: Connection, df: pd.DataFrame, run_id: str) -> LoadResult:
    result = LoadResult("fact_sales", staged=len(df))
    if df.empty:
        return result

    df = df.drop_duplicates(["cart_id", "product_id"])
    result.staged = len(df)
    stg = STAGING["fact_sales"]
    _stage(conn, df, stg)

    orphans = conn.execute(
        text(
            f"SELECT COUNT(*) FROM {stg} s "
            "LEFT JOIN dim_customer c ON c.customer_id = s.customer_id "
            "LEFT JOIN dim_product p ON p.product_id = s.product_id "
            "LEFT JOIN dim_date d ON d.date_key = s.date_key "
            "WHERE c.customer_key IS NULL OR p.product_key IS NULL OR d.date_key IS NULL"
        )
    ).scalar_one()
    if orphans:
        raise LoadError(f"{orphans} fact rows cannot be mapped to dimension keys; aborting load")

    rows = conn.execute(
        text(
            "INSERT INTO fact_sales (cart_id, customer_key, product_key, date_key, quantity, unit_price, line_total, etl_run_id) "
            "SELECT s.cart_id, c.customer_key, p.product_key, s.date_key, s.quantity, s.unit_price, s.line_total, :run_id "
            f"FROM {stg} s "
            "JOIN dim_customer c ON c.customer_id = s.customer_id "
            "JOIN dim_product p ON p.product_id = s.product_id "
            "ON CONFLICT (cart_id, product_key) DO UPDATE SET "
            "customer_key = EXCLUDED.customer_key, date_key = EXCLUDED.date_key, quantity = EXCLUDED.quantity, "
            "unit_price = EXCLUDED.unit_price, line_total = EXCLUDED.line_total, "
            "etl_run_id = EXCLUDED.etl_run_id, updated_at = now() "
            "WHERE (fact_sales.customer_key, fact_sales.date_key, fact_sales.quantity, fact_sales.unit_price) "
            "IS DISTINCT FROM (EXCLUDED.customer_key, EXCLUDED.date_key, EXCLUDED.quantity, EXCLUDED.unit_price) "
            "RETURNING (xmax = 0) AS inserted"
        ),
        {"run_id": run_id},
    ).fetchall()
    result.inserted, result.updated = _count_upsert(rows)
    _drop_stage(conn, stg)
    return result


def load_all(conn: Connection, frames: dict[str, pd.DataFrame], run_id: str) -> list[LoadResult]:
    """Load dimensions first (FK order), then the fact table."""
    results = [
        load_dim_date(conn, frames["dim_date"]),
        load_dim_customer(conn, frames["dim_customer"]),
        load_dim_product(conn, frames["dim_product"]),
        load_fact_sales(conn, frames["fact_sales"], run_id),
    ]
    for r in results:
        logger.info(
            "Loaded %-13s staged=%d inserted=%d updated=%d unchanged=%d",
            r.table, r.staged, r.inserted, r.updated, r.unchanged,
        )
    return results


def refresh_aggregates(conn: Connection, sql_dir: Path) -> None:
    run_sql_file(conn, Path(sql_dir) / "refresh_aggregates.sql")
    logger.info("Analytics aggregate tables refreshed")
