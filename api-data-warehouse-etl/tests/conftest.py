"""Shared fixtures: realistic sample API payloads and a scriptable fake HTTP session."""
from __future__ import annotations

import copy
from typing import Any

import pytest
import requests


def make_products() -> list[dict]:
    return [
        {"id": 1, "title": "  Fjallraven Backpack ", "price": 109.95, "description": "Your perfect pack",
         "category": "Men's Clothing", "image": "https://img/1.jpg", "rating": {"rate": 3.9, "count": 120}},
        {"id": 2, "title": "Slim Fit T-Shirt", "price": 22.3, "description": "Slim-fitting style",
         "category": "men's clothing", "image": "https://img/2.jpg", "rating": {"rate": 4.7, "count": 259}},
        {"id": 3, "title": "Gold Ring", "price": 695, "description": "Classic ring",
         "category": "jewelery", "image": "https://img/3.jpg", "rating": {"rate": 4.6, "count": 400}},
    ]


def make_users() -> list[dict]:
    return [
        {"id": 1, "email": "John@Gmail.com", "username": "JohnD", "password": "secret",
         "name": {"firstname": "john", "lastname": "doe"}, "phone": "1-570-236-7033",
         "address": {"city": "kilcoole", "street": "new road", "number": 7682, "zipcode": "12926-3874",
                     "geolocation": {"lat": "-37.3159", "long": "81.1496"}}},
        {"id": 2, "email": "morrison@gmail.com", "username": "mor_2314", "password": "x",
         "name": {"firstname": "david", "lastname": "morrison"}, "phone": "1-570-236-7033",
         "address": {"city": "kilcoole", "street": "Lovers Ln", "number": 7267, "zipcode": "12926-3874",
                     "geolocation": {"lat": "-37.3159", "long": "81.1496"}}},
    ]


def make_carts() -> list[dict]:
    return [
        {"id": 1, "userId": 1, "date": "2020-03-02T00:00:00.000Z",
         "products": [{"productId": 1, "quantity": 4}, {"productId": 2, "quantity": 1}]},
        {"id": 2, "userId": 2, "date": "2020-03-04T00:00:00.000Z",
         "products": [{"productId": 3, "quantity": 2}, {"productId": 1, "quantity": 1}]},
        {"id": 3, "userId": 1, "date": "2020-03-10T00:00:00.000Z",
         "products": [{"productId": 2, "quantity": 3}]},
    ]


@pytest.fixture
def products() -> list[dict]:
    return make_products()


@pytest.fixture
def users() -> list[dict]:
    return make_users()


@pytest.fixture
def carts() -> list[dict]:
    return make_carts()


@pytest.fixture
def payloads() -> dict[str, list[dict]]:
    return {"products": make_products(), "users": make_users(), "carts": make_carts()}


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None, headers: dict | None = None, bad_json: bool = False):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return copy.deepcopy(self._json)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    """Plays back a script of responses/exceptions in order; the last item repeats."""

    def __init__(self, script: list[Any] | dict[str, Any]):
        self.script = script
        self.headers: dict = {}
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if isinstance(self.script, dict):  # route by endpoint suffix
            for suffix, item in self.script.items():
                if url.endswith(suffix):
                    return self._play(item)
            raise AssertionError(f"unexpected url {url}")
        idx = min(len(self.calls) - 1, len(self.script) - 1)
        return self._play(self.script[idx])

    @staticmethod
    def _play(item):
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_session_factory():
    return FakeSession


@pytest.fixture
def api_session(payloads) -> FakeSession:
    """A well-behaved API serving the sample payloads."""
    return FakeSession({
        "/products": FakeResponse(200, payloads["products"]),
        "/users": FakeResponse(200, payloads["users"]),
        "/carts": FakeResponse(200, payloads["carts"]),
    })
