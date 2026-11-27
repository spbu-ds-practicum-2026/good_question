from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import text

from order_payment.orders.service import Checkout, OrderService
from order_payment.shared import (
    DomainError, Principal, Settings, install_errors, make_sessions, principal, require_admin,
)


SKU = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")]


class CartChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: int = Field(gt=0, le=10000, strict=True)


class CartAdd(CartChange):
    sku: SKU


class StockInit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: int = Field(ge=0, le=2_000_000_000, strict=True)


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["PENDING_PAYMENT", "PAID", "READY", "COMPLETED", "CANCELLED"]


@dataclass(frozen=True)
class Price:
    sku: str
    amount: int
    currency: str = "RUB"


class CatalogClient:
    def __init__(self, base_url: str, timeout: float = 2.0, transport=None):
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self.client.close()

    def price(self, sku: str) -> Price:
        try:
            response = self.client.get(f"/internal/products/{quote(sku, safe='')}/price")
        except httpx.TimeoutException as exc:
            raise DomainError(504, "catalog_timeout", "Catalog Service не ответил вовремя") from exc
        except httpx.RequestError as exc:
            raise DomainError(503, "catalog_unavailable", "Catalog Service недоступен") from exc
        if response.status_code == 404:
            raise DomainError(409, "product_not_found", f"Товар {sku} не найден в каталоге")
        if response.status_code != 200:
            raise DomainError(503, "catalog_unavailable", "Ошибка ответа Catalog Service")
        try:
            data = response.json()
            price = Decimal(str(data["price"]))
            if (
                data["sku"] != sku
                or data["currency"] != "RUB"
                or not price.is_finite()
                or price <= 0
                or price > Decimal("10000000")
                or price * 100 != (price * 100).to_integral_value()
            ):
                raise ValueError("Invalid price")
            return Price(sku, int(price * 100))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise DomainError(
                503, "catalog_invalid_response", "Некорректная цена от Catalog"
            ) from exc

User = Annotated[Principal, Depends(principal)]


Limit = Annotated[int, Query(ge=1, le=100)]


Offset = Annotated[int, Query(ge=0)]


def create_app(settings: Settings | None = None, *, sessions=None, catalog=None) -> FastAPI:
    settings = settings or Settings()
    owns_sessions = sessions is None
    owns_catalog = catalog is None
    sessions = sessions if sessions is not None else make_sessions(settings.orders_database_url)
    catalog = catalog if catalog is not None else CatalogClient(
        settings.catalog_url, settings.catalog_timeout
    )
    service = OrderService(sessions, catalog)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owns_catalog:
            catalog.close()
        if owns_sessions:
            sessions.kw["bind"].dispose()

    app = FastAPI(title="Order Service", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    install_errors(app)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with sessions() as session:
            session.execute(text("SELECT 1 FROM schema_migrations LIMIT 1"))
        return {"status": "ok"}

    @app.get("/cart")
    def cart(user: User):
        return service.cart(user.user_id)

    @app.post("/cart/items", status_code=204)
    def add_cart(body: CartAdd, user: User):
        service.change_cart(user.user_id, body.sku, body.quantity, add=True)
        return Response(status_code=204)

    @app.put("/cart/items/{sku}", status_code=204)
    def set_cart(sku: SKU, body: CartChange, user: User):
        service.change_cart(user.user_id, sku, body.quantity)
        return Response(status_code=204)

    @app.delete("/cart/items/{sku}", status_code=204)
    def delete_cart(sku: SKU, user: User):
        service.change_cart(user.user_id, sku, None)
        return Response(status_code=204)

    @app.put("/stock/{sku}/initial")
    def initialize_stock(sku: SKU, body: StockInit, user: User):
        require_admin(user)
        return service.initialize_stock(sku, body.quantity)

    @app.get("/stock/{sku}")
    def get_stock(sku: SKU, user: User):
        from order_payment.orders.models import Stock

        with sessions() as session:
            row = session.get(Stock, sku)
            if row is None:
                raise DomainError(404, "stock_not_found", "Остаток не найден")
            return {"sku": row.sku, "available": row.available}

    @app.post("/orders", status_code=201)
    def checkout(
        body: Checkout, user: User, response: Response,
        idempotency_key: Annotated[
            str, Header(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
        ],
    ):
        result, replayed = service.checkout(user.user_id, idempotency_key, body)
        response.headers["Idempotency-Replayed"] = str(replayed).lower()
        return result

    @app.get("/orders")
    def list_orders(user: User, limit: Limit = 20, offset: Offset = 0):
        return service.list_orders(None if user.role == "admin" else user.user_id, limit, offset)

    @app.get("/orders/{order_id}")
    def get_order(order_id: UUID, user: User):
        return service.get_order(str(order_id), None if user.role == "admin" else user.user_id)

    @app.get("/orders/{order_id}/history")
    def history(order_id: UUID, user: User):
        return service.history(str(order_id), None if user.role == "admin" else user.user_id)

    @app.patch("/orders/{order_id}/status")
    def change_status(order_id: UUID, body: StatusChange, user: User):
        require_admin(user)
        return service.change_status(str(order_id), body.status)

    @app.get("/notifications")
    def notifications(user: User, limit: Limit = 20, offset: Offset = 0):
        return service.notifications(user.user_id, limit, offset)

    return app
