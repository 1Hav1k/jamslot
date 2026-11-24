"""Асинхронный обмен сообщениями.

* Конверт сообщения — как в api/asyncapi.yaml (EventEnvelope).
* Издатель пишет сообщение в таблицу outbox в той же транзакции, что и изменение данных;
  OutboxRelay публикует его в брокер отдельно (transactional outbox).
* Подписчик хранит обработанные eventId в processed_events и пропускает повторы.
* Две реализации брокера с одним интерфейсом: RabbitBroker (RabbitMQ, docker-compose)
  и InMemoryBroker (тесты и запуск без RabbitMQ).
"""
import json
import logging
import threading
import time
import uuid
from collections import deque

from . import context
from .timeutil import to_iso

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5

EXCHANGES = {
    "booking.events": "topic",
    "payment.events": "topic",
    "review.events": "topic",
    "payment.commands": "direct",
}

MESSAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id {{serial_pk}},
    event_id TEXT NOT NULL UNIQUE,
    exchange TEXT NOT NULL,
    routing_key TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    published_at BIGINT
);
CREATE INDEX IF NOT EXISTS outbox_unpublished ON outbox (id) WHERE published_at IS NULL;
CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_at BIGINT NOT NULL
);
"""


def exchange_for(event_type: str) -> str:
    if event_type == "payment.refund":
        return "payment.commands"
    prefix = event_type.split(".", 1)[0]
    return f"{prefix}.events"


def make_event(event_type: str, payload: dict, producer: str, now: int) -> dict:
    return {
        "eventId": str(uuid.uuid4()),
        "eventType": event_type,
        "schemaVersion": 1,
        "occurredAt": to_iso(now),
        "correlationId": context.ensure_correlation_id(),
        "producer": producer,
        "payload": payload,
    }


def outbox_add(tx, event: dict, now: int) -> None:
    """Записать сообщение в outbox в текущей транзакции."""
    tx.execute(
        "INSERT INTO outbox (event_id, exchange, routing_key, body, created_at) "
        "VALUES (:event_id, :exchange, :routing_key, :body, :created_at)",
        {"event_id": event["eventId"], "exchange": exchange_for(event["eventType"]),
         "routing_key": event["eventType"], "body": json.dumps(event, ensure_ascii=False),
         "created_at": now},
    )
    log.info("Событие %s (%s) записано в outbox", event["eventType"], event["eventId"])


def emit(tx, event_type: str, payload: dict, producer: str, now: int) -> dict:
    event = make_event(event_type, payload, producer, now)
    outbox_add(tx, event, now)
    return event


# --------------------------------------------------------------------------------------
# Публикация из outbox
# --------------------------------------------------------------------------------------
class OutboxRelay:
    def __init__(self, db, broker, clock):
        self.db, self.broker, self.clock = db, broker, clock

    def flush(self, limit: int = 100) -> int:
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT id, event_id, exchange, routing_key, body FROM outbox "
                "WHERE published_at IS NULL ORDER BY id LIMIT :limit", {"limit": limit})
        sent = 0
        for row in rows:
            event = json.loads(row["body"])
            self.broker.publish(row["exchange"], row["routing_key"], row["body"],
                                message_id=row["event_id"], correlation_id=event.get("correlationId"),
                                event_type=row["routing_key"])
            with self.db.transaction() as tx:
                tx.execute("UPDATE outbox SET published_at = :t WHERE id = :id",
                           {"t": self.clock.now(), "id": row["id"]})
            sent += 1
        return sent


# --------------------------------------------------------------------------------------
# Обработка входящих сообщений
# --------------------------------------------------------------------------------------
class Subscription:
    """Очередь сервиса, привязанная к exchange, и обработчики по eventType."""

    def __init__(self, db, clock, queue: str, exchange: str, handlers: dict):
        self.db, self.clock = db, clock
        self.queue, self.exchange = queue, exchange
        self.handlers = handlers

    @property
    def routing_keys(self) -> list[str]:
        return sorted(self.handlers)

    def __call__(self, body: str) -> None:
        event = json.loads(body)
        handler = self.handlers.get(event.get("eventType"))
        if handler is None:
            log.warning("Нет обработчика для %s, сообщение пропущено", event.get("eventType"))
            return
        token = context.set_correlation_id(event.get("correlationId"))
        try:
            with self.db.transaction() as tx:
                fresh = tx.execute(
                    "INSERT INTO processed_events (event_id, event_type, processed_at) "
                    "VALUES (:id, :type, :t) ON CONFLICT DO NOTHING",
                    {"id": event["eventId"], "type": event["eventType"], "t": self.clock.now()})
                if not fresh:
                    log.info("Повтор %s (%s) пропущен", event["eventType"], event["eventId"])
                    return
                log.info("Обработка %s (%s) из очереди %s", event["eventType"], event["eventId"], self.queue)
                handler(tx, event)
        finally:
            context.reset_correlation_id(token)


def _topic_match(pattern: str, key: str) -> bool:
    p, k = pattern.split("."), key.split(".")

    def m(i, j):
        if i == len(p):
            return j == len(k)
        if p[i] == "#":
            return any(m(i + 1, jj) for jj in range(j, len(k) + 1))
        if j < len(k) and (p[i] == "*" or p[i] == k[j]):
            return m(i + 1, j + 1)
        return False
    return m(0, 0)


class InMemoryBroker:
    """Брокер в памяти процесса — та же маршрутизация, что у RabbitMQ (topic/direct),
    повторная доставка до MAX_ATTEMPTS и очередь недоставленных (DLQ)."""

    def __init__(self, validator=None):
        self._lock = threading.RLock()
        self.bindings: list[tuple[str, str, str]] = []  # (exchange, routing_key, queue)
        self.queues: dict[str, deque] = {}
        self.consumers: dict[str, object] = {}
        self.dlq: dict[str, list] = {}
        self.published: list[dict] = []
        self.validator = validator

    def publish(self, exchange, routing_key, body, message_id=None, correlation_id=None, event_type=None):
        if self.validator:
            self.validator(json.loads(body))
        with self._lock:
            self.published.append(json.loads(body))
            kind = EXCHANGES.get(exchange, "topic")
            for ex, key, queue in self.bindings:
                if ex != exchange:
                    continue
                if (kind == "direct" and key == routing_key) or (kind == "topic" and _topic_match(key, routing_key)):
                    self.queues[queue].append((body, 1))

    def subscribe(self, sub: Subscription) -> None:
        with self._lock:
            self.queues.setdefault(sub.queue, deque())
            self.consumers[sub.queue] = sub
            for key in sub.routing_keys:
                self.bindings.append((sub.exchange, key, sub.queue))

    def drain(self) -> int:
        """Доставить все накопившиеся сообщения. Возвращает число доставок."""
        delivered = 0
        while True:
            with self._lock:
                item = next(((q, dq.popleft()) for q, dq in self.queues.items() if dq), None)
            if item is None:
                return delivered
            queue, (body, attempt) = item
            delivered += 1
            try:
                self.consumers[queue](body)
            except Exception:
                log.exception("Ошибка обработки в %s, попытка %d", queue, attempt)
                with self._lock:
                    if attempt < MAX_ATTEMPTS:
                        self.queues[queue].append((body, attempt + 1))
                    else:
                        self.dlq.setdefault(queue, []).append(body)

    def start(self):
        pass

    def tick(self):
        pass

    def close(self):
        pass


class RabbitBroker:
    """RabbitMQ через pika. Публикация — из потока OutboxRelay, потребление — по потоку на очередь."""

    def __init__(self, url: str):
        import pika  # ленивый импорт: тестам на InMemoryBroker pika не нужна
        self._pika = pika
        self.url = url
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self._conn = None
        self._channel = None
        self._declared: set[str] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- публикация ---------------------------------------------------------------
    def _channel_for_publish(self):
        if self._conn is None or self._conn.is_closed:
            self._conn = self._pika.BlockingConnection(self._pika.URLParameters(self.url))
            self._channel = self._conn.channel()
            self._channel.confirm_delivery()
            self._declared.clear()
        return self._channel

    def _declare_exchange(self, channel, exchange: str):
        if exchange not in self._declared:
            channel.exchange_declare(exchange=exchange, exchange_type=EXCHANGES.get(exchange, "topic"), durable=True)
            self._declared.add(exchange)

    def publish(self, exchange, routing_key, body, message_id=None, correlation_id=None, event_type=None):
        props = self._pika.BasicProperties(
            content_type="application/json", delivery_mode=2, message_id=message_id,
            correlation_id=correlation_id, type=event_type)
        data = body.encode() if isinstance(body, str) else body
        with self._lock:
            for attempt in (1, 2):
                try:
                    ch = self._channel_for_publish()
                    self._declare_exchange(ch, exchange)
                    ch.basic_publish(exchange=exchange, routing_key=routing_key, body=data, properties=props)
                    log.info("Опубликовано %s в %s", routing_key, exchange)
                    return
                except self._pika.exceptions.AMQPError:
                    self._reset()
                    if attempt == 2:
                        raise

    def _reset(self):
        try:
            if self._conn is not None and self._conn.is_open:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._channel = None

    def tick(self):
        """Обслужить heartbeat соединения публикации, пока outbox пуст."""
        with self._lock:
            if self._conn is not None and self._conn.is_open:
                try:
                    self._conn.process_data_events(time_limit=0)
                except self._pika.exceptions.AMQPError:
                    self._reset()

    # --- потребление --------------------------------------------------------------
    def subscribe(self, sub: Subscription) -> None:
        self._subs.append(sub)

    def start(self):
        for sub in self._subs:
            t = threading.Thread(target=self._consume_forever, args=(sub,), name=f"consumer-{sub.queue}", daemon=True)
            t.start()
            self._threads.append(t)

    def _consume_forever(self, sub: Subscription):
        pika = self._pika
        while not self._stop.is_set():
            conn = None
            try:
                conn = pika.BlockingConnection(pika.URLParameters(self.url))
                ch = conn.channel()
                ch.exchange_declare(exchange=sub.exchange, exchange_type=EXCHANGES.get(sub.exchange, "topic"), durable=True)
                ch.queue_declare(queue=sub.queue, durable=True)
                ch.queue_declare(queue=f"{sub.queue}.dlq", durable=True)
                for key in sub.routing_keys:
                    ch.queue_bind(queue=sub.queue, exchange=sub.exchange, routing_key=key)
                ch.basic_qos(prefetch_count=1)
                log.info("Подписка на %s (%s) по ключам %s", sub.queue, sub.exchange, ", ".join(sub.routing_keys))
                for method, props, body in ch.consume(sub.queue, inactivity_timeout=1):
                    if self._stop.is_set():
                        break
                    if method is None:
                        continue
                    self._handle(ch, sub, method, props, body)
            except Exception as exc:  # обрыв соединения, рестарт брокера и т. п. — переподключаемся
                log.warning("RabbitMQ недоступен для %s (%s), переподключение через 3 с", sub.queue, exc.__class__.__name__)
                time.sleep(3)
            finally:
                try:
                    if conn is not None and conn.is_open:
                        conn.close()
                except Exception:
                    pass

    def _handle(self, ch, sub, method, props, body):
        pika = self._pika
        try:
            sub(body.decode())
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        except Exception as exc:
            attempt = int((props.headers or {}).get("x-attempt", 1))
            log.exception("Ошибка обработки сообщения из %s, попытка %d", sub.queue, attempt)
            retry = attempt < MAX_ATTEMPTS
            target = sub.queue if retry else f"{sub.queue}.dlq"
            if retry:
                time.sleep(min(attempt, 5))
            ch.basic_publish(
                exchange="", routing_key=target, body=body,
                properties=pika.BasicProperties(
                    content_type="application/json", delivery_mode=2, message_id=props.message_id,
                    correlation_id=props.correlation_id, type=props.type,
                    headers={"x-attempt": attempt + 1, "x-last-error": str(exc)[:200]}))
            ch.basic_ack(delivery_tag=method.delivery_tag)
            if not retry:
                log.error("Сообщение %s отправлено в %s.dlq", props.message_id, sub.queue)

    def close(self):
        self._stop.set()
        with self._lock:
            self._reset()


def make_broker(url: str):
    if url.startswith("memory"):
        return InMemoryBroker()
    return RabbitBroker(url)
