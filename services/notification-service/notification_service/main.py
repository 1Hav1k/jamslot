"""notification-service — письма и напоминания по событиям брони и платежей.

Сервис только подписан на события: обработчик сохраняет уведомление в статусе SCHEDULED,
отдельная фоновая задача отправляет письма через SMTP (в docker-compose — Mailpit).
Сбой SMTP не мешает обработке событий: письмо остаётся в очереди и отправляется повторно.
"""
import logging
import smtplib
import uuid
from email.message import EmailMessage

from pydantic import BaseModel, ConfigDict, Field

from jamslot_common.context import setup_logging
from jamslot_common.runtime import ServiceRuntime
from jamslot_common.timeutil import local_parts, parse_iso, to_iso
from jamslot_common.web import (create_base_app, current_user, health_route, json_response, page_body,
                                page_params, parse_body, query_enum)

SERVICE = "notification-service"
log = logging.getLogger(SERVICE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    booking_id TEXT,
    scheduled_at BIGINT NOT NULL,
    sent_at BIGINT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_due ON notifications (status, scheduled_at);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications (user_id, scheduled_at);
CREATE TABLE IF NOT EXISTS preferences (
    user_id TEXT PRIMARY KEY,
    email_enabled INTEGER NOT NULL,
    reminder_enabled INTEGER NOT NULL,
    reminder_hours INTEGER NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    updated_at BIGINT NOT NULL
);
"""

TYPES = ("BOOKING_CONFIRMED", "BOOKING_CANCELLED", "BOOKING_EXPIRED", "BOOKING_REMINDER", "REVIEW_INVITATION",
         "REFUND_COMPLETED")
DEFAULT_PREFS = {"emailEnabled": True, "reminderEnabled": True, "reminderHoursBefore": 2}
# О сбоях при создании брони клиент узнаёт из ответа API — письмо не нужно
SILENT_CANCEL_REASONS = ("EQUIPMENT_UNAVAILABLE", "PAYMENT_UNAVAILABLE")
MAX_SEND_ATTEMPTS = 5

CANCEL_TEXT = {
    "USER_REQUEST": "по вашему запросу",
    "MANAGER_REQUEST": "менеджером площадки",
    "PAYMENT_FAILED": "потому что оплата не прошла",
}


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emailEnabled: bool
    reminderEnabled: bool
    reminderHoursBefore: int = Field(ge=1, le=48)


class SmtpMailer:
    def __init__(self, host: str, port: int, sender: str):
        self.host, self.port, self.sender = host, port, sender

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=5) as smtp:
            smtp.send_message(msg)


def compose(kind: str, b: dict, tz: str, extra: dict | None = None) -> tuple[str, str]:
    """Тема и текст письма."""
    start = local_parts(parse_iso(b["startsAt"]), tz)
    end = local_parts(parse_iso(b["endsAt"]), tz)
    when = f"{start:%d.%m.%Y}, {start:%H:%M}–{end:%H:%M}"
    where = f"{b['venueName']}, {b['roomName']}"
    greet = f"Здравствуйте, {b['customer']['name']}!\n\n"
    extra = extra or {}
    if kind == "BOOKING_CONFIRMED":
        return (f"Бронь подтверждена — {b['venueName']}, {start:%d.%m}, {start:%H:%M}",
                greet + f"Оплата получена, бронь подтверждена.\n{where}\n{when}\nСумма: {b['totalPrice']:.2f} ₽")
    if kind == "BOOKING_CANCELLED":
        why = CANCEL_TEXT.get(extra.get("reason"), "")
        return (f"Бронь отменена — {b['venueName']}, {start:%d.%m}",
                greet + f"Бронь отменена {why}.\n{where}\n{when}".replace(" .", "."))
    if kind == "BOOKING_EXPIRED":
        return (f"Бронь не оплачена и снята — {b['venueName']}, {start:%d.%m}",
                greet + f"Мы не получили оплату за отведённое время, интервал освобождён.\n{where}\n{when}")
    if kind == "BOOKING_REMINDER":
        return (f"Напоминание: репетиция {start:%d.%m} в {start:%H:%M}",
                greet + f"Напоминаем о брони.\n{where}\n{when}")
    if kind == "REVIEW_INVITATION":
        return (f"Как прошла репетиция в «{b['venueName']}»?",
                greet + f"Оставьте отзыв о {where} — это поможет другим музыкантам.")
    raise ValueError(kind)


def create_app(settings: dict | None = None, *, db=None, broker=None, clock=None, mailer=None,
               start_background: bool = True):
    setup_logging(SERVICE)
    rt = ServiceRuntime(SERVICE, SCHEMA, db=db, broker=broker, clock=clock, settings=settings)
    app = create_base_app(SERVICE)
    app.extensions["runtime"] = rt
    health_route(app, rt.db)
    db, clock = rt.db, rt.clock
    tz = rt.setting("VENUE_TIMEZONE", "Europe/Moscow")
    mailer = mailer or SmtpMailer(rt.setting("SMTP_HOST", "localhost"), int(rt.setting("SMTP_PORT", "1025")),
                                  rt.setting("MAIL_FROM", "JamSlot <no-reply@jamslot.local>"))
    app.extensions["mailer"] = mailer

    def prefs(tx, user_id: str) -> dict:
        r = tx.one("SELECT * FROM preferences WHERE user_id = :u", {"u": user_id})
        if not r:
            return dict(DEFAULT_PREFS)
        return {"emailEnabled": bool(r["email_enabled"]), "reminderEnabled": bool(r["reminder_enabled"]),
                "reminderHoursBefore": r["reminder_hours"]}

    def schedule(tx, event, kind: str, user_id: str, recipient: str, subject: str, body: str,
                 booking_id: str | None, at: int) -> None:
        n = tx.execute(
            "INSERT INTO notifications (id, dedup_key, user_id, type, channel, status, recipient, subject, body, "
            "booking_id, scheduled_at, created_at) VALUES (:id, :k, :u, :type, 'EMAIL', 'SCHEDULED', :to, :s, :b, "
            ":bid, :at, :t) ON CONFLICT DO NOTHING",
            {"id": str(uuid.uuid4()), "k": f"{event['eventId']}:{kind}", "u": user_id, "type": kind, "to": recipient,
             "s": subject, "b": body, "bid": booking_id, "at": at, "t": clock.now()})
        if n:
            log.info("Уведомление %s для %s запланировано на %s", kind, user_id, to_iso(at))

    def remember_contact(tx, b: dict) -> None:
        params = {"u": b["userId"], "n": b["customer"]["name"], "e": b["customer"]["email"], "t": clock.now()}
        if not tx.execute("UPDATE contacts SET name = :n, email = :e, updated_at = :t WHERE user_id = :u", params):
            tx.execute("INSERT INTO contacts (user_id, name, email, updated_at) VALUES (:u, :n, :e, :t) "
                       "ON CONFLICT DO NOTHING", params)

    def booking_handler(kind: str):
        def handle(tx, event):
            b = event["payload"]
            remember_contact(tx, b)
            p = prefs(tx, b["userId"])
            now = clock.now()
            if kind == "BOOKING_CANCELLED":
                tx.execute("DELETE FROM notifications WHERE booking_id = :b AND type = 'BOOKING_REMINDER' "
                           "AND status = 'SCHEDULED'", {"b": b["bookingId"]})
                if b.get("reason") in SILENT_CANCEL_REASONS:
                    log.info("Отмена брони %s (%s) — клиент уже получил ошибку API, письмо не нужно",
                             b["bookingId"], b["reason"])
                    return
            if kind in ("BOOKING_CANCELLED", "BOOKING_EXPIRED") and not p["emailEnabled"]:
                log.info("Письма об отмене отключены пользователем %s", b["userId"])
                return
            subject, body = compose(kind, b, tz, {"reason": b.get("reason")})
            schedule(tx, event, kind, b["userId"], b["customer"]["email"], subject, body, b["bookingId"], now)
            if kind == "BOOKING_CONFIRMED" and p["reminderEnabled"]:
                at = parse_iso(b["startsAt"]) - p["reminderHoursBefore"] * 3600
                if at > now:
                    subject, body = compose("BOOKING_REMINDER", b, tz)
                    schedule(tx, event, "BOOKING_REMINDER", b["userId"], b["customer"]["email"], subject, body,
                             b["bookingId"], at)
        return handle

    def on_refunded(tx, event):
        p = event["payload"]
        c = tx.one("SELECT * FROM contacts WHERE user_id = :u", {"u": p["userId"]})
        name = c["name"] if c else "клиент"
        schedule(tx, event, "REFUND_COMPLETED", p["userId"], c["email"] if c else "", "Деньги возвращены",
                 f"Здравствуйте, {name}!\n\nМы вернули {p['amount']:.2f} ₽ по брони {p['bookingId']}. "
                 f"Деньги поступят на карту в течение нескольких дней.", p["bookingId"], clock.now())

    rt.subscribe("notification.booking-events", "booking.events", {
        "booking.confirmed": booking_handler("BOOKING_CONFIRMED"),
        "booking.cancelled": booking_handler("BOOKING_CANCELLED"),
        "booking.expired": booking_handler("BOOKING_EXPIRED"),
        "booking.completed": booking_handler("REVIEW_INVITATION"),
    })
    rt.subscribe("notification.payment-events", "payment.events", {"payment.refunded": on_refunded})

    # --- отправка писем -----------------------------------------------------------------
    def send_due() -> int:
        now = clock.now()
        with db.transaction() as tx:
            due = tx.query("SELECT * FROM notifications WHERE status = 'SCHEDULED' AND scheduled_at <= :now "
                           "ORDER BY scheduled_at LIMIT 50", {"now": now})
        sent = 0
        for n in due:
            try:
                if not n["recipient"]:
                    raise ValueError("нет адреса получателя")
                mailer.send(n["recipient"], n["subject"], n["body"])
            except Exception as exc:
                attempts = n["attempts"] + 1
                failed = attempts >= MAX_SEND_ATTEMPTS or isinstance(exc, ValueError)
                with db.transaction() as tx:
                    tx.execute("UPDATE notifications SET attempts = :a, last_error = :e, status = :s, "
                               "scheduled_at = :at WHERE id = :id",
                               {"a": attempts, "e": str(exc)[:300], "s": "FAILED" if failed else "SCHEDULED",
                                "at": now + 10 * attempts, "id": n["id"]})
                log.warning("Письмо %s не отправлено (%s), попытка %d", n["id"], exc, attempts)
                continue
            with db.transaction() as tx:
                tx.execute("UPDATE notifications SET status = 'SENT', sent_at = :t, attempts = attempts + 1 "
                           "WHERE id = :id", {"t": clock.now(), "id": n["id"]})
            log.info("Письмо «%s» отправлено на %s", n["subject"], n["recipient"])
            sent += 1
        return sent

    app.extensions["send_due"] = send_due
    rt.every("send-emails", 2, send_due)

    # --- API ----------------------------------------------------------------------------
    @app.get("/api/v1/notifications")
    def list_notifications():
        user = current_user()
        kind = query_enum("type", TYPES)
        page, size = page_params()
        sql, params = "FROM notifications WHERE user_id = :u", {"u": user.id}
        if kind:
            sql += " AND type = :type"
            params["type"] = kind
        with db.transaction() as tx:
            total = tx.one("SELECT COUNT(*) AS n " + sql, params)["n"]
            rows = tx.query("SELECT * " + sql + " ORDER BY scheduled_at DESC, created_at DESC LIMIT :l OFFSET :o",
                            {**params, "l": size, "o": page * size})
        items = [{"id": n["id"], "type": n["type"], "channel": n["channel"], "status": n["status"],
                  "subject": n["subject"], "bookingId": n["booking_id"], "scheduledAt": to_iso(n["scheduled_at"]),
                  "sentAt": to_iso(n["sent_at"])} for n in rows]
        return json_response(page_body(items, page, size, total))

    @app.get("/api/v1/notification-preferences")
    def get_prefs():
        user = current_user()
        with db.transaction() as tx:
            return json_response(prefs(tx, user.id))

    @app.put("/api/v1/notification-preferences")
    def put_prefs():
        user = current_user()
        data = parse_body(Preferences)
        params = {"u": user.id, "e": int(data.emailEnabled), "r": int(data.reminderEnabled),
                  "h": data.reminderHoursBefore, "t": clock.now()}
        with db.transaction() as tx:
            if not tx.execute("UPDATE preferences SET email_enabled = :e, reminder_enabled = :r, reminder_hours = :h, "
                              "updated_at = :t WHERE user_id = :u", params):
                tx.execute("INSERT INTO preferences (user_id, email_enabled, reminder_enabled, reminder_hours, "
                           "updated_at) VALUES (:u, :e, :r, :h, :t)", params)
            return json_response(prefs(tx, user.id))

    if start_background:
        rt.start()
    return app
