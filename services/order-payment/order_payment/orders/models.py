from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from order_payment.shared import OutboxColumns, ProcessedColumns


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Cart(Base):
    __tablename__ = "carts"
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (CheckConstraint("quantity > 0 AND quantity <= 10000"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("carts.user_id"), primary_key=True)
    sku: Mapped[str] = mapped_column(String(100), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    token: Mapped[str] = mapped_column(String(36), default=new_id)


class Stock(Base):
    __tablename__ = "stock"
    __table_args__ = (
        CheckConstraint("available >= 0"),
        CheckConstraint("initial_quantity >= 0"),
    )
    sku: Mapped[str] = mapped_column(String(100), primary_key=True)
    available: Mapped[int] = mapped_column(Integer)
    initial_quantity: Mapped[int] = mapped_column(Integer)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total > 0"),
        CheckConstraint("currency = 'RUB'"),
        CheckConstraint("status IN ('PENDING_PAYMENT', 'PAID', 'READY', 'COMPLETED', 'CANCELLED')"),
        CheckConstraint("payment_mode IN ('success', 'fail')"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_PAYMENT", index=True)
    total: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    payment_mode: Mapped[str] = mapped_column(String(10))
    pickup_point: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    refund_requested: Mapped[bool] = mapped_column(default=False, server_default="false")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (CheckConstraint("quantity > 0"), CheckConstraint("unit_price > 0"))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    sku: Mapped[str] = mapped_column(String(100), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(BigInteger)
    cart_token: Mapped[str] = mapped_column(String(36))


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0"),
        CheckConstraint("status IN ('ACTIVE', 'CONSUMED', 'RELEASED')"),
    )
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("stock.sku"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    response: Mapped[dict] = mapped_column(JSONB)


class History(Base):
    __tablename__ = "order_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    message: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Outbox(OutboxColumns, Base):
    __tablename__ = "outbox"


class ProcessedEvent(ProcessedColumns, Base):
    __tablename__ = "processed_events"
