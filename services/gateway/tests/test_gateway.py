import asyncio
import unittest
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.tokens import issue_token

KEY = "local-tests-key-which-is-longer-than-32-bytes"


def auth(role="user", key=KEY, hours=1):
    token = issue_token(key, "test-user", role, timedelta(hours=hours))
    return {"Authorization": f"Bearer {token}"}


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(key=KEY, timeout=0.1)
        self.client = self.enterContext(TestClient(self.app))

    def use_upstream(self, handler):
        transport = httpx.MockTransport(handler)
        self.app.state.client = httpx.AsyncClient(transport=transport)
        self.addCleanup(asyncio.run, self.app.state.client.aclose())

    def test_signature_expiration_and_role(self):
        headers_list = [{}, auth(key="#" * 32), auth(hours=-1)]
        for headers in headers_list:
            response = self.client.get("/products", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "unauthorized")
        response = self.client.patch("/products/MILK-001", headers=auth())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/cart", headers=auth("guest")).status_code, 403)

    def test_admin_routes_and_trusted_headers(self):
        def upstream(request):
            self.assertEqual(request.url.host, "catalog")
            self.assertEqual(request.url.path, "/products/MILK-001")
            self.assertEqual(request.url.params["tag"], "A+B")
            self.assertEqual(request.headers.get_list("x-user-id"), ["test-user"])
            self.assertEqual(request.headers.get_list("x-user-role"), ["admin"])
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(request.content, b'{"price":90}')
            return httpx.Response(200, json={"price": 90})

        self.use_upstream(upstream)
        response = self.client.patch(
            "/products/MILK-001?tag=A%2BB",
            headers={**auth("admin"), "X-User-Id": "another-user", "X-User-Role": "user"},
            json={"price": 90},
        )
        self.assertEqual(response.json(), {"price": 90})

    def test_idempotency_headers_and_internal_routes(self):
        def upstream(request):
            self.assertEqual(request.url.host, "order-api")
            self.assertEqual(request.headers["Idempotency-Key"], "order-1")
            return httpx.Response(201, headers={"Idempotency-Replayed": "true"})

        self.use_upstream(upstream)
        response = self.client.post("/orders", headers={**auth(), "Idempotency-Key": "order-1"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Idempotency-Replayed"], "true")
        for path in ("/internal/products/MILK-001/price", "/orders/%2e%2e", "/products%3Fhidden"):
            self.assertEqual(self.client.get(path, headers=auth("admin")).status_code, 404)

    def test_upstream_errors(self):
        error = {"error": {"code": "sold_out", "message": "Товара нет"}}
        self.use_upstream(lambda request: httpx.Response(409, json=error))
        response = self.client.post("/orders", headers=auth())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), error)

        def unavailable(request):
            raise httpx.ConnectError("Connection refused", request=request)

        self.use_upstream(unavailable)
        response = self.client.get("/products", headers=auth())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "upstream_unavailable")

    def test_total_timeout(self):
        async def slow(request):
            await asyncio.sleep(5)
            return httpx.Response(200)

        self.use_upstream(slow)
        response = self.client.get("/products", headers=auth())
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "upstream_timeout")


if __name__ == "__main__":
    unittest.main()
