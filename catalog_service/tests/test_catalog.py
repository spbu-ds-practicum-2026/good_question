import os
os.environ.setdefault("CATALOG_DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from catalog_service.app import create_app
from catalog_service.config import Settings
from catalog_service.database import Base


class FakeOrderClient:
    def __init__(self):
        self.calls = []
        self.fail = False

    def set_initial_stock(self, sku: str, quantity: int):
        self.calls.append((sku, quantity))
        if self.fail:
            from catalog_service.clients.order_client import OrderServiceUnavailable
            raise OrderServiceUnavailable


def make_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=__import__("sqlalchemy").pool.StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    fake_order = FakeOrderClient()
    settings = Settings(
        catalog_database_url="sqlite://",
        auto_create_schema=False,
        order_service_url="http://order.test",
    )
    app = create_app(settings, session_factory=sessions, engine=engine, order_client=fake_order)
    return TestClient(app), fake_order


def admin_headers():
    return {"X-User-Id": "admin-1", "X-User-Role": "admin"}


def test_product_crud_search_and_price():
    client, order = make_client()
    response = client.post("/products", headers=admin_headers(), json={
        "sku": "MILK-01", "name": "Молоко", "category": "Молочные продукты",
        "price": 90, "currency": "RUB", "initial_stock": 0,
    })
    assert response.status_code == 201
    assert response.json()["price"] == 90

    assert client.get("/products?q=Мол",).json()["total"] == 1
    assert client.get("/internal/products/MILK-01/price").json() == {
        "sku": "MILK-01", "price": 90, "currency": "RUB"
    }
    assert client.patch("/products/MILK-01", headers=admin_headers(), json={"price": 95}).json()["price"] == 95
    assert order.calls == [("MILK-01", 0)]


def test_initial_stock_is_confirmed_before_product_becomes_visible():
    client, order = make_client()
    order.fail = True
    response = client.post("/products", headers=admin_headers(), json={
        "sku": "BREAD-01", "name": "Хлеб", "category": "Хлеб",
        "price": 50, "initial_stock": 7,
    })
    assert response.status_code == 503
    assert client.get("/products/BREAD-01").status_code == 404

    order.fail = False
    response = client.post("/products", headers=admin_headers(), json={
        "sku": "BREAD-01", "name": "Хлеб", "category": "Хлеб",
        "price": 50, "initial_stock": 7,
    })
    assert response.status_code == 201
    assert client.get("/products/BREAD-01").status_code == 200
    assert order.calls == [("BREAD-01", 7), ("BREAD-01", 7)]


def test_admin_required_and_validation():
    client, _ = make_client()
    payload = {"sku": "EGG-01", "name": "Яйца", "category": "Яйца", "price": -1}
    assert client.post("/products", json=payload).status_code == 401
    assert client.post("/products", headers=admin_headers(), json=payload).status_code == 422
