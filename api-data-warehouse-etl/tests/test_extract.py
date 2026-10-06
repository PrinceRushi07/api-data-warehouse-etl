"""API / extraction tests: retries, timeouts, error handling and raw JSON storage."""
import json

import pytest
import requests

from src import extract
from src.extract import ApiClient, ExtractionError
from tests.conftest import FakeResponse, FakeSession


def client(session, retries=3):
    return ApiClient("https://api.example.com", timeout=1, max_retries=retries, backoff_seconds=0.01,
                     session=session, sleep=lambda s: None)


def test_get_json_success():
    session = FakeSession([FakeResponse(200, [{"id": 1}])])
    assert client(session).get_json("/products") == [{"id": 1}]
    assert len(session.calls) == 1


def test_retries_on_500_then_succeeds():
    session = FakeSession([FakeResponse(500), FakeResponse(503), FakeResponse(200, [{"id": 1}])])
    assert client(session).get_json("/products") == [{"id": 1}]
    assert len(session.calls) == 3


def test_retries_on_timeout_then_succeeds():
    session = FakeSession([requests.Timeout("slow"), FakeResponse(200, [])])
    assert client(session).get_json("/carts") == []
    assert len(session.calls) == 2


def test_retries_on_connection_error():
    session = FakeSession([requests.ConnectionError("dns"), FakeResponse(200, [{"id": 9}])])
    assert client(session).get_json("/users") == [{"id": 9}]


def test_gives_up_after_max_retries():
    session = FakeSession([requests.Timeout("slow")])
    with pytest.raises(ExtractionError, match="failed after 4 attempts"):
        client(session, retries=3).get_json("/products")
    assert len(session.calls) == 4


def test_non_retryable_4xx_fails_fast():
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(ExtractionError, match="non-retryable status 404"):
        client(session).get_json("/nope")
    assert len(session.calls) == 1


def test_invalid_json_is_retried_then_fails():
    session = FakeSession([FakeResponse(200, bad_json=True)])
    with pytest.raises(ExtractionError):
        client(session, retries=1).get_json("/products")
    assert len(session.calls) == 2


def test_rate_limit_honours_retry_after():
    slept = []
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, [])])
    c = ApiClient("https://x", session=session, max_retries=2, sleep=slept.append)
    c.get_json("/products")
    assert slept == [7.0]


def test_extract_entity_saves_timestamped_raw_json(tmp_path, products):
    session = FakeSession([FakeResponse(200, products)])
    result = extract.extract_entity(client(session), "products", tmp_path, "run-1")

    assert result.record_count == 3
    assert result.path.parent == tmp_path / "products"
    assert result.path.name.startswith("products_") and result.path.suffix == ".json"
    saved = json.loads(result.path.read_text())
    assert saved["_meta"]["run_id"] == "run-1"
    assert saved["_meta"]["record_count"] == 3
    assert len(saved["data"]) == 3


def test_raw_files_are_timestamped_and_unique(tmp_path, products):
    c = client(FakeSession([FakeResponse(200, products)]))
    p1 = extract.extract_entity(c, "products", tmp_path, "r").path
    p2 = extract.extract_entity(c, "products", tmp_path, "r").path
    assert p1 != p2


def test_passwords_are_redacted_before_storage(tmp_path, users):
    c = client(FakeSession([FakeResponse(200, users)]))
    result = extract.extract_entity(c, "users", tmp_path, "r")
    assert all("password" not in u for u in result.records)
    assert "secret" not in result.path.read_text()


def test_rejects_non_list_payload(tmp_path):
    c = client(FakeSession([FakeResponse(200, {"error": "oops"})]))
    with pytest.raises(ExtractionError, match="expected a JSON array"):
        extract.extract_entity(c, "products", tmp_path, "r")


def test_empty_dimension_payload_is_an_error_but_empty_carts_are_fine(tmp_path):
    c = client(FakeSession([FakeResponse(200, [])]))
    with pytest.raises(ExtractionError, match="empty dataset"):
        extract.extract_entity(c, "products", tmp_path, "r")
    assert extract.extract_entity(c, "carts", tmp_path, "r").record_count == 0
