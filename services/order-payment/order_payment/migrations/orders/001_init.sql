CREATE TABLE carts (
    user_id varchar(128) PRIMARY KEY
);
CREATE TABLE cart_items (
    user_id varchar(128) NOT NULL REFERENCES carts(user_id),
    sku varchar(100) NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0 AND quantity <= 10000),
    token varchar(36) NOT NULL,
    PRIMARY KEY (user_id, sku)
);
CREATE TABLE stock (
    sku varchar(100) PRIMARY KEY,
    available integer NOT NULL CHECK (available >= 0),
    initial_quantity integer NOT NULL CHECK (initial_quantity >= 0)
);
CREATE TABLE orders (
    id varchar(36) PRIMARY KEY,
    user_id varchar(128) NOT NULL,
    status varchar(30) NOT NULL CHECK (
        status IN ('PENDING_PAYMENT', 'PAID', 'READY', 'COMPLETED', 'CANCELLED')
    ),
    total bigint NOT NULL CHECK (total > 0),
    currency varchar(3) NOT NULL CHECK (currency = 'RUB'),
    payment_mode varchar(10) NOT NULL CHECK (payment_mode IN ('success', 'fail')),
    pickup_point varchar(200) NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    refund_requested boolean NOT NULL DEFAULT false
);
CREATE INDEX ix_orders_user_id ON orders(user_id);
CREATE INDEX ix_orders_status ON orders(status);
CREATE INDEX ix_orders_expires_at ON orders(expires_at);
CREATE INDEX ix_orders_pending_expiry ON orders(expires_at) WHERE status = 'PENDING_PAYMENT';
CREATE TABLE order_items (
    order_id varchar(36) NOT NULL REFERENCES orders(id),
    sku varchar(100) NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price bigint NOT NULL CHECK (unit_price > 0),
    cart_token varchar(36) NOT NULL,
    PRIMARY KEY (order_id, sku)
);
CREATE TABLE reservations (
    order_id varchar(36) NOT NULL REFERENCES orders(id),
    sku varchar(100) NOT NULL REFERENCES stock(sku),
    quantity integer NOT NULL CHECK (quantity > 0),
    status varchar(16) NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'RELEASED')),
    PRIMARY KEY (order_id, sku)
);
CREATE TABLE idempotency_keys (
    user_id varchar(128) NOT NULL,
    key varchar(200) NOT NULL,
    request_hash varchar(64) NOT NULL,
    order_id varchar(36) NOT NULL REFERENCES orders(id),
    response jsonb NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE order_history (
    id varchar(36) PRIMARY KEY,
    order_id varchar(36) NOT NULL REFERENCES orders(id),
    status varchar(30) NOT NULL,
    reason varchar(100) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_order_history_order_id ON order_history(order_id);
CREATE TABLE notifications (
    id varchar(36) PRIMARY KEY,
    user_id varchar(128) NOT NULL,
    order_id varchar(36) NOT NULL REFERENCES orders(id),
    message varchar(300) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_notifications_user_id ON notifications(user_id);
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
