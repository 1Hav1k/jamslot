"""venue-service — каталог площадок и комнат."""
import logging
import uuid
from typing import Literal

from flask import request
from pydantic import BaseModel, ConfigDict, Field, condecimal

from jamslot_common import demo
from jamslot_common.context import setup_logging
from jamslot_common.money import to_kopecks, to_rub
from jamslot_common.problems import ApiError, forbidden, not_found
from jamslot_common.runtime import ServiceRuntime
from jamslot_common.timeutil import parse_iso, to_iso
from jamslot_common.web import (create_base_app, current_user, health_route, idempotent, json_response,
                                page_body, page_params, parse_body, path_uuid, query_enum)

from .domain import check_interval, room_price

SERVICE = "venue-service"
log = logging.getLogger(SERVICE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    address TEXT NOT NULL,
    description TEXT,
    phone TEXT,
    opens_at TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    rating_avg DOUBLE PRECISION,
    rating_count INTEGER NOT NULL DEFAULT 0,
    rating_calculated_at BIGINT,
    status TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues (id),
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    description TEXT,
    capacity INTEGER NOT NULL,
    price_per_hour BIGINT NOT NULL,
    min_booking_hours INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS rooms_venue ON rooms (venue_id);
"""

ROOM_TYPES = ("REHEARSAL", "RECORDING", "DRUM_ROOM")
Hour = Field(pattern=r"^([01]\d|2[0-3]):00$")
Price = condecimal(ge=0, max_digits=10, decimal_places=2)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkingHours(Strict):
    opensAt: str = Hour
    closesAt: str = Hour


class CreateVenue(Strict):
    name: str = Field(min_length=2, max_length=120)
    city: str = Field(min_length=1, max_length=80)
    address: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, max_length=20)
    workingHours: WorkingHours


class UpdateVenue(Strict):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    address: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, max_length=20)
    workingHours: WorkingHours | None = None
    status: Literal["ACTIVE", "HIDDEN"] | None = None


class CreateRoom(Strict):
    name: str = Field(min_length=2, max_length=120)
    type: Literal["REHEARSAL", "RECORDING", "DRUM_ROOM"]
    description: str | None = Field(default=None, max_length=2000)
    capacity: int = Field(ge=1, le=50)
    pricePerHour: Price
    minBookingHours: int = Field(default=1, ge=1)


class UpdateRoom(Strict):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    capacity: int | None = Field(default=None, ge=1, le=50)
    pricePerHour: Price | None = None
    minBookingHours: int | None = Field(default=None, ge=1)
    status: Literal["ACTIVE", "INACTIVE"] | None = None


def venue_dto(v: dict) -> dict:
    body = {
        "id": v["id"], "name": v["name"], "city": v["city"], "address": v["address"],
        "workingHours": {"opensAt": v["opens_at"], "closesAt": v["closes_at"]},
        "ratingAvg": round(v["rating_avg"], 2) if v["rating_avg"] is not None else None,
        "ratingCount": v["rating_count"], "status": v["status"], "createdAt": to_iso(v["created_at"]),
    }
    if v["description"] is not None:
        body["description"] = v["description"]
    if v["phone"] is not None:
        body["phone"] = v["phone"]
    return body


def room_dto(r: dict) -> dict:
    body = {"id": r["id"], "venueId": r["venue_id"], "name": r["name"], "type": r["type"],
            "capacity": r["capacity"], "pricePerHour": to_rub(r["price_per_hour"]), "currency": "RUB",
            "minBookingHours": r["min_booking_hours"], "status": r["status"]}
    if r["description"] is not None:
        body["description"] = r["description"]
    return body


def seed(rt: ServiceRuntime) -> None:
    now = rt.clock.now()
    venues = [
        (demo.VENUE_GARAGE, "Гараж 61", "Ростов-на-Дону", "ул. Текучёва, 139",
         "Три репетиционные комнаты и студия записи", "+78632000000", "10:00", "23:00", 4.7, 38),
        (demo.VENUE_BASEMENT, "Подвал на Пушкинской", "Ростов-на-Дону", "ул. Пушкинская, 54",
         "Небольшая барабанная комната", "+78632000001", "12:00", "22:00", None, 0),
    ]
    rooms = [
        (demo.ROOM_RED, demo.VENUE_GARAGE, "Комната «Красная»", "REHEARSAL",
         "28 м², барабаны Tama, два гитарных комбо, бас-комбо, вокальная линия", 6, 70000, 1),
        (demo.ROOM_BLUE, demo.VENUE_GARAGE, "Комната «Синяя»", "REHEARSAL", "18 м², для дуэтов и акустики", 4, 60000, 1),
        (demo.ROOM_STUDIO, demo.VENUE_GARAGE, "Студия записи", "RECORDING", "Аппаратная и live-комната", 5, 150000, 2),
        (demo.ROOM_BASEMENT, demo.VENUE_BASEMENT, "Барабанная", "DRUM_ROOM", "Ударная установка Pearl", 2, 40000, 1),
    ]
    with rt.db.transaction() as tx:
        for v in venues:
            tx.execute(
                "INSERT INTO venues (id, name, city, address, description, phone, opens_at, closes_at, rating_avg, "
                "rating_count, status, manager_id, created_at) VALUES (:id, :name, :city, :address, :d, :p, :o, :c, "
                ":ra, :rc, 'ACTIVE', :m, :t) ON CONFLICT DO NOTHING",
                {"id": v[0], "name": v[1], "city": v[2], "address": v[3], "d": v[4], "p": v[5], "o": v[6], "c": v[7],
                 "ra": v[8], "rc": v[9], "m": demo.USER_MANAGER, "t": now})
        for r in rooms:
            tx.execute(
                "INSERT INTO rooms (id, venue_id, name, type, description, capacity, price_per_hour, min_booking_hours, "
                "status, created_at) VALUES (:id, :v, :n, :t, :d, :c, :p, :m, 'ACTIVE', :now) ON CONFLICT DO NOTHING",
                {"id": r[0], "v": r[1], "n": r[2], "t": r[3], "d": r[4], "c": r[5], "p": r[6], "m": r[7], "now": now})


def create_app(settings: dict | None = None, *, db=None, broker=None, clock=None, start_background: bool = True):
    setup_logging(SERVICE)
    rt = ServiceRuntime(SERVICE, SCHEMA, db=db, broker=broker, clock=clock, settings=settings)
    tz = rt.setting("VENUE_TIMEZONE", "Europe/Moscow")
    if str(rt.setting("SEED_DEMO_DATA", "true")).lower() == "true":
        seed(rt)
    app = create_base_app(SERVICE)
    app.extensions["runtime"] = rt
    health_route(app, rt.db)
    db, clock = rt.db, rt.clock

    def load_venue(tx, venue_id: str) -> dict:
        v = tx.one("SELECT * FROM venues WHERE id = :id", {"id": venue_id})
        if not v:
            raise not_found("Площадка не найдена")
        return v

    def can_manage(user, venue: dict) -> bool:
        return user.is_admin or venue["manager_id"] == user.id or venue["id"] in user.venue_ids

    # --- площадки ----------------------------------------------------------------------
    @app.get("/api/v1/venues")
    def list_venues():
        page, size = page_params()
        room_type = query_enum("roomType", ROOM_TYPES)
        city, q = request.args.get("city"), request.args.get("q")
        if q is not None and len(q) < 2:
            raise ApiError(400, "VALIDATION_ERROR", "Строка поиска слишком короткая",
                           [{"field": "q", "message": "минимум 2 символа"}])
        sql, params = "SELECT * FROM venues WHERE status = 'ACTIVE'", {}
        if city:
            sql += " AND city = :city"
            params["city"] = city
        if room_type:
            sql += " AND id IN (SELECT venue_id FROM rooms WHERE type = :rt AND status = 'ACTIVE')"
            params["rt"] = room_type
        with db.transaction() as tx:
            rows = tx.query(sql + " ORDER BY name", params)
        if q:  # поиск по подстроке без учёта регистра (в том числе для кириллицы)
            rows = [r for r in rows if q.casefold() in r["name"].casefold()]
        items = [venue_dto(r) for r in rows[page * size:(page + 1) * size]]
        return json_response(page_body(items, page, size, len(rows)))

    @app.post("/api/v1/venues")
    def create_venue():
        user = current_user()
        if not user.has_role("venue_manager", "admin"):
            raise forbidden("Создавать площадки может только менеджер площадки")

        def do():
            data = parse_body(CreateVenue)
            vid = str(uuid.uuid4())
            with db.transaction() as tx:
                tx.execute(
                    "INSERT INTO venues (id, name, city, address, description, phone, opens_at, closes_at, rating_count, "
                    "status, manager_id, created_at) VALUES (:id, :name, :city, :address, :d, :p, :o, :c, 0, 'ACTIVE', "
                    ":m, :t)",
                    {"id": vid, "name": data.name, "city": data.city, "address": data.address, "d": data.description,
                     "p": data.phone, "o": data.workingHours.opensAt, "c": data.workingHours.closesAt,
                     "m": user.id, "t": clock.now()})
                v = load_venue(tx, vid)
            log.info("Создана площадка %s", vid)
            return 201, venue_dto(v)
        return idempotent(db, clock, user.id, do)

    @app.get("/api/v1/venues/<venue_id>")
    def get_venue(venue_id):
        user = current_user(required=False)
        with db.transaction() as tx:
            v = load_venue(tx, path_uuid(venue_id))
        if v["status"] != "ACTIVE" and not (user and can_manage(user, v)):
            raise not_found("Площадка не найдена")
        return json_response(venue_dto(v))

    @app.patch("/api/v1/venues/<venue_id>")
    def update_venue(venue_id):
        user = current_user()
        data = parse_body(UpdateVenue)
        if not data.model_fields_set:
            raise ApiError(400, "VALIDATION_ERROR", "Не передано ни одного поля для изменения")
        with db.transaction() as tx:
            v = load_venue(tx, path_uuid(venue_id))
            if not can_manage(user, v):
                raise forbidden("Изменять площадку может только её менеджер")
            fields = data.model_dump(exclude_unset=True)
            cols = {"name": "name", "address": "address", "description": "description", "phone": "phone",
                    "status": "status"}
            params = {"id": v["id"]}
            sets = []
            for k, col in cols.items():
                if k in fields:
                    sets.append(f"{col} = :{col}")
                    params[col] = fields[k]
            if data.workingHours is not None:
                sets += ["opens_at = :o", "closes_at = :c"]
                params.update(o=data.workingHours.opensAt, c=data.workingHours.closesAt)
            if sets:
                tx.execute(f"UPDATE venues SET {', '.join(sets)} WHERE id = :id", params)
            v = load_venue(tx, v["id"])
        return json_response(venue_dto(v))

    # --- комнаты -----------------------------------------------------------------------
    @app.get("/api/v1/venues/<venue_id>/rooms")
    def list_rooms(venue_id):
        page, size = page_params()
        with db.transaction() as tx:
            v = load_venue(tx, path_uuid(venue_id))
            if v["status"] != "ACTIVE":
                user = current_user(required=False)
                if not (user and can_manage(user, v)):
                    raise not_found("Площадка не найдена")
            total = tx.one("SELECT COUNT(*) AS n FROM rooms WHERE venue_id = :v", {"v": v["id"]})["n"]
            rows = tx.query("SELECT * FROM rooms WHERE venue_id = :v ORDER BY created_at, name LIMIT :l OFFSET :o",
                            {"v": v["id"], "l": size, "o": page * size})
        return json_response(page_body([room_dto(r) for r in rows], page, size, total))

    @app.post("/api/v1/venues/<venue_id>/rooms")
    def create_room(venue_id):
        user = current_user()
        venue_id = path_uuid(venue_id)

        def do():
            data = parse_body(CreateRoom)
            rid = str(uuid.uuid4())
            with db.transaction() as tx:
                v = load_venue(tx, venue_id)
                if not can_manage(user, v):
                    raise forbidden("Добавлять комнаты может только менеджер площадки")
                tx.execute(
                    "INSERT INTO rooms (id, venue_id, name, type, description, capacity, price_per_hour, "
                    "min_booking_hours, status, created_at) VALUES (:id, :v, :n, :t, :d, :c, :p, :m, 'ACTIVE', :now)",
                    {"id": rid, "v": venue_id, "n": data.name, "t": data.type, "d": data.description,
                     "c": data.capacity, "p": to_kopecks(data.pricePerHour), "m": data.minBookingHours,
                     "now": clock.now()})
                r = tx.one("SELECT * FROM rooms WHERE id = :id", {"id": rid})
            return 201, room_dto(r)
        return idempotent(db, clock, user.id, do)

    @app.get("/api/v1/rooms/<room_id>")
    def get_room(room_id):
        with db.transaction() as tx:
            r = tx.one("SELECT * FROM rooms WHERE id = :id", {"id": path_uuid(room_id)})
        if not r:
            raise not_found("Комната не найдена")
        return json_response(room_dto(r))

    @app.patch("/api/v1/rooms/<room_id>")
    def update_room(room_id):
        user = current_user()
        data = parse_body(UpdateRoom)
        if not data.model_fields_set:
            raise ApiError(400, "VALIDATION_ERROR", "Не передано ни одного поля для изменения")
        with db.transaction() as tx:
            r = tx.one("SELECT * FROM rooms WHERE id = :id", {"id": path_uuid(room_id)})
            if not r:
                raise not_found("Комната не найдена")
            if not can_manage(user, load_venue(tx, r["venue_id"])):
                raise forbidden("Изменять комнату может только менеджер площадки")
            fields = data.model_dump(exclude_unset=True)
            cols = {"name": "name", "description": "description", "capacity": "capacity",
                    "minBookingHours": "min_booking_hours", "status": "status"}
            sets, params = [], {"id": r["id"]}
            for k, col in cols.items():
                if k in fields:
                    sets.append(f"{col} = :{col}")
                    params[col] = fields[k]
            if "pricePerHour" in fields and data.pricePerHour is not None:
                sets.append("price_per_hour = :price")
                params["price"] = to_kopecks(data.pricePerHour)
            if sets:
                tx.execute(f"UPDATE rooms SET {', '.join(sets)} WHERE id = :id", params)
            r = tx.one("SELECT * FROM rooms WHERE id = :id", {"id": r["id"]})
        return json_response(room_dto(r))

    # --- внутренний API: расчёт для booking-service --------------------------------------
    @app.get("/internal/v1/rooms/<room_id>/quote")
    def quote(room_id):
        errors, times = [], {}
        for name in ("startsAt", "endsAt"):
            raw = request.args.get(name)
            if raw is None:
                errors.append({"field": name, "message": "обязательный параметр"})
                continue
            try:
                times[name] = parse_iso(raw)
            except ValueError:
                errors.append({"field": name, "message": "ожидается дата-время ISO 8601 с часовым поясом"})
        if errors:
            raise ApiError(400, "VALIDATION_ERROR", "Некорректные параметры интервала", errors)
        with db.transaction() as tx:
            r = tx.one("SELECT * FROM rooms WHERE id = :id", {"id": path_uuid(room_id)})
            if not r:
                raise not_found("Комната не найдена")
            v = load_venue(tx, r["venue_id"])
        if r["status"] != "ACTIVE" or v["status"] != "ACTIVE":
            raise ApiError(422, "BUSINESS_RULE_VIOLATION", "Комната сейчас недоступна для бронирования")
        hours = check_interval(times["startsAt"], times["endsAt"], clock.now(), v["opens_at"], v["closes_at"],
                               r["min_booking_hours"], tz)
        total = room_price(r["price_per_hour"], hours)
        return json_response({
            "roomId": r["id"], "venueId": v["id"], "venueName": v["name"], "roomName": r["name"],
            "startsAt": to_iso(times["startsAt"]), "endsAt": to_iso(times["endsAt"]), "hours": hours,
            "pricePerHour": to_rub(r["price_per_hour"]), "totalPrice": to_rub(total), "currency": "RUB",
        })

    # --- события review-service: копия рейтинга -----------------------------------------
    def on_rating(tx, event):
        p = event["payload"]
        rating = p["venueRating"]
        changed = tx.execute(
            "UPDATE venues SET rating_avg = :avg, rating_count = :cnt, rating_calculated_at = :at "
            "WHERE id = :id AND (rating_calculated_at IS NULL OR rating_calculated_at < :at)",
            {"avg": rating["average"], "cnt": rating["count"], "at": parse_iso(rating["calculatedAt"]),
             "id": p["venueId"]})
        log.info("Рейтинг площадки %s %s", p["venueId"], "обновлён" if changed else "не изменён (устаревшее событие)")

    rt.subscribe("venue.review-events", "review.events",
                 {"review.published": on_rating, "review.removed": on_rating})

    if start_background:
        rt.start()
    return app
