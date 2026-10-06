"""End-to-end test against a real PostgreSQL (skipped unless TEST_DATABASE_URL is set).

    docker compose up -d postgres
    TEST_DATABASE_URL=postgresql+psycopg2://etl_user:change_me@localhost:5432/sales_dwh pytest -m integration
"""
import dataclasses
import os

import pytest

pytestmark = pytest.mark.integration

DB_URL = os.getenv("TEST_DATABASE_URL")
if not DB_URL:
    pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)

from sqlalchemy import create_engine, text  # noqa: E402

from src.config import load_settings  # noqa: E402
from src.extract import ApiClient  # noqa: E402
from src.pipeline import DataQualityError, run_pipeline  # noqa: E402
from src.s3_upload import S3Uploader  # noqa: E402
from tests.conftest import FakeResponse, FakeSession  # noqa: E402

TABLES = ["data_quality_results", "etl_watermark", "etl_run_audit", "agg_customer_summary", "agg_category_sales",
          "agg_daily_sales", "fact_sales", "dim_product", "dim_customer", "dim_date"]


@pytest.fixture
def engine():
    eng = create_engine(DB_URL, future=True)
    with eng.begin() as conn:
        for t in TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
    yield eng
    eng.dispose()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    s = load_settings(env_file=tmp_path / "missing.env")
    return dataclasses.replace(s, data_dir=tmp_path / "data", log_dir=tmp_path / "logs", s3_enabled=False)


def client_for(payloads):
    session = FakeSession({f"/{k}": FakeResponse(200, v) for k, v in payloads.items()})
    return ApiClient("https://api.example.com", session=session, sleep=lambda s: None)


def count(engine, table):
    with engine.connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def test_full_run_loads_star_schema_and_aggregates(engine, settings, payloads):
    summary = run_pipeline(settings, engine=engine, api_client=client_for(payloads),
                           uploader=S3Uploader("", enabled=False))
    assert summary.status == "SUCCESS"
    assert count(engine, "dim_customer") == 2
    assert count(engine, "dim_product") == 3
    assert count(engine, "fact_sales") == 5
    assert count(engine, "dim_date") == 9
    assert count(engine, "agg_daily_sales") == 3
    with engine.connect() as c:
        assert c.execute(text("SELECT SUM(line_total) FROM fact_sales")).scalar() == pytest.approx(
            c.execute(text("SELECT SUM(revenue) FROM agg_daily_sales")).scalar())
        assert c.execute(text("SELECT status FROM etl_run_audit")).scalar() == "SUCCESS"
        assert c.execute(text("SELECT COUNT(*) FROM data_quality_results WHERE status = 'FAIL'")).scalar() == 0
        assert str(c.execute(text("SELECT watermark_value FROM etl_watermark")).scalar()) == "2020-03-10"


def test_rerun_is_idempotent(engine, settings, payloads):
    for _ in range(2):
        run_pipeline(settings, engine=engine, api_client=client_for(payloads), uploader=S3Uploader("", enabled=False))
    assert count(engine, "fact_sales") == 5
    assert count(engine, "dim_customer") == 2
    assert count(engine, "etl_run_audit") == 2


def test_changed_source_data_is_upserted_not_duplicated(engine, settings, payloads):
    run_pipeline(settings, engine=engine, api_client=client_for(payloads), uploader=S3Uploader("", enabled=False))
    payloads["products"][0]["price"] = 120.0
    payloads["carts"][0]["products"][0]["quantity"] = 10
    summary = run_pipeline(settings, engine=engine, api_client=client_for(payloads), full_refresh=True,
                           uploader=S3Uploader("", enabled=False))
    assert summary.loaded["dim_product"]["updated"] == 1
    assert count(engine, "fact_sales") == 5
    with engine.connect() as c:
        qty = c.execute(text("SELECT quantity FROM fact_sales f JOIN dim_product p USING (product_key) "
                             "WHERE f.cart_id = 1 AND p.product_id = 1")).scalar()
    assert qty == 10


def test_bad_records_are_rejected_not_loaded(engine, settings, payloads):
    payloads["products"].append({"id": 4, "title": "Bad", "price": -1, "category": "x"})
    summary = run_pipeline(settings, engine=engine, api_client=client_for(payloads), uploader=S3Uploader("", enabled=False))
    assert summary.rejected["products"] == 1
    assert count(engine, "dim_product") == 3
    assert list((settings.data_dir / "rejected").glob("products_rejected_*.csv"))


def test_quality_gate_blocks_load_and_records_failure(engine, settings, payloads):
    for p in payloads["products"]:
        p["price"] = -1
    with pytest.raises(DataQualityError):
        run_pipeline(settings, engine=engine, api_client=client_for(payloads), uploader=S3Uploader("", enabled=False))
    assert count(engine, "fact_sales") == 0
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM etl_run_audit")).scalar() == "FAILED"
        assert c.execute(text("SELECT COUNT(*) FROM data_quality_results WHERE status = 'FAIL'")).scalar() > 0
