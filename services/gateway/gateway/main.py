"""API Gateway — единственная точка входа клиента.

Минимальная реализация (по условию ЛР4 Gateway можно реализовать минимально):
* маршрутизация по префиксам /api/v1/** на сервисы;
* /internal/** снаружи недоступны;
* проверка JWT и передача пользователя сервисам заголовками X-User-*;
* X-Correlation-Id на каждый запрос;
* ограничение частоты запросов;
* POST /auth/token — мок Keycloak: выдаёт токены демо-пользователям.
"""
import logging
import threading
import time
from urllib.parse import quote

import jwt
import requests
from flask import Response, request

from jamslot_common import context, demo
from jamslot_common.context import setup_logging
from jamslot_common.problems import ApiError
from jamslot_common.runtime import env
from jamslot_common.web import create_base_app, json_response

SERVICE = "api-gateway"
log = logging.getLogger(SERVICE)

DEMO_USERS = {
    "ilya": {"sub": demo.USER_ILYA, "name": "Илья", "email": "ilya@example.org", "roles": ["client"], "venue_ids": []},
    "anna": {"sub": demo.USER_ANNA, "name": "Анна", "email": "anna@example.org", "roles": ["client"], "venue_ids": []},
    "manager": {"sub": demo.USER_MANAGER, "name": "Олег", "email": "manager@example.org", "roles": ["venue_manager"],
                "venue_ids": [demo.VENUE_GARAGE]},
    "admin": {"sub": demo.USER_ADMIN, "name": "Администратор", "email": "admin@example.org", "roles": ["admin"],
              "venue_ids": []},
}
DEMO_PASSWORD = "password"
TOKEN_TTL = 3600
ISSUER = "http://localhost:8000/auth/realms/jamslot"

# Префикс пути → переменная окружения с адресом сервиса
ROUTES = [
    ("/api/v1/venues", "VENUE_SERVICE_URL"),
    ("/api/v1/rooms", "VENUE_SERVICE_URL"),
    ("/api/v1/bookings", "BOOKING_SERVICE_URL"),
    ("/api/v1/occupancy", "BOOKING_SERVICE_URL"),
    ("/api/v1/equipment", "EQUIPMENT_SERVICE_URL"),
    ("/api/v1/equipment-availability", "EQUIPMENT_SERVICE_URL"),
    ("/api/v1/payments", "PAYMENT_SERVICE_URL"),
    ("/api/v1/notifications", "NOTIFICATION_SERVICE_URL"),
    ("/api/v1/notification-preferences", "NOTIFICATION_SERVICE_URL"),
]
DEFAULT_URLS = {
    "VENUE_SERVICE_URL": "http://venue-service:8080",
    "BOOKING_SERVICE_URL": "http://booking-service:8080",
    "EQUIPMENT_SERVICE_URL": "http://equipment-service:8080",
    "PAYMENT_SERVICE_URL": "http://payment-service:8080",
    "NOTIFICATION_SERVICE_URL": "http://notification-service:8080",
}
FORWARD_REQUEST_HEADERS = ("Content-Type", "Accept", "Idempotency-Key")
FORWARD_RESPONSE_HEADERS = ("Content-Type", "Retry-After", "Idempotent-Replayed")


class RateLimiter:
    """Фиксированное окно: не больше limit запросов в минуту с одного адреса."""

    def __init__(self, limit: int):
        self.limit = limit
        self._hits: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> int | None:
        window = int(time.time() // 60)
        with self._lock:
            if len(self._hits) > 10000:
                self._hits = {k: v for k, v in self._hits.items() if k[1] == window}
            n = self._hits.get((client, window), 0) + 1
            self._hits[(client, window)] = n
        return None if n <= self.limit else 60 - int(time.time() % 60)


def create_app(settings: dict | None = None, *, http_session=None):
    setup_logging(SERVICE)
    settings = settings or {}

    def setting(key, default=None):
        return settings.get(key, env(key, default))

    secret = setting("JWT_SECRET", "jamslot-dev-secret-change-me-in-production")
    limiter = RateLimiter(int(setting("RATE_LIMIT_PER_MINUTE", "300")))
    session = http_session or requests.Session()
    upstreams = {k: setting(k, v).rstrip("/") for k, v in DEFAULT_URLS.items()}
    app = create_base_app(SERVICE)

    @app.get("/health")
    def health():
        return json_response({"status": "UP"})

    # --- мок Keycloak -------------------------------------------------------------------
    @app.post("/auth/token")
    def token():
        data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        data = data or {}
        user = DEMO_USERS.get(str(data.get("username", "")))
        if not user or data.get("password") != DEMO_PASSWORD:
            raise ApiError(401, "UNAUTHORIZED", "Неверный логин или пароль. Демо-пользователи: "
                                                + ", ".join(DEMO_USERS) + f"; пароль {DEMO_PASSWORD}")
        now = int(time.time())
        claims = {"iss": ISSUER, "sub": user["sub"], "name": user["name"], "email": user["email"],
                  "roles": user["roles"], "venue_ids": user["venue_ids"], "iat": now, "exp": now + TOKEN_TTL}
        return json_response({"access_token": jwt.encode(claims, secret, algorithm="HS256"),
                              "token_type": "Bearer", "expires_in": TOKEN_TTL})

    # --- проксирование ------------------------------------------------------------------
    def resolve(path: str) -> str | None:
        for prefix, key in ROUTES:
            if path == prefix or path.startswith(prefix + "/"):
                return upstreams[key]
        return None

    def user_headers() -> dict:
        auth = request.headers.get("Authorization")
        if not auth:
            return {}
        if not auth.startswith("Bearer "):
            raise ApiError(401, "UNAUTHORIZED", "Ожидается заголовок Authorization: Bearer <токен>")
        try:
            claims = jwt.decode(auth[7:], secret, algorithms=["HS256"], issuer=ISSUER,
                                options={"require": ["exp", "sub"]})
        except jwt.ExpiredSignatureError:
            raise ApiError(401, "UNAUTHORIZED", "Access-токен просрочен")
        except jwt.InvalidTokenError:
            raise ApiError(401, "UNAUTHORIZED", "Access-токен не прошёл проверку")
        return {"X-User-Id": claims["sub"], "X-User-Name": quote(claims.get("name", "")),
                "X-User-Email": quote(claims.get("email", "")), "X-User-Roles": ",".join(claims.get("roles", [])),
                "X-User-Venue-Ids": ",".join(claims.get("venue_ids", []))}

    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def proxy(path):
        full = "/" + path
        retry_after = limiter.check(request.remote_addr or "unknown")
        if retry_after is not None:
            raise ApiError(429, "RATE_LIMITED", "Превышен лимит запросов, повторите позже",
                           headers={"Retry-After": str(retry_after)})
        if full.startswith("/internal/"):
            raise ApiError(404, "NOT_FOUND", "Маршрут не найден")
        upstream = resolve(full)
        if upstream is None:
            raise ApiError(404, "NOT_FOUND", "Маршрут не найден")
        headers = {h: request.headers[h] for h in FORWARD_REQUEST_HEADERS if h in request.headers}
        headers["X-Correlation-Id"] = context.ensure_correlation_id()
        headers.update(user_headers())  # входящие X-User-* не пересылаются: подделать пользователя нельзя
        try:
            resp = session.request(request.method, upstream + full, params=list(request.args.items(multi=True)),
                                   data=request.get_data(), headers=headers, timeout=15, allow_redirects=False)
        except requests.RequestException as exc:
            log.warning("Upstream %s недоступен: %s", upstream, exc.__class__.__name__)
            raise ApiError(503, "SERVICE_UNAVAILABLE", f"Сервис {upstream.split('//')[-1].split(':')[0]} недоступен",
                           headers={"Retry-After": "5"})
        out = Response(resp.content, status=resp.status_code)
        for h in FORWARD_RESPONSE_HEADERS:
            if h in resp.headers:
                out.headers[h] = resp.headers[h]
        return out

    return app
