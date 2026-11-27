import asyncio
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from gateway.tokens import ROLES, SUBJECT, signing_key

SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
REQUEST_HEADERS = ("accept", "content-type", "idempotency-key")
RESPONSE_HEADERS = ("content-type", "idempotency-replayed")


@dataclass(frozen=True)
class Route:
    path: re.Pattern[str]
    upstream: str
    methods: frozenset[str]
    admin_methods: frozenset[str] = frozenset()


ROUTES = (
    Route(re.compile(r"/products"), "catalog", frozenset({"GET", "POST"}), frozenset({"POST"})),
    Route(re.compile(f"/products/{SEGMENT}"), "catalog",
          frozenset({"GET", "PATCH"}), frozenset({"PATCH"})),
    Route(re.compile(r"/cart"), "order", frozenset({"GET"})),
    Route(re.compile(r"/cart/items"), "order", frozenset({"POST"})),
    Route(re.compile(f"/cart/items/{SEGMENT}"), "order", frozenset({"PUT", "DELETE"})),
    Route(re.compile(r"/orders"), "order", frozenset({"GET", "POST"})),
    Route(re.compile(f"/orders/{SEGMENT}(?:/history)?"), "order", frozenset({"GET"})),
    Route(re.compile(f"/orders/{SEGMENT}/status"), "order",
          frozenset({"PATCH"}), frozenset({"PATCH"})),
    Route(re.compile(f"/stock/{SEGMENT}"), "order", frozenset({"GET"})),
    Route(re.compile(f"/stock/{SEGMENT}/initial"), "order",
          frozenset({"PUT"}), frozenset({"PUT"})),
    Route(re.compile(r"/notifications"), "order", frozenset({"GET"})),
)


def error(status: int, code: str, message: str, headers=None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def create_app(
    *, key: str | None = None, catalog_url: str | None = None, order_url: str | None = None,
    timeout: float | None = None,
) -> FastAPI:
    key = key if key is not None else signing_key()
    upstreams = {
        "catalog": (catalog_url or os.getenv("CATALOG_URL", "http://catalog:8001")).rstrip("/"),
        "order": (order_url or os.getenv("ORDER_URL", "http://order-api:8002")).rstrip("/"),
    }
    timeout = timeout if timeout is not None else float(os.getenv("GATEWAY_TIMEOUT", "6"))
    if not 0 < timeout <= 60:
        raise ValueError("GATEWAY_TIMEOUT must be greater than 0 and at most 60 seconds")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(2, timeout)), trust_env=False
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )

    @app.get("/health/live")
    @app.get("/health/ready")
    async def health():
        return {"status": "ok"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy(request: Request):
        path = request.scope["path"]
        route = next((route for route in ROUTES if route.path.fullmatch(path)), None)
        if route is None:
            return error(404, "not_found", "Маршрут не найден")
        if request.method not in route.methods:
            return error(
                405, "method_not_allowed", "Метод не поддерживается",
                {"Allow": ", ".join(sorted(route.methods))},
            )

        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        try:
            if scheme.lower() != "bearer" or not token:
                raise jwt.InvalidTokenError("Missing Bearer token")
            claims = jwt.decode(
                token, key, algorithms=["HS256"], options={"require": ["sub", "exp", "role"]},
            )
            subject, role = claims["sub"], claims["role"]
            if not SUBJECT.fullmatch(subject) or type(claims["exp"]) is not int:
                raise jwt.InvalidTokenError("Invalid subject or expiration")
        except (jwt.InvalidTokenError, TypeError, ValueError, OverflowError):
            return error(
                401, "unauthorized", "Требуется действительный JWT",
                {"WWW-Authenticate": "Bearer"},
            )
        if role not in ROLES or (request.method in route.admin_methods and role != "admin"):
            return error(403, "forbidden", "Недостаточно прав для запроса")

        headers = {name: request.headers[name] for name in REQUEST_HEADERS if name in request.headers}
        headers.update({"X-User-Id": subject, "X-User-Role": role})
        url = httpx.URL(upstreams[route.upstream] + path).copy_with(query=request.scope["query_string"] or None)
        body = await request.body()
        try:
            async with asyncio.timeout(timeout):
                upstream = await request.app.state.client.request(
                    request.method, url, headers=headers, content=body,
                )
        except (httpx.TimeoutException, TimeoutError):
            return error(504, "upstream_timeout", f"Сервис {route.upstream} не ответил вовремя")
        except httpx.RequestError:
            return error(503, "upstream_unavailable", f"Сервис {route.upstream} временно недоступен")

        if upstream.is_error:
            try:
                payload = upstream.json()
            except ValueError:
                payload = None
            details = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(details, dict) or not all(
                isinstance(details.get(field), str) for field in ("code", "message")
            ):
                return error(upstream.status_code, "upstream_error", f"Ошибка сервиса {route.upstream}")

        response_headers = {
            name: upstream.headers[name] for name in RESPONSE_HEADERS if name in upstream.headers
        }
        return Response(upstream.content, status_code=upstream.status_code, headers=response_headers)

    return app
