import json
import logging
from contextlib import contextmanager
from datetime import timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4

import pika
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from order_payment.shared import InvalidEvent, utcnow

log = logging.getLogger(__name__)


EventType = Literal[
    "payment.requested", "payment.completed", "payment.failed", "payment.refund.requested"
]


class PaymentData(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    order_id: UUID = Field(alias="orderId")
    amount: int = Field(gt=0, le=10**15)
    currency: Literal["RUB"] = "RUB"
    payment_mode: Literal["success", "fail"] = Field(default="success", alias="paymentMode")


class Event(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4, alias="eventId")
    event_type: EventType = Field(alias="eventType")
    occurred_at: AwareDatetime = Field(default_factory=utcnow, alias="occurredAt")
    data: PaymentData

    def wire(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


def enqueue(session, outbox_model: type, event: Event) -> None:
    session.add(
        outbox_model(
            event_id=str(event.event_id), event_type=event.event_type, body=event.wire()
        )
    )

EXCHANGE = "payments.events"


DEAD_EXCHANGE = "payments.dead"


DEAD_QUEUE = "payments.dead"


QUEUES = {
    "orders.payments": ("payment.completed", "payment.failed"),
    "payments.commands": ("payment.requested", "payment.refund.requested"),
}


@contextmanager
def connection(settings):
    parameters = pika.URLParameters(settings.rabbitmq_url)
    parameters.socket_timeout = settings.broker_timeout
    parameters.stack_timeout = settings.broker_timeout + 2
    parameters.blocked_connection_timeout = settings.broker_timeout
    parameters.connection_attempts = 1
    parameters.heartbeat = 30
    conn = pika.BlockingConnection(parameters)
    try:
        yield conn
    finally:
        if conn.is_open:
            conn.close()


class RabbitPublisher:
    def __init__(self, settings):
        self.settings = settings

    def publish(self, body: dict, routing_key: str, event_id: str) -> None:
        with connection(self.settings) as conn:
            channel = conn.channel()
            channel.confirm_delivery()
            channel.basic_publish(
                exchange=EXCHANGE, routing_key=routing_key,
                body=json.dumps(body).encode(), mandatory=True,
                properties=pika.BasicProperties(
                    content_type="application/json", delivery_mode=2, message_id=event_id
                ),
            )


def consume_once(settings, queue: str, handler) -> bool:
    with connection(settings) as conn:
        channel = conn.channel()
        channel.basic_qos(prefetch_count=1)
        method, properties, body = channel.basic_get(queue=queue, auto_ack=False)
        if method is None:
            return False
        try:
            event = Event.model_validate_json(body)
            if event.event_type not in QUEUES[queue] or method.routing_key != event.event_type:
                raise InvalidEvent("Event type does not match route")
            if properties.message_id != str(event.event_id):
                raise InvalidEvent("Message ID does not match eventId")
            handler(event)
        except (ValidationError, InvalidEvent) as exc:
            log.warning("Rejected invalid message: %s", exc)
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
        except Exception:
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            raise
        else:
            # The handler has already committed inbox + business effect + new outbox.
            channel.basic_ack(delivery_tag=method.delivery_tag)
        return True

class Publisher(Protocol):
    def publish(self, body: dict, routing_key: str, event_id: str) -> None: ...


def relay_once(sessions, outbox_model, publisher: Publisher, retry_seconds: int = 3) -> bool:
    """One row per transaction; publish may repeat if the process dies before commit."""
    with sessions.begin() as session:
        row = session.scalar(select(outbox_model).where(
            outbox_model.published_at.is_(None),
            outbox_model.next_attempt_at <= func.clock_timestamp(),
        ).order_by(outbox_model.created_at, outbox_model.event_id)
            .limit(1).with_for_update(skip_locked=True))
        if row is None:
            return False
        row.attempts += 1
        try:
            publisher.publish(row.body, row.event_type, row.event_id)
        except Exception as exc:
            row.last_error = str(exc)[:500]
            row.next_attempt_at = session.scalar(select(func.clock_timestamp())) + timedelta(
                seconds=retry_seconds
            )
            log.warning("Publish failed for %s: %s", row.event_id, type(exc).__name__)
        else:
            row.published_at = session.scalar(select(func.clock_timestamp()))
            row.last_error = None
        return True
