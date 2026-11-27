from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from catalog_service.auth import Principal, principal, require_admin
from catalog_service.clients.order_client import (
    OrderServiceRejected,
    OrderServiceUnavailable,
    OrderStockClient,
)
from catalog_service.config import Settings
from catalog_service.database import Base, make_sessions
from catalog_service.errors import DomainError, install_errors
from catalog_service.models import Product
from catalog_service.schemas import (
    ProductCreate,
    ProductListOut,
    ProductOut,
    ProductPriceOut,
    ProductUpdate,
)


def create_app(
    settings: Settings | None = None,
    *,
    session_factory=None,
    engine=None,
    order_client: OrderStockClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    owns_engine = session_factory is None
    if session_factory is None:
        session_factory, engine = make_sessions(settings.catalog_database_url)
    order_client = order_client or OrderStockClient(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auto_create_schema:
            Base.metadata.create_all(engine)
        yield
        if owns_engine and engine is not None:
            engine.dispose()

    app = FastAPI(title="Catalog Service", version="1.0.0", lifespan=lifespan)
    install_errors(app)

    def get_session():
        with session_factory() as session:
            yield session

    DbSession = Annotated[Session, Depends(get_session)]

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(session: DbSession):
        session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/products", response_model=ProductListOut)
    def list_products(
        session: DbSession,
        q: str | None = Query(default=None, max_length=200),
        category: str | None = Query(default=None, max_length=100),
        min_price: int | None = Query(default=None, alias="minPrice", ge=0),
        max_price: int | None = Query(default=None, alias="maxPrice", ge=0),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ):
        if min_price is not None and max_price is not None and min_price > max_price:
            raise DomainError(422, "invalid_price_range", "minPrice не может быть больше maxPrice")

        conditions = [Product.is_active.is_(True)]
        if q:
            conditions.append(Product.name.ilike(f"%{q.strip()}%"))
        if category:
            conditions.append(Product.category == category.strip())
        if min_price is not None:
            conditions.append(Product.price >= min_price)
        if max_price is not None:
            conditions.append(Product.price <= max_price)

        total = session.scalar(select(func.count()).select_from(Product).where(*conditions)) or 0
        items = session.scalars(
            select(Product).where(*conditions)
            .order_by(Product.name, Product.sku)
            .limit(limit).offset(offset)
        ).all()
        return ProductListOut(items=items, total=total, limit=limit, offset=offset)

    @app.get("/products/{sku}", response_model=ProductOut)
    def get_product(sku: str, session: DbSession):
        product = session.get(Product, sku)
        if product is None or not product.is_active:
            raise DomainError(404, "product_not_found", "Товар не найден")
        return product

    @app.get("/internal/products/{sku}/price", response_model=ProductPriceOut)
    def get_price_for_order(sku: str, session: DbSession):
        """Internal REST contract consumed by Order Service."""
        product = session.get(Product, sku)
        if product is None or not product.is_active:
            raise DomainError(404, "product_not_found", "Товар не найден")
        return ProductPriceOut(sku=product.sku, price=product.price, currency=product.currency)

    @app.post("/products", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
    def create_product(
        payload: ProductCreate,
        session: DbSession,
        user: Annotated[Principal, Depends(principal)],
    ):
        require_admin(user)
        product = session.get(Product, payload.sku)
        if product is not None and product.is_active:
            raise DomainError(409, "product_already_exists", "Товар с таким SKU уже существует")

        if product is None:
            product = Product(
                sku=payload.sku,
                name=payload.name,
                category=payload.category,
                price=payload.price,
                currency=payload.currency,
                is_active=False,
            )
            session.add(product)
            session.commit()
        else:
            # A previous attempt may have timed out after Order Service committed the stock.
            # Repeating the same PUT for this SKU preserves the stock originally set.
            if (product.name, product.category, product.price, product.currency) != (
                payload.name, payload.category, payload.price, payload.currency
            ):
                raise DomainError(409, "pending_product_payload_conflict", "Для незавершённого SKU повторите исходные данные")

        if not product.is_active:
            try:
                order_client.set_initial_stock(payload.sku, payload.initial_stock)
            except OrderServiceUnavailable:
                raise DomainError(
                    503, "order_service_unavailable",
                    "Не удалось подтвердить начальный остаток в Order Service; товар сохранён как неактивный. Повторите тот же запрос.",
                )
            except OrderServiceRejected as exc:
                status_code = exc.status_code if exc.status_code in {409, 422} else 502
                raise DomainError(
                    status_code, "initial_stock_rejected",
                    "Order Service отклонил установку начального остатка",
                )
            product.is_active = True
            session.commit()

        return product

    @app.patch("/products/{sku}", response_model=ProductOut)
    def update_product(
        sku: str,
        payload: ProductUpdate,
        session: DbSession,
        user: Annotated[Principal, Depends(principal)],
    ):
        require_admin(user)
        product = session.get(Product, sku)
        if product is None or not product.is_active:
            raise DomainError(404, "product_not_found", "Товар не найден")
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise DomainError(422, "empty_update", "Не переданы поля для изменения")
        for field, value in changes.items():
            if value is not None:
                setattr(product, field, value)
        session.commit()
        session.refresh(product)
        return product

    return app


app = create_app()
