import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, Integer, String, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    orders_database_url: str = "postgresql+psycopg://demo:demo@localhost:5432/orders"
    payments_database_url: str = "postgresql+psycopg://demo:demo@localhost:5433/payments"
    rabbitmq_url: str = "amqp://demo:demo@localhost:5672/%2F"
    catalog_url: str = "http://localhost:8001"
    catalog_timeout: float = Field(default=2.0, gt=0, le=30)
    worker_interval: float = Field(default=1.0, gt=0, le=60)
    broker_timeout: float = Field(default=5.0, gt=0, le=30)
    outbox_retry_seconds: int = Field(default=3, ge=1, le=300)

def utcnow() -> datetime:
    return datetime.now(UTC)


def make_sessions(url: str) -> sessionmaker[Session]:
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=15000 -c lock_timeout=10000",
        },
    )
    return sessionmaker(engine, expire_on_commit=False)


def advisory_lock(session: Session, namespace: str, key: str) -> None:
    # Transaction-scoped and shared by all replicas; collisions only serialize extra work.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{namespace}:{key}"},
    )


class OutboxColumns:
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500))


class ProcessedColumns:
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def process_once(session: Session, model: type, event_id: str, action: Callable[[], None]) -> bool:
    inserted = session.scalar(
        insert(model).values(event_id=event_id).on_conflict_do_nothing().returning(model.event_id)
    )
    if inserted is None:
        return False
    action()
    return True


def pending_count(session: Session, model: type) -> int:
    query = select(func.count()).select_from(model).where(model.published_at.is_(None))
    return session.scalar(query)

class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class InvalidEvent(Exception):
    """A valid envelope whose business fields conflict with the stored operation."""

@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str


def principal(
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
) -> Principal:
    if not x_user_id or not x_user_role:
        raise DomainError(401, "unauthorized", "Требуются заголовки доверенного Gateway")
    if len(x_user_id) > 128 or not x_user_id.strip():
        raise DomainError(401, "unauthorized", "Некорректный идентификатор пользователя")
    if x_user_role not in {"user", "admin"}:
        raise DomainError(403, "forbidden", "Неизвестная роль")
    return Principal(x_user_id, x_user_role)


def require_admin(user: Principal) -> None:
    if user.role != "admin":
        raise DomainError(403, "forbidden", "Требуется роль admin")


def install_errors(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {
            "code": "validation_error", "message": "Некорректные параметры запроса",
            "fields": [
                {"location": list(err["loc"]), "message": err["msg"]} for err in exc.errors()
            ],
        }})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        log.exception("Database operation failed")
        return JSONResponse(status_code=503, content={"error": {
            "code": "database_unavailable", "message": "База данных временно недоступна",
        }})
