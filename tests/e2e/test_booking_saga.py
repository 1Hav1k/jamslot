"""Сквозные сценарии саги бронирования: все сервисы, Gateway, брокер и мок ЮKassa.

Каждый ответ, каждый внутренний запрос и каждое событие автоматически сверяются
с OpenAPI/AsyncAPI из ЛР3 (см. support/harness.py).
"""
from jamslot_common import demo

STRAT = {"itemId": demo.ITEM_STRAT, "quantity": 1}


def book(system, user="ilya", room=demo.ROOM_RED, days=2, hour=18, hours=2, equipment=(STRAT,), key="auto"):
    starts, ends = system.slot(days, hour, hours)
    return system.call("POST", "/api/v1/bookings", user, key=key,
                       json={"roomId": room, "startsAt": starts, "endsAt": ends, "equipment": list(equipment)})


def available(system, item=demo.ITEM_STRAT, days=2, hour=18, hours=2):
    starts, ends = system.slot(days, hour, hours)
    r = system.client.get("/api/v1/equipment-availability",
                          query_string={"venueId": demo.VENUE_GARAGE, "startsAt": starts, "endsAt": ends})
    return {i["itemId"]: i["availableQuantity"] for i in r.get_json()["items"]}[item]


def reservation_status(system, booking_id):
    with system.dbs["equipment"].transaction() as tx:
        row = tx.one("SELECT status FROM reservations WHERE booking_id = :b", {"b": booking_id})
    return row["status"] if row else None


def booking(system, booking_id, user="ilya"):
    return system.call("GET", f"/api/v1/bookings/{booking_id}", user).body


def payment(system, payment_id, user="ilya"):
    return system.call("GET", f"/api/v1/payments/{payment_id}", user).body


def event_types(system):
    return [e["eventType"] for e in system.broker.published]


# --- успешный путь ------------------------------------------------------------------------
def test_happy_path(system):
    r = book(system)
    assert r.status_code == 201, r.body
    b = r.body
    assert b["status"] == "PENDING_PAYMENT"
    assert (b["roomPrice"], b["equipmentPrice"], b["totalPrice"]) == (1400.0, 600.0, 2000.0)
    assert b["paymentUrl"].startswith("http://localhost:8089/checkout?orderId=yk-")
    assert reservation_status(system, b["id"]) == "HELD"
    assert available(system) == 0
    assert payment(system, b["paymentId"])["status"] == "PENDING"

    assert system.pay(b).status_code == 200
    system.settle()

    assert booking(system, b["id"])["status"] == "CONFIRMED"
    assert reservation_status(system, b["id"]) == "CONFIRMED"
    assert payment(system, b["paymentId"])["status"] == "SUCCEEDED"
    assert event_types(system) == ["payment.completed", "booking.confirmed"]
    # весь сценарий связан одним correlationId — тем, что Gateway выдал на POST /bookings
    cid = r.headers["X-Correlation-Id"]
    assert {e["correlationId"] for e in system.broker.published} == {cid}
    assert system.emails_to("ilya@example.org") == ["Бронь подтверждена — Гараж 61, 22.11, 18:00"]
    notes = system.call("GET", "/api/v1/notifications", "ilya").body["items"]
    assert {(n["type"], n["status"]) for n in notes} == {("BOOKING_CONFIRMED", "SENT"), ("BOOKING_REMINDER", "SCHEDULED")}


def test_booking_completes_and_invites_review(system):
    b = book(system, equipment=()).body
    system.pay(b)
    system.settle()
    system.advance(2 * 86400 + 9 * 3600)  # после окончания брони
    assert booking(system, b["id"])["status"] == "COMPLETED"
    subjects = system.emails_to("ilya@example.org")
    assert "Напоминание: репетиция 22.11 в 18:00" in subjects
    assert "Как прошла репетиция в «Гараж 61»?" in subjects


# --- сбои и компенсации ---------------------------------------------------------------------
def test_payment_declined_releases_slot_and_equipment(system):
    b = book(system).body
    assert system.pay(b, "payment.canceled", reason="general_decline").status_code == 200
    system.settle()

    got = booking(system, b["id"])
    assert (got["status"], got["cancelReason"]) == ("CANCELLED", "PAYMENT_FAILED")
    assert reservation_status(system, b["id"]) == "RELEASED"
    assert available(system) == 1
    assert payment(system, b["paymentId"])["status"] == "FAILED"
    assert event_types(system) == ["payment.failed", "booking.cancelled"]
    assert system.emails_to("ilya@example.org") == ["Бронь отменена — Гараж 61, 22.11"]
    # интервал снова свободен
    assert book(system, user="anna").status_code == 201


def test_equipment_unavailable_cancels_booking(system):
    assert book(system, user="anna", room=demo.ROOM_BLUE).status_code == 201  # гитара занята Анной
    r = book(system)
    assert r.status_code == 409
    assert r.body["code"] == "EQUIPMENT_UNAVAILABLE"
    assert "Fender Stratocaster — свободно 0 из 1" in r.body["detail"]
    mine = system.call("GET", "/api/v1/bookings", "ilya").body["items"]
    assert [(x["status"], x["cancelReason"]) for x in mine] == [("CANCELLED", "EQUIPMENT_UNAVAILABLE")]
    # платёж не создавался, интервал комнаты свободен
    assert sum(1 for j in system.wiremock.journal if j["path"] == "/v3/payments") == 1
    assert book(system, equipment=()).status_code == 201


def test_payment_service_down_compensates(system):
    system.stop("payment")
    r = book(system)
    assert r.status_code == 503
    assert r.body["code"] == "SERVICE_UNAVAILABLE"
    assert r.headers["Retry-After"] == "5"
    system.settle()
    mine = system.call("GET", "/api/v1/bookings", "ilya").body["items"]
    assert [(x["status"], x["cancelReason"]) for x in mine] == [("CANCELLED", "PAYMENT_UNAVAILABLE")]
    assert reservation_status(system, mine[0]["id"]) == "RELEASED"
    assert available(system) == 1
    assert system.emails_to("ilya@example.org") == []  # об ошибке клиент узнал из ответа API


def test_payment_gateway_down_compensates(system):
    system.session.down.add("http://wiremock:8080")
    r = book(system)
    assert r.status_code == 503
    system.settle()
    mine = system.call("GET", "/api/v1/bookings", "ilya").body["items"]
    assert mine[0]["cancelReason"] == "PAYMENT_UNAVAILABLE"
    assert reservation_status(system, mine[0]["id"]) == "RELEASED"


def test_equipment_service_down_compensates(system):
    system.stop("equipment")
    r = book(system)
    assert r.status_code == 503
    system.settle()
    assert event_types(system) == ["booking.cancelled"]
    assert system.broker.published[0]["payload"]["reason"] == "EQUIPMENT_UNAVAILABLE"


def test_venue_service_down_creates_nothing(system):
    system.stop("venue")
    r = book(system)
    assert (r.status_code, r.body["code"]) == (503, "SERVICE_UNAVAILABLE")
    assert system.call("GET", "/api/v1/bookings", "ilya").body["items"] == []


def test_slot_already_taken(system):
    assert book(system, equipment=()).status_code == 201
    r = book(system, user="anna", hour=19, equipment=())
    assert (r.status_code, r.body["code"]) == (409, "SLOT_ALREADY_TAKEN")
    assert r.body["detail"] == "Комната занята с 18:00 до 20:00"


def test_expired_hold_then_late_payment_is_refunded(short_hold_system):
    system = short_hold_system
    b = book(system).body
    system.advance(61)
    got = booking(system, b["id"])
    assert (got["status"], got["paymentUrl"]) == ("EXPIRED", None)
    assert reservation_status(system, b["id"]) == "RELEASED"
    assert payment(system, b["paymentId"])["status"] == "CANCELLED"
    assert any(j["path"].endswith("/cancel") for j in system.wiremock.journal)

    # гонка: шлюз успел провести оплату до отмены
    system.pay(b)
    system.settle()
    assert booking(system, b["id"])["status"] == "EXPIRED"
    p = payment(system, b["paymentId"])
    assert (p["status"], p["refundedAmount"]) == ("REFUNDED", 2000.0)
    assert event_types(system) == ["booking.expired", "payment.completed", "payment.refund", "payment.refunded"]
    assert system.emails_to("ilya@example.org") == ["Бронь не оплачена и снята — Гараж 61, 22.11", "Деньги возвращены"]


def test_user_cancel_more_than_24h_before_refunds(system):
    b = book(system).body
    system.pay(b)
    system.settle()
    r = system.call("POST", f"/api/v1/bookings/{b['id']}/cancel", "ilya", json={"comment": "Перенесли репетицию"})
    assert r.status_code == 200
    assert (r.body["status"], r.body["cancelReason"]) == ("CANCELLED", "USER_REQUEST")
    system.settle()
    assert reservation_status(system, b["id"]) == "RELEASED"
    assert payment(system, b["paymentId"])["status"] == "REFUNDED"
    again = system.call("POST", f"/api/v1/bookings/{b['id']}/cancel", "ilya")
    assert (again.status_code, again.body["code"]) == (409, "BOOKING_NOT_CANCELLABLE")


def test_user_cancel_less_than_24h_before_keeps_money(system):
    b = book(system, days=0, hour=20, equipment=()).body
    system.pay(b)
    system.settle()
    assert system.call("POST", f"/api/v1/bookings/{b['id']}/cancel", "ilya").status_code == 200
    system.settle()
    assert payment(system, b["paymentId"])["status"] == "SUCCEEDED"
    assert "payment.refund" not in event_types(system)


def test_manager_sees_and_cancels_with_full_refund(system):
    b = book(system, days=0, hour=20, equipment=()).body
    system.pay(b)
    system.settle()
    venue_bookings = system.call("GET", f"/api/v1/bookings?venueId={demo.VENUE_GARAGE}", "manager").body
    assert [x["id"] for x in venue_bookings["items"]] == [b["id"]]
    assert system.call("GET", f"/api/v1/bookings?venueId={demo.VENUE_GARAGE}", "anna").status_code == 403
    r = system.call("POST", f"/api/v1/bookings/{b['id']}/cancel", "manager")
    assert r.body["cancelReason"] == "MANAGER_REQUEST"
    system.settle()
    assert payment(system, b["paymentId"])["status"] == "REFUNDED"


# --- надёжность обмена сообщениями ----------------------------------------------------------
def test_duplicate_event_is_processed_once(system):
    b = book(system, equipment=()).body
    system.pay(b)
    system.settle()
    completed = next(e for e in system.broker.published if e["eventType"] == "payment.completed")
    import json
    system.broker.publish("payment.events", "payment.completed", json.dumps(completed))
    system.settle()
    assert event_types(system).count("booking.confirmed") == 1


def test_handler_failure_goes_to_dlq_after_retries(short_hold_system):
    system = short_hold_system
    b = book(system, equipment=()).body
    system.session.down.add("http://wiremock:8080")  # payment-service не может отменить платёж у шлюза
    system.advance(61)
    assert len(system.broker.dlq["payment.booking-events"]) == 1
    assert payment(system, b["paymentId"])["status"] == "PENDING"
    # остальные подписчики обработали событие независимо
    assert system.emails_to("ilya@example.org") == ["Бронь не оплачена и снята — Гараж 61, 22.11"]


def test_smtp_failure_does_not_block_events(system):
    system.mailer.fail = True
    b = book(system, equipment=()).body
    system.pay(b)
    system.settle()
    assert booking(system, b["id"])["status"] == "CONFIRMED"
    system.mailer.fail = False
    system.advance(30)
    assert system.emails_to("ilya@example.org") == ["Бронь подтверждена — Гараж 61, 22.11, 18:00"]


# --- идемпотентность ------------------------------------------------------------------------
def test_idempotent_booking_creation(system):
    key = "7c1f0a52-3b7e-4d0e-9a41-2f6d8c5b1e90"
    first = book(system, key=key)
    second = book(system, key=key)
    assert first.status_code == second.status_code == 201
    assert first.body["id"] == second.body["id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert len(system.call("GET", "/api/v1/bookings", "ilya").body["items"]) == 1
    assert sum(1 for j in system.wiremock.journal if j["path"] == "/v3/payments") == 1

    other = book(system, key=key, hour=12)
    assert (other.status_code, other.body["code"]) == (422, "IDEMPOTENCY_KEY_REUSED")
    missing = book(system, key=None)
    assert (missing.status_code, missing.body["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")


def test_idempotent_error_is_replayed(system):
    book(system, user="anna", room=demo.ROOM_BLUE)
    key = "11111111-2222-4333-8444-555555555555"
    assert book(system, key=key).status_code == 409
    replay = book(system, key=key)
    assert (replay.status_code, replay.body["code"]) == (409, "EQUIPMENT_UNAVAILABLE")
    assert len(system.call("GET", "/api/v1/bookings", "ilya").body["items"]) == 1
