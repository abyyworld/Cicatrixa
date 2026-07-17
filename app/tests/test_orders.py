"""Baseline suite — must stay green after any autonomous patch."""
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_order_no_discount():
    r = client.post("/orders", json={"item": "widget", "quantity": 3, "price_cents": 500})
    assert r.status_code == 200
    assert r.json()["total_cents"] == 1500


def test_order_half_discount():
    r = client.post("/orders", json={
        "item": "widget", "quantity": 2, "price_cents": 1000, "discount_pct": 50,
    })
    assert r.status_code == 200
    assert r.json()["total_cents"] == 1000


def test_get_order_roundtrip():
    r = client.post("/orders", json={"item": "gizmo", "quantity": 1, "price_cents": 250})
    order_id = r.json()["order_id"]
    r2 = client.get(f"/orders/{order_id}")
    assert r2.status_code == 200
    assert r2.json()["item"] == "gizmo"


def test_get_missing_order_404():
    assert client.get("/orders/999999").status_code == 404
