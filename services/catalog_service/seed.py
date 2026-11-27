import argparse

from catalog_service.clients.order_client import OrderStockClient
from catalog_service.config import settings
from catalog_service.database import Base, make_sessions
from catalog_service.models import Product

DEMO_PRODUCTS = [
    ("MILK-001", "Молоко 2,5% 1 л", "Молочные продукты", 89),
    ("BREAD-001", "Хлеб пшеничный", "Хлеб", 49),
    ("EGG-001", "Яйцо куриное С1, 10 шт.", "Яйца", 119),
    ("TEA-001", "Чай чёрный 25 пакетиков", "Напитки", 159),
    ("APPLE-001", "Яблоки сезонные, 1 кг", "Фрукты", 129),
]


def main():
    parser = argparse.ArgumentParser(description="Seed the demo catalog")
    parser.add_argument("--with-stock", action="store_true", help="Initialize stock in Order Service")
    args = parser.parse_args()
    if args.with_stock:
        client = OrderStockClient(settings)
        for sku, _, _, _ in DEMO_PRODUCTS:
            client.set_initial_stock(sku, 25)

    sessions, engine = make_sessions(settings.catalog_database_url)
    Base.metadata.create_all(engine)
    with sessions.begin() as session:
        for sku, name, category, price in DEMO_PRODUCTS:
            if session.get(Product, sku) is None:
                session.add(Product(
                    sku=sku, name=name, category=category, price=price,
                    currency="RUB", is_active=True,
                ))
    engine.dispose()
    print(f"Добавлено/проверено демонстрационных товаров: {len(DEMO_PRODUCTS)}")


if __name__ == "__main__":
    main()
