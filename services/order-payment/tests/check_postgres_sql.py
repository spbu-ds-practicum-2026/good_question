"""SQL-only checks on PostgreSQL via psql; NOT a substitute for application tests.

Run: python3 services/order-payment/tests/check_postgres_sql.py --database postgres
Creates only uniquely named test schemas and removes them in finally.
Uses the current user's ordinary PostgreSQL connection settings.
"""

import argparse
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4


class SQLChecks:
    def __init__(self, database: str):
        self.database = database
        self.schemas: list[str] = []
        self.passed: list[str] = []
        self.prefix = f"orders_sql_check_{uuid4().hex}"

    def sql(self, sql: str, schema: str | None = None, *, error: str | None = None) -> str:
        if schema is not None:
            if schema not in self.schemas:
                raise ValueError("Refusing to use a schema not created by this run")
            sql = f'SET search_path TO "{schema}";\n' + sql
        result = subprocess.run(
            ["psql", "-X", "-q", "-w", "-A", "-t", "-d", self.database,
             "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose"],
            input=sql, capture_output=True, text=True, timeout=20,
            env={**os.environ, "PGCONNECT_TIMEOUT": "5",
                 "PGOPTIONS": "-c statement_timeout=10000 -c lock_timeout=8000"},
        )
        if error is not None:
            if result.returncode == 0 or error not in result.stderr:
                raise AssertionError(f"Expected SQLSTATE {error}: {result.stderr}")
        elif result.returncode != 0:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    def passed_check(self, name: str) -> None:
        self.passed.append(name)
        print(f"PASS: {name}", flush=True)

    def equal(self, actual, expected) -> None:
        if actual != expected:
            raise AssertionError(f"Expected {expected!r}, got {actual!r}")

    def prepare(self, service: str, count: int) -> str:
        schema = f"{self.prefix}_{service}"
        self.sql(f'CREATE SCHEMA "{schema}";')
        self.schemas.append(schema)
        root = Path(__file__).resolve().parents[1]
        migration = root / "order_payment" / "migrations" / service / "001_init.sql"
        self.sql("BEGIN;\n" + migration.read_text() + "\nCOMMIT;", schema)
        self.equal(self.sql(
            "SELECT count(*) FROM information_schema.tables "
            f"WHERE table_schema = '{schema}';"
        ), str(count))
        self.passed_check(f"{service}: SQL migration executes ({count} tables)")
        return schema

    def parallel(self, schema: str, statement: str) -> list[int]:
        barrier = Barrier(2)

        def attempt(_):
            barrier.wait(timeout=5)
            output = self.sql(
                "BEGIN;\n" + statement + "\nSELECT pg_sleep(0.2);\nCOMMIT;", schema
            )
            return int(output.strip())

        with ThreadPoolExecutor(max_workers=2) as pool:
            return sorted(pool.map(attempt, range(2)))

    def run(self) -> None:
        orders = self.prepare("orders", 11)
        payments = self.prepare("payments", 3)
        self.sql("INSERT INTO stock VALUES ('MILK', 1, 1);", orders)
        self.sql("UPDATE stock SET available = -1 WHERE sku = 'MILK';", orders, error="23514")
        self.equal(self.sql("SELECT available FROM stock WHERE sku = 'MILK';", orders), "1")
        self.passed_check("stock: CHECK forbids negative quantity")

        results = self.parallel(orders, """
            WITH changed AS (
                UPDATE stock SET available = available - 1
                WHERE sku = 'MILK' AND available >= 1 RETURNING sku
            ) SELECT count(*) FROM changed;
        """)
        self.equal(results, [0, 1])
        self.equal(self.sql("SELECT available FROM stock WHERE sku = 'MILK';", orders), "0")
        self.passed_check("stock: two concurrent conditional updates, only one reserves")

        self.sql("UPDATE stock SET available = 1 WHERE sku = 'MILK';", orders)
        order_id = str(uuid4())
        insert_order = f"""
            INSERT INTO orders (
                id, user_id, status, total, currency, payment_mode, pickup_point, expires_at
            ) VALUES (
                '{order_id}', 'u', 'PENDING_PAYMENT', 100, 'RUB', 'success', 'shop-1',
                clock_timestamp() + interval '15 minutes'
            );
        """
        self.sql("BEGIN;\n" + insert_order + f"""
            UPDATE stock SET available = available - 1
            WHERE sku = 'MILK' AND available >= 1;
            INSERT INTO reservations VALUES ('{order_id}', 'MILK', 1, 'ACTIVE');
            INSERT INTO outbox(event_id, event_type, body)
            VALUES ('{uuid4()}', 'payment.requested', '{{}}');
            SELECT 1 / 0;
            COMMIT;
        """, orders, error="22012")
        self.equal(self.sql("SELECT available FROM stock WHERE sku = 'MILK';", orders), "1")
        for table in ("orders", "reservations", "outbox"):
            self.equal(self.sql(f"SELECT count(*) FROM {table};", orders), "0")
        self.passed_check("transaction: order/reservation/outbox/decrement roll back on SQL error")

        self.sql(insert_order, orders)
        self.sql(f"""
            INSERT INTO idempotency_keys VALUES (
                'u', 'key-1', repeat('a', 64), '{order_id}', '{{}}'
            );
        """, orders)
        self.sql(f"""
            INSERT INTO idempotency_keys VALUES (
                'u', 'key-1', repeat('b', 64), '{order_id}', '{{}}'
            );
        """, orders, error="23505")
        self.passed_check("idempotency: database forbids duplicate (user, key)")

        self.sql(f"UPDATE orders SET status = 'INVALID' WHERE id = '{order_id}';",
                 orders, error="23514")
        self.sql(f"INSERT INTO reservations VALUES ('{order_id}', 'MILK', 1, 'INVALID');",
                 orders, error="23514")
        self.sql(f"INSERT INTO reservations VALUES ('{uuid4()}', 'MILK', 1, 'ACTIVE');",
                 orders, error="23503")
        self.passed_check("orders/reservations: state constraints and foreign keys enforced")

        for service, schema in (("orders", orders), ("payments", payments)):
            event_id = str(uuid4())
            results = self.parallel(schema, f"""
                WITH inserted AS (
                    INSERT INTO processed_events(event_id) VALUES ('{event_id}')
                    ON CONFLICT DO NOTHING RETURNING event_id
                ) SELECT count(*) FROM inserted;
            """)
            self.equal(results, [0, 1])
            self.equal(self.sql("SELECT count(*) FROM processed_events;", schema), "1")
            self.passed_check(f"{service}: concurrent inbox insert admits one eventId")

        payment_id = str(uuid4())
        payment = f"""
            INSERT INTO payments(order_id, amount, currency, payment_mode, status)
            VALUES ('{payment_id}', 100, 'RUB', 'success', 'COMPLETED');
        """
        self.sql(payment, payments)
        self.sql(payment, payments, error="23505")
        self.sql(f"UPDATE payments SET amount = 0 WHERE order_id = '{payment_id}';",
                 payments, error="23514")
        self.sql(f"UPDATE payments SET status = 'INVALID' WHERE order_id = '{payment_id}';",
                 payments, error="23514")
        self.sql(f"UPDATE payments SET status = 'REFUNDED' WHERE order_id = '{payment_id}';",
                 payments)
        self.equal(self.sql("SELECT status FROM payments;", payments), "REFUNDED")
        self.passed_check("payments: unique order, positive amount, valid state, REFUNDED accepted")

    def cleanup(self) -> None:
        for schema in reversed(self.schemas):
            self.sql(f'DROP SCHEMA "{schema}" CASCADE;')
        for schema in self.schemas:
            self.equal(self.sql(
                f"SELECT count(*) FROM pg_namespace WHERE nspname = '{schema}';"
            ), "0")
        if self.schemas:
            print("PASS: only test schemas created by this run were removed", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="postgres")
    args = parser.parse_args()
    checks = SQLChecks(args.database)
    try:
        checks.run()
    finally:
        checks.cleanup()
    print(f"SQL checks passed: {len(checks.passed)}. Application/HTTP/AMQP tests NOT run.")


if __name__ == "__main__":
    main()
