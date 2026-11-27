import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from order_payment.cli import migrate
from order_payment.orders.service import OrderService
from order_payment.payments import PaymentService


@dataclass(frozen=True)
class FakePrice:
    sku: str
    amount: int
    currency: str = "RUB"


class FakeCatalog:
    def price(self, sku: str) -> FakePrice:
        return FakePrice(sku, 12550)


@dataclass
class System:
    orders: OrderService
    payments: PaymentService
    order_sessions: sessionmaker
    payment_sessions: sessionmaker
    catalog: FakeCatalog


@pytest.fixture
def database_factory():
    resources = []

    def create(service):
        variable = f"TEST_{service.upper()}_DATABASE_URL"
        url = os.environ.get(variable)
        if not url:
            pytest.skip(f"Set {variable} to run PostgreSQL integration tests")
        if not url.startswith("postgresql+psycopg://"):
            pytest.fail(f"{variable} must use real PostgreSQL with psycopg")
        schema = f"test_{uuid4().hex}"
        admin = create_engine(url, connect_args={"connect_timeout": 5})
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={
            "connect_timeout": 5,
            "options": f"-csearch_path={schema} -cstatement_timeout=10000 -clock_timeout=8000",
        })
        resources.append((engine, admin, schema))
        sessions = sessionmaker(engine, expire_on_commit=False)
        migrate(sessions, service)
        return sessions

    yield create
    for engine, admin, schema in reversed(resources):
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def system(database_factory):
    orders = database_factory("orders")
    payments = database_factory("payments")
    catalog = FakeCatalog()
    return System(
        OrderService(orders, catalog), PaymentService(payments), orders, payments, catalog
    )
