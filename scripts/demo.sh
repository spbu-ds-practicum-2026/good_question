#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

dc() { docker compose "$@"; }
say() { printf '%s\n' "$*"; }
fail() { say "$*" >&2; exit 1; }
token() { dc exec -T gateway python -m gateway.tokens "$@"; }

expect_http() {
    local expected=$1 path=$2 status
    shift 2
    status=$(curl -sS --max-time 15 -o "$response" -w '%{http_code}' "$@" "$base$path")
    say "$path → HTTP $status"
    if [[ $status != "$expected" ]]; then
        cat "$response" >&2
        fail "Ожидался HTTP $expected"
    fi
}

orders_sql() {
    dc exec -T orders-db psql -X -qAt -v ON_ERROR_STOP=1 -v "order_id=$order_id" -U demo -d orders
}

poll() {
    local attempt
    for ((attempt = 0; attempt < 40; attempt++)); do
        if "$@"; then return; fi
        sleep 1
    done
    fail "Проверка не прошла вовремя: $*"
}

pending_outbox() {
    [[ $(orders_sql <<'SQL'
SELECT count(*) FROM outbox
WHERE event_type = 'payment.requested' AND body->'data'->>'orderId' = :'order_id'
  AND published_at IS NULL AND attempts > 0;
SQL
) == 1 ]]
}

paid_order() {
    curl -fsS --max-time 8 -H "$user_header" "$base/orders/$order_id" |
        jq -e '.status == "PAID"' >/dev/null
}

payment_effect() {
    orders_sql <<'SQL'
SELECT json_build_object(
    'status', (SELECT status FROM orders WHERE id = :'order_id'),
    'reserves', (SELECT array_agg(status ORDER BY sku) FROM reservations WHERE order_id = :'order_id'),
    'history', (SELECT count(*) FROM order_history WHERE order_id = :'order_id'),
    'notifications', (SELECT count(*) FROM notifications WHERE order_id = :'order_id')
);
SQL
}

republish_result() {
    dc exec -T payment-mock python - "$order_id" <<'PY'
import sys
from sqlalchemy import select
from order_payment.messaging import RabbitPublisher
from order_payment.payments import Outbox
from order_payment.shared import Settings, make_sessions

settings = Settings()
with make_sessions(settings.payments_database_url)() as session:
    event = session.scalars(select(Outbox).where(
        Outbox.event_type == "payment.completed",
        Outbox.body["data"]["orderId"].astext == sys.argv[1],
    )).one()
    RabbitPublisher(settings).publish(event.body, event.event_type, event.event_id)
    print(event.event_id)
PY
}

consume_duplicate() {
    dc exec -T order-api python - "$1" <<'PY'
import sys
import time
from order_payment.messaging import consume_once
from order_payment.orders.service import OrderService
from order_payment.shared import Settings, make_sessions

settings = Settings()
orders = OrderService(make_sessions(settings.orders_database_url), None)
duplicate_seen = False

def handle(event):
    global duplicate_seen
    applied = orders.handle_payment(event)
    if str(event.event_id) == sys.argv[1]:
        if applied:
            raise RuntimeError("Duplicate event was applied again")
        duplicate_seen = True

deadline = time.monotonic() + 15
while not duplicate_seen and time.monotonic() < deadline:
    if not consume_once(settings, "orders.payments", handle):
        time.sleep(0.25)
if not duplicate_seen:
    raise RuntimeError("Duplicate event did not arrive")
PY
}

case "${1:-}" in
    auth|catalog-outage|broker-outage) ;;
    *) fail "Запуск: bash scripts/demo.sh {auth|catalog-outage|broker-outage}" ;;
esac

response=$(mktemp)
recovery=
cleanup() {
    if [[ -n $recovery ]]; then dc start "$recovery" >/dev/null 2>&1 || true; fi
    rm -f "$response"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
base="http://$(dc port gateway 8000)"
demo_id="$(date +%s)-$$"
user_header="Authorization: Bearer $(token user --subject "demo-$demo_id")"

case "$1" in
    auth)
        expect_http 401 /products
        expect_http 200 '/products?limit=1' -H "$user_header"
        jq -e '.items | length == 1' "$response" >/dev/null
        data="{\"sku\":\"DEMO-$demo_id\",\"name\":\"Товар для проверки Gateway\",\"category\":\"Демо\",\"price\":100,\"initial_stock\":2}"
        expect_http 403 /products -H "$user_header" -H "Content-Type: application/json" --data "$data"
        admin_header="Authorization: Bearer $(token admin)"
        expect_http 201 /products -H "$admin_header" -H "Content-Type: application/json" --data "$data"
        expect_http 200 "/stock/DEMO-$demo_id" -H "$user_header"
        jq -e '.available == 2' "$response" >/dev/null
        ;;
    catalog-outage)
        expect_http 200 /products -H "$user_header"
        recovery=catalog
        dc stop catalog
        expect_http 503 /products -H "$user_header"
        jq -e '.error.code == "upstream_unavailable"' "$response" >/dev/null
        expect_http 200 /orders -H "$user_header"
        dc start --wait --wait-timeout 60 catalog
        recovery=
        expect_http 200 /products -H "$user_header"
        ;;
    broker-outage)
        expect_http 204 /cart/items -H "$user_header" -H "Content-Type: application/json" \
            --data '{"sku":"MILK-001","quantity":1}'
        recovery=rabbitmq
        dc stop rabbitmq
        expect_http 201 /orders -H "$user_header" -H "Content-Type: application/json" \
            -H "Idempotency-Key: demo-$demo_id" \
            --data '{"pickup_point":"shop-1","payment_mode":"success"}'
        order_id=$(jq -er '.id | strings' "$response")
        jq -e '.status == "PENDING_PAYMENT"' "$response" >/dev/null
        poll pending_outbox
        say "Заказ $order_id · PENDING_PAYMENT · payment.requested остался в Outbox"
        dc start --wait --wait-timeout 75 rabbitmq
        recovery=
        poll paid_order
        published_event=$(orders_sql <<'SQL'
SELECT event_id FROM outbox
WHERE event_type = 'payment.requested' AND body->'data'->>'orderId' = :'order_id'
  AND published_at IS NOT NULL;
SQL
)
        [[ -n $published_event ]] || fail "Outbox не подтвердил публикацию"
        say "Брокер восстановлен · $published_event опубликован · заказ PAID"
        before=$(payment_effect)
        recovery=orders-consumer
        dc stop orders-consumer
        duplicate_id=$(republish_result)
        consume_duplicate "$duplicate_id"
        [[ $before == "$(payment_effect)" ]] || fail "Повторное событие изменило состояние заказа"
        dc start orders-consumer
        recovery=
        say "Повтор payment.completed ($duplicate_id) обработан один раз. Состояние заказа:"
        jq . <<<"$before"
        ;;
esac
