"""Unit-тесты общей библиотеки: идемпотентность, брокер, дедупликация, HTTP-клиент, ошибки."""
import json

import pytest
import requests

from jamslot_common.db import Database, to_pyformat
from jamslot_common.http import ServiceClient, UpstreamError, UpstreamUnavailable
from jamslot_common.idempotency import IDEMPOTENCY_SCHEMA, request_hash, require_key, run_idempotent
from jamslot_common.messaging import (MESSAGING_SCHEMA, InMemoryBroker, OutboxRelay, Subscription, _topic_match,
                                      emit)
from jamslot_common.problems import ApiError
from jamslot_common.timeutil import ManualClock


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 't.db'}")
    d.apply_schema(MESSAGING_SCHEMA + IDEMPOTENCY_SCHEMA + "CREATE TABLE IF NOT EXISTS hits (id TEXT PRIMARY KEY);")
    return d


def test_pyformat_conversion_keeps_casts_and_escapes_percent():
    assert to_pyformat("SELECT a::text FROM t WHERE b = :b AND c LIKE '%x'") == \
        "SELECT a::text FROM t WHERE b = %(b)s AND c LIKE '%%x'"


# --- идемпотентность ------------------------------------------------------------------------
def test_idempotency_runs_once_and_replays(db):
    clock, calls = ManualClock(1000), []

    def fn():
        calls.append(1)
        return 201, {"id": "x"}
    h = request_hash("POST", "/a", {"q": 1})
    assert run_idempotent(db, clock, "u", "k1", h, fn) == (201, {"id": "x"}, False)
    assert run_idempotent(db, clock, "u", "k1", h, fn) == (201, {"id": "x"}, True)
    assert len(calls) == 1
    with pytest.raises(ApiError) as e:
        run_idempotent(db, clock, "u", "k1", request_hash("POST", "/a", {"q": 2}), fn)
    assert e.value.code == "IDEMPOTENCY_KEY_REUSED"
    # другой пользователь с тем же ключом — независимый запрос
    assert run_idempotent(db, clock, "other", "k1", h, fn)[2] is False


def test_idempotency_does_not_store_5xx_and_expires(db):
    clock = ManualClock(1000)

    def boom():
        raise ApiError(503, "SERVICE_UNAVAILABLE")
    with pytest.raises(ApiError):
        run_idempotent(db, clock, "u", "k", "h", boom)
    assert run_idempotent(db, clock, "u", "k", "h", lambda: (200, {}))[2] is False
    clock.advance(24 * 3600 + 1)
    assert run_idempotent(db, clock, "u", "k", "h2", lambda: (200, {"n": 2}))[1] == {"n": 2}


def test_idempotency_key_must_be_uuid():
    with pytest.raises(ApiError) as e:
        require_key(None)
    assert e.value.code == "IDEMPOTENCY_KEY_REQUIRED"
    with pytest.raises(ApiError):
        require_key("abc")


# --- брокер и outbox ------------------------------------------------------------------------
def test_topic_routing():
    assert _topic_match("booking.*", "booking.confirmed")
    assert _topic_match("#", "payment.completed")
    assert not _topic_match("booking.confirmed", "booking.cancelled")


def test_outbox_relay_and_dedup(db):
    clock, broker, seen = ManualClock(1000), InMemoryBroker(), []
    sub = Subscription(db, clock, "q", "booking.events", {"booking.confirmed": lambda tx, e: seen.append(e["eventId"])})
    broker.subscribe(sub)
    with db.transaction() as tx:
        event = emit(tx, "booking.confirmed", {"bookingId": "b1"}, "test", clock.now())
    assert OutboxRelay(db, broker, clock).flush() == 1
    assert OutboxRelay(db, broker, clock).flush() == 0  # уже опубликовано
    broker.publish("booking.events", "booking.confirmed", json.dumps(event))  # повторная доставка
    broker.drain()
    assert seen == [event["eventId"]]


def test_failed_handler_is_retried_then_dead_lettered(db):
    clock, broker, attempts = ManualClock(1000), InMemoryBroker(), []

    def handler(tx, e):
        attempts.append(1)
        raise RuntimeError("сбой")
    broker.subscribe(Subscription(db, clock, "q", "payment.events", {"payment.failed": handler}))
    with db.transaction() as tx:
        emit(tx, "payment.failed", {}, "test", clock.now())
    OutboxRelay(db, broker, clock).flush()
    broker.drain()
    assert len(attempts) == 5 and len(broker.dlq["q"]) == 1
    with db.transaction() as tx:  # неудачная обработка не помечена как выполненная
        assert tx.one("SELECT COUNT(*) AS n FROM processed_events")["n"] == 0


def test_handler_changes_roll_back_on_failure(db):
    clock, broker = ManualClock(1000), InMemoryBroker()

    def handler(tx, e):
        tx.execute("INSERT INTO hits (id) VALUES ('x')")
        raise RuntimeError("после записи")
    broker.subscribe(Subscription(db, clock, "q", "booking.events", {"booking.expired": handler}))
    with db.transaction() as tx:
        emit(tx, "booking.expired", {}, "test", clock.now())
    OutboxRelay(db, broker, clock).flush()
    broker.drain()
    with db.transaction() as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM hits")["n"] == 0


# --- HTTP-клиент ----------------------------------------------------------------------------
class ScriptedSession:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("headers", {})))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out

        class R:
            status_code = out[0]

            def json(self_inner):
                return out[1]
        return R()


def test_client_retries_get_once_then_succeeds():
    s = ScriptedSession(requests.Timeout(), (200, {"ok": True}))
    assert ServiceClient("v", "http://v", session=s).call("GET", "/x") == (200, {"ok": True})
    assert len(s.calls) == 2


def test_client_does_not_retry_post_without_key():
    s = ScriptedSession(requests.ConnectionError())
    with pytest.raises(UpstreamUnavailable):
        ServiceClient("v", "http://v", session=s).call("POST", "/x", json={})
    assert len(s.calls) == 1


def test_client_passes_4xx_as_contract_error_and_keys():
    s = ScriptedSession((409, {"code": "EQUIPMENT_UNAVAILABLE"}))
    with pytest.raises(UpstreamError) as e:
        ServiceClient("e", "http://e", session=s, caller="booking").call("POST", "/r", json={}, idempotency_key="k")
    assert e.value.status == 409 and e.value.problem["code"] == "EQUIPMENT_UNAVAILABLE"
    headers = s.calls[0][2]
    assert headers["Idempotency-Key"] == "k" and headers["X-Service-Name"] == "booking" and headers["X-Correlation-Id"]


def test_circuit_breaker_opens_after_failures():
    s = ScriptedSession(*[requests.ConnectionError()] * 10)
    client = ServiceClient("p", "http://p", session=s, retries=0)
    for _ in range(5):
        with pytest.raises(UpstreamUnavailable):
            client.call("POST", "/x")
    with pytest.raises(UpstreamUnavailable) as e:
        client.call("POST", "/x")
    assert "circuit breaker" in str(e.value) and len(s.calls) == 5


def test_problem_details_shape():
    body = ApiError(409, "SLOT_ALREADY_TAKEN", "занято").to_problem("/api/v1/bookings", "cid")
    assert body == {"type": "urn:jamslot:problem:slot-already-taken", "title": "Интервал уже занят", "status": 409,
                    "code": "SLOT_ALREADY_TAKEN", "detail": "занято", "instance": "/api/v1/bookings", "traceId": "cid"}
