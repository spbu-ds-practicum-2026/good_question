/* Настройки интеграции. В Docker/Nginx API обычно доступен через тот же origin по /api. */
window.APP_CONFIG = {
  API_BASE_URL: "/api",
  // Маршруты ниже должны быть сверены с реальными контрактами Gateway/Order Service.
  ROUTES: {
    products: "/products",
    product: (sku) => `/products/${encodeURIComponent(sku)}`,
    cart: "/cart",
    orders: "/orders",
    order: (id) => `/orders/${encodeURIComponent(id)}`,
    notifications: "/notifications"
  },
  PAGE_SIZE: 12,
  CURRENCY: "RUB"
};
