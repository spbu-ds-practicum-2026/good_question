CREATE TABLE payments (
    order_id varchar(36) PRIMARY KEY,
    amount bigint NOT NULL CHECK (amount > 0),
    currency varchar(3) NOT NULL CHECK (currency = 'RUB'),
    payment_mode varchar(10) NOT NULL CHECK (payment_mode IN ('success', 'fail')),
    status varchar(20) NOT NULL CHECK (status IN ('COMPLETED', 'FAILED', 'REFUNDED')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE outbox (
    event_id varchar(36) PRIMARY KEY,
    event_type varchar(80) NOT NULL,
    body jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0,
    last_error varchar(500)
);
CREATE INDEX ix_outbox_next_attempt_at ON outbox(next_attempt_at);
CREATE INDEX ix_outbox_pending ON outbox(created_at) WHERE published_at IS NULL;
CREATE TABLE processed_events (
    event_id varchar(36) PRIMARY KEY,
    processed_at timestamptz NOT NULL DEFAULT now()
);
