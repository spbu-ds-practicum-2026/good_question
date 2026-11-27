from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    catalog_database_url: str = "postgresql+psycopg://demo:demo@localhost:5434/catalog"
    host: str = "0.0.0.0"
    port: int = Field(default=8001, ge=1, le=65535)
    auto_create_schema: bool = True

    # Order Service owns stock. Catalog only calls its idempotent initialization endpoint.
    order_service_url: str = "http://localhost:8002"
    order_initial_stock_path: str = "/stock/{sku}/initial"
    order_timeout: float = Field(default=2.0, gt=0, le=30)
    order_retries: int = Field(default=1, ge=0, le=2)


settings = Settings()
