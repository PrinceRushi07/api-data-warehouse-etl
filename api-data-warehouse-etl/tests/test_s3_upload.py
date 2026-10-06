"""S3 upload tests using moto (no real AWS calls)."""
from datetime import date

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from src.s3_upload import S3UploadError, S3Uploader  # noqa: E402

BUCKET = "test-etl-bucket"


@pytest.fixture
def s3(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def uploader(s3_client, **kw):
    return S3Uploader(BUCKET, prefix="api-dwh", client=s3_client, run_date=date(2026, 10, 6), **kw)


def test_key_layout():
    u = S3Uploader(BUCKET, prefix="/api-dwh/", enabled=False, run_date=date(2026, 10, 6))
    assert u.build_key("raw", "products_1.json", "products") == "api-dwh/raw/products/dt=2026-10-06/products_1.json"
    assert u.build_key("logs", "run.log") == "api-dwh/logs/dt=2026-10-06/run.log"


def test_invalid_category_rejected():
    with pytest.raises(ValueError):
        S3Uploader(BUCKET, enabled=False).build_key("bogus", "x")


def test_disabled_uploader_is_a_noop(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("{}")
    assert S3Uploader("", enabled=False).upload_file(f, "raw", "products") is None


def test_enabled_without_bucket_raises():
    with pytest.raises(ValueError):
        S3Uploader("", enabled=True, client=object())


def test_upload_raw_processed_rejected_logs(s3, tmp_path):
    u = uploader(s3)
    u.verify_bucket()
    files = {}
    for name in ("a.json", "b.csv", "c.csv", "run.log"):
        p = tmp_path / name
        p.write_text("data")
        files[name] = p

    uris = [
        u.upload_file(files["a.json"], "raw", "products"),
        u.upload_file(files["b.csv"], "processed", "fact_sales"),
        u.upload_file(files["c.csv"], "rejected", "products"),
        u.upload_file(files["run.log"], "logs"),
    ]
    assert uris[0] == f"s3://{BUCKET}/api-dwh/raw/products/dt=2026-10-06/a.json"
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert keys == {
        "api-dwh/raw/products/dt=2026-10-06/a.json",
        "api-dwh/processed/fact_sales/dt=2026-10-06/b.csv",
        "api-dwh/rejected/products/dt=2026-10-06/c.csv",
        "api-dwh/logs/dt=2026-10-06/run.log",
    }


def test_upload_files_batch_and_sse(s3, tmp_path):
    p1, p2 = tmp_path / "x.csv", tmp_path / "y.csv"
    p1.write_text("1"), p2.write_text("2")
    uris = uploader(s3).upload_files("processed", {"dim_a": p1, "dim_b": p2})
    assert len(uris) == 2
    head = s3.head_object(Bucket=BUCKET, Key="api-dwh/processed/dim_a/dt=2026-10-06/x.csv")
    assert head["ServerSideEncryption"] == "AES256"


def test_missing_file_raises(s3, tmp_path):
    with pytest.raises(S3UploadError, match="not found"):
        uploader(s3).upload_file(tmp_path / "nope.json", "raw", "products")


def test_missing_bucket_raises_s3_error(s3, tmp_path):
    u = S3Uploader("does-not-exist", client=s3, run_date=date(2026, 10, 6))
    with pytest.raises(S3UploadError):
        u.verify_bucket()
    f = tmp_path / "a.json"
    f.write_text("{}")
    with pytest.raises(S3UploadError):
        u.upload_file(f, "raw", "products")
