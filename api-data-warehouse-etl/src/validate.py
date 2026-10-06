"""Validation: required fields, nulls, data types, ranges, duplicates, referential checks.

Every record ends up in exactly one of two outputs:
  * ``valid``    - clean DataFrame handed to the transform step
  * ``rejected`` - DataFrame(entity, reject_reason, raw_record) written to data/rejected/
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
REJECT_COLUMNS = ["entity", "reject_reason", "raw_record"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    dtype: str  # int | float | str | datetime | list
    required: bool = True
    gt: float | None = None
    ge: float | None = None
    le: float | None = None
    pattern: str | None = None


@dataclass
class ValidationResult:
    entity: str
    input_count: int
    valid: pd.DataFrame
    rejected: pd.DataFrame

    @property
    def valid_count(self) -> int:
        return len(self.valid)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def reject_ratio(self) -> float:
        return self.rejected_count / self.input_count if self.input_count else 0.0

    def summary(self) -> str:
        return (
            f"{self.entity}: input={self.input_count} valid={self.valid_count} "
            f"rejected={self.rejected_count} ({self.reject_ratio:.1%})"
        )


# --------------------------------------------------------------------------- specs
PRODUCT_SPECS = [
    FieldSpec("id", "int", gt=0),
    FieldSpec("title", "str"),
    FieldSpec("price", "float", gt=0),
    FieldSpec("category", "str"),
    FieldSpec("description", "str", required=False),
    FieldSpec("image", "str", required=False),
    FieldSpec("rating_rate", "float", required=False, ge=0, le=5),
    FieldSpec("rating_count", "int", required=False, ge=0),
]

USER_SPECS = [
    FieldSpec("id", "int", gt=0),
    FieldSpec("email", "str", pattern=EMAIL_RE),
    FieldSpec("username", "str"),
    FieldSpec("name_firstname", "str"),
    FieldSpec("name_lastname", "str"),
    FieldSpec("phone", "str", required=False),
    FieldSpec("address_city", "str"),
    FieldSpec("address_street", "str", required=False),
    FieldSpec("address_number", "int", required=False),
    FieldSpec("address_zipcode", "str", required=False),
    FieldSpec("address_geolocation_lat", "float", required=False, ge=-90, le=90),
    FieldSpec("address_geolocation_long", "float", required=False, ge=-180, le=180),
]

CART_SPECS = [
    FieldSpec("id", "int", gt=0),
    FieldSpec("userId", "int", gt=0),
    FieldSpec("date", "datetime"),
    FieldSpec("products", "list"),
]

CART_ITEM_SPECS = [
    FieldSpec("cart_id", "int", gt=0),
    FieldSpec("user_id", "int", gt=0),
    FieldSpec("order_date", "datetime"),
    FieldSpec("product_id", "int", gt=0),
    FieldSpec("quantity", "int", gt=0),
]


# --------------------------------------------------------------------------- helpers
class _Reasons:
    """Collects rejection reasons per row index."""

    def __init__(self, index: pd.Index) -> None:
        self._reasons: dict[Any, list[str]] = {i: [] for i in index}

    def add(self, mask: pd.Series, message: str) -> None:
        mask = mask.fillna(False).astype(bool)
        for idx in mask.index[mask]:
            if message not in self._reasons[idx]:
                self._reasons[idx].append(message)

    def failed(self, index: pd.Index) -> pd.Series:
        return pd.Series([bool(self._reasons[i]) for i in index], index=index, dtype=bool)

    def joined(self, index: pd.Index) -> pd.Series:
        return pd.Series(["; ".join(self._reasons[i]) for i in index], index=index, dtype=object)


def _is_blank(series: pd.Series) -> pd.Series:
    def blank(v: Any) -> bool:
        if isinstance(v, (list, dict)):
            return len(v) == 0
        if isinstance(v, str):
            return v.strip() == ""
        if v is None:
            return True
        try:
            return bool(pd.isna(v))
        except (TypeError, ValueError):
            return False

    return series.map(blank).astype(bool)


def _to_numeric(series: pd.Series) -> pd.Series:
    cleaned = series.map(lambda v: np.nan if isinstance(v, (bool, list, dict)) else v)
    return pd.to_numeric(cleaned, errors="coerce")


def _type_ok(series: pd.Series, dtype: str) -> pd.Series:
    if dtype == "int":
        num = _to_numeric(series)
        return num.notna() & (num == num.round())
    if dtype == "float":
        return _to_numeric(series).notna()
    if dtype == "str":
        return series.map(lambda v: isinstance(v, str))
    if dtype == "datetime":
        parsed = pd.to_datetime(series.astype(str), errors="coerce", utc=True, format="ISO8601")
        return parsed.notna()
    if dtype == "list":
        return series.map(lambda v: isinstance(v, list))
    raise ValueError(f"Unknown dtype '{dtype}'")


def _apply_specs(df: pd.DataFrame, specs: list[FieldSpec], reasons: _Reasons) -> None:
    for spec in specs:
        col = df[spec.name]
        blank = _is_blank(col)

        if spec.required:
            reasons.add(blank, f"missing_required:{spec.name}")

        present = ~blank
        bad_type = present & ~_type_ok(col, spec.dtype)
        reasons.add(bad_type, f"invalid_type:{spec.name}(expected {spec.dtype})")

        if spec.dtype in ("int", "float"):
            num = _to_numeric(col)
            checked = present & ~bad_type
            if spec.gt is not None:
                reasons.add(checked & ~(num > spec.gt), f"out_of_range:{spec.name}(must be > {spec.gt})")
            if spec.ge is not None:
                reasons.add(checked & ~(num >= spec.ge), f"out_of_range:{spec.name}(must be >= {spec.ge})")
            if spec.le is not None:
                reasons.add(checked & ~(num <= spec.le), f"out_of_range:{spec.name}(must be <= {spec.le})")

        if spec.pattern:
            rx = re.compile(spec.pattern)
            is_str = present & col.map(lambda v: isinstance(v, str))
            no_match = is_str & ~col.map(lambda v: isinstance(v, str) and bool(rx.match(v.strip())))
            reasons.add(no_match, f"invalid_format:{spec.name}")


def _prepare(entity: str, records: list[Any], specs: list[FieldSpec]) -> tuple[pd.DataFrame, list[dict]]:
    """Flatten dict records; non-dict records are rejected immediately."""
    good: list[dict] = []
    rejected: list[dict] = []
    for rec in records:
        if isinstance(rec, dict):
            good.append(rec)
        else:
            rejected.append(
                {"entity": entity, "reject_reason": "record_not_an_object", "raw_record": json.dumps(rec, default=str)}
            )

    df = pd.json_normalize(good, sep="_") if good else pd.DataFrame()
    df = df.reset_index(drop=True)
    df["_raw"] = [json.dumps(r, default=str, sort_keys=True) for r in good]
    for spec in specs:
        if spec.name not in df.columns:
            df[spec.name] = pd.Series([None] * len(df), dtype=object)
    return df, rejected


def _validate_frame(
    entity: str,
    df: pd.DataFrame,
    specs: list[FieldSpec],
    key: list[str],
    pre_rejected: list[dict],
    input_count: int,
) -> ValidationResult:
    reasons = _Reasons(df.index)
    _apply_specs(df, specs, reasons)

    # Duplicate detection: first occurrence wins, later ones are rejected.
    # (rows that already failed other checks do not "occupy" the key).
    candidate = ~reasons.failed(df.index)
    for k in key:
        candidate &= ~_is_blank(df[k])
    key_frame = df[key].astype(str)
    dup = pd.Series(False, index=df.index)
    dup[candidate] = key_frame[candidate].duplicated(keep="first")
    reasons.add(dup, f"duplicate_key:{'+'.join(key)}")

    failed = reasons.failed(df.index)
    rejected_df = df.loc[failed, ["_raw"]].copy()
    rejected_df["entity"] = entity
    rejected_df["reject_reason"] = reasons.joined(df.index)[failed]
    rejected_df = rejected_df.rename(columns={"_raw": "raw_record"})[REJECT_COLUMNS]
    if pre_rejected:
        rejected_df = pd.concat([pd.DataFrame(pre_rejected, columns=REJECT_COLUMNS), rejected_df], ignore_index=True)

    valid_df = df.loc[~failed].drop(columns=["_raw"]).reset_index(drop=True)
    result = ValidationResult(entity, input_count, valid_df, rejected_df.reset_index(drop=True))
    logger.info(result.summary())
    return result


# --------------------------------------------------------------------------- public API
def validate_products(records: list[Any]) -> ValidationResult:
    df, pre = _prepare("products", records, PRODUCT_SPECS)
    return _validate_frame("products", df, PRODUCT_SPECS, ["id"], pre, len(records))


def validate_users(records: list[Any]) -> ValidationResult:
    df, pre = _prepare("users", records, USER_SPECS)
    return _validate_frame("users", df, USER_SPECS, ["id"], pre, len(records))


def validate_carts(records: list[Any]) -> tuple[ValidationResult, ValidationResult]:
    """Validate cart headers, then explode and validate their line items.

    Returns (carts_result, cart_items_result).
    """
    df, pre = _prepare("carts", records, CART_SPECS)
    carts = _validate_frame("carts", df, CART_SPECS, ["id"], pre, len(records))

    raw_lines: list[dict] = []
    for _, cart in carts.valid.iterrows():
        for line in cart["products"]:
            is_obj = isinstance(line, dict)
            row = {
                "cart_id": cart["id"],
                "user_id": cart["userId"],
                "order_date": cart["date"],
                "product_id": line.get("productId") if is_obj else None,
                "quantity": line.get("quantity") if is_obj else None,
            }
            row["_raw"] = json.dumps({**row, "line": line}, default=str, sort_keys=True)
            raw_lines.append(row)

    cols = ["cart_id", "user_id", "order_date", "product_id", "quantity", "_raw"]
    lines = pd.DataFrame(raw_lines, columns=cols).reset_index(drop=True)
    items = _validate_frame("cart_items", lines, CART_ITEM_SPECS, ["cart_id", "product_id"], [], len(lines))
    return carts, items


def validate_references(
    items: ValidationResult, users: ValidationResult, products: ValidationResult
) -> ValidationResult:
    """Referential check: every cart line must point to a valid user and product."""
    if items.valid.empty:
        return items

    df = items.valid.copy()
    known_users = set(pd.to_numeric(users.valid["id"]).astype(int))
    known_products = set(pd.to_numeric(products.valid["id"]).astype(int))
    orphan_user = ~pd.to_numeric(df["user_id"]).astype(int).isin(known_users)
    orphan_product = ~pd.to_numeric(df["product_id"]).astype(int).isin(known_products)

    reason = pd.Series("", index=df.index, dtype=object)
    reason = reason.mask(orphan_user & ~orphan_product, "orphan_reference:user_id")
    reason = reason.mask(orphan_product & ~orphan_user, "orphan_reference:product_id")
    reason = reason.mask(orphan_user & orphan_product, "orphan_reference:user_id; orphan_reference:product_id")
    bad = orphan_user | orphan_product

    if not bad.any():
        return items

    new_rejects = pd.DataFrame(
        {
            "entity": "cart_items",
            "reject_reason": reason[bad].values,
            "raw_record": [json.dumps(r, default=str, sort_keys=True) for r in df.loc[bad].to_dict("records")],
        }
    )[REJECT_COLUMNS]
    result = ValidationResult(
        "cart_items",
        items.input_count,
        df.loc[~bad].reset_index(drop=True),
        pd.concat([items.rejected, new_rejects], ignore_index=True),
    )
    logger.warning("Referential check moved %d cart_items to rejected", int(bad.sum()))
    logger.info(result.summary())
    return result


def validate_all(extracted: dict[str, list[Any]]) -> dict[str, ValidationResult]:
    products = validate_products(extracted["products"])
    users = validate_users(extracted["users"])
    carts, items = validate_carts(extracted["carts"])
    items = validate_references(items, users, products)
    return {"products": products, "users": users, "carts": carts, "cart_items": items}


def write_rejected(results: dict[str, ValidationResult], rejected_dir: Path, run_id: str) -> dict[str, Path]:
    """Write one CSV per entity that has rejected rows."""
    rejected_dir = Path(rejected_dir)
    rejected_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    paths: dict[str, Path] = {}
    for entity, res in results.items():
        if res.rejected.empty:
            continue
        path = rejected_dir / f"{entity}_rejected_{stamp}.csv"
        out = res.rejected.copy()
        out.insert(0, "run_id", run_id)
        out.to_csv(path, index=False)
        paths[entity] = path
        logger.warning("Wrote %d rejected %s records -> %s", len(out), entity, path)
    return paths
