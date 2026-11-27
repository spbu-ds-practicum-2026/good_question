from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI
from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from order_payment.messaging import Event, enqueue
from order_payment.shared import (
    DomainError, InvalidEvent, OutboxColumns, Principal, ProcessedColumns, Settings,
    advisory_lock, install_errors, make_sessions, principal, process_once, require_admin,
)


class Base(DeclarativeBase):
    pass


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0"),
        CheckConstraint("currency = 'RUB'"),
        CheckConstraint("status IN ('COMPLETED', 'FAILED', 'REFUNDED')"),
        CheckConstraint("payment_mode IN ('success', 'fail')"),
    )
    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    payment_mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Outbox(OutboxColumns, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedColumns, Base):
    __tablename__ = "processed_events"

class PaymentService:
    def __init__(self, sessions):
        self.sessions = sessions

    def handle(self, event: Event) -> bool:
        if event.event_type not in {"payment.requested", "payment.refund.requested"}:
            raise InvalidEvent("Payment Mock received an unsupported command")
        with self.sessions.begin() as session:
            return process_once(
                session, ProcessedEvent, str(event.event_id),
                lambda: self._apply(session, event),
            )

    @staticmethod
    def _apply(session, event: Event) -> None:
        data = event.data
        order_id = str(data.order_id)
        advisory_lock(session, "payment", order_id)
        payment = session.get(Payment, order_id)
        if payment is not None and (payment.amount, payment.currency, payment.payment_mode) != (
            data.amount, data.currency, data.payment_mode
        ):
            raise InvalidEvent("Repeated payment command has conflicting fields")
        if event.event_type == "payment.refund.requested":
            if payment is None:
                # A tombstone also handles broker delivery out of order: never charge later.
                session.add(Payment(
                    order_id=order_id, amount=data.amount, currency=data.currency,
                    payment_mode=data.payment_mode, status="REFUNDED",
                ))
            elif payment.status == "COMPLETED":
                payment.status = "REFUNDED"
            return
        if payment is not None:
            return
        success = data.payment_mode == "success"
        session.add(Payment(
            order_id=order_id, amount=data.amount, currency=data.currency,
            payment_mode=data.payment_mode, status="COMPLETED" if success else "FAILED",
        ))
        enqueue(session, Outbox, Event(
            event_type="payment.completed" if success else "payment.failed", data=data
        ))

    def get(self, order_id: str) -> dict | None:
        with self.sessions() as session:
            payment = session.scalar(select(Payment).where(Payment.order_id == order_id))
            if payment is None:
                return None
            return {
                "order_id": payment.order_id, "amount": payment.amount,
                "currency": payment.currency, "status": payment.status,
            }

def create_app(settings: Settings | None = None, *, sessions=None) -> FastAPI:
    settings = settings or Settings()
    owns_sessions = sessions is None
    sessions = sessions if sessions is not None else make_sessions(settings.payments_database_url)
    service = PaymentService(sessions)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owns_sessions:
            sessions.kw["bind"].dispose()

    app = FastAPI(title="Payment Mock", version="0.1.0", lifespan=lifespan)
    install_errors(app)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with sessions() as session:
            session.execute(text("SELECT 1 FROM schema_migrations LIMIT 1"))
        return {"status": "ok"}

    @app.get("/payments/{order_id}")
    def get_payment(order_id: UUID, user: Annotated[Principal, Depends(principal)]):
        require_admin(user)
        result = service.get(str(order_id))
        if result is None:
            raise DomainError(404, "payment_not_found", "Платёж не найден")
        return result

    return app
