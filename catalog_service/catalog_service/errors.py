import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def install_errors(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={
            "error": {
                "code": "validation_error",
                "message": "Некорректные параметры запроса",
                "fields": [
                    {"location": list(err["loc"]), "message": err["msg"]}
                    for err in exc.errors()
                ],
            }
        })

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        log.exception("Catalog database operation failed")
        return JSONResponse(status_code=503, content={
            "error": {
                "code": "catalog_database_unavailable",
                "message": "Каталог временно недоступен",
            }
        })
