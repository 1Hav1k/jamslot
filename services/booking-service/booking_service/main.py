"""booking-service — брони комнат и оркестрация саги «бронь → резерв оборудования → платёж»."""
import logging
import uuid

from flask import request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from jamslot_common import context
from jamslot_common.context import setup_logging
from jamslot_common.http import ServiceClient, UpstreamError, UpstreamUnavailable
from jamslot_common.messaging import emit
from jamslot_common.money import to_kopecks, to_rub
from jamslot_common.problems import ApiError, bad_request, forbidden, not_found
from jamslot_common.runtime import ServiceRuntime
from jamslot_common.timeutil import day_bounds, from_datetime, local_parts, to_iso
from jamslot_common.web import (create_base_app, current_user, health_route, idempotent, json_response,
                                page_body, page_params, parse_body, path_uuid, query_enum, query_uuid)

from . import domain

SERVICE = "booking-service"
log = logging.getLogger(SERVICE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    room_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    venue_name TEXT NOT NULL,
    room_name TEXT NOT NULL,
    starts_at BIGINT NOT NULL,
    ends_at BIGINT NOT NULL,
    status TEXT NOT NULL,
    room_price BIGINT NOT NULL,
    equipment_price BIGINT NOT NULL DEFAULT 0,
    reservation_id TEXT,
    payment_id TEXT,
    payment_url TEXT,
    expires_at BIGINT NOT NULL,
    cancel_reason TEXT,
    comment TEXT,
    correlation_id TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL{{pg:,
    CONSTRAINT bookings_no_overlap EXCLUDE USING gist (
        room_id WITH =, int8range(starts_at, ends_at) WITH &&
    ) WHERE (status IN ('PENDING_PAYMENT', 'CONFIRMED'))}}
);
CREATE INDEX IF NOT EXISTS bookings_room_time ON bookings (room_id, starts_at);
CREATE INDEX IF NOT EXISTS bookings_user ON bookings (user_id, created_at);
CREATE INDEX IF NOT EXISTS bookings_status ON bookings (status, expires_at);
CREATE TABLE IF NOT EXISTS booking_equipment (
    booking_id TEXT NOT NULL REFERENCES bookings (id),
    item_id TEXT NOT NULL,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price BIGINT NOT NULL,
    PRIMARY KEY (booking_id, item_id)
);
"""

STATUSES = ("PENDING_PAYMENT", "CONFIRMED", "CANCELLED", "EXPIRED", "COMPLETED")


class EquipmentLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    itemId: uuid.UUID
    quantity: int = Field(ge=1, le=10)


class CreateBooking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roomId: uuid.UUID
    startsAt: AwareDatetime
    endsAt: AwareDatetime
    equipment: list[EquipmentLine] = Field(default_factory=list, max_length=10)
    comment: str | None = Field(default=None, max_length=500)

    @field_validator("equipment")
    @classmethod
    def unique_items(cls, lines):
        ids = [l.itemId for l in lines]
        if len(ids) != len(set(ids)):
            raise ValueError("позиции оборудования не должны повторяться")
        return lines


class CancelBooking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=500)


def step_key(booking_id: str, step: str) -> str:
    """Стабильный Idempotency-Key для шага саги: повтор шага не создаст дубль у соседа."""
    return str(uuid.uuid5(uuid.UUID(booking_id), step))


def create_app(settings: dict | None = None, *, db=None, broker=None, clock=None, http_session=None,
               start_background: bool = True):
    setup_logging(SERVICE)
    rt = ServiceRuntime(SERVICE, SCHEMA, db=db, broker=broker, clock=clock, settings=settings)
    app = create_base_app(SERVICE)
    app.extensions["runtime"] = rt
    health_route(app, rt.db)
    db, clock = rt.db, rt.clock
    tz = rt.setting("VENUE_TIMEZONE", "Europe/Moscow")
    hold_seconds = int(rt.setting("BOOKING_HOLD_SECONDS", "900"))
    public_url = rt.setting("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/")

    venue = ServiceClient("venue-service", rt.setting("VENUE_SERVICE_URL", "http://venue-service:8080"),
                          session=http_session, timeout=1.0, caller=SERVICE)
    equipment = ServiceClient("equipment-service", rt.setting("EQUIPMENT_SERVICE_URL", "http://equipment-service:8080"),
                              session=http_session, timeout=2.0, caller=SERVICE)
    payment = ServiceClient("payment-service", rt.setting("PAYMENT_SERVICE_URL", "http://payment-service:8080"),
                            session=http_session, timeout=5.0, caller=SERVICE)
    app.extensions["clients"] = {"venue": venue, "equipment": equipment, "payment": payment}

    # --- представление ------------------------------------------------------------------
    def booking_dto(tx, b: dict) -> dict:
        lines = tx.query("SELECT * FROM booking_equipment WHERE booking_id = :id ORDER BY name", {"id": b["id"]})
        pending = b["status"] == "PENDING_PAYMENT"
        return {
            "id": b["id"], "userId": b["user_id"], "roomId": b["room_id"], "venueId": b["venue_id"],
            "venueName": b["venue_name"], "roomName": b["room_name"],
            "startsAt": to_iso(b["starts_at"]), "endsAt": to_iso(b["ends_at"]), "status": b["status"],
            "equipment": [{"itemId": l["item_id"], "name": l["name"], "quantity": l["quantity"],
                           "price": to_rub(l["price"])} for l in lines],
            "roomPrice": to_rub(b["room_price"]), "equipmentPrice": to_rub(b["equipment_price"]),
            "totalPrice": to_rub(b["room_price"] + b["equipment_price"]), "currency": "RUB",
            "paymentId": b["payment_id"], "paymentUrl": b["payment_url"] if pending else None,
            "expiresAt": to_iso(b["expires_at"]) if pending else None,
            "cancelReason": b["cancel_reason"], "createdAt": to_iso(b["created_at"]),
        }

    def snapshot(b: dict) -> dict:
        return {
            "bookingId": b["id"], "userId": b["user_id"],
            "customer": {"name": b["customer_name"], "email": b["customer_email"]},
            "venueId": b["venue_id"], "roomId": b["room_id"], "venueName": b["venue_name"], "roomName": b["room_name"],
            "startsAt": to_iso(b["starts_at"]), "endsAt": to_iso(b["ends_at"]),
            "totalPrice": to_rub(b["room_price"] + b["equipment_price"]), "currency": "RUB",
        }

    def load(tx, booking_id: str, lock: bool = False) -> dict:
        if lock:
            tx.lock("bookings", "id", booking_id)
        b = tx.one("SELECT * FROM bookings WHERE id = :id", {"id": booking_id})
        if not b:
            raise not_found("Бронь не найдена")
        return b

    def can_view(user, b: dict) -> bool:
        return b["user_id"] == user.id or user.is_admin or \
            (user.has_role("venue_manager") and b["venue_id"] in user.venue_ids)

    def hhmm(epoch: int) -> str:
        return local_parts(epoch, tz).strftime("%H:%M")

    # --- переходы статуса с публикацией событий -----------------------------------------
    def transition(tx, b: dict, target: str, reason: str | None = None, emit_event: bool = True) -> bool:
        if not domain.can_transition(b["status"], target):
            return False
        n = tx.execute("UPDATE bookings SET status = :to, cancel_reason = COALESCE(:r, cancel_reason), updated_at = :t "
                       "WHERE id = :id AND status = :from",
                       {"to": target, "r": reason, "t": clock.now(), "id": b["id"], "from": b["status"]})
        if not n:
            return False
        log.info("Бронь %s: %s → %s%s", b["id"], b["status"], target, f" ({reason})" if reason else "")
        b = dict(b, status=target, cancel_reason=reason or b["cancel_reason"])
        if emit_event:
            payload = snapshot(b)
            if target == "CANCELLED":
                payload["reason"] = reason
            emit(tx, f"booking.{target.lower()}", payload, SERVICE, clock.now())
        return True

    def request_refund(tx, b: dict, amount: float, payment_id: str, reason: str) -> None:
        emit(tx, "payment.refund", {"paymentId": payment_id, "bookingId": b["id"], "amount": amount,
                                    "currency": "RUB", "reason": reason}, SERVICE, clock.now())
        log.info("Бронь %s: запрошен возврат %.2f ₽ (%s)", b["id"], amount, reason)

    def compensate(booking_id: str, reason: str, notify_others: bool) -> None:
        """Отменить бронь при сбое синхронного шага саги."""
        with db.transaction() as tx:
            b = load(tx, booking_id, lock=True)
            transition(tx, b, "CANCELLED", reason, emit_event=notify_others)

    # --- создание брони: оркестрация саги -----------------------------------------------
    @app.post("/api/v1/bookings")
    def create_booking():
        user = current_user()

        def do():
            data = parse_body(CreateBooking)
            starts, ends = from_datetime(data.startsAt), from_datetime(data.endsAt)
            room_id = str(data.roomId)

            # Шаг 1. venue-service: интервал допустим, стоимость комнаты
            try:
                _, quote = venue.call("GET", f"/internal/v1/rooms/{room_id}/quote",
                                      params={"startsAt": to_iso(starts), "endsAt": to_iso(ends)})
            except UpstreamError as e:
                raise ApiError.from_problem(e.status, e.problem)
            except UpstreamUnavailable:
                raise ApiError(503, "SERVICE_UNAVAILABLE", "Каталог площадок временно недоступен",
                               headers={"Retry-After": "5"})

            # Шаг 2. Локальная транзакция: удержать интервал
            expire_due()  # освободить просроченные удержания до проверки пересечений
            bid, now = str(uuid.uuid4()), clock.now()
            try:
                with db.transaction() as tx:
                    clash = tx.one(
                        "SELECT starts_at, ends_at FROM bookings WHERE room_id = :r "
                        "AND status IN ('PENDING_PAYMENT', 'CONFIRMED') AND starts_at < :e AND ends_at > :s "
                        "ORDER BY starts_at LIMIT 1", {"r": room_id, "s": starts, "e": ends})
                    if clash:
                        raise ApiError(409, "SLOT_ALREADY_TAKEN",
                                       f"Комната занята с {hhmm(clash['starts_at'])} до {hhmm(clash['ends_at'])}")
                    tx.execute(
                        "INSERT INTO bookings (id, user_id, customer_name, customer_email, room_id, venue_id, venue_name, "
                        "room_name, starts_at, ends_at, status, room_price, expires_at, comment, correlation_id, "
                        "created_at, updated_at) VALUES (:id, :u, :cn, :ce, :r, :v, :vn, :rn, :s, :e, 'PENDING_PAYMENT', "
                        ":p, :exp, :c, :cid, :t, :t)",
                        {"id": bid, "u": user.id, "cn": user.name or "Клиент", "ce": user.email or "",
                         "r": room_id, "v": quote["venueId"], "vn": quote["venueName"], "rn": quote["roomName"],
                         "s": starts, "e": ends, "p": to_kopecks(quote["totalPrice"]), "exp": now + hold_seconds,
                         "c": data.comment, "cid": context.get_correlation_id(), "t": now})
            except db.integrity_errors:  # ограничение EXCLUDE в PostgreSQL поймало гонку
                raise ApiError(409, "SLOT_ALREADY_TAKEN", "Интервал только что заняли")
            log.info("Бронь %s создана, интервал удержан до %s", bid, to_iso(now + hold_seconds))

            # Шаг 3. equipment-service: резерв оборудования
            if data.equipment:
                try:
                    _, res = equipment.call("POST", "/internal/v1/reservations", idempotency_key=step_key(bid, "reserve"),
                                            json={"bookingId": bid, "venueId": quote["venueId"],
                                                  "startsAt": to_iso(starts), "endsAt": to_iso(ends),
                                                  "holdUntil": to_iso(now + hold_seconds + domain.EQUIPMENT_HOLD_GRACE),
                                                  "items": [{"itemId": str(l.itemId), "quantity": l.quantity}
                                                            for l in data.equipment]})
                except UpstreamError as e:  # резерв не создан: компенсировать у соседей нечего
                    compensate(bid, "EQUIPMENT_UNAVAILABLE", notify_others=False)
                    raise ApiError.from_problem(e.status, e.problem)
                except UpstreamUnavailable:  # результат неизвестен: просим equipment снять резерв, если он есть
                    compensate(bid, "EQUIPMENT_UNAVAILABLE", notify_others=True)
                    raise ApiError(503, "SERVICE_UNAVAILABLE", "Сервис оборудования не ответил, бронь отменена",
                                   headers={"Retry-After": "5"})
                with db.transaction() as tx:
                    for line in res["items"]:
                        tx.execute("INSERT INTO booking_equipment (booking_id, item_id, name, quantity, price) "
                                   "VALUES (:b, :i, :n, :q, :p)",
                                   {"b": bid, "i": line["itemId"], "n": line["name"], "q": line["quantity"],
                                    "p": to_kopecks(line["price"])})
                    tx.execute("UPDATE bookings SET reservation_id = :r, equipment_price = :p, updated_at = :t "
                               "WHERE id = :id",
                               {"r": res["id"], "p": to_kopecks(res["totalPrice"]), "t": clock.now(), "id": bid})

            # Шаг 4. payment-service: платёж и ссылка на оплату
            with db.transaction() as tx:
                b = load(tx, bid)
            try:
                _, pay = payment.call("POST", "/internal/v1/payments", idempotency_key=step_key(bid, "payment"),
                                      json={"bookingId": bid, "userId": user.id,
                                            "amount": to_rub(b["room_price"] + b["equipment_price"]), "currency": "RUB",
                                            "description": f"{b['venue_name']}, {b['room_name']}, "
                                                           f"{local_parts(starts, tz):%d.%m %H:%M}–{hhmm(ends)}"[:128],
                                            "returnUrl": f"{public_url}/bookings/{bid}",
                                            "expiresAt": to_iso(b["expires_at"])})
            except (UpstreamError, UpstreamUnavailable) as exc:
                # резерв оборудования снимет equipment-service, созданный платёж отменит payment-service
                compensate(bid, "PAYMENT_UNAVAILABLE", notify_others=True)
                log.warning("Платёж по брони %s не создан: %s", bid, exc)
                raise ApiError(503, "SERVICE_UNAVAILABLE", "Не удалось создать платёж, бронь отменена",
                               headers={"Retry-After": "5"})
            with db.transaction() as tx:
                tx.execute("UPDATE bookings SET payment_id = :p, payment_url = :u, updated_at = :t WHERE id = :id",
                           {"p": pay["id"], "u": pay.get("paymentUrl"), "t": clock.now(), "id": bid})
                return 201, booking_dto(tx, load(tx, bid))
        return idempotent(db, clock, user.id, do)

    # --- чтение -------------------------------------------------------------------------
    @app.get("/api/v1/bookings")
    def list_bookings():
        user = current_user()
        status = query_enum("status", STATUSES)
        venue_id = query_uuid("venueId")
        page, size = page_params()
        if venue_id:
            if not (user.is_admin or (user.has_role("venue_manager") and venue_id in user.venue_ids)):
                raise forbidden("Брони площадки доступны только её менеджеру")
            sql, params = "FROM bookings WHERE venue_id = :v", {"v": venue_id}
        else:
            sql, params = "FROM bookings WHERE user_id = :u", {"u": user.id}
        if status:
            sql += " AND status = :st"
            params["st"] = status
        with db.transaction() as tx:
            total = tx.one("SELECT COUNT(*) AS n " + sql, params)["n"]
            rows = tx.query("SELECT * " + sql + " ORDER BY created_at DESC, id LIMIT :l OFFSET :o",
                            {**params, "l": size, "o": page * size})
            items = [booking_dto(tx, b) for b in rows]
        return json_response(page_body(items, page, size, total))

    @app.get("/api/v1/bookings/<booking_id>")
    def get_booking(booking_id):
        user = current_user()
        with db.transaction() as tx:
            b = load(tx, path_uuid(booking_id))
            if not can_view(user, b):
                raise forbidden("Бронь принадлежит другому пользователю")
            return json_response(booking_dto(tx, b))

    @app.post("/api/v1/bookings/<booking_id>/cancel")
    def cancel_booking(booking_id):
        user = current_user()
        booking_id = path_uuid(booking_id)

        def do():
            parse_body(CancelBooking, required=False)
            with db.transaction() as tx:
                b = load(tx, booking_id, lock=True)
                owner = b["user_id"] == user.id
                manager = user.is_admin or (user.has_role("venue_manager") and b["venue_id"] in user.venue_ids)
                if not (owner or manager):
                    raise forbidden("Отменить бронь может её владелец или менеджер площадки")
                if b["status"] not in domain.CANCELLABLE:
                    raise ApiError(409, "BOOKING_NOT_CANCELLABLE",
                                   {"CANCELLED": "Бронь уже отменена", "EXPIRED": "Бронь истекла",
                                    "COMPLETED": "Бронь уже завершена"}.get(b["status"], "Бронь нельзя отменить"))
                by_manager = manager and not owner
                previous = b["status"]
                transition(tx, b, "CANCELLED", "MANAGER_REQUEST" if by_manager else "USER_REQUEST")
                if b["payment_id"] and domain.refund_on_cancel(previous, b["starts_at"], clock.now(), by_manager):
                    request_refund(tx, b, to_rub(b["room_price"] + b["equipment_price"]), b["payment_id"],
                                   "BOOKING_CANCELLED")
                return 200, booking_dto(tx, load(tx, booking_id))
        return idempotent(db, clock, user.id, do)

    @app.get("/api/v1/occupancy/rooms/<room_id>")
    def occupancy(room_id):
        room_id = path_uuid(room_id)
        date = request.args.get("date")
        try:
            day_start, day_end = day_bounds(date, tz) if date and len(date) == 10 else (None, None)
        except ValueError:
            day_start = None
        if day_start is None:
            raise bad_request("Некорректная дата", [{"field": "date", "message": "ожидается дата в формате ГГГГ-ММ-ДД"}])
        with db.transaction() as tx:
            rows = tx.query(
                "SELECT starts_at, ends_at FROM bookings WHERE room_id = :r AND starts_at < :e AND ends_at > :s "
                "AND (status = 'CONFIRMED' OR (status = 'PENDING_PAYMENT' AND expires_at > :now)) ORDER BY starts_at",
                {"r": room_id, "s": day_start, "e": day_end, "now": clock.now()})
        return json_response({"roomId": room_id, "date": date,
                              "busy": [{"startsAt": to_iso(r["starts_at"]), "endsAt": to_iso(r["ends_at"])} for r in rows]})

    # --- события payment-service ---------------------------------------------------------
    def on_payment_completed(tx, event):
        p = event["payload"]
        b = tx.one("SELECT * FROM bookings WHERE id = :id", {"id": p["bookingId"]})
        if not b:
            log.error("payment.completed по неизвестной брони %s", p["bookingId"])
            return
        b = load(tx, b["id"], lock=True)
        action = domain.reaction_to_payment(b["status"])
        if action == "confirm":
            transition(tx, b, "CONFIRMED")
        elif action == "refund":
            reason = "PAID_AFTER_EXPIRATION" if b["status"] == "EXPIRED" else "BOOKING_CANCELLED"
            log.warning("Оплата пришла по брони %s в статусе %s — возвращаем деньги", b["id"], b["status"])
            request_refund(tx, b, p["amount"], p["paymentId"], reason)
        else:
            log.info("payment.completed по брони %s в статусе %s — пропущено", b["id"], b["status"])

    def on_payment_failed(tx, event):
        b = tx.one("SELECT * FROM bookings WHERE id = :id", {"id": event["payload"]["bookingId"]})
        if not b:
            return
        b = load(tx, b["id"], lock=True)
        if not transition(tx, b, "CANCELLED", "PAYMENT_FAILED"):
            log.info("payment.failed по брони %s в статусе %s — пропущено", b["id"], b["status"])

    rt.subscribe("booking.payment-events", "payment.events",
                 {"payment.completed": on_payment_completed, "payment.failed": on_payment_failed})

    # --- планировщик: истечение удержаний и завершение броней ---------------------------
    def _close_due(where: str, target: str) -> int:
        with db.transaction() as tx:
            ids = [r["id"] for r in tx.query(f"SELECT id FROM bookings WHERE {where} ORDER BY id LIMIT 100",
                                             {"now": clock.now()})]
        done = 0
        for bid in ids:
            with db.transaction() as tx:
                b = load(tx, bid, lock=True)
                token = context.set_correlation_id(b["correlation_id"])
                try:
                    done += transition(tx, b, target)
                finally:
                    context.reset_correlation_id(token)
        return done

    def expire_due() -> int:
        return _close_due("status = 'PENDING_PAYMENT' AND expires_at <= :now", "EXPIRED")

    def complete_due() -> int:
        return _close_due("status = 'CONFIRMED' AND ends_at <= :now", "COMPLETED")

    rt.every("expire-holds", 5, expire_due)
    rt.every("complete-bookings", 60, complete_due)

    if start_background:
        rt.start()
    return app
