import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from order_payment.messaging import Event, PaymentData, enqueue
from order_payment.orders import models as m
from order_payment.shared import DomainError, InvalidEvent, advisory_lock, process_once


class Checkout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pickup_point: str = Field(min_length=1, max_length=200)
    payment_mode: Literal["success", "fail"] = "success"

    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class CartLine:
    sku: str
    quantity: int
    token: str


class OrderService:
    def __init__(self, sessions, catalog):
        self.sessions = sessions
        self.catalog = catalog

    @staticmethod
    def _cart(session, user: str) -> list[CartLine]:
        rows = session.scalars(
            select(m.CartItem).where(m.CartItem.user_id == user).order_by(m.CartItem.sku)
        )
        return [CartLine(row.sku, row.quantity, row.token) for row in rows]

    def change_cart(self, user: str, sku: str, quantity: int | None, *, add=False) -> None:
        if quantity is not None:
            self.catalog.price(sku)
        with self.sessions.begin() as session:
            advisory_lock(session, "cart", user)
            session.execute(insert(m.Cart).values(user_id=user).on_conflict_do_nothing())
            item = session.get(m.CartItem, (user, sku))
            if quantity is None:
                if item:
                    session.delete(item)
                return
            if item is None:
                count = session.scalar(
                    select(func.count()).select_from(m.CartItem).where(m.CartItem.user_id == user)
                )
                if count >= 50:
                    raise DomainError(409, "cart_limit", "В корзине допускается не более 50 SKU")
                session.add(m.CartItem(user_id=user, sku=sku, quantity=quantity))
            else:
                updated = item.quantity + quantity if add else quantity
                if updated > 10000:
                    raise DomainError(422, "quantity_limit", "Максимум 10000 единиц одного SKU")
                item.quantity = updated
                item.token = str(uuid4())

    def cart(self, user: str) -> dict:
        with self.sessions() as session:
            lines = self._cart(session, user)
        items = []
        for line in lines:
            price = self.catalog.price(line.sku)
            items.append({"sku": line.sku, "quantity": line.quantity, "unit_price": price.amount})
        return {
            "items": items,
            "total": sum(item["quantity"] * item["unit_price"] for item in items),
            "currency": "RUB",
        }

    def initialize_stock(self, sku: str, quantity: int) -> dict:
        with self.sessions.begin() as session:
            session.execute(
                insert(m.Stock)
                .values(sku=sku, available=quantity, initial_quantity=quantity)
                .on_conflict_do_nothing()
            )
            stock = session.get(m.Stock, sku)
            if stock.initial_quantity != quantity:
                raise DomainError(409, "stock_already_initialized", "Начальный остаток уже задан")
            return {"sku": sku, "available": stock.available, "initial_quantity": quantity}

    @staticmethod
    def _replay(session, user: str, key: str, fingerprint: str) -> dict | None:
        record = session.get(m.IdempotencyKey, (user, key))
        if record is None:
            return None
        if record.request_hash != fingerprint:
            raise DomainError(422, "idempotency_conflict", "Ключ уже использован с другим телом")
        return record.response

    def checkout(self, user: str, key: str, request: Checkout) -> tuple[dict, bool]:
        fingerprint = request.fingerprint()
        with self.sessions() as session:
            previous = self._replay(session, user, key, fingerprint)
            if previous is not None:
                return previous, True
            snapshot = self._cart(session, user)
        if not snapshot:
            raise DomainError(409, "empty_cart", "Корзина пуста")
        # No locks/connections are held during the network calls.
        prices = {line.sku: self.catalog.price(line.sku).amount for line in snapshot}
        with self.sessions.begin() as session:
            advisory_lock(session, "checkout", f"{user}:{key}")
            previous = self._replay(session, user, key, fingerprint)
            if previous is not None:
                return previous, True
            advisory_lock(session, "cart", user)
            if self._cart(session, user) != snapshot:
                raise DomainError(409, "cart_changed", "Корзина изменилась; повторите оформление")
            now = session.scalar(select(func.clock_timestamp()))
            order = m.Order(
                user_id=user,
                status="PENDING_PAYMENT",
                total=sum(prices[line.sku] * line.quantity for line in snapshot),
                currency="RUB",
                payment_mode=request.payment_mode,
                pickup_point=request.pickup_point,
                expires_at=now + timedelta(minutes=15),
            )
            session.add(order)
            session.flush()
            # Stable SKU order avoids deadlocks for overlapping multi-item orders.
            for line in snapshot:
                reserved = session.scalar(
                    update(m.Stock)
                    .where(m.Stock.sku == line.sku, m.Stock.available >= line.quantity)
                    .values(available=m.Stock.available - line.quantity)
                    .returning(m.Stock.sku)
                )
                if reserved is None:
                    raise DomainError(409, "insufficient_stock", f"Недостаточно товара {line.sku}")
                session.add(
                    m.OrderItem(
                        order_id=order.id, sku=line.sku, quantity=line.quantity,
                        unit_price=prices[line.sku], cart_token=line.token,
                    )
                )
                session.add(
                    m.Reservation(
                        order_id=order.id, sku=line.sku, quantity=line.quantity, status="ACTIVE"
                    )
                )
            self._record(session, order, "checkout")
            enqueue(
                session, m.Outbox, Event(event_type="payment.requested", data=self._data(order))
            )
            session.flush()
            result = self._serialize(session, order)
            session.add(
                m.IdempotencyKey(
                    user_id=user, key=key, request_hash=fingerprint,
                    order_id=order.id, response=result,
                )
            )
            return result, False

    @staticmethod
    def _data(order) -> PaymentData:
        return PaymentData(
            order_id=order.id, amount=order.total,
            currency=order.currency, payment_mode=order.payment_mode,
        )

    @staticmethod
    def _record(session, order, reason: str) -> None:
        session.add(m.History(order_id=order.id, status=order.status, reason=reason))
        session.add(
            m.Notification(
                user_id=order.user_id, order_id=order.id,
                message=f"Заказ {order.id}: {order.status} ({reason})",
            )
        )

    @staticmethod
    def _serialize(session, order) -> dict:
        items = session.scalars(
            select(m.OrderItem).where(m.OrderItem.order_id == order.id).order_by(m.OrderItem.sku)
        )
        return {
            "id": order.id, "user_id": order.user_id, "status": order.status,
            "total": order.total, "currency": order.currency,
            "pickup_point": order.pickup_point,
            "expires_at": order.expires_at.isoformat(),
            "created_at": order.created_at.isoformat(),
            "refund_requested": order.refund_requested,
            "items": [
                {"sku": item.sku, "quantity": item.quantity, "unit_price": item.unit_price}
                for item in items
            ],
        }

    @staticmethod
    def _get_order(session, order_id: str, user: str | None = None, *, lock=False):
        query = select(m.Order).where(m.Order.id == order_id)
        if user is not None:
            query = query.where(m.Order.user_id == user)
        if lock:
            query = query.with_for_update()
        order = session.scalar(query)
        if order is None:
            raise DomainError(404, "order_not_found", "Заказ не найден")
        return order

    def get_order(self, order_id: str, user: str | None) -> dict:
        with self.sessions() as session:
            return self._serialize(session, self._get_order(session, order_id, user))

    def list_orders(self, user: str | None, limit: int, offset: int) -> list[dict]:
        with self.sessions() as session:
            query = select(m.Order).order_by(m.Order.created_at.desc(), m.Order.id)
            if user is not None:
                query = query.where(m.Order.user_id == user)
            return [self._serialize(session, row) for row in session.scalars(
                query.limit(limit).offset(offset)
            )]

    def history(self, order_id: str, user: str | None) -> list[dict]:
        with self.sessions() as session:
            self._get_order(session, order_id, user)
            query = select(m.History).where(m.History.order_id == order_id).order_by(
                m.History.created_at, m.History.id
            )
            rows = session.scalars(query)
            return [
                {
                    "status": row.status, "reason": row.reason,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def notifications(self, user: str, limit: int, offset: int) -> list[dict]:
        with self.sessions() as session:
            rows = session.scalars(select(m.Notification).where(m.Notification.user_id == user)
                .order_by(m.Notification.created_at.desc(), m.Notification.id)
                .limit(limit).offset(offset))
            return [
                {"id": row.id, "order_id": row.order_id, "message": row.message,
                 "created_at": row.created_at.isoformat()}
                for row in rows
            ]

    @staticmethod
    def _reservations(session, order):
        return list(session.scalars(select(m.Reservation).where(
            m.Reservation.order_id == order.id, m.Reservation.status == "ACTIVE"
        ).order_by(m.Reservation.sku).with_for_update()))

    def _cancel(self, session, order, reason: str) -> None:
        if order.status != "PENDING_PAYMENT":
            return
        for reservation in self._reservations(session, order):
            session.execute(update(m.Stock).where(m.Stock.sku == reservation.sku).values(
                available=m.Stock.available + reservation.quantity
            ))
            reservation.status = "RELEASED"
        order.status = "CANCELLED"
        self._record(session, order, reason)

    def change_status(self, order_id: str, status: str) -> dict:
        if status == "PAID":
            raise DomainError(409, "manual_payment_forbidden", "Оплата возможна только событием")
        with self.sessions.begin() as session:
            order = self._get_order(session, order_id, lock=True)
            if status == "CANCELLED" and order.status == "PENDING_PAYMENT":
                self._cancel(session, order, "admin_cancelled")
            elif (order.status, status) in {("PAID", "READY"), ("READY", "COMPLETED")}:
                order.status = status
                self._record(session, order, "admin_status")
            else:
                raise DomainError(409, "invalid_transition", f"Недопустим переход в {status}")
            return self._serialize(session, order)

    def expire(self, batch_size: int = 100) -> int:
        count = 0
        for _ in range(batch_size):
            # One order per transaction: stock locks never span multiple SKU sequences.
            with self.sessions.begin() as session:
                order = session.scalar(select(m.Order).where(
                    m.Order.status == "PENDING_PAYMENT",
                    m.Order.expires_at <= func.clock_timestamp(),
                ).order_by(m.Order.expires_at, m.Order.id).limit(1)
                    .with_for_update(skip_locked=True))
                if order is None:
                    break
                self._cancel(session, order, "reservation_expired")
                count += 1
        return count

    def handle_payment(self, event: Event) -> bool:
        if event.event_type not in {"payment.completed", "payment.failed"}:
            raise InvalidEvent("Order Service received an unsupported event")
        with self.sessions.begin() as session:
            return process_once(
                session, m.ProcessedEvent, str(event.event_id),
                lambda: self._apply_payment(session, event),
            )

    def _apply_payment(self, session, event: Event) -> None:
        order = session.scalar(select(m.Order).where(
            m.Order.id == str(event.data.order_id)
        ).with_for_update())
        if order is None:
            raise InvalidEvent("Unknown order")
        if (order.total, order.currency, order.payment_mode) != (
            event.data.amount, event.data.currency, event.data.payment_mode
        ):
            raise InvalidEvent("Payment fields do not match order")
        now = session.scalar(select(func.clock_timestamp()))
        if order.status == "PENDING_PAYMENT" and order.expires_at <= now:
            self._cancel(session, order, "reservation_expired")
        if event.event_type == "payment.failed":
            self._cancel(session, order, "payment_failed")
            return
        if order.status == "CANCELLED":
            if not order.refund_requested:
                enqueue(session, m.Outbox, Event(
                    event_type="payment.refund.requested", data=self._data(order)
                ))
                order.refund_requested = True
                session.add(m.Notification(
                    user_id=order.user_id, order_id=order.id,
                    message=f"Заказ {order.id}: запрошен возврат запоздавшей оплаты",
                ))
            return
        if order.status != "PENDING_PAYMENT":
            return
        advisory_lock(session, "cart", order.user_id)
        for reservation in self._reservations(session, order):
            reservation.status = "CONSUMED"
        for item in session.scalars(select(m.OrderItem).where(m.OrderItem.order_id == order.id)):
            # Tokens keep edits/re-added lines made after checkout out of this deletion.
            session.execute(delete(m.CartItem).where(
                m.CartItem.user_id == order.user_id, m.CartItem.sku == item.sku,
                m.CartItem.token == item.cart_token,
            ))
        order.status = "PAID"
        self._record(session, order, "payment_completed")
