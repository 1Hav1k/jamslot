"""equipment-service — прокатный фонд и резервы оборудования под брони."""
import logging
import uuid
from typing import Literal

from flask import request
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, condecimal, field_validator

from jamslot_common import demo
from jamslot_common.context import setup_logging
from jamslot_common.money import to_kopecks, to_rub
from jamslot_common.problems import ApiError, forbidden, not_found
from jamslot_common.runtime import ServiceRuntime
from jamslot_common.timeutil import from_datetime, parse_iso, to_iso
from jamslot_common.web import (create_base_app, current_user, health_route, idempotent, json_response,
                                page_body, page_params, parse_body, path_uuid, query_enum, query_uuid)

SERVICE = "equipment-service"
log = logging.getLogger(SERVICE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS equipment_items (
    id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT,
    total_quantity INTEGER NOT NULL,
    price_per_hour BIGINT NOT NULL,
    status TEXT NOT NULL,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS equipment_items_venue ON equipment_items (venue_id);
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL UNIQUE,
    venue_id TEXT NOT NULL,
    status TEXT NOT NULL,
    starts_at BIGINT NOT NULL,
    ends_at BIGINT NOT NULL,
    hold_until BIGINT NOT NULL,
    total_price BIGINT NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservations_active ON reservations (status, starts_at, ends_at);
CREATE TABLE IF NOT EXISTS reservation_items (
    reservation_id TEXT NOT NULL REFERENCES reservations (id),
    item_id TEXT NOT NULL REFERENCES equipment_items (id),
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price_per_hour BIGINT NOT NULL,
    price BIGINT NOT NULL,
    PRIMARY KEY (reservation_id, item_id)
);
"""

CATEGORIES = ("GUITAR", "BASS", "DRUMS", "KEYS", "AMP", "MICROPHONE", "OTHER")
Price = condecimal(ge=0, max_digits=10, decimal_places=2)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateItem(Strict):
    venueId: uuid.UUID
    name: str = Field(min_length=2, max_length=120)
    category: Literal["GUITAR", "BASS", "DRUMS", "KEYS", "AMP", "MICROPHONE", "OTHER"]
    description: str | None = Field(default=None, max_length=1000)
    totalQuantity: int = Field(ge=1, le=100)
    pricePerHour: Price


class UpdateItem(Strict):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    totalQuantity: int | None = Field(default=None, ge=0, le=100)
    pricePerHour: Price | None = None
    status: Literal["ACTIVE", "RETIRED"] | None = None


class ReservationLine(Strict):
    itemId: uuid.UUID
    quantity: int = Field(ge=1, le=10)


class CreateReservation(Strict):
    bookingId: uuid.UUID
    venueId: uuid.UUID
    startsAt: AwareDatetime
    endsAt: AwareDatetime
    holdUntil: AwareDatetime
    items: list[ReservationLine] = Field(min_length=1, max_length=10)

    @field_validator("items")
    @classmethod
    def unique_items(cls, items):
        ids = [i.itemId for i in items]
        if len(ids) != len(set(ids)):
            raise ValueError("позиции оборудования не должны повторяться")
        return items


def item_dto(r: dict) -> dict:
    body = {"id": r["id"], "venueId": r["venue_id"], "name": r["name"], "category": r["category"],
            "totalQuantity": r["total_quantity"], "pricePerHour": to_rub(r["price_per_hour"]), "currency": "RUB",
            "status": r["status"]}
    if r["description"] is not None:
        body["description"] = r["description"]
    return body


def reserved_quantity(tx, item_id: str, starts: int, ends: int) -> int:
    row = tx.one(
        "SELECT COALESCE(SUM(ri.quantity), 0) AS n FROM reservation_items ri "
        "JOIN reservations r ON r.id = ri.reservation_id "
        "WHERE ri.item_id = :item AND r.status IN ('HELD', 'CONFIRMED') "
        "AND r.starts_at < :ends AND r.ends_at > :starts",
        {"item": item_id, "starts": starts, "ends": ends})
    return int(row["n"])


def reservation_dto(tx, r: dict) -> dict:
    lines = tx.query("SELECT * FROM reservation_items WHERE reservation_id = :id ORDER BY name", {"id": r["id"]})
    return {
        "id": r["id"], "bookingId": r["booking_id"], "venueId": r["venue_id"], "status": r["status"],
        "startsAt": to_iso(r["starts_at"]), "endsAt": to_iso(r["ends_at"]), "holdUntil": to_iso(r["hold_until"]),
        "items": [{"itemId": l["item_id"], "name": l["name"], "quantity": l["quantity"],
                   "pricePerHour": to_rub(l["price_per_hour"]), "price": to_rub(l["price"])} for l in lines],
        "totalPrice": to_rub(r["total_price"]), "currency": "RUB",
    }


def seed(rt: ServiceRuntime) -> None:
    items = [
        (demo.ITEM_STRAT, "Fender Stratocaster", "GUITAR", "Mexican Standard, струны 10–46", 1, 30000),
        (demo.ITEM_SM58, "Shure SM58", "MICROPHONE", "Вокальный динамический микрофон", 3, 10000),
        (demo.ITEM_CYMBALS, "Тарелки Zildjian A Custom", "DRUMS", "Хай-хэт 14\", крэш 16\", райд 20\"", 1, 20000),
        (demo.ITEM_BASS_AMP, "Ampeg BA-210", "AMP", "Бас-комбо 450 Вт", 1, 25000),
    ]
    with rt.db.transaction() as tx:
        for i in items:
            tx.execute(
                "INSERT INTO equipment_items (id, venue_id, name, category, description, total_quantity, "
                "price_per_hour, status, created_at) VALUES (:id, :v, :n, :c, :d, :q, :p, 'ACTIVE', :t) "
                "ON CONFLICT DO NOTHING",
                {"id": i[0], "v": demo.VENUE_GARAGE, "n": i[1], "c": i[2], "d": i[3], "q": i[4], "p": i[5],
                 "t": rt.clock.now()})


def create_app(settings: dict | None = None, *, db=None, broker=None, clock=None, start_background: bool = True):
    setup_logging(SERVICE)
    rt = ServiceRuntime(SERVICE, SCHEMA, db=db, broker=broker, clock=clock, settings=settings)
    if str(rt.setting("SEED_DEMO_DATA", "true")).lower() == "true":
        seed(rt)
    app = create_base_app(SERVICE)
    app.extensions["runtime"] = rt
    health_route(app, rt.db)
    db, clock = rt.db, rt.clock

    def manages(user, venue_id: str) -> bool:
        return user.is_admin or venue_id in user.venue_ids

    def load_item(tx, item_id: str) -> dict:
        r = tx.one("SELECT * FROM equipment_items WHERE id = :id", {"id": item_id})
        if not r:
            raise not_found("Позиция оборудования не найдена")
        return r

    def interval_params() -> tuple[int, int]:
        errors, out = [], {}
        for name in ("startsAt", "endsAt"):
            raw = request.args.get(name)
            if raw is None:
                errors.append({"field": name, "message": "обязательный параметр"})
                continue
            try:
                out[name] = parse_iso(raw)
            except ValueError:
                errors.append({"field": name, "message": "ожидается дата-время ISO 8601 с часовым поясом"})
        if not errors and out["endsAt"] <= out["startsAt"]:
            errors.append({"field": "endsAt", "message": "должно быть больше startsAt"})
        if errors:
            raise ApiError(400, "VALIDATION_ERROR", "Некорректные параметры интервала", errors)
        return out["startsAt"], out["endsAt"]

    # --- каталог оборудования ----------------------------------------------------------
    @app.get("/api/v1/equipment")
    def list_items():
        venue_id = query_uuid("venueId", required=True)
        category = query_enum("category", CATEGORIES)
        page, size = page_params()
        sql, params = "FROM equipment_items WHERE venue_id = :v", {"v": venue_id}
        if category:
            sql += " AND category = :c"
            params["c"] = category
        with db.transaction() as tx:
            total = tx.one("SELECT COUNT(*) AS n " + sql, params)["n"]
            rows = tx.query("SELECT * " + sql + " ORDER BY name LIMIT :l OFFSET :o", {**params, "l": size, "o": page * size})
        return json_response(page_body([item_dto(r) for r in rows], page, size, total))

    @app.post("/api/v1/equipment")
    def create_item():
        user = current_user()

        def do():
            data = parse_body(CreateItem)
            if not manages(user, str(data.venueId)):
                raise forbidden("Добавлять оборудование может только менеджер этой площадки")
            iid = str(uuid.uuid4())
            with db.transaction() as tx:
                tx.execute(
                    "INSERT INTO equipment_items (id, venue_id, name, category, description, total_quantity, "
                    "price_per_hour, status, created_at) VALUES (:id, :v, :n, :c, :d, :q, :p, 'ACTIVE', :t)",
                    {"id": iid, "v": str(data.venueId), "n": data.name, "c": data.category, "d": data.description,
                     "q": data.totalQuantity, "p": to_kopecks(data.pricePerHour), "t": clock.now()})
                return 201, item_dto(load_item(tx, iid))
        return idempotent(db, clock, user.id, do)

    @app.get("/api/v1/equipment/<item_id>")
    def get_item(item_id):
        with db.transaction() as tx:
            return json_response(item_dto(load_item(tx, path_uuid(item_id))))

    @app.patch("/api/v1/equipment/<item_id>")
    def update_item(item_id):
        user = current_user()
        data = parse_body(UpdateItem)
        if not data.model_fields_set:
            raise ApiError(400, "VALIDATION_ERROR", "Не передано ни одного поля для изменения")
        with db.transaction() as tx:
            item = load_item(tx, path_uuid(item_id))
            if not manages(user, item["venue_id"]):
                raise forbidden("Изменять оборудование может только менеджер площадки")
            tx.lock("equipment_items", "id", item["id"])
            fields = data.model_dump(exclude_unset=True)
            if "totalQuantity" in fields:
                busy = tx.one(
                    "SELECT COALESCE(MAX(s.n), 0) AS n FROM (SELECT SUM(ri.quantity) AS n FROM reservation_items ri "
                    "JOIN reservations r ON r.id = ri.reservation_id WHERE ri.item_id = :item "
                    "AND r.status IN ('HELD', 'CONFIRMED') AND r.ends_at > :now GROUP BY r.id) s",
                    {"item": item["id"], "now": clock.now()})["n"]
                if data.totalQuantity < busy:
                    raise ApiError(422, "BUSINESS_RULE_VIOLATION",
                                   f"Нельзя уменьшить количество ниже зарезервированного ({busy} шт.)")
            cols = {"name": "name", "description": "description", "totalQuantity": "total_quantity", "status": "status"}
            sets, params = [], {"id": item["id"]}
            for k, col in cols.items():
                if k in fields:
                    sets.append(f"{col} = :{col}")
                    params[col] = fields[k]
            if "pricePerHour" in fields and data.pricePerHour is not None:
                sets.append("price_per_hour = :price")
                params["price"] = to_kopecks(data.pricePerHour)
            if sets:
                tx.execute(f"UPDATE equipment_items SET {', '.join(sets)} WHERE id = :id", params)
            return json_response(item_dto(load_item(tx, item["id"])))

    @app.get("/api/v1/equipment-availability")
    def availability():
        venue_id = query_uuid("venueId", required=True)
        starts, ends = interval_params()
        page, size = page_params()
        with db.transaction() as tx:
            rows = tx.query("SELECT * FROM equipment_items WHERE venue_id = :v AND status = 'ACTIVE' ORDER BY name",
                            {"v": venue_id})
            items = [{"itemId": r["id"], "name": r["name"], "category": r["category"],
                      "availableQuantity": max(0, r["total_quantity"] - reserved_quantity(tx, r["id"], starts, ends)),
                      "pricePerHour": to_rub(r["price_per_hour"]), "currency": "RUB"} for r in rows]
        return json_response(page_body(items[page * size:(page + 1) * size], page, size, len(items)))

    # --- внутренний API: резервы для booking-service ------------------------------------
    @app.post("/internal/v1/reservations")
    def create_reservation():
        caller = request.headers.get("X-Service-Name", "internal")

        def do():
            data = parse_body(CreateReservation)
            starts, ends = from_datetime(data.startsAt), from_datetime(data.endsAt)
            if ends <= starts:
                raise ApiError(400, "VALIDATION_ERROR", "Конец интервала должен быть позже начала",
                               [{"field": "endsAt", "message": "должно быть больше startsAt"}])
            booking_id, venue_id = str(data.bookingId), str(data.venueId)
            hours = max(1, (ends - starts + 3599) // 3600)
            with db.transaction() as tx:
                existing = tx.one("SELECT * FROM reservations WHERE booking_id = :b", {"b": booking_id})
                if existing:
                    log.info("Резерв для брони %s уже есть — возвращаем его", booking_id)
                    return 201, reservation_dto(tx, existing)
                shortages, lines, total = [], [], 0
                for line in sorted(data.items, key=lambda l: str(l.itemId)):
                    item_id = str(line.itemId)
                    tx.lock("equipment_items", "id", item_id)  # сериализуем резервы одной позиции
                    item = tx.one("SELECT * FROM equipment_items WHERE id = :id", {"id": item_id})
                    if not item or item["venue_id"] != venue_id:
                        raise not_found(f"Позиция оборудования {item_id} не найдена на этой площадке")
                    if item["status"] != "ACTIVE":
                        raise ApiError(422, "BUSINESS_RULE_VIOLATION", f"{item['name']} выведен из проката")
                    free = item["total_quantity"] - reserved_quantity(tx, item_id, starts, ends)
                    if line.quantity > free:
                        shortages.append(f"{item['name']} — свободно {max(free, 0)} из {line.quantity}")
                    price = item["price_per_hour"] * hours * line.quantity
                    total += price
                    lines.append((item, line.quantity, price))
                if shortages:
                    raise ApiError(409, "EQUIPMENT_UNAVAILABLE", "; ".join(shortages))
                rid, now = str(uuid.uuid4()), clock.now()
                tx.execute(
                    "INSERT INTO reservations (id, booking_id, venue_id, status, starts_at, ends_at, hold_until, "
                    "total_price, created_at, updated_at) VALUES (:id, :b, :v, 'HELD', :s, :e, :h, :p, :t, :t)",
                    {"id": rid, "b": booking_id, "v": venue_id, "s": starts, "e": ends,
                     "h": from_datetime(data.holdUntil), "p": total, "t": now})
                for item, qty, price in lines:
                    tx.execute(
                        "INSERT INTO reservation_items (reservation_id, item_id, name, quantity, price_per_hour, price) "
                        "VALUES (:r, :i, :n, :q, :pph, :p)",
                        {"r": rid, "i": item["id"], "n": item["name"], "q": qty, "pph": item["price_per_hour"], "p": price})
                log.info("Резерв %s для брони %s создан (HELD)", rid, booking_id)
                return 201, reservation_dto(tx, tx.one("SELECT * FROM reservations WHERE id = :id", {"id": rid}))
        return idempotent(db, clock, caller, do)

    @app.get("/internal/v1/reservations/<reservation_id>")
    def get_reservation(reservation_id):
        with db.transaction() as tx:
            r = tx.one("SELECT * FROM reservations WHERE id = :id", {"id": path_uuid(reservation_id)})
            if not r:
                raise not_found("Резерв не найден")
            return json_response(reservation_dto(tx, r))

    # --- события booking-service --------------------------------------------------------
    def set_status(tx, booking_id: str, to: str, allowed: tuple[str, ...]):
        params = {"b": booking_id, "to": to, "t": clock.now()}
        params.update({f"s{i}": s for i, s in enumerate(allowed)})
        placeholders = ", ".join(f":s{i}" for i in range(len(allowed)))
        n = tx.execute(f"UPDATE reservations SET status = :to, updated_at = :t WHERE booking_id = :b "
                       f"AND status IN ({placeholders})", params)
        if n:
            log.info("Резерв брони %s → %s", booking_id, to)
        else:
            log.info("Резерв брони %s не найден или уже в конечном статусе — событие пропущено", booking_id)

    def on_confirmed(tx, event):
        set_status(tx, event["payload"]["bookingId"], "CONFIRMED", ("HELD",))

    def on_released(tx, event):
        set_status(tx, event["payload"]["bookingId"], "RELEASED", ("HELD", "CONFIRMED"))

    rt.subscribe("equipment.booking-events", "booking.events", {
        "booking.confirmed": on_confirmed,
        "booking.cancelled": on_released,
        "booking.expired": on_released,
    })

    def report_stuck():
        with db.transaction() as tx:
            n = tx.one("SELECT COUNT(*) AS n FROM reservations WHERE status = 'HELD' AND hold_until < :now",
                       {"now": clock.now()})["n"]
        if n:
            log.warning("Резервов, зависших в HELD дольше holdUntil: %d (проверьте DLQ equipment.booking-events)", n)

    rt.every("stuck-reservations", 60, report_stuck)

    if start_background:
        rt.start()
    return app
