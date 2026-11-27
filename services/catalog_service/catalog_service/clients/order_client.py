import hashlib
import time

import httpx

from catalog_service.config import Settings


class OrderServiceUnavailable(Exception):
    pass


class OrderServiceRejected(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Order Service rejected request: {status_code}")


class OrderStockClient:
    """Calls the Order-owned endpoint; stock is never stored or modified in Catalog DB."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def idempotency_key(sku: str) -> str:
        # Stable across retries and process restarts. Order Service must deduplicate this key
        # and reject a changed body for the same key.
        digest = hashlib.sha256(sku.encode("utf-8")).hexdigest()[:32]
        return f"catalog-initial-stock-{digest}"

    def set_initial_stock(self, sku: str, quantity: int) -> None:
        url = self.settings.order_service_url.rstrip("/") + self.settings.order_initial_stock_path
        headers = {"Idempotency-Key": self.idempotency_key(sku)}
        payload = {"sku": sku, "quantity": quantity}
        attempts = self.settings.order_retries + 1

        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=self.settings.order_timeout) as client:
                    response = client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise OrderServiceUnavailable from exc

            if response.status_code in {200, 201, 204}:
                return
            if response.status_code >= 500 and attempt + 1 < attempts:
                time.sleep(0.1 * (attempt + 1))
                continue
            if response.status_code >= 500:
                raise OrderServiceUnavailable
            raise OrderServiceRejected(response.status_code)
