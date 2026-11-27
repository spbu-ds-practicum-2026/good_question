import time
from urllib.parse import quote

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

    def set_initial_stock(self, sku: str, quantity: int) -> None:
        # Order stores the original quantity per SKU, so repeating the PUT never increases stock.
        path = self.settings.order_initial_stock_path.format(sku=quote(sku, safe=""))
        url = self.settings.order_service_url.rstrip("/") + path
        headers = {"X-User-Id": "service-catalog", "X-User-Role": "admin"}
        payload = {"quantity": quantity}
        attempts = self.settings.order_retries + 1

        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=self.settings.order_timeout, trust_env=False) as client:
                    response = client.put(url, json=payload, headers=headers)
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
