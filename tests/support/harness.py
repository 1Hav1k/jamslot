"""Сборка всей системы JamSlot в одном процессе для тестов.

* Каждый сервис — настоящее Flask-приложение со своей БД (SQLite-файл или отдельная база PostgreSQL).
* HTTP между сервисами идёт через RoutingSession: тот же ServiceClient, что в бою, но запрос
  попадает в test client нужного сервиса. Можно «выключить» сервис — клиент получит ConnectionError.
* Брокер — InMemoryBroker с той же маршрутизацией, что у RabbitMQ.
* ЮKassa — эмулятор, который читает те же стабы WireMock из mocks/wiremock/mappings.
* Каждый ответ сервиса, каждый запрос к внутреннему API и каждое событие сверяются с контрактами ЛР3.
"""
import base64
import json
import os
import pathlib
import re
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo

import requests

from jamslot_common.db import Database
from jamslot_common.messaging import InMemoryBroker
from jamslot_common.timeutil import ManualClock, to_iso

from .contracts import Contracts

ROOT = pathlib.Path(__file__).resolve().parents[2]
WIREMOCK_DIR = ROOT / "mocks" / "wiremock"
TZ = ZoneInfo("Europe/Moscow")
START = int(datetime(2026, 11, 20, 12, 0, tzinfo=TZ).timestamp())  # «сейчас» в тестах


class FakeResponse:
    def __init__(self, status, headers, content: bytes):
        self.status_code = status
        self.headers = requests.structures.CaseInsensitiveDict(headers)
        self.content = content
        self.text = content.decode(errors="replace")

    def json(self):
        return json.loads(self.content)


# --------------------------------------------------------------------------------------
# Эмулятор WireMock (подмножество, достаточное для стабов из mocks/wiremock)
# --------------------------------------------------------------------------------------
class WireMockEmulator:
    def __init__(self, root: pathlib.Path = WIREMOCK_DIR):
        self.root = root
        self.mappings = [json.loads(p.read_text()) for p in sorted((root / "mappings").glob("*.json"))]
        self.journal: list[dict] = []

    @staticmethod
    def _json_path(body, path: str):
        cur = body
        for part in path.lstrip("$.").split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def _matches(self, req: dict, m: dict) -> bool:
        if m.get("method", "ANY") not in ("ANY", req["method"]):
            return False
        if "urlPath" in m and m["urlPath"] != req["path"]:
            return False
        if "urlPathPattern" in m and not re.fullmatch(m["urlPathPattern"], req["path"]):
            return False
        creds = m.get("basicAuthCredentials")
        if creds:
            expected = "Basic " + base64.b64encode(f"{creds['username']}:{creds['password']}".encode()).decode()
            if req["headers"].get("Authorization") != expected:
                return False
        for name, rule in (m.get("headers") or {}).items():
            val = req["headers"].get(name)
            if val is None:
                return False
            if "equalTo" in rule and val != rule["equalTo"]:
                return False
            if "contains" in rule and rule["contains"] not in val:
                return False
            if "matches" in rule and not re.fullmatch(rule["matches"], val):
                return False
        for pat in m.get("bodyPatterns") or []:
            try:
                body = json.loads(req["body"] or b"null")
            except ValueError:
                return False
            if "matchesJsonPath" in pat and self._json_path(body, pat["matchesJsonPath"]) is None:
                return False
        return True

    def _render(self, text: str, req: dict) -> str:
        body = None
        try:
            body = json.loads(req["body"] or b"null")
        except ValueError:
            pass

        def sub(mt):
            expr = mt.group(1).strip()
            if expr.startswith("jsonPath request.body"):
                path = re.search(r"'([^']+)'", expr).group(1)
                v = self._json_path(body, path)
                return "" if v is None else str(v)
            if expr.startswith("randomValue"):
                return str(uuid.uuid4())
            if expr == "now":
                return datetime.now().astimezone().isoformat()
            if expr.startswith("request.query."):
                return dict(req["query"]).get(expr.split(".", 2)[2], "")
            raise AssertionError(f"эмулятор WireMock не знает выражение {{{{{expr}}}}}")
        return re.sub(r"\{\{(.+?)\}\}", sub, text)

    def handle(self, method, path, query, headers, body):
        req = {"method": method, "path": path, "query": query, "headers": headers, "body": body}
        self.journal.append(req)
        candidates = [m for m in self.mappings if self._matches(req, m["request"])]
        if not candidates:
            return 404, {"Content-Type": "text/plain"}, b"Request was not matched"
        m = sorted(candidates, key=lambda x: x.get("priority", 5))[0]["response"]
        templ = "response-template" in m.get("transformers", [])
        if "jsonBody" in m:
            text = json.dumps(m["jsonBody"], ensure_ascii=False)
        elif "bodyFileName" in m:
            text = (self.root / "__files" / m["bodyFileName"]).read_text()
        else:
            text = m.get("body", "")
        if templ:
            text = self._render(text, req)
        return m.get("status", 200), m.get("headers", {}), text.encode()


# --------------------------------------------------------------------------------------
# HTTP-маршрутизация между сервисами внутри процесса
# --------------------------------------------------------------------------------------
class RoutingSession:
    def __init__(self, contracts: Contracts):
        self.targets: dict[str, object] = {}
        self.down: set[str] = set()
        self.contracts = contracts
        self.calls: list[tuple[str, str, int]] = []

    def mount(self, base_url: str, target):
        self.targets[base_url.rstrip("/")] = target

    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None, auth=None,
                allow_redirects=None):
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        headers = dict(headers or {})
        if base in self.down or base not in self.targets:
            raise requests.ConnectionError(f"{base} недоступен")
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        query = list(params.items()) if isinstance(params, dict) else list(params or [])
        query += parse_qsl(parts.query)
        if json is not None:
            body = __import__("json").dumps(json, ensure_ascii=False).encode()
            headers.setdefault("Content-Type", "application/json")
        else:
            body = data if isinstance(data, (bytes, type(None))) else str(data).encode()
        target = self.targets[base]
        internal = parts.path.startswith("/internal/")
        if internal and json is not None:
            self.contracts.check_request(method, parts.path, json)
        if isinstance(target, WireMockEmulator):
            status, hdrs, content = target.handle(method, parts.path, query, headers, body)
            resp = FakeResponse(status, hdrs, content)
        else:
            r = target.test_client().open(parts.path, method=method, query_string=urlencode(query), data=body,
                                          headers=headers)
            resp = FakeResponse(r.status_code, dict(r.headers), r.get_data())
            if internal:
                self._check(method, parts.path, resp)
        self.calls.append((method, url, resp.status_code))
        return resp

    def _check(self, method, path, resp):
        body = None
        if resp.content:
            body = resp.json()
        self.contracts.check_response(method, path, resp.status_code, resp.headers.get("Content-Type", ""), body)


class FakeMailer:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False

    def send(self, to, subject, body):
        if self.fail:
            raise ConnectionRefusedError("SMTP недоступен")
        self.sent.append({"to": to, "subject": subject, "body": body})


# --------------------------------------------------------------------------------------
# Система целиком
# --------------------------------------------------------------------------------------
SERVICE_URLS = {
    "VENUE_SERVICE_URL": "http://venue-service:8080",
    "BOOKING_SERVICE_URL": "http://booking-service:8080",
    "EQUIPMENT_SERVICE_URL": "http://equipment-service:8080",
    "PAYMENT_SERVICE_URL": "http://payment-service:8080",
    "NOTIFICATION_SERVICE_URL": "http://notification-service:8080",
    "YOOKASSA_URL": "http://wiremock:8080",
}


def make_databases(tmp_path, names):
    """SQLite по умолчанию; с JAMSLOT_TEST_PG — отдельные базы на настоящем PostgreSQL."""
    pg = os.environ.get("JAMSLOT_TEST_PG")
    if not pg:
        return {n: Database(f"sqlite:///{tmp_path / (n + '.db')}") for n in names}, None
    from . import pgwire
    suffix = uuid.uuid4().hex[:8]
    dbs, created = {}, []
    base = pg.rsplit("/", 1)[0]
    for n in names:
        name = f"t_{n.replace('-', '_')}_{suffix}"
        pgwire.admin(pg, f"CREATE DATABASE {name}")
        if n == "booking":
            pgwire.admin(f"{base}/{name}", "CREATE EXTENSION IF NOT EXISTS btree_gist")
        created.append(name)
        dbs[n] = Database(f"{base}/{name}", driver=pgwire.Driver)

    def drop():
        for name in created:
            pgwire.admin(pg, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    return dbs, drop


class System:
    def __init__(self, tmp_path, settings: dict | None = None, clock=None):
        from booking_service.main import create_app as booking_app
        from equipment_service.main import create_app as equipment_app
        from gateway.main import create_app as gateway_app
        from notification_service.main import create_app as notification_app
        from payment_service.main import create_app as payment_app
        from venue_service.main import create_app as venue_app

        self.contracts = Contracts()
        self.clock = clock or ManualClock(START)
        self.broker = InMemoryBroker(validator=self.contracts.check_event)
        self.session = RoutingSession(self.contracts)
        self.wiremock = WireMockEmulator()
        self.mailer = FakeMailer()
        self.dbs, self._drop = make_databases(tmp_path, ["venue", "booking", "equipment", "payment", "notification"])
        s = {**SERVICE_URLS, "BOOKING_HOLD_SECONDS": "900", **(settings or {})}
        common = dict(broker=self.broker, clock=self.clock, start_background=False)
        self.venue = venue_app(s, db=self.dbs["venue"], **common)
        self.equipment = equipment_app(s, db=self.dbs["equipment"], **common)
        self.payment = payment_app(s, db=self.dbs["payment"], http_session=self.session, **common)
        self.booking = booking_app(s, db=self.dbs["booking"], http_session=self.session, **common)
        self.notification = notification_app(s, db=self.dbs["notification"], mailer=self.mailer, **common)
        self.apps = {"venue": self.venue, "equipment": self.equipment, "payment": self.payment,
                     "booking": self.booking, "notification": self.notification}
        for name, app in self.apps.items():
            self.session.mount(SERVICE_URLS[f"{name.upper()}_SERVICE_URL"], app)
        self.session.mount(SERVICE_URLS["YOOKASSA_URL"], self.wiremock)
        self.gateway = gateway_app(s, http_session=self.session)
        self.client = self.gateway.test_client()
        self._tokens: dict[str, str] = {}

    def close(self):
        if self._drop:
            self._drop()

    # --- асинхронная часть --------------------------------------------------------------
    def settle(self, rounds: int = 20) -> int:
        """Опубликовать всё из outbox и доставить все сообщения, пока система не успокоится."""
        total = 0
        for _ in range(rounds):
            moved = sum(app.extensions["runtime"].relay.flush() for app in self.apps.values())
            moved += self.broker.drain()
            total += moved
            if not moved:
                break
        self.notification.extensions["send_due"]()
        return total

    def advance(self, seconds: int):
        """Сдвинуть время и запустить фоновые задачи всех сервисов."""
        self.clock.advance(seconds)
        for app in self.apps.values():
            app.extensions["runtime"].run_periodic()
        self.settle()

    def stop(self, name: str):
        self.session.down.add(SERVICE_URLS[f"{name.upper()}_SERVICE_URL"])

    def start(self, name: str):
        self.session.down.discard(SERVICE_URLS[f"{name.upper()}_SERVICE_URL"])

    # --- запросы клиента через Gateway --------------------------------------------------
    def token(self, user: str) -> str:
        if user not in self._tokens:
            r = self.client.post("/auth/token", json={"username": user, "password": "password"})
            assert r.status_code == 200, r.get_data(as_text=True)
            self._tokens[user] = r.get_json()["access_token"]
        return self._tokens[user]

    def call(self, method: str, path: str, user: str | None = None, json=None, key: str | None = "auto",
             headers: dict | None = None):
        h = dict(headers or {})
        if user:
            h["Authorization"] = f"Bearer {self.token(user)}"
        if key == "auto":
            key = str(uuid.uuid4()) if method == "POST" else None
        if key:
            h["Idempotency-Key"] = key
        r = self.client.open(path, method=method, json=json, headers=h)
        body = r.get_json(silent=True) if r.get_data() else None
        self.contracts.check_response(method, urlsplit(path).path, r.status_code, r.headers.get("Content-Type", ""), body)
        r.body = body
        return r

    # --- демо-данные --------------------------------------------------------------------
    def slot(self, days: int, hour: int, hours: int = 2) -> tuple[str, str]:
        start = datetime.fromtimestamp(START, TZ).replace(hour=hour, minute=0, second=0) + timedelta(days=days)
        return start.isoformat(), (start + timedelta(hours=hours)).isoformat()

    def pay(self, booking: dict, event: str = "payment.succeeded", reason: str | None = None):
        """Имитировать уведомление ЮKassa (в docker-compose его отправляет Postman)."""
        provider_id = booking["paymentUrl"].split("orderId=")[1]
        obj = {"id": provider_id, "status": "succeeded" if event == "payment.succeeded" else "canceled",
               "amount": {"value": f"{booking['totalPrice']:.2f}", "currency": "RUB"},
               "metadata": {"paymentId": booking["paymentId"]}}
        if reason:
            obj["cancellation_details"] = {"party": "payment_network", "reason": reason}
        return self.call("POST", "/api/v1/payments/webhooks/yookassa", json={"type": "notification", "event": event,
                                                                             "object": obj}, key=None)

    def emails_to(self, address: str) -> list[str]:
        return [m["subject"] for m in self.mailer.sent if m["to"] == address]


def iso(epoch: int) -> str:
    return to_iso(epoch)
