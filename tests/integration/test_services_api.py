"""Интеграционные тесты эндпоинтов через API Gateway: права, валидация, ошибки, пагинация.

Последний тест проверяет, что тестами покрыта каждая операция из OpenAPI реализованных сервисов.
"""
import pytest

from jamslot_common import demo


# --- API Gateway --------------------------------------------------------------------------
def test_gateway_hides_internal_api(system):
    r = system.client.get(f"/internal/v1/rooms/{demo.ROOM_RED}/quote")
    assert r.status_code == 404 and r.content_type == "application/problem+json"


def test_gateway_auth(system):
    assert system.call("GET", "/api/v1/bookings").status_code == 401
    bad = system.call("GET", "/api/v1/bookings", headers={"Authorization": "Bearer abc"})
    assert (bad.status_code, bad.body["detail"]) == (401, "Access-токен не прошёл проверку")
    spoofed = system.call("GET", "/api/v1/bookings", headers={"X-User-Id": demo.USER_ILYA})
    assert spoofed.status_code == 401  # X-User-* снаружи не принимаются
    wrong = system.client.post("/auth/token", json={"username": "ilya", "password": "nope"})
    assert wrong.status_code == 401


def test_gateway_correlation_id_and_unknown_route(system):
    r = system.call("GET", "/api/v1/venues", headers={"X-Correlation-Id": "0b9d6c1e-52a4-4f3b-8e77-1c2d3e4f5a6b"})
    assert r.headers["X-Correlation-Id"] == "0b9d6c1e-52a4-4f3b-8e77-1c2d3e4f5a6b"
    missing = system.call("GET", "/api/v1/unknown")
    assert missing.status_code == 404 and missing.body["traceId"]


def test_gateway_rate_limit(tmp_path):
    from support.harness import System
    s = System(tmp_path, {"RATE_LIMIT_PER_MINUTE": "3"})
    try:
        codes = [s.call("GET", "/api/v1/venues").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
    finally:
        s.close()


def test_gateway_reports_unavailable_service(system):
    system.stop("venue")
    r = system.call("GET", "/api/v1/venues")
    assert (r.status_code, r.body["code"]) == (503, "SERVICE_UNAVAILABLE")


# --- venue-service ------------------------------------------------------------------------
def test_venue_catalog_filters(system):
    body = system.call("GET", "/api/v1/venues?city=Ростов-на-Дону").body
    assert [v["name"] for v in body["items"]] == ["Гараж 61", "Подвал на Пушкинской"]
    assert [v["name"] for v in system.call("GET", "/api/v1/venues?roomType=DRUM_ROOM").body["items"]] == \
        ["Подвал на Пушкинской"]
    assert [v["name"] for v in system.call("GET", "/api/v1/venues?q=гараж").body["items"]] == ["Гараж 61"]
    page = system.call("GET", "/api/v1/venues?size=1&page=1").body
    assert page["page"] == {"page": 1, "size": 1, "totalElements": 2, "totalPages": 2}
    bad = system.call("GET", "/api/v1/venues?size=500&roomType=GARAGE")
    assert bad.status_code == 400 and {e["field"] for e in bad.body["errors"]} == {"size"}


def test_venue_management_rights(system):
    new = {"name": "Новая база", "city": "Ростов-на-Дону", "address": "ул. Садовая, 1",
           "workingHours": {"opensAt": "09:00", "closesAt": "22:00"}}
    assert system.call("POST", "/api/v1/venues", "ilya", json=new).status_code == 403
    created = system.call("POST", "/api/v1/venues", "manager", json=new)
    assert created.status_code == 201 and created.body["ratingAvg"] is None
    vid = created.body["id"]
    assert system.call("PATCH", f"/api/v1/venues/{vid}", "anna", json={"name": "Чужая"}).status_code == 403
    upd = system.call("PATCH", f"/api/v1/venues/{vid}", "manager", json={"workingHours": {"opensAt": "10:00",
                                                                                          "closesAt": "23:00"}})
    assert upd.body["workingHours"] == {"opensAt": "10:00", "closesAt": "23:00"}
    room = system.call("POST", f"/api/v1/venues/{vid}/rooms", "manager",
                       json={"name": "Зал 1", "type": "REHEARSAL", "capacity": 5, "pricePerHour": 550})
    assert room.status_code == 201 and room.body["minBookingHours"] == 1
    patched = system.call("PATCH", f"/api/v1/rooms/{room.body['id']}", "manager", json={"pricePerHour": 600.5})
    assert patched.body["pricePerHour"] == 600.5
    rooms = system.call("GET", f"/api/v1/venues/{vid}/rooms").body
    assert [r["name"] for r in rooms["items"]] == ["Зал 1"]
    assert system.call("GET", f"/api/v1/rooms/{room.body['id']}").body["pricePerHour"] == 600.5
    # скрытая площадка не видна клиентам, но видна менеджеру
    system.call("PATCH", f"/api/v1/venues/{vid}", "manager", json={"status": "HIDDEN"})
    assert system.call("GET", f"/api/v1/venues/{vid}").status_code == 404
    assert system.call("GET", f"/api/v1/venues/{vid}", "manager").status_code == 200


def test_venue_validation_errors(system):
    r = system.call("POST", "/api/v1/venues", "manager",
                    json={"name": "X", "city": "Ростов", "address": "a", "workingHours": {"opensAt": "9:30",
                                                                                          "closesAt": "22:00"},
                          "extra": 1})
    assert r.status_code == 400
    assert {e["field"] for e in r.body["errors"]} == {"name", "workingHours.opensAt", "extra"}
    assert system.call("PATCH", f"/api/v1/rooms/{demo.ROOM_RED}", "manager", json={}).status_code == 400
    assert system.call("GET", "/api/v1/rooms/not-a-uuid").status_code == 404


def test_rating_copy_ignores_stale_events(system):
    import json
    for avg, count, at in ((4.8, 40, "2026-11-20T10:00:00Z"), (4.1, 10, "2026-11-19T10:00:00Z")):
        event = {"eventId": f"00000000-0000-4000-8000-00000000000{count % 10}", "eventType": "review.published",
                 "schemaVersion": 1, "occurredAt": at, "correlationId": "0b9d6c1e-52a4-4f3b-8e77-1c2d3e4f5a6b",
                 "producer": "review-service",
                 "payload": {"reviewId": "6e5d4c3b-2a19-4f08-b7c6-d5e4f3a2b1c0", "venueId": demo.VENUE_GARAGE,
                             "roomId": demo.ROOM_RED, "rating": 5,
                             "venueRating": {"average": avg, "count": count, "calculatedAt": at}}}
        system.broker.publish("review.events", "review.published", json.dumps(event))
    system.settle()
    v = system.call("GET", f"/api/v1/venues/{demo.VENUE_GARAGE}").body
    assert (v["ratingAvg"], v["ratingCount"]) == (4.8, 40)


# --- equipment-service --------------------------------------------------------------------
def test_equipment_catalog_and_management(system):
    items = system.call("GET", f"/api/v1/equipment?venueId={demo.VENUE_GARAGE}&category=MICROPHONE").body["items"]
    assert [(i["name"], i["totalQuantity"]) for i in items] == [("Shure SM58", 3)]
    assert system.call("GET", "/api/v1/equipment").status_code == 400  # venueId обязателен
    new = {"venueId": demo.VENUE_GARAGE, "name": "Nord Stage 3", "category": "KEYS", "totalQuantity": 1,
           "pricePerHour": 400}
    assert system.call("POST", "/api/v1/equipment", "anna", json=new).status_code == 403
    created = system.call("POST", "/api/v1/equipment", "manager", json=new)
    assert created.status_code == 201
    iid = created.body["id"]
    assert system.call("GET", f"/api/v1/equipment/{iid}").body["name"] == "Nord Stage 3"
    assert system.call("PATCH", f"/api/v1/equipment/{iid}", "manager", json={"totalQuantity": 2}).body["totalQuantity"] == 2


def test_equipment_quantity_cannot_drop_below_reserved(system):
    starts, ends = system.slot(2, 18)
    system.call("POST", "/api/v1/bookings", "ilya", json={"roomId": demo.ROOM_RED, "startsAt": starts, "endsAt": ends,
                                                          "equipment": [{"itemId": demo.ITEM_SM58, "quantity": 2}]})
    r = system.call("PATCH", f"/api/v1/equipment/{demo.ITEM_SM58}", "manager", json={"totalQuantity": 1})
    assert (r.status_code, r.body["code"]) == (422, "BUSINESS_RULE_VIOLATION")
    avail = system.call("GET", f"/api/v1/equipment-availability?venueId={demo.VENUE_GARAGE}"
                               f"&startsAt={starts.replace('+', '%2B')}&endsAt={ends.replace('+', '%2B')}").body
    assert {i["name"]: i["availableQuantity"] for i in avail["items"]}["Shure SM58"] == 1


# --- booking-service ----------------------------------------------------------------------
def test_booking_validation_and_business_rules(system):
    starts, ends = system.slot(2, 18)
    r = system.call("POST", "/api/v1/bookings", "ilya", json={"roomId": demo.ROOM_RED, "startsAt": "завтра",
                                                              "endsAt": ends, "equipment": [{"itemId": demo.ITEM_SM58,
                                                                                             "quantity": 0}]})
    assert r.status_code == 400 and {e["field"] for e in r.body["errors"]} == {"startsAt", "equipment.0.quantity"}
    early_s, early_e = system.slot(2, 8)
    r = system.call("POST", "/api/v1/bookings", "ilya", json={"roomId": demo.ROOM_RED, "startsAt": early_s, "endsAt": early_e})
    assert (r.status_code, r.body["code"]) == (422, "BUSINESS_RULE_VIOLATION")
    r = system.call("POST", "/api/v1/bookings", "ilya",
                    json={"roomId": "00000000-0000-4000-8000-000000000000", "startsAt": starts, "endsAt": ends})
    assert r.status_code == 404
    r = system.call("POST", "/api/v1/bookings", "ilya",
                    json={"roomId": demo.ROOM_RED, "startsAt": starts, "endsAt": ends,
                          "equipment": [{"itemId": "00000000-0000-4000-8000-000000000000", "quantity": 1}]})
    assert r.status_code == 404
    assert system.call("GET", "/api/v1/bookings", "ilya").body["items"][0]["cancelReason"] == "EQUIPMENT_UNAVAILABLE"


def test_booking_visibility_and_occupancy(system):
    starts, ends = system.slot(2, 18)
    b = system.call("POST", "/api/v1/bookings", "ilya",
                    json={"roomId": demo.ROOM_RED, "startsAt": starts, "endsAt": ends}).body
    assert system.call("GET", f"/api/v1/bookings/{b['id']}", "anna").status_code == 403
    assert system.call("GET", f"/api/v1/bookings/{b['id']}", "manager").status_code == 200
    occ = system.call("GET", f"/api/v1/occupancy/rooms/{demo.ROOM_RED}?date=2026-11-22").body
    assert occ["busy"] == [{"startsAt": "2026-11-22T15:00:00Z", "endsAt": "2026-11-22T17:00:00Z"}]
    assert system.call("GET", f"/api/v1/occupancy/rooms/{demo.ROOM_RED}?date=22.11.2026").status_code == 400
    assert system.call("GET", "/api/v1/bookings?status=PENDING_PAYMENT", "ilya").body["page"]["totalElements"] == 1
    assert system.call("GET", "/api/v1/bookings?status=CONFIRMED", "ilya").body["items"] == []


def test_overlap_is_guarded_by_database_constraint(system):
    """В PostgreSQL пересечение запрещает ограничение EXCLUDE — даже если проверка в коде пропущена (гонка)."""
    db = system.dbs["booking"]
    if db.dialect != "postgresql":
        pytest.skip("EXCLUDE есть только в PostgreSQL; в SQLite запись сериализует BEGIN IMMEDIATE")
    row = {"u": demo.USER_ILYA, "r": demo.ROOM_RED, "s": 1795186800, "e": 1795194000}
    sql = ("INSERT INTO bookings (id, user_id, customer_name, customer_email, room_id, venue_id, venue_name, room_name, "
           "starts_at, ends_at, status, room_price, expires_at, created_at, updated_at) VALUES (:id, :u, 'x', 'x', :r, "
           "'v', 'v', 'r', :s, :e, 'PENDING_PAYMENT', 0, 0, 0, 0)")
    with db.transaction() as tx:
        tx.execute(sql, {**row, "id": "a"})
    with pytest.raises(db.integrity_errors):
        with db.transaction() as tx:
            tx.execute(sql, {**row, "id": "b", "s": row["s"] + 3600, "e": row["e"] + 3600})
    with db.transaction() as tx:  # отменённая бронь интервал не держит
        tx.execute("UPDATE bookings SET status = 'CANCELLED' WHERE id = 'a'")
        tx.execute(sql, {**row, "id": "c"})


# --- payment-service ----------------------------------------------------------------------
def test_payments_are_private_and_webhook_is_validated(system):
    starts, ends = system.slot(2, 18)
    b = system.call("POST", "/api/v1/bookings", "ilya",
                    json={"roomId": demo.ROOM_RED, "startsAt": starts, "endsAt": ends}).body
    mine = system.call("GET", f"/api/v1/payments?bookingId={b['id']}", "ilya").body["items"]
    assert [p["id"] for p in mine] == [b["paymentId"]]
    assert system.call("GET", f"/api/v1/payments/{b['paymentId']}", "anna").status_code == 403
    assert system.call("GET", "/api/v1/payments", "anna").body["items"] == []
    bad = system.call("POST", "/api/v1/payments/webhooks/yookassa", json={"event": "payment.succeeded"}, key=None)
    assert bad.status_code == 400
    unknown = system.call("POST", "/api/v1/payments/webhooks/yookassa", key=None,
                          json={"type": "notification", "event": "payment.succeeded", "object": {"id": "yk-x"}})
    assert unknown.status_code == 200  # шлюз не должен повторять уведомление по чужому платежу
    assert system.pay(b).status_code == 200 and system.pay(b).status_code == 200  # повтор уведомления безопасен
    system.settle()
    assert [e["eventType"] for e in system.broker.published].count("payment.completed") == 1


def test_internal_payment_is_idempotent_per_booking(system):
    client = system.payment.test_client()
    body = {"bookingId": "9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a", "userId": demo.USER_ILYA, "amount": 2000.0,
            "currency": "RUB", "description": "Тест", "returnUrl": "http://localhost:8000/bookings/x",
            "expiresAt": "2026-11-20T09:15:00Z"}
    a = client.post("/internal/v1/payments", json=body, headers={"Idempotency-Key": "11111111-1111-4111-8111-111111111111"})
    b = client.post("/internal/v1/payments", json=body, headers={"Idempotency-Key": "22222222-2222-4222-8222-222222222222"})
    assert a.status_code == b.status_code == 201 and a.get_json()["id"] == b.get_json()["id"]
    other = client.post("/internal/v1/payments", json={**body, "amount": 1.0},
                        headers={"Idempotency-Key": "33333333-3333-4333-8333-333333333333"})
    assert (other.status_code, other.get_json()["code"]) == (409, "PAYMENT_ALREADY_EXISTS")


def test_payment_provider_rejects_request_outside_contract(system):
    """Стаб WireMock проверяет авторизацию магазина: с чужими ключами шлюз отвечает 401 → 502 у payment-service."""
    system.payment.extensions["provider"].client.auth = ("123456", "wrong")
    client = system.payment.test_client()
    r = client.post("/internal/v1/payments", headers={"Idempotency-Key": "44444444-4444-4444-8444-444444444444"},
                    json={"bookingId": "9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a", "userId": demo.USER_ILYA,
                          "amount": 10, "currency": "RUB", "description": "Тест", "returnUrl": "http://x/y",
                          "expiresAt": "2026-11-20T09:15:00Z"})
    assert (r.status_code, r.get_json()["code"]) == (502, "PAYMENT_PROVIDER_ERROR")


# --- notification-service -----------------------------------------------------------------
def test_notification_preferences(system):
    assert system.call("GET", "/api/v1/notification-preferences", "ilya").body == \
        {"emailEnabled": True, "reminderEnabled": True, "reminderHoursBefore": 2}
    r = system.call("PUT", "/api/v1/notification-preferences", "ilya",
                    json={"emailEnabled": False, "reminderEnabled": False, "reminderHoursBefore": 3})
    assert r.body == {"emailEnabled": False, "reminderEnabled": False, "reminderHoursBefore": 3}
    assert system.call("PUT", "/api/v1/notification-preferences", "ilya",
                       json={"emailEnabled": True}).status_code == 400
    starts, ends = system.slot(2, 18)
    b = system.call("POST", "/api/v1/bookings", "ilya",
                    json={"roomId": demo.ROOM_RED, "startsAt": starts, "endsAt": ends}).body
    system.pay(b)
    system.settle()
    notes = system.call("GET", "/api/v1/notifications?type=BOOKING_CONFIRMED", "ilya").body["items"]
    assert [n["status"] for n in notes] == ["SENT"]  # о подтверждении пишем всегда
    assert system.call("GET", "/api/v1/notifications?type=BOOKING_REMINDER", "ilya").body["items"] == []


# --- покрытие контракта -------------------------------------------------------------------
def test_every_operation_of_implemented_services_is_exercised(system):
    """Вызывает каждую операцию и сверяет список с OpenAPI (review-service в ЛР4 не реализован)."""
    starts, ends = system.slot(3, 12)
    q = f"startsAt={starts.replace('+', '%2B')}&endsAt={ends.replace('+', '%2B')}"
    b = system.call("POST", "/api/v1/bookings", "ilya",
                    json={"roomId": demo.ROOM_BLUE, "startsAt": starts, "endsAt": ends,
                          "equipment": [{"itemId": demo.ITEM_SM58, "quantity": 1}]}).body
    with system.dbs["equipment"].transaction() as tx:
        reservation_id = tx.one("SELECT id FROM reservations WHERE booking_id = :b", {"b": b["id"]})["id"]
    calls = [
        ("GET", "/api/v1/venues", None), ("GET", f"/api/v1/venues/{demo.VENUE_GARAGE}", None),
        ("PATCH", f"/api/v1/venues/{demo.VENUE_GARAGE}", "manager"),
        ("GET", f"/api/v1/venues/{demo.VENUE_GARAGE}/rooms", None),
        ("GET", f"/api/v1/rooms/{demo.ROOM_RED}", None), ("PATCH", f"/api/v1/rooms/{demo.ROOM_RED}", "manager"),
        ("GET", f"/api/v1/equipment?venueId={demo.VENUE_GARAGE}", None),
        ("GET", f"/api/v1/equipment/{demo.ITEM_SM58}", None),
        ("PATCH", f"/api/v1/equipment/{demo.ITEM_SM58}", "manager"),
        ("GET", f"/api/v1/equipment-availability?venueId={demo.VENUE_GARAGE}&{q}", None),
        ("GET", "/api/v1/bookings", "ilya"), ("GET", f"/api/v1/bookings/{b['id']}", "ilya"),
        ("GET", f"/api/v1/occupancy/rooms/{demo.ROOM_BLUE}?date=2026-11-23", None),
        ("GET", "/api/v1/payments", "ilya"), ("GET", f"/api/v1/payments/{b['paymentId']}", "ilya"),
        ("GET", "/api/v1/notifications", "ilya"), ("GET", "/api/v1/notification-preferences", "ilya"),
    ]
    bodies = {"PATCH": {"description": "Обновлено"}}
    for method, path, user in calls:
        r = system.call(method, path, user, json=bodies.get(method))
        assert r.status_code == 200, (method, path, r.body)
    system.call("POST", "/api/v1/venues", "manager", json={"name": "Ещё база", "city": "Ростов-на-Дону",
                                                           "address": "a", "workingHours": {"opensAt": "10:00",
                                                                                            "closesAt": "22:00"}})
    system.call("POST", f"/api/v1/venues/{demo.VENUE_GARAGE}/rooms", "manager",
                json={"name": "Зал 9", "type": "DRUM_ROOM", "capacity": 2, "pricePerHour": 300})
    system.call("POST", "/api/v1/equipment", "manager", json={"venueId": demo.VENUE_GARAGE, "name": "Пэд", "category":
                                                               "OTHER", "totalQuantity": 1, "pricePerHour": 50})
    system.call("PUT", "/api/v1/notification-preferences", "ilya",
                json={"emailEnabled": True, "reminderEnabled": True, "reminderHoursBefore": 2})
    system.pay(b)
    system.call("POST", f"/api/v1/bookings/{b['id']}/cancel", "ilya")
    with system.equipment.test_client() as c:  # внутренний GET резерва (сверка состояния booking-service)
        r = c.get(f"/internal/v1/reservations/{reservation_id}")
        system.contracts.check_response("GET", f"/internal/v1/reservations/{reservation_id}", r.status_code,
                                        r.content_type, r.get_json())

    expected = set()
    for spec in system.contracts.specs:
        if spec.name == "review-service":
            continue
        expected |= {f"{spec.name} {op['operationId']}" for _, _, _, op in spec.ops}
    assert expected - system.contracts.seen_operations == set()
