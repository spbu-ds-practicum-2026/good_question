from order_payment.messaging import DEAD_EXCHANGE, DEAD_QUEUE, EXCHANGE, QUEUES, connection
from order_payment.shared import Settings


def main() -> None:
    with connection(Settings()) as conn:
        channel = conn.channel()
        channel.exchange_declare(exchange=EXCHANGE, exchange_type="direct", durable=True)
        channel.exchange_declare(exchange=DEAD_EXCHANGE, exchange_type="fanout", durable=True)
        channel.queue_declare(queue=DEAD_QUEUE, durable=True, arguments={"x-queue-type": "classic"})
        channel.queue_bind(queue=DEAD_QUEUE, exchange=DEAD_EXCHANGE)
        for queue, routing_keys in QUEUES.items():
            channel.queue_declare(
                queue=queue, durable=True,
                arguments={"x-queue-type": "classic", "x-dead-letter-exchange": DEAD_EXCHANGE},
            )
            for routing_key in routing_keys:
                channel.queue_bind(queue=queue, exchange=EXCHANGE, routing_key=routing_key)


if __name__ == "__main__":
    main()
