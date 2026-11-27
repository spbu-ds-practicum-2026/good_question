from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from order_payment.messaging import Event, PaymentData
from order_payment.orders.api import CartChange, CatalogClient, create_app
from order_payment.orders.service import Checkout
from order_payment.shared import DomainError


def client_with(handler):
    return CatalogClient("http://catalog.test", transport=httpx.MockTransport(handler))


def test_catalog_price_converts_decimal_to_minor_units():
    catalog = client_with(lambda request: httpx.Response(200, json={
        "sku": "MILK", "price": "125.50", "currency": "RUB",
    }))
    try:
        assert catalog.price("MILK").amount == 12550
    finally:
        catalog.close()


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-1", "0", "1.001", "10000001", None])
def test_catalog_rejects_invalid_prices(price):
    catalog = client_with(lambda request: httpx.Response(200, json={
        "sku": "MILK", "price": price, "currency": "RUB",
    }))
    try:
        with pytest.raises(DomainError) as exc:
            catalog.price("MILK")
        assert exc.value.status == 503
    finally:
        catalog.close()


@pytest.mark.parametrize("status,expected", [(404, 409), (500, 503), (429, 503)])
def test_catalog_http_errors(status, expected):
    catalog = client_with(lambda request: httpx.Response(status))
    try:
        with pytest.raises(DomainError) as exc:
            catalog.price("MILK")
        assert exc.value.status == expected
    finally:
        catalog.close()


@pytest.mark.parametrize("error,expected", [
    (httpx.ReadTimeout, 504), (httpx.ConnectError, 503),
])
def test_catalog_network_errors(error, expected):
    def fail(request):
        raise error("unavailable", request=request)
    catalog = client_with(fail)
    try:
        with pytest.raises(DomainError) as exc:
            catalog.price("MILK")
        assert exc.value.status == expected
    finally:
        catalog.close()


def test_request_hash_is_canonical_and_includes_payment_mode():
    original = Checkout(pickup_point="shop-1")
    assert original.fingerprint() == Checkout.model_validate({
        "payment_mode": "success", "pickup_point": "shop-1",
    }).fingerprint()
    assert original.fingerprint() != Checkout(
        pickup_point="shop-1", payment_mode="fail"
    ).fingerprint()


@pytest.mark.parametrize("quantity", [0, -1, 10001, 1.5, True, "1"])
def test_quantity_validation(quantity):
    with pytest.raises(ValidationError):
        CartChange(quantity=quantity)


def test_event_round_trip_has_stable_id():
    event = Event(event_type="payment.requested", data=PaymentData(order_id=uuid4(), amount=100))
    assert Event.model_validate(event.wire()) == event
    assert event.wire()["eventId"] == str(event.event_id)


def test_health_and_authentication_do_not_require_database():
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/orders").status_code == 401
        response = client.get("/orders", headers={"X-User-Id": "u", "X-User-Role": "other"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        assert client.put("/stock/MILK/initial", json={"quantity": 1}, headers={
            "X-User-Id": "u", "X-User-Role": "user",
        }).status_code == 403
        assert client.post("/orders", json={"pickup_point": "shop-1"}, headers={
            "X-User-Id": "u", "X-User-Role": "user",
        }).status_code == 422
