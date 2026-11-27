import uvicorn

from catalog_service.app import app
from catalog_service.config import settings


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
