from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from catalog_service.config import settings


class Base(DeclarativeBase):
    pass


def make_sessions(database_url: str | None = None):
    engine = create_engine(
        database_url or settings.catalog_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5} if (database_url or settings.catalog_database_url).startswith("postgresql") else {},
    )
    return sessionmaker(engine, expire_on_commit=False), engine
