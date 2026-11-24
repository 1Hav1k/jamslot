"""Idempotency-Key: повтор запроса с тем же ключом возвращает сохранённый результат.

* тот же ключ и то же тело — сохранённый ответ (без повторного выполнения);
* тот же ключ, другое тело — 422 IDEMPOTENCY_KEY_REUSED;
* запрос с этим ключом ещё выполняется — 409 IDEMPOTENCY_REQUEST_IN_PROGRESS;
* ответы 5xx не сохраняются, чтобы запрос можно было повторить;
* ключ хранится 24 часа.
"""
import hashlib
import json
import logging
import uuid

from .problems import ApiError

log = logging.getLogger(__name__)

TTL_SECONDS = 24 * 3600

IDEMPOTENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    response_status INTEGER,
    response_body TEXT,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (scope, idem_key)
);
"""


def require_key(raw: str | None) -> str:
    if not raw:
        raise ApiError(400, "IDEMPOTENCY_KEY_REQUIRED", "Заголовок Idempotency-Key обязателен для этой операции")
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise ApiError(400, "VALIDATION_ERROR", "Idempotency-Key должен быть UUID",
                       [{"field": "Idempotency-Key", "message": "ожидается UUID"}])


def request_hash(method: str, path: str, body) -> str:
    raw = json.dumps({"m": method, "p": path, "b": body}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def run_idempotent(db, clock, scope: str, key: str, req_hash: str, fn):
    """Выполнить fn() не более одного раза для (scope, key). fn возвращает (status, body)."""
    now = clock.now()
    with db.transaction() as tx:
        tx.execute("DELETE FROM idempotency_keys WHERE scope = :s AND idem_key = :k AND created_at < :old",
                   {"s": scope, "k": key, "old": now - TTL_SECONDS})
        inserted = tx.execute(
            "INSERT INTO idempotency_keys (scope, idem_key, request_hash, state, created_at) "
            "VALUES (:s, :k, :h, 'IN_PROGRESS', :t) ON CONFLICT DO NOTHING",
            {"s": scope, "k": key, "h": req_hash, "t": now})
        if not inserted:
            row = tx.one("SELECT * FROM idempotency_keys WHERE scope = :s AND idem_key = :k", {"s": scope, "k": key})
    if not inserted:
        if row["request_hash"] != req_hash:
            raise ApiError(422, "IDEMPOTENCY_KEY_REUSED",
                           "Этот Idempotency-Key уже использован для запроса с другими параметрами")
        if row["state"] != "DONE":
            raise ApiError(409, "IDEMPOTENCY_REQUEST_IN_PROGRESS", "Запрос с этим ключом ещё выполняется",
                           headers={"Retry-After": "1"})
        log.info("Повтор запроса с Idempotency-Key %s — возвращён сохранённый ответ", key)
        return row["response_status"], json.loads(row["response_body"]), True

    try:
        status, body = fn()
    except ApiError as err:
        if err.status >= 500:
            _forget(db, scope, key)
        else:
            _store(db, scope, key, err.status, {"__problem__": err.to_problem(), "headers": err.headers})
        raise
    except BaseException:
        _forget(db, scope, key)
        raise
    _store(db, scope, key, status, body)
    return status, body, False


def _store(db, scope, key, status, body):
    with db.transaction() as tx:
        tx.execute("UPDATE idempotency_keys SET state = 'DONE', response_status = :st, response_body = :b "
                   "WHERE scope = :s AND idem_key = :k",
                   {"st": status, "b": json.dumps(body, ensure_ascii=False), "s": scope, "k": key})


def _forget(db, scope, key):
    with db.transaction() as tx:
        tx.execute("DELETE FROM idempotency_keys WHERE scope = :s AND idem_key = :k", {"s": scope, "k": key})
