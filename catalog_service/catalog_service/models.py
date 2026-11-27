from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from catalog_service.database import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price > 0", name="ck_products_price_positive"),
        CheckConstraint("currency = 'RUB'", name="ck_products_currency_rub"),
        Index("ix_products_category", "category"),
        Index("ix_products_price", "price"),
        Index("ix_products_name", "name"),
    )

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    # Integer RUB matches PaymentData.amount (int) in Maxim's Payment Mock.
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB", server_default="RUB")
    # Inactive products are hidden until Order Service confirms initial stock setup.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
