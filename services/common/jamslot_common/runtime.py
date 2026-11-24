"""Сборка сервиса: БД, брокер, outbox-relay, подписки и фоновые задачи."""
import logging
import os

from .db import Database
from .idempotency import IDEMPOTENCY_SCHEMA
from .messaging import MESSAGING_SCHEMA, OutboxRelay, Subscription, make_broker
from .timeutil import SystemClock
from .web import PeriodicWorker

log = logging.getLogger(__name__)


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


class ServiceRuntime:
    def __init__(self, name: str, schema: str, *, db=None, broker=None, clock=None, settings: dict | None = None):
        self.name = name
        self.settings = settings or {}
        self.clock = clock or SystemClock()
        self.db = db or Database(self.setting("DATABASE_URL", f"sqlite:///{name}.db"))
        self.db.apply_schema(MESSAGING_SCHEMA + IDEMPOTENCY_SCHEMA + schema)
        self.broker = broker if broker is not None else make_broker(self.setting("AMQP_URL", "memory://"))
        self.relay = OutboxRelay(self.db, self.broker, self.clock)
        self.subscriptions: list[Subscription] = []
        self.workers: list[PeriodicWorker] = []
        self._periodic: list[tuple[str, float, object]] = []

    def setting(self, key: str, default=None):
        if key in self.settings:
            return self.settings[key]
        return env(key, default)

    def subscribe(self, queue: str, exchange: str, handlers: dict) -> Subscription:
        sub = Subscription(self.db, self.clock, queue, exchange, handlers)
        self.subscriptions.append(sub)
        self.broker.subscribe(sub)
        return sub

    def every(self, name: str, interval: float, fn) -> None:
        """Зарегистрировать периодическую задачу (в тестах её вызывают вручную через run_periodic)."""
        self._periodic.append((name, interval, fn))

    def run_periodic(self) -> None:
        for _, _, fn in self._periodic:
            fn()

    def start(self) -> None:
        def relay_tick():
            if not self.relay.flush():
                self.broker.tick()
        self.workers.append(PeriodicWorker(f"{self.name}-outbox", 0.5, relay_tick))
        for name, interval, fn in self._periodic:
            self.workers.append(PeriodicWorker(f"{self.name}-{name}", interval, fn))
        self.broker.start()
        for w in self.workers:
            w.start()
        log.info("%s запущен: %d подписок, %d фоновых задач", self.name, len(self.subscriptions), len(self.workers))
