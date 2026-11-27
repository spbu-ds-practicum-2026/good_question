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
POST /internal/stock/initial
Idempotency-Key: catalog-initial-stock-<stable-hash>
Content-Type: application/json

{"sku":"MILK-001","quantity":20}
```

**Важно:** в загруженных файлах Максима этого endpoint пока нет. Это контракт, который Максим должен реализовать в Order Service. Его endpoint должен:

1. принимать `sku` и `quantity`;
2. обрабатывать `Idempotency-Key`;
3. при повторе того же ключа и тела возвращать прежний успешный результат без увеличения остатка;
4. при том же ключе с другим телом возвращать `422`.

Путь можно изменить через `ORDER_INITIAL_STOCK_PATH`, если Максим согласует другой. Catalog делает ограниченное число повторов с тем же ключом и таймаутом по умолчанию 2 секунды. Пока Order не подтвердил начальный остаток, товар остаётся неактивным и не появляется в публичном каталоге. Повторите тот же `POST /products` с теми же данными, чтобы безопасно завершить незавершённую операцию.

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
- `ORDER_INITIAL_STOCK_PATH` — согласуемый endpoint установки начального остатка.
- `ORDER_TIMEOUT` — таймаут вызова Order Service, по умолчанию 2 секунды.
- `ORDER_RETRIES` — число дополнительных повторов безопасного идемпотентного вызова.
