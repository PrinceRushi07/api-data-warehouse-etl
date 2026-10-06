"""Transformation: clean, standardise and model validated data into star-schema frames."""
from __future__ import annotations

import logging
import string
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Column contracts (also used by quality checks for schema validation)
EXPECTED_COLUMNS: dict[str, list[str]] = {
    "dim_date": [
        "date_key", "full_date", "year", "quarter", "month", "month_name", "year_month",
        "week_of_year", "day", "day_of_week", "day_name", "is_weekend",
    ],
    "dim_customer": [
        "customer_id", "email", "username", "first_name", "last_name", "full_name", "phone",
        "city", "street_address", "zipcode", "latitude", "longitude", "email_domain",
    ],
    "dim_product": [
        "product_id", "title", "category", "category_display", "description", "image_url",
        "price", "price_tier", "rating_rate", "rating_count", "rating_band",
    ],
    "fact_sales": [
        "cart_id", "customer_id", "product_id", "date_key", "order_date",
        "quantity", "unit_price", "line_total",
    ],
}

# Natural / grain keys per frame
KEYS: dict[str, list[str]] = {
    "dim_date": ["date_key"],
    "dim_customer": ["customer_id"],
    "dim_product": ["product_id"],
    "fact_sales": ["cart_id", "product_id"],
}


class TransformError(RuntimeError):
    pass


def _squash_spaces(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"\s+", " ", regex=True)


def _title(s: pd.Series) -> pd.Series:
    """Title-case without the apostrophe bug of str.title() (Men's -> Men's, not Men'S)."""
    return s.map(lambda v: string.capwords(v) if isinstance(v, str) else v)


def transform_products(df: pd.DataFrame) -> pd.DataFrame:
    cols = EXPECTED_COLUMNS["dim_product"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    price = pd.to_numeric(df["price"]).round(2)
    rate = pd.to_numeric(df["rating_rate"], errors="coerce")
    category = _squash_spaces(df["category"]).str.lower()

    out = pd.DataFrame(
        {
            "product_id": pd.to_numeric(df["id"]).astype("int64"),
            "title": _squash_spaces(df["title"]),
            "category": category,
            "category_display": _title(category),
            "description": df["description"].fillna("").astype(str).str.strip(),
            "image_url": df["image"].fillna("").astype(str).str.strip(),
            "price": price,
            "price_tier": np.select([price < 25, price < 100], ["budget", "standard"], default="premium"),
            "rating_rate": rate.round(2),
            "rating_count": pd.to_numeric(df["rating_count"], errors="coerce").astype("Int64"),
            "rating_band": np.select(
                [rate.isna(), rate >= 4.5, rate >= 4.0, rate >= 3.0],
                ["unrated", "excellent", "good", "average"],
                default="poor",
            ),
        }
    )
    return out[cols].drop_duplicates("product_id").reset_index(drop=True)


def transform_users(df: pd.DataFrame) -> pd.DataFrame:
    cols = EXPECTED_COLUMNS["dim_customer"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    first = _title(_squash_spaces(df["name_firstname"]))
    last = _title(_squash_spaces(df["name_lastname"]))
    email = df["email"].astype(str).str.strip().str.lower()
    number = pd.to_numeric(df["address_number"], errors="coerce").astype("Int64").astype(str).replace("<NA>", "")
    street = _title(df["address_street"].fillna("").astype(str).str.strip())

    out = pd.DataFrame(
        {
            "customer_id": pd.to_numeric(df["id"]).astype("int64"),
            "email": email,
            "username": df["username"].astype(str).str.strip().str.lower(),
            "first_name": first,
            "last_name": last,
            "full_name": (first + " " + last).str.strip(),
            "phone": df["phone"].fillna("").astype(str).str.strip(),
            "city": _title(_squash_spaces(df["address_city"])),
            "street_address": (number + " " + street).str.strip(),
            "zipcode": df["address_zipcode"].fillna("").astype(str).str.strip(),
            "latitude": pd.to_numeric(df["address_geolocation_lat"], errors="coerce"),
            "longitude": pd.to_numeric(df["address_geolocation_long"], errors="coerce"),
            "email_domain": email.str.split("@").str[-1],
        }
    )
    return out[cols].drop_duplicates("customer_id").reset_index(drop=True)


def transform_sales(items: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    """Build the fact frame at (cart, product) grain with derived measures."""
    cols = EXPECTED_COLUMNS["fact_sales"]
    if items.empty:
        return pd.DataFrame(columns=cols)

    ts = pd.to_datetime(items["order_date"].astype(str), utc=True, format="ISO8601").dt.tz_localize(None)
    order_date = ts.dt.normalize()

    fact = pd.DataFrame(
        {
            "cart_id": pd.to_numeric(items["cart_id"]).astype("int64"),
            "customer_id": pd.to_numeric(items["user_id"]).astype("int64"),
            "product_id": pd.to_numeric(items["product_id"]).astype("int64"),
            "date_key": order_date.dt.strftime("%Y%m%d").astype("int64"),
            "order_date": order_date.dt.date,
            "quantity": pd.to_numeric(items["quantity"]).astype("int64"),
        }
    )
    fact = fact.merge(
        products[["product_id", "price"]].rename(columns={"price": "unit_price"}),
        on="product_id",
        how="left",
        validate="many_to_one",
    )
    if fact["unit_price"].isna().any():
        raise TransformError("Cart items reference products missing from dim_product")

    fact["line_total"] = (fact["quantity"] * fact["unit_price"]).round(2)
    return fact[cols].drop_duplicates(KEYS["fact_sales"]).reset_index(drop=True)


def build_dim_date(fact: pd.DataFrame) -> pd.DataFrame:
    """Calendar dimension covering the full date range present in the fact frame."""
    cols = EXPECTED_COLUMNS["dim_date"]
    if fact.empty:
        return pd.DataFrame(columns=cols)

    dates = pd.to_datetime(fact["order_date"])
    rng = pd.date_range(dates.min().normalize(), dates.max().normalize(), freq="D")
    iso = rng.isocalendar()
    out = pd.DataFrame(
        {
            "date_key": rng.strftime("%Y%m%d").astype("int64"),
            "full_date": rng.date,
            "year": rng.year.astype("int64"),
            "quarter": rng.quarter.astype("int64"),
            "month": rng.month.astype("int64"),
            "month_name": rng.strftime("%B"),
            "year_month": rng.strftime("%Y-%m"),
            "week_of_year": iso["week"].astype("int64").to_numpy(),
            "day": rng.day.astype("int64"),
            "day_of_week": (rng.dayofweek + 1).astype("int64"),
            "day_name": rng.strftime("%A"),
            "is_weekend": rng.dayofweek >= 5,
        }
    )
    return out[cols]


def transform_all(valid: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """valid: {'products','users','cart_items'} validated frames -> star-schema frames."""
    dim_product = transform_products(valid["products"])
    dim_customer = transform_users(valid["users"])
    fact_sales = transform_sales(valid["cart_items"], dim_product)
    dim_date = build_dim_date(fact_sales)

    frames = {
        "dim_date": dim_date,
        "dim_customer": dim_customer,
        "dim_product": dim_product,
        "fact_sales": fact_sales,
    }
    for name, frame in frames.items():
        logger.info("Transformed %s: %d rows x %d cols", name, len(frame), frame.shape[1])
    return frames


def write_processed(frames: dict[str, pd.DataFrame], processed_dir: Path) -> dict[str, Path]:
    """Write each frame as a timestamped CSV in data/processed/."""
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    paths: dict[str, Path] = {}
    for name, frame in frames.items():
        path = processed_dir / f"{name}_{stamp}.csv"
        frame.to_csv(path, index=False)
        paths[name] = path
        logger.info("Wrote processed %s (%d rows) -> %s", name, len(frame), path)
    return paths
