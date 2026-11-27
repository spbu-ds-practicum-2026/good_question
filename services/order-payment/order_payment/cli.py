import argparse
import logging
import signal
import threading
from hashlib import sha256
from importlib.resources import files

from sqlalchemy import text

from order_payment.shared import Settings, advisory_lock, make_sessions

log = logging.getLogger(__name__)


def migrate(sessions, service: str) -> None:
    path = files("order_payment").joinpath("migrations", service)
    with sessions.begin() as session:
        advisory_lock(session, "migrations", service)
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version varchar(200) PRIMARY KEY, checksum varchar(64) NOT NULL)"
        ))
        for file in sorted(path.iterdir(), key=lambda item: item.name):
            if not file.name.endswith(".sql"):
                continue
            sql = file.read_text(encoding="utf-8")
            checksum = sha256(sql.encode()).hexdigest()
            previous = session.scalar(text(
                "SELECT checksum FROM schema_migrations WHERE version = :version"
            ), {"version": file.name})
            if previous is not None:
                if previous != checksum:
                    raise RuntimeError(f"Applied migration was modified: {file.name}")
                continue
            # These migrations contain only DDL, no semicolons in strings/functions.
            for statement in sql.split(";"):
                if statement.strip():
                    session.execute(text(statement))
            session.execute(text(
                "INSERT INTO schema_migrations(version, checksum) VALUES (:version, :checksum)"
            ), {"version": file.name, "checksum": checksum})


def run_worker(service_name: str, task: str, sessions, settings: Settings) -> None:
    from order_payment import payments
    from order_payment.messaging import RabbitPublisher, consume_once, relay_once
    from order_payment.orders import models as orders
    from order_payment.orders.service import OrderService

    service = OrderService(sessions, None) if service_name == "orders" else payments.PaymentService(
        sessions
    )
    outbox = orders.Outbox if service_name == "orders" else payments.Outbox
    publisher = RabbitPublisher(settings)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            if task == "relay":
                worked = relay_once(sessions, outbox, publisher, settings.outbox_retry_seconds)
            elif task == "expire":
                worked = bool(service.expire())
            else:
                worked = consume_once(
                    settings,
                    "orders.payments" if service_name == "orders" else "payments.commands",
                    service.handle_payment if service_name == "orders" else service.handle,
                )
        except Exception:
            log.exception("Worker iteration failed; will retry")
            worked = False
        stop.wait(0.01 if worked else settings.worker_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Order/Payment migrations and background workers")
    commands = parser.add_subparsers(dest="command", required=True)
    migration = commands.add_parser("migrate", help="Apply versioned SQL migrations")
    migration.add_argument("service", choices=["orders", "payments"])
    worker = commands.add_parser("worker", help="Run one background process")
    worker.add_argument("service", choices=["orders", "payments"])
    worker.add_argument("task", choices=["relay", "consume", "expire"])
    args = parser.parse_args()
    if args.command == "worker" and args.service == "payments" and args.task == "expire":
        parser.error("expire belongs to Order Service")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    sessions = make_sessions(
        settings.orders_database_url if args.service == "orders" else settings.payments_database_url
    )
    try:
        if args.command == "migrate":
            migrate(sessions, args.service)
        else:
            run_worker(args.service, args.task, sessions, settings)
    finally:
        sessions.kw["bind"].dispose()


if __name__ == "__main__":
    main()
