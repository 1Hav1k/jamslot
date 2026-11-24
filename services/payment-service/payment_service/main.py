"""payment-service — платежи через ЮKassa (в docker-compose — WireMock), уведомления шлюза, возвраты."""
import json
import logging
import uuid
from typing import Literal

from flask import Response, request
from pydantic import AnyUrl, AwareDatetime, BaseModel, ConfigDict, Field, condecimal

from jamslot_common import context
from jamslot_common.context import setup_logging
from jamslot_common.http import ServiceClient, UpstreamError, UpstreamUnavailable
from jamslot_common.messaging import emit
from jamslot_common.money import to_kopecks, to_provider_amount, to_rub
from jamslot_common.problems import ApiError, bad_request, forbidden, not_found
from jamslot_common.runtime import ServiceRuntime
from jamslot_common.timeutil import from_datetime, to_iso
from jamslot_common.web import (create_base_app, current_user, health_route, idempotent, json_response,
                                page_body, page_params, parse_body, path_uuid, query_uuid)

SERVICE = "payment-service"
log = logging.getLogger(SERVICE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    amount BIGINT NOT NULL,
    refunded_amount BIGINT NOT NULL DEFAULT 0,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    description TEXT NOT NULL,
    return_url TEXT NOT NULL,
    provider_payment_id TEXT UNIQUE,
    payment_url TEXT,
    expires_at BIGINT NOT NULL,
    created_at BIGINT NOT NULL,
    paid_at BIGINT,
    correlation_id TEXT,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_user ON payments (user_id, created_at);
CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES payments (id),
    amount BIGINT NOT NULL,
    reason TEXT NOT NULL,
    provider_refund_id TEXT,
    command_event_id TEXT NOT NULL UNIQUE,
    created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_notifications (
    id {{serial_pk}},
    provider_object_id TEXT NOT NULL,
    event TEXT NOT NULL,
    body TEXT NOT NULL,
    received_at BIGINT NOT NULL
);
"""

Amount = condecimal(gt=0, max_digits=12, decimal_places=2)


class CreatePayment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bookingId: uuid.UUID
    userId: uuid.UUID
    amount: Amount
    currency: Literal["RUB"]
    description: str = Field(min_length=1, max_length=128)
    returnUrl: AnyUrl
    expiresAt: AwareDatetime


class YooKassa:
    """Клиент платёжного шлюза. Формат запросов — API ЮKassa v3; в docker-compose его изображает WireMock
    (mocks/wiremock), в тестах — тот же набор стабов."""

    def __init__(self, client: ServiceClient):
        self.client = client

    def create_payment(self, payment_id: str, booking_id: str, amount: int, description: str, return_url: str):
        _, body = self.client.call("POST", "/v3/payments", json={
            "amount": {"value": to_provider_amount(amount), "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description,
            "metadata": {"paymentId": payment_id, "bookingId": booking_id},
        }, headers={"Idempotence-Key": payment_id}, retry=True)
        return body["id"], body["confirmation"]["confirmation_url"]

    def cancel_payment(self, provider_id: str, key: str) -> None:
        self.client.call("POST", f"/v3/payments/{provider_id}/cancel", json={},
                         headers={"Idempotence-Key": key}, retry=True)

    def create_refund(self, provider_id: str, amount: int, key: str) -> str:
        _, body = self.client.call("POST", "/v3/refunds", json={
            "payment_id": provider_id,
            "amount": {"value": to_provider_amount(amount), "currency": "RUB"},
        }, headers={"Idempotence-Key": key}, retry=True)
        return body["id"]


def payment_dto(p: dict) -> dict:
    return {
        "id": p["id"], "bookingId": p["booking_id"], "userId": p["user_id"], "amount": to_rub(p["amount"]),
        "refundedAmount": to_rub(p["refunded_amount"]), "currency": p["currency"], "status": p["status"],
        "paymentUrl": p["payment_url"] if p["status"] == "PENDING" else None,
        "expiresAt": to_iso(p["expires_at"]), "createdAt": to_iso(p["created_at"]), "paidAt": to_iso(p["paid_at"]),
    }


def create_app(settings: dict | None = None, *, db=None, broker=None, clock=None, http_session=None,
               start_background: bool = True):
    setup_logging(SERVICE)
    rt = ServiceRuntime(SERVICE, SCHEMA, db=db, broker=broker, clock=clock, settings=settings)
    app = create_base_app(SERVICE)
    app.extensions["runtime"] = rt
    health_route(app, rt.db)
    db, clock = rt.db, rt.clock
    provider = YooKassa(ServiceClient(
        "yookassa", rt.setting("YOOKASSA_URL", "http://localhost:8089"), session=http_session,
        timeout=float(rt.setting("YOOKASSA_TIMEOUT", "5")), caller=SERVICE,
        auth=(rt.setting("YOOKASSA_SHOP_ID", "123456"), rt.setting("YOOKASSA_SECRET_KEY", "test_secret"))))
    app.extensions["provider"] = provider

    def load(tx, payment_id: str) -> dict:
        p = tx.one("SELECT * FROM payments WHERE id = :id", {"id": payment_id})
        if not p:
            raise not_found("Платёж не найден")
        return p

    # --- публичный API ------------------------------------------------------------------
    @app.get("/api/v1/payments")
    def list_payments():
        user = current_user()
        page, size = page_params()
        booking_id = query_uuid("bookingId")
        sql, params = "FROM payments WHERE user_id = :u", {"u": user.id}
        if booking_id:
            sql += " AND booking_id = :b"
            params["b"] = booking_id
        with db.transaction() as tx:
            total = tx.one("SELECT COUNT(*) AS n " + sql, params)["n"]
            rows = tx.query("SELECT * " + sql + " ORDER BY created_at DESC LIMIT :l OFFSET :o",
                            {**params, "l": size, "o": page * size})
        return json_response(page_body([payment_dto(p) for p in rows], page, size, total))

    @app.get("/api/v1/payments/<payment_id>")
    def get_payment(payment_id):
        user = current_user()
        with db.transaction() as tx:
            p = load(tx, path_uuid(payment_id))
        if p["user_id"] != user.id and not user.is_admin:
            raise forbidden("Платёж принадлежит другому пользователю")
        return json_response(payment_dto(p))

    # --- уведомления платёжного шлюза ---------------------------------------------------
    @app.post("/api/v1/payments/webhooks/yookassa")
    def webhook():
        data = request.get_json(silent=True)
        obj = (data or {}).get("object") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("type") != "notification" or not isinstance(obj, dict) \
                or not obj.get("id") or data.get("event") not in ("payment.succeeded", "payment.canceled", "refund.succeeded"):
            raise bad_request("Некорректное уведомление платёжного шлюза")
        event, provider_id, now = data["event"], str(obj["id"]), clock.now()
        with db.transaction() as tx:
            tx.execute("INSERT INTO provider_notifications (provider_object_id, event, body, received_at) "
                       "VALUES (:o, :e, :b, :t)",
                       {"o": provider_id, "e": event, "b": json.dumps(data, ensure_ascii=False), "t": now})
            if event == "refund.succeeded":
                log.info("Шлюз подтвердил возврат %s", provider_id)
                return Response(status=200)
            p = tx.one("SELECT * FROM payments WHERE provider_payment_id = :o", {"o": provider_id})
            if not p:
                log.warning("Уведомление %s по неизвестному платежу %s — пропущено", event, provider_id)
                return Response(status=200)
            tx.lock("payments", "id", p["id"])
            p = load(tx, p["id"])
            # события по уведомлению шлюза продолжают сценарий, начатый созданием брони
            context.set_correlation_id(p["correlation_id"] or context.get_correlation_id())
            if event == "payment.succeeded":
                if p["status"] in ("SUCCEEDED", "REFUNDED"):
                    log.info("Платёж %s уже в статусе %s — повторное уведомление", p["id"], p["status"])
                else:
                    # CANCELLED тоже: оплата могла пройти раньше, чем шлюз принял отмену
                    tx.execute("UPDATE payments SET status = 'SUCCEEDED', paid_at = :t, updated_at = :t WHERE id = :id",
                               {"t": now, "id": p["id"]})
                    emit(tx, "payment.completed", {"paymentId": p["id"], "bookingId": p["booking_id"],
                                                   "amount": to_rub(p["amount"]), "currency": p["currency"],
                                                   "paidAt": to_iso(now)}, SERVICE, now)
                    log.info("Платёж %s оплачен (был %s)", p["id"], p["status"])
            else:  # payment.canceled
                if p["status"] == "PENDING":
                    reason = {"general_decline": "DECLINED", "expired_on_confirmation": "CANCELLED_BY_USER"}.get(
                        ((obj.get("cancellation_details") or {}).get("reason")), "DECLINED")
                    tx.execute("UPDATE payments SET status = 'FAILED', updated_at = :t WHERE id = :id",
                               {"t": now, "id": p["id"]})
                    emit(tx, "payment.failed", {"paymentId": p["id"], "bookingId": p["booking_id"], "reason": reason},
                         SERVICE, now)
                    log.info("Платёж %s отклонён шлюзом (%s)", p["id"], reason)
                else:
                    log.info("Отмена платежа %s в статусе %s — без изменений", p["id"], p["status"])
        return Response(status=200)

    # --- внутренний API: создание платежа для booking-service ---------------------------
    @app.post("/internal/v1/payments")
    def create_payment():
        caller = request.headers.get("X-Service-Name", "internal")

        def do():
            data = parse_body(CreatePayment)
            booking_id, amount, now = str(data.bookingId), to_kopecks(data.amount), clock.now()
            with db.transaction() as tx:
                p = tx.one("SELECT * FROM payments WHERE booking_id = :b", {"b": booking_id})
                if p and p["amount"] != amount:
                    raise ApiError(409, "PAYMENT_ALREADY_EXISTS", "Для брони уже создан платёж на другую сумму")
                if p and not (p["status"] == "FAILED" and p["provider_payment_id"] is None):
                    log.info("Платёж по брони %s уже существует — возвращаем его", booking_id)
                    return 201, payment_dto(p)
                if p is None:
                    pid = str(uuid.uuid4())
                    tx.execute(
                        "INSERT INTO payments (id, booking_id, user_id, amount, currency, status, description, "
                        "return_url, expires_at, correlation_id, created_at, updated_at) VALUES (:id, :b, :u, :a, :c, "
                        "'PENDING', :d, :r, :e, :cid, :t, :t)",
                        {"id": pid, "b": booking_id, "u": str(data.userId), "a": amount, "c": data.currency,
                         "d": data.description, "r": str(data.returnUrl), "e": from_datetime(data.expiresAt),
                         "cid": context.get_correlation_id(), "t": now})
                else:  # прошлая попытка не дошла до шлюза — пробуем снова с тем же платежом
                    pid = p["id"]
                    tx.execute("UPDATE payments SET status = 'PENDING', updated_at = :t WHERE id = :id", {"t": now, "id": pid})
            try:
                provider_id, url = provider.create_payment(pid, booking_id, amount, data.description, str(data.returnUrl))
            except (UpstreamError, UpstreamUnavailable, KeyError, TypeError) as exc:
                with db.transaction() as tx:
                    tx.execute("UPDATE payments SET status = 'FAILED', updated_at = :t WHERE id = :id",
                               {"t": clock.now(), "id": pid})
                if isinstance(exc, UpstreamUnavailable):
                    raise ApiError(503, "SERVICE_UNAVAILABLE", "Платёжный шлюз не ответил", headers={"Retry-After": "5"})
                raise ApiError(502, "PAYMENT_PROVIDER_ERROR", "Шлюз отклонил создание платежа")
            with db.transaction() as tx:
                tx.execute("UPDATE payments SET provider_payment_id = :o, payment_url = :u, updated_at = :t WHERE id = :id",
                           {"o": provider_id, "u": url, "t": clock.now(), "id": pid})
                p = load(tx, pid)
            log.info("Платёж %s создан у шлюза (%s)", pid, provider_id)
            return 201, payment_dto(p)
        return idempotent(db, clock, caller, do)

    # --- события и команды --------------------------------------------------------------
    def on_booking_closed(tx, event):
        booking_id = event["payload"]["bookingId"]
        p = tx.one("SELECT * FROM payments WHERE booking_id = :b", {"b": booking_id})
        if p:
            tx.lock("payments", "id", p["id"])
            p = load(tx, p["id"])
        if not p or p["status"] != "PENDING":
            log.info("Бронь %s закрыта, платёж %s — отменять нечего", booking_id, p["status"] if p else "отсутствует")
            return
        if p["provider_payment_id"]:
            provider.cancel_payment(p["provider_payment_id"], f"cancel-{p['id']}")
        tx.execute("UPDATE payments SET status = 'CANCELLED', updated_at = :t WHERE id = :id AND status = 'PENDING'",
                   {"t": clock.now(), "id": p["id"]})
        log.info("Платёж %s отменён (бронь %s: %s)", p["id"], booking_id, event["eventType"])

    def on_refund(tx, event):
        cmd = event["payload"]
        p = tx.one("SELECT * FROM payments WHERE id = :id", {"id": cmd["paymentId"]})
        if not p:
            log.error("Команда возврата по неизвестному платежу %s", cmd["paymentId"])
            return
        tx.lock("payments", "id", p["id"])
        p = load(tx, p["id"])
        if p["status"] != "SUCCEEDED":
            log.warning("Возврат по платежу %s в статусе %s невозможен — команда пропущена", p["id"], p["status"])
            return
        amount = min(to_kopecks(cmd["amount"]), p["amount"] - p["refunded_amount"])
        refund_id = provider.create_refund(p["provider_payment_id"], amount, event["eventId"])
        now = clock.now()
        rid = str(uuid.uuid4())
        tx.execute("INSERT INTO refunds (id, payment_id, amount, reason, provider_refund_id, command_event_id, "
                   "created_at) VALUES (:id, :p, :a, :r, :o, :e, :t)",
                   {"id": rid, "p": p["id"], "a": amount, "r": cmd["reason"], "o": refund_id, "e": event["eventId"], "t": now})
        refunded = p["refunded_amount"] + amount
        tx.execute("UPDATE payments SET refunded_amount = :r, status = :s, updated_at = :t WHERE id = :id",
                   {"r": refunded, "s": "REFUNDED" if refunded >= p["amount"] else "SUCCEEDED", "t": now, "id": p["id"]})
        emit(tx, "payment.refunded", {"paymentId": p["id"], "refundId": rid, "bookingId": p["booking_id"],
                                      "userId": p["user_id"], "amount": to_rub(amount), "currency": p["currency"]},
             SERVICE, now)
        log.info("Возврат %s по платежу %s выполнен (%s)", rid, p["id"], cmd["reason"])

    rt.subscribe("payment.booking-events", "booking.events",
                 {"booking.cancelled": on_booking_closed, "booking.expired": on_booking_closed})
    rt.subscribe("payment.refund-commands", "payment.commands", {"payment.refund": on_refund})

    if start_background:
        rt.start()
    return app
