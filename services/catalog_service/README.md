# Catalog Service — Shop Semerochka

Отдельный stateless FastAPI-сервис Елизаветы. Каталог хранит только данные о товарах: SKU, название, категорию, цену и валюту. Остатки, резервы, корзины и заказы принадлежат Order Service Максима.

## Совместимость с кодом Максима

В `shared.py` Максима уже есть `catalog_url=http://localhost:8001` и `catalog_timeout=2.0`. Поэтому сервис по умолчанию слушает порт `8001`, а внутренний запрос цены доступен по адресу:

```http
GET /internal/products/{sku}/price
```

Ответ:

```json
{"sku":"MILK-001","price":89,"currency":"RUB"}
```

`price` — целое число рублей. Это согласуется с `PaymentData.amount: int` и `currency: "RUB"` в `payments.py` Максима. Остаток в ответ не включается: его проверяет и резервирует только Order Service.

### Начальная установка остатка

При административном `POST /products` обязательно передаётся `initial_stock` (в том числе `0`, чтобы Order Service создал запись остатка). Catalog вызывает Order Service:

```http
PUT /stock/MILK-001/initial
X-User-Id: service-catalog
X-User-Role: admin
Content-Type: application/json

{"quantity":20}
```

Order Service хранит исходный остаток по SKU. Повтор с тем же количеством
сохраняет текущий остаток, запрос с новым количеством возвращает `409`.
Путь настраивается через `ORDER_INITIAL_STOCK_PATH` и содержит `{sku}`.
Catalog делает ограниченное число повторов с тем же телом и таймаутом
по умолчанию 2 секунды. Пока Order не подтвердил начальный остаток, товар
остаётся неактивным. После временной ошибки повторите `POST /products`
с теми же данными.

## API

| Метод | Путь | Назначение | Доступ |
|---|---|---|---|
| GET | `/health/live` | Проверка процесса | публичный |
| GET | `/health/ready` | Проверка БД | публичный |
| GET | `/products` | Список, поиск и фильтры | пользователь |
| GET | `/products/{sku}` | Карточка товара | пользователь |
| GET | `/internal/products/{sku}/price` | Актуальная цена для Order | внутренняя сеть |
| POST | `/products` | Создание товара и установка начального остатка | admin |
| PATCH | `/products/{sku}` | Изменение товара | admin |

Фильтры списка: `q`, `category`, `minPrice`, `maxPrice`, `limit`, `offset`.

Административные запросы используют доверенные заголовки Gateway, как и сервисы Максима:

```http
X-User-Id: admin-1
X-User-Role: admin
```

Catalog Service должен быть доступен только из внутренней сети/Gateway, иначе эти заголовки можно подделать.

## Запуск локально

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Укажите реальный URL отдельной Catalog DB
uvicorn catalog_service.app:app --host 0.0.0.0 --port 8001 --reload
```

Swagger: `http://localhost:8001/docs`.

Для демонстрационных записей:

```bash
python seed.py
```

## Проверка

```bash
pytest -q
```

## Переменные окружения

- `CATALOG_DATABASE_URL` — отдельная PostgreSQL Catalog DB.
- `PORT` — порт сервиса, по умолчанию 8001.
- `AUTO_CREATE_SCHEMA` — создание таблиц при старте для демо; SQL миграция лежит в `migrations/001_create_products.sql`.
- `ORDER_SERVICE_URL` — адрес Order Service.
- `ORDER_INITIAL_STOCK_PATH` — шаблон пути установки начального остатка.
- `ORDER_TIMEOUT` — таймаут вызова Order Service, по умолчанию 2 секунды.
- `ORDER_RETRIES` — число дополнительных повторов безопасного идемпотентного вызова.
