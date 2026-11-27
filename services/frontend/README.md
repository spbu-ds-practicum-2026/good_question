# Frontend — Магазин «Семёрочка»

Статический frontend на HTML, CSS и vanilla JavaScript. Не требует Node.js, npm или сборщика.

## Запуск локально

Из этой папки:

```bash
python -m http.server 5500
```

Откройте `http://localhost:5500`.

Для работы с API задайте `API_BASE_URL` в `config.js`. По умолчанию используется `/api`, чтобы запросы шли через reverse proxy/Gateway на том же origin.

## Размещение в репозитории

Папка проекта должна находиться здесь:

```text
services/frontend/
```

Ожидаемая структура:

```text
services/frontend/
├── index.html
├── styles.css
├── config.js
├── app.js
├── Dockerfile
├── nginx.conf
└── README.md
```

## Интеграция с Backend

Frontend должен обращаться **только к API Gateway**, а не напрямую к контейнерам Catalog/Order. Gateway проверяет JWT и передаёт доверенные `X-User-Id` и `X-User-Role`.

| Действие UI | Сервис | Используемый маршрут в заготовке |
|---|---|---|
| Список, поиск, фильтры | Catalog | `GET /products?q=&category=&minPrice=&maxPrice=&limit=&offset=` |
| Карточка товара | Catalog | `GET /products/{sku}` |
| Создание товара | Catalog | `POST /products` |
| Редактирование товара | Catalog | `PATCH /products/{sku}` |
| Корзина | Order | `GET/POST/PATCH/DELETE /cart` (нужно согласовать и подключить, если корзина серверная) |
| Оформление заказа | Order | `POST /orders` |
| История заказов | Order | `GET /orders` |
| Уведомления | Order | `GET /notifications` (нужно согласовать) |

### Контракт Catalog, уже известный по реализации Catalog Service

- `GET /products`: query-параметры `q`, `category`, `minPrice`, `maxPrice`, `limit`, `offset`.
- `GET /products/{sku}`.
- `GET /internal/products/{sku}/price` — внутренний маршрут для Order, frontend его не вызывает.
- `POST /products`: `sku`, `name`, `category`, `price`, `currency`, `initial_stock`.
- `PATCH /products/{sku}`.

В коде список товаров ожидает массив или объект с `items`, `products` либо `results`. Если ваш Catalog возвращает другую форму JSON, измените `productList()` в `app.js`.

### Что нужно согласовать с Максимом

В предоставленном распределении задач не зафиксированы точные публичные маршруты и JSON-схемы Order API. Поэтому `/cart`, `/orders`, `/notifications` и payload оформления в `app.js` являются **предлагаемым контрактом**, а не утверждением, что такие endpoints уже реализованы.

До интеграции согласовать:
1. `GET/POST/PATCH/DELETE /cart` и форму позиции корзины.
2. `POST /orders`: точную схему `items`, самовывоза и пользователя.
3. `Idempotency-Key`: повтор после сетевого таймаута должен отправлять тот же ключ и тот же payload. В интерфейсе ключ сохраняется на повторной попытке до успешного ответа.
4. `GET /orders`, `GET /orders/{id}` и `GET /notifications`.
5. Статусы и структуру ответа заказа.

### Важные замечания для интеграции

- В `config.js` можно изменить `API_BASE_URL` и маршруты в `ROUTES`.
- Не встраивайте секретные JWT в исходники. Для демо токен вводится вручную и хранится в `localStorage`; это упрощение, не production-аутентификация.
- Сейчас корзина хранится в `localStorage`, а не в Order DB. Для серверной корзины заменить локальные операции на согласованные endpoints Order.
- Начальный остаток отправляется в `POST /products` как `initial_stock`; Catalog Service вызывает Order Service самостоятельно. Frontend не должен напрямую вызывать `/internal/stock/initial`.
- При создании товара и редактировании используются `POST /products` и `PATCH /products/{sku}`. Установка начального остатка предусмотрена только при создании.
- Ошибки `401`, `403`, `409`, `422` и сетевые ошибки показываются пользователю сообщением.
- Для production нужно настроить CORS либо same-origin reverse proxy. Рекомендуемый вариант — проксирование `/api` через Nginx/Gateway.

## Docker

```bash
docker build -t semerochka-frontend .
docker run --rm -p 8080:80 semerochka-frontend
```

Откройте `http://localhost:8080`.

## Ручной QA-чек-лист

- [ ] Каталог загружается.
- [ ] Поиск, категория и диапазон цены фильтруют товары.
- [ ] Пагинация работает.
- [ ] Товар добавляется в корзину, количество меняется, товар удаляется.
- [ ] Корзина переживает перезагрузку страницы.
- [ ] Неавторизованный пользователь не может оформить заказ.
- [ ] Повтор оформления после таймаута использует прежний `Idempotency-Key`.
- [ ] Успешный заказ появляется в истории.
- [ ] Недостаток остатка отображается как понятная ошибка.
- [ ] При недоступности Catalog/Gateway интерфейс не зависает.
- [ ] Администратор может создать и изменить товар.
- [ ] Пользователь без admin-роли не может выполнять административные операции.
- [ ] Ошибки `401`, `403`, `409`, `422` отображаются понятно.
