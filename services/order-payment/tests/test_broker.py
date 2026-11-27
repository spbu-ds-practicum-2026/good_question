import os
import time
from uuid import uuid4

import pytest

from order_payment import messaging as broker
from order_payment.shared import Settings
from order_payment.messaging import Event, PaymentData

pytestmark = pytest.mark.broker


def _declare_test_topology(channel) -> None:
    # Only isolated test resources; production topology belongs to infrastructure.
    channel.exchange_declare(exchange=broker.EXCHANGE, exchange_type="direct", durable=True)
    channel.exchange_declare(exchange=broker.DEAD_EXCHANGE, exchange_type="fanout", durable=True)
    channel.queue_declare(queue=broker.DEAD_QUEUE, durable=True)
    channel.queue_bind(queue=broker.DEAD_QUEUE, exchange=broker.DEAD_EXCHANGE)
    for queue, keys in broker.QUEUES.items():
        channel.queue_declare(
            queue=queue, durable=True,
            arguments={"x-dead-letter-exchange": broker.DEAD_EXCHANGE},
        )
        for key in keys:
            channel.queue_bind(queue=queue, exchange=broker.EXCHANGE, routing_key=key)


@pytest.fixture
def isolated_broker(monkeypatch):
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("Set TEST_RABBITMQ_URL to run real RabbitMQ tests")
    prefix = f"test.{uuid4().hex}"
    queues = {
        f"{prefix}.orders": ("payment.completed", "payment.failed"),
        f"{prefix}.payments": ("payment.requested", "payment.refund.requested"),
    }
    monkeypatch.setattr(broker, "EXCHANGE", f"{prefix}.events")
    monkeypatch.setattr(broker, "DEAD_EXCHANGE", f"{prefix}.dead.exchange")
    monkeypatch.setattr(broker, "DEAD_QUEUE", f"{prefix}.dead")
    monkeypatch.setattr(broker, "QUEUES", queues)
    settings = Settings(rabbitmq_url=url)
    with broker.connection(settings) as conn:
        _declare_test_topology(conn.channel())
    try:
        yield settings, f"{prefix}.payments"
    finally:
        with broker.connection(settings) as conn:
            channel = conn.channel()
            for queue in [*queues, broker.DEAD_QUEUE]:
                channel.queue_delete(queue=queue)
            channel.exchange_delete(exchange=broker.EXCHANGE)
            channel.exchange_delete(exchange=broker.DEAD_EXCHANGE)


def test_confirmed_publication_and_redelivery_after_handler_failure(isolated_broker):
    settings, queue = isolated_broker
    event = Event(event_type="payment.requested", data=PaymentData(order_id=uuid4(), amount=100))
    broker.RabbitPublisher(settings).publish(event.wire(), event.event_type, str(event.event_id))
    deliveries = []

    def failing_handler(message):
        deliveries.append(message.event_id)
        raise RuntimeError("database temporarily unavailable")

    with pytest.raises(RuntimeError):
        broker.consume_once(settings, queue, failing_handler)
    assert broker.consume_once(settings, queue, lambda message: deliveries.append(message.event_id))
    assert deliveries == [event.event_id, event.event_id]
    assert not broker.consume_once(settings, queue, lambda _: pytest.fail("Queue should be empty"))


@pytest.mark.integration
def test_full_outbox_pipeline_with_duplicate_delivery(isolated_broker, system):
    from sqlalchemy import func, select

    from order_payment.messaging import relay_once
    from order_payment.orders import models as orders
    from order_payment.orders.service import Checkout
    from order_payment import payments

    settings, payment_queue = isolated_broker
    order_queue = next(q for q, keys in broker.QUEUES.items() if "payment.completed" in keys)
    publisher = broker.RabbitPublisher(settings)
    system.orders.initialize_stock("MILK", 1)
    system.orders.change_cart("u", "MILK", 1)
    order, _ = system.orders.checkout("u", "real-broker", Checkout(pickup_point="shop-1"))
    assert relay_once(system.order_sessions, orders.Outbox, publisher)
    assert broker.consume_once(settings, payment_queue, system.payments.handle)

    with system.order_sessions() as session:
        row = session.scalar(select(orders.Outbox))
        publisher.publish(row.body, row.event_type, row.event_id)
    assert broker.consume_once(settings, payment_queue, system.payments.handle)
    assert relay_once(system.payment_sessions, payments.Outbox, publisher)
    assert broker.consume_once(settings, order_queue, system.orders.handle_payment)

    with system.payment_sessions() as session:
        row = session.scalar(select(payments.Outbox))
        publisher.publish(row.body, row.event_type, row.event_id)
        assert session.scalar(select(func.count()).select_from(payments.Payment)) == 1
        assert session.scalar(select(func.count()).select_from(payments.Outbox)) == 1
    assert broker.consume_once(settings, order_queue, system.orders.handle_payment)
    assert system.orders.get_order(order["id"], "u")["status"] == "PAID"
    assert system.orders.cart("u")["items"] == []
    with system.order_sessions() as session:
        assert session.get(orders.Stock, "MILK").available == 0
        assert session.scalar(select(func.count()).select_from(orders.Notification)) == 2


def test_invalid_message_is_dead_lettered(isolated_broker):
    settings, queue = isolated_broker
    with broker.connection(settings) as conn:
        channel = conn.channel()
        channel.basic_publish(exchange=broker.EXCHANGE, routing_key="payment.requested", body=b"{}")
    assert broker.consume_once(settings, queue, lambda _: pytest.fail("Invalid event was handled"))
    with broker.connection(settings) as conn:
        channel = conn.channel()
        deadline = time.monotonic() + 3
        while True:
            method, _, body = channel.basic_get(queue=broker.DEAD_QUEUE, auto_ack=True)
            if method is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        assert method is not None
        assert body == b"{}"
