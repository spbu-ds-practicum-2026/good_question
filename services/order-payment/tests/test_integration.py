from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event as sa_event, func, inspect, select, update

from order_payment import payments as pm
from order_payment.cli import migrate
from order_payment.messaging import Event, relay_once
from order_payment.orders import models as m
from order_payment.orders.service import Checkout
from order_payment.shared import DomainError, InvalidEvent, pending_count, utcnow

pytestmark = pytest.mark.integration
REQUEST = Checkout(pickup_point="shop-1")


def prepare(system, user="u", quantity=1, stock=10):
    system.orders.initialize_stock("MILK", stock)
    system.orders.change_cart(user, "MILK", quantity)


def checkout(system, user="u", key="key-1", mode="success"):
    return system.orders.checkout(user, key, Checkout(pickup_point="shop-1", payment_mode=mode))[0]


def outbox_events(sessions, model):
    with sessions() as session:
        return [Event.model_validate(row.body) for row in session.scalars(
            select(model).order_by(model.created_at, model.event_id)
        )]


def complete(system, order):
    command = next(e for e in outbox_events(system.order_sessions, m.Outbox)
                   if str(e.data.order_id) == order["id"] and e.event_type == "payment.requested")
    system.payments.handle(command)
    result = next(e for e in outbox_events(system.payment_sessions, pm.Outbox)
                  if str(e.data.order_id) == order["id"])
    system.orders.handle_payment(result)
    return command, result


def stock(system):
    with system.order_sessions() as session:
        return session.get(m.Stock, "MILK").available


def expire_order(system, order_id):
    with system.order_sessions.begin() as session:
        session.execute(update(m.Order).where(m.Order.id == order_id).values(
            expires_at=func.clock_timestamp() - timedelta(seconds=1)
        ))


def test_migrations_are_repeatable_and_match_models(system):
    for service, sessions, base in [
        ("orders", system.order_sessions, m.Base),
        ("payments", system.payment_sessions, pm.Base),
    ]:
        migrate(sessions, service)
        inspector = inspect(sessions.kw["bind"])
        for table in base.metadata.tables.values():
            assert {c.name for c in table.columns} == {
                col["name"] for col in inspector.get_columns(table.name)
            }


def test_atomic_order_and_reservation(system):
    prepare(system, quantity=2)
    order = checkout(system)
    assert order["total"] == 25100
    assert stock(system) == 8
    with system.order_sessions() as session:
        reservation = session.get(m.Reservation, (order["id"], "MILK"))
        assert (reservation.status, reservation.quantity) == ("ACTIVE", 2)
        now = session.scalar(select(func.clock_timestamp()))
        remaining = (session.get(m.Order, order["id"]).expires_at - now).total_seconds()
        assert 890 < remaining <= 900
        assert pending_count(session, m.Outbox) == 1


def test_insufficient_stock_rolls_back_all_products(system):
    prepare(system, quantity=2, stock=1)
    system.orders.initialize_stock("BREAD", 2)
    system.orders.change_cart("u", "BREAD", 1)
    with pytest.raises(DomainError) as exc:
        checkout(system)
    assert exc.value.code == "insufficient_stock"
    assert stock(system) == 1
    with system.order_sessions() as session:
        assert session.get(m.Stock, "BREAD").available == 2
        for model in [
            m.Order, m.Reservation, m.IdempotencyKey, m.Outbox, m.History, m.Notification
        ]:
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_error_after_decrement_rolls_back_transaction(system):
    prepare(system)
    engine = system.order_sessions.kw["bind"]

    def fail_outbox(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO outbox"):
            raise RuntimeError("injected failure after reservation")

    sa_event.listen(engine, "before_cursor_execute", fail_outbox)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            checkout(system)
    finally:
        sa_event.remove(engine, "before_cursor_execute", fail_outbox)
    assert stock(system) == 10
    with system.order_sessions() as session:
        for model in [m.Order, m.Reservation, m.IdempotencyKey, m.Outbox]:
            assert session.scalar(select(func.count()).select_from(model)) == 0
    assert checkout(system)["status"] == "PENDING_PAYMENT"


def test_parallel_orders_cannot_oversell(system):
    prepare(system, "a", stock=1)
    prepare(system, "b", stock=1)
    barrier = Barrier(2)

    def attempt(user):
        barrier.wait(timeout=5)
        try:
            return system.orders.checkout(user, user, REQUEST)[0]["id"]
        except DomainError as exc:
            assert exc.code == "insufficient_stock"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["a", "b"]))
    assert sum(result is not None for result in results) == 1
    assert stock(system) == 0


def test_concurrent_same_key_creates_one_order(system):
    prepare(system)
    barrier = Barrier(2)

    def attempt(_):
        barrier.wait(timeout=5)
        return system.orders.checkout("u", "same", REQUEST)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results[0] == results[1]
    assert stock(system) == 9
    with system.order_sessions() as session:
        assert session.scalar(select(func.count()).select_from(m.Order)) == 1


def test_replay_returns_original_response_even_after_payment_and_catalog_failure(system):
    prepare(system)
    order = checkout(system)
    complete(system, order)
    assert system.orders.cart("u")["items"] == []

    class UnavailableCatalog:
        def price(self, sku):
            pytest.fail("Replay must not call catalog")

    system.orders.catalog = UnavailableCatalog()
    result, replay = system.orders.checkout("u", "key-1", REQUEST)
    assert replay and result == order
    assert system.orders.get_order(order["id"], "u")["status"] == "PAID"


def test_retry_after_lost_http_response_keeps_single_order(system):
    import httpx
    from fastapi.testclient import TestClient

    from order_payment.orders.api import create_app

    prepare(system)
    app = create_app(sessions=system.order_sessions, catalog=system.catalog)
    headers = {"X-User-Id": "u", "X-User-Role": "user", "Idempotency-Key": "lost-response"}
    lose_first_response = True

    with TestClient(app) as server:
        def forward(request):
            nonlocal lose_first_response
            response = server.request(
                request.method, request.url.path,
                headers=dict(request.headers), content=request.content,
            )
            if lose_first_response:
                assert response.status_code == 201
                lose_first_response = False
                # Commit completed, but the client never received the HTTP response.
                raise httpx.ReadTimeout("Response lost after checkout commit", request=request)
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        transport = httpx.MockTransport(forward)
        with httpx.Client(transport=transport, base_url="http://orders.test") as client:
            with pytest.raises(httpx.ReadTimeout):
                client.post("/orders", headers=headers, json=REQUEST.model_dump())
            retry = client.post("/orders", headers=headers, json=REQUEST.model_dump())
    assert retry.status_code == 201
    assert retry.headers["Idempotency-Replayed"] == "true"
    assert stock(system) == 9
    with system.order_sessions() as session:
        assert session.scalar(select(func.count()).select_from(m.Order)) == 1
        assert session.scalar(select(func.count()).select_from(m.IdempotencyKey)) == 1
        saved = session.scalar(select(m.IdempotencyKey))
        assert retry.json() == saved.response


def test_key_with_different_body_is_422(system):
    prepare(system)
    checkout(system)
    with pytest.raises(DomainError) as exc:
        system.orders.checkout("u", "key-1", Checkout(pickup_point="shop-2"))
    assert exc.value.status == 422


def test_keys_are_scoped_by_user(system):
    prepare(system, "u")
    prepare(system, "v")
    assert checkout(system, "u")["id"] != checkout(system, "v")["id"]


@pytest.mark.parametrize("mode,status,reservation,available", [
    ("success", "PAID", "CONSUMED", 9), ("fail", "CANCELLED", "RELEASED", 10),
])
def test_payment_and_repeated_events(system, mode, status, reservation, available):
    prepare(system)
    order = checkout(system, mode=mode)
    command, result = complete(system, order)
    assert not system.payments.handle(command)
    assert not system.orders.handle_payment(result)
    # Even a new eventId for the same business outcome has no second effect.
    assert system.orders.handle_payment(result.model_copy(update={"event_id": uuid4()}))
    assert system.orders.get_order(order["id"], "u")["status"] == status
    assert stock(system) == available
    with system.order_sessions() as session:
        assert session.get(m.Reservation, (order["id"], "MILK")).status == reservation
        assert session.scalar(select(func.count()).select_from(m.Notification)) == 2


def test_expiration_releases_only_once(system):
    prepare(system)
    order = checkout(system)
    expire_order(system, order["id"])
    assert system.orders.expire() == 1
    assert system.orders.expire() == 0
    assert stock(system) == 10
    assert system.orders.get_order(order["id"], "u")["status"] == "CANCELLED"


def test_late_success_refunds_without_reviving_order(system):
    prepare(system)
    order = checkout(system)
    expire_order(system, order["id"])
    system.orders.expire()
    _, result = complete(system, order)
    system.orders.handle_payment(result.model_copy(update={"event_id": uuid4()}))
    refunds = [e for e in outbox_events(system.order_sessions, m.Outbox)
               if e.event_type == "payment.refund.requested"]
    assert len(refunds) == 1
    assert system.payments.handle(refunds[0])
    assert not system.payments.handle(refunds[0])
    assert system.payments.get(order["id"])["status"] == "REFUNDED"
    assert system.orders.get_order(order["id"], "u")["status"] == "CANCELLED"
    assert stock(system) == 10


def test_payment_checks_deadline_without_expiry_worker(system):
    prepare(system)
    order = checkout(system)
    expire_order(system, order["id"])
    complete(system, order)
    assert system.orders.get_order(order["id"], "u")["refund_requested"]
    assert stock(system) == 10


def test_expiry_racing_payment_is_serialized(system):
    prepare(system)
    order = checkout(system)
    command = outbox_events(system.order_sessions, m.Outbox)[0]
    system.payments.handle(command)
    result = outbox_events(system.payment_sessions, pm.Outbox)[0]
    expire_order(system, order["id"])
    barrier = Barrier(2)

    def expire():
        barrier.wait(timeout=5)
        system.orders.expire()

    def pay():
        barrier.wait(timeout=5)
        system.orders.handle_payment(result)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(expire), pool.submit(pay)]
        for future in futures:
            future.result(timeout=15)
    assert stock(system) == 10
    assert system.orders.get_order(order["id"], "u")["refund_requested"]


def test_modified_cart_lines_survive_payment(system):
    prepare(system)
    order = checkout(system)
    system.orders.change_cart("u", "MILK", 3)
    system.orders.change_cart("u", "BREAD", 2)
    complete(system, order)
    assert {i["sku"]: i["quantity"] for i in system.orders.cart("u")["items"]} == {
        "MILK": 3, "BREAD": 2,
    }


def test_admin_transitions(system):
    prepare(system)
    order = checkout(system)
    with pytest.raises(DomainError):
        system.orders.change_status(order["id"], "PAID")
    with pytest.raises(DomainError):
        system.orders.change_status(order["id"], "READY")
    complete(system, order)
    with pytest.raises(DomainError):
        system.orders.change_status(order["id"], "CANCELLED")
    assert system.orders.change_status(order["id"], "READY")["status"] == "READY"
    assert system.orders.change_status(order["id"], "COMPLETED")["status"] == "COMPLETED"


def test_admin_cancellation_then_late_payment(system):
    prepare(system)
    order = checkout(system)
    system.orders.change_status(order["id"], "CANCELLED")
    complete(system, order)
    assert stock(system) == 10
    assert system.orders.get_order(order["id"], "u")["refund_requested"]


def test_stock_initialization_is_idempotent_after_reservation(system):
    prepare(system)
    checkout(system)
    assert system.orders.initialize_stock("MILK", 10)["available"] == 9
    with pytest.raises(DomainError):
        system.orders.initialize_stock("MILK", 11)


def test_invalid_payment_rolls_back_inbox(system):
    prepare(system)
    order = checkout(system)
    command = outbox_events(system.order_sessions, m.Outbox)[0]
    invalid = Event(
        event_type="payment.completed", data=command.data.model_copy(update={"amount": 1})
    )
    with pytest.raises(InvalidEvent):
        system.orders.handle_payment(invalid)
    with system.order_sessions() as session:
        assert session.get(m.ProcessedEvent, str(invalid.event_id)) is None
    assert system.orders.get_order(order["id"], "u")["status"] == "PENDING_PAYMENT"


def test_cart_changed_during_price_lookup_is_rejected(system):
    prepare(system)
    original_price = system.catalog.price

    class EditingCatalog:
        def price(self, sku):
            with system.order_sessions.begin() as session:
                session.execute(update(m.CartItem).where(m.CartItem.user_id == "u").values(
                    quantity=2, token=str(uuid4())
                ))
            return original_price(sku)

    system.orders.catalog = EditingCatalog()
    with pytest.raises(DomainError) as exc:
        checkout(system)
    assert exc.value.code == "cart_changed"
    assert stock(system) == 10


def test_empty_cart_does_not_create_order(system):
    with pytest.raises(DomainError) as exc:
        checkout(system)
    assert exc.value.code == "empty_cart"


def test_refund_before_payment_prevents_future_charge(system):
    prepare(system)
    checkout(system)
    command = outbox_events(system.order_sessions, m.Outbox)[0]
    refund = Event(event_type="payment.refund.requested", data=command.data)
    system.payments.handle(refund)
    system.payments.handle(command)
    assert system.payments.get(str(command.data.order_id))["status"] == "REFUNDED"
    assert outbox_events(system.payment_sessions, pm.Outbox) == []


def test_outbox_failure_and_recovery_keeps_same_event_id(system):
    prepare(system)
    checkout(system)

    class Publisher:
        broken = True
        sent = []

        def publish(self, body, routing_key, event_id):
            if self.broken:
                raise ConnectionError("broker stopped")
            self.sent.append((body, event_id))

    publisher = Publisher()
    assert relay_once(system.order_sessions, m.Outbox, publisher)
    with system.order_sessions.begin() as session:
        row = session.scalar(select(m.Outbox))
        assert row.published_at is None
        assert row.attempts == 1
        original_id = row.event_id
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
    publisher.broken = False
    assert relay_once(system.order_sessions, m.Outbox, publisher)
    assert publisher.sent[0][1] == original_id
    with system.order_sessions() as session:
        assert pending_count(session, m.Outbox) == 0


def test_api_cart_orders_history_and_ownership(system):
    from fastapi.testclient import TestClient

    from order_payment.orders.api import create_app

    system.orders.initialize_stock("MILK", 10)
    app = create_app(sessions=system.order_sessions, catalog=system.catalog)
    headers = {"X-User-Id": "u", "X-User-Role": "user"}
    with TestClient(app) as client:
        assert client.post("/cart/items", headers=headers, json={"sku": "MILK", "quantity": 1}
                           ).status_code == 204
        assert client.put("/cart/items/MILK", headers=headers, json={"quantity": 2}
                          ).status_code == 204
        assert client.get("/cart", headers=headers).json()["total"] == 25100
        response = client.post("/orders", headers={**headers, "Idempotency-Key": "api-key"},
                               json=REQUEST.model_dump())
        assert response.status_code == 201
        order = response.json()
        assert len(client.get(f'/orders/{order["id"]}/history', headers=headers).json()) == 1
        assert client.get(f'/orders/{order["id"]}', headers={
            **headers, "X-User-Id": "other",
        }).status_code == 404
        replay = client.post("/orders", headers={**headers, "Idempotency-Key": "api-key"},
                             json=REQUEST.model_dump())
        assert replay.json() == order
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert client.delete("/cart/items/MILK", headers=headers).status_code == 204
        assert client.get("/cart", headers=headers).json()["items"] == []
