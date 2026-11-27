CREATE TABLE IF NOT EXISTS products (
    sku varchar(64) PRIMARY KEY,
    name varchar(200) NOT NULL,
    category varchar(100) NOT NULL,
    price integer NOT NULL CHECK (price > 0),
    currency varchar(3) NOT NULL DEFAULT 'RUB' CHECK (currency = 'RUB'),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_products_category ON products(category);
CREATE INDEX IF NOT EXISTS ix_products_price ON products(price);
CREATE INDEX IF NOT EXISTS ix_products_name ON products(name);
