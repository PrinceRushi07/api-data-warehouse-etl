"""Data quality tests (in-memory checks). Database checks are covered by the integration test."""
import pytest

from src import quality_checks as q
from src import transform as t
from src import validate as v


@pytest.fixture
def validation(payloads):
    return v.validate_all(payloads)


@pytest.fixture
def frames(validation):
    return t.transform_all(
        {"products": validation["products"].valid, "users": validation["users"].valid,
         "cart_items": validation["cart_items"].valid}
    )


def test_all_checks_pass_on_clean_data(validation, frames):
    results = q.run_pre_load_checks(validation, frames, max_reject_ratio=0.3)
    assert results and not q.has_failures(results)
    assert q.summarize(results)["FAIL"] == 0


def test_record_count_reconciliation_detects_lost_rows(validation):
    validation["products"].input_count += 1
    results = q.check_record_counts(validation)
    failed = [r for r in results if r.failed]
    assert [r.entity for r in failed] == ["products"]


def test_reject_threshold_fails_when_too_many_rejects(payloads):
    for p in payloads["products"]:
        p["price"] = -1
    validation = v.validate_all(payloads)
    results = q.check_reject_threshold(validation, 0.3)
    assert any(r.failed and r.entity == "products" for r in results)


def test_schema_check_detects_missing_column(frames):
    frames["dim_product"] = frames["dim_product"].drop(columns=["price_tier"])
    results = q.check_frame_schema(frames)
    assert any(r.failed and r.entity == "dim_product" for r in results)


def test_duplicate_key_check(frames):
    import pandas as pd

    frames["dim_customer"] = pd.concat([frames["dim_customer"], frames["dim_customer"].iloc[[0]]])
    results = q.check_frame_keys(frames)
    assert any(r.failed and r.check_name == "duplicate_keys" and r.entity == "dim_customer" for r in results)


def test_null_key_check(frames):
    frames["fact_sales"].loc[0, "cart_id"] = None
    results = q.check_frame_keys(frames)
    assert any(r.failed and r.check_name == "null_keys" and r.entity == "fact_sales" for r in results)
