import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    price: int = Field(gt=0, le=10**9)
    currency: Literal["RUB"] = "RUB"
    initial_stock: int = Field(ge=0, le=10**9)

    @field_validator("sku")
    @classmethod
    def validate_sku(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}", value):
            raise ValueError("SKU должен содержать 2–64 символа: латинские буквы, цифры, '.', '_' или '-'")
        return value

    @field_validator("name", "category")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Поле не может быть пустым")
        return value


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    price: int | None = Field(default=None, gt=0, le=10**9)
    currency: Literal["RUB"] | None = None

    @field_validator("name", "category")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Поле не может быть пустым")
        return value


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sku: str
    name: str
    category: str
    price: int
    currency: str


class ProductPriceOut(BaseModel):
    sku: str
    price: int
    currency: Literal["RUB"]


class ProductListOut(BaseModel):
    items: list[ProductOut]
    total: int
    limit: int
    offset: int
