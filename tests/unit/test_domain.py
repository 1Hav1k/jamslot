"""Unit-тесты бизнес-правил без БД и HTTP."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking_service import domain as booking
from jamslot_common.money import to_kopecks, to_provider_amount, to_rub
from jamslot_common.problems import ApiError
from jamslot_common.timeutil import day_bounds, parse_iso, to_iso
from notification_service.main import compose
from venue_service.domain import check_interval, room_price

TZ = "Europe/Moscow"
NOW = int(datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo(TZ)).timestamp())


def at(day: int, hour: int) -> int:
    return int((datetime(2026, 11, 20, tzinfo=ZoneInfo(TZ)) + timedelta(days=day, hours=hour)).timestamp())


# --- venue-service: проверка интервала ------------------------------------------------------
def test_interval_ok_returns_hours():
    assert check_interval(at(1, 18), at(1, 20), NOW, "10:00", "23:00", 1, TZ) == 2
    assert room_price(70000, 2) == 140000


@pytest.mark.parametrize("starts,ends,min_hours,opens,closes,message", [
    (at(1, 18) + 1800, at(1, 20), 1, "10:00", "23:00", "кратны часу"),
    (at(-1, 18), at(-1, 20), 1, "10:00", "23:00", "в прошлом"),
    (at(1, 18), at(1, 19), 2, "10:00", "23:00", "Минимальная длительность"),
    (at(1, 9), at(1, 11), 1, "10:00", "23:00", "часы работы"),
    (at(1, 22), at(2, 0), 1, "10:00", "23:00", "часы работы"),
    (at(1, 22), at(2, 1), 1, "10:00", "00:00", "один день"),
    (at(1, 8), at(1, 21), 1, "08:00", "23:00", "Максимальная длительность"),
])
def test_interval_business_rules(starts, ends, min_hours, opens, closes, message):
    with pytest.raises(ApiError) as e:
        check_interval(starts, ends, NOW, opens, closes, min_hours, TZ)
    assert e.value.status == 422 and e.value.code == "BUSINESS_RULE_VIOLATION"
    assert message in e.value.detail


def test_interval_until_midnight_when_open_till_midnight():
    assert check_interval(at(1, 22), at(2, 0), NOW, "10:00", "00:00", 1, TZ) == 2


def test_interval_end_before_start_is_validation_error():
    with pytest.raises(ApiError) as e:
        check_interval(at(1, 20), at(1, 18), NOW, "10:00", "23:00", 1, TZ)
    assert (e.value.status, e.value.code) == (400, "VALIDATION_ERROR")


# --- booking-service: статусы и правила отмены ---------------------------------------------
def test_transitions():
    assert booking.can_transition("PENDING_PAYMENT", "CONFIRMED")
    assert booking.can_transition("CONFIRMED", "COMPLETED")
    assert not booking.can_transition("EXPIRED", "CONFIRMED")
    assert not booking.can_transition("CANCELLED", "CANCELLED")
    assert not booking.can_transition("COMPLETED", "CANCELLED")


def test_overlaps_half_open_intervals():
    assert booking.overlaps(10, 20, 15, 25)
    assert not booking.overlaps(10, 20, 20, 30)  # стык — не пересечение


@pytest.mark.parametrize("status,hours_before,by_manager,expected", [
    ("CONFIRMED", 24, False, True),
    ("CONFIRMED", 23, False, False),
    ("CONFIRMED", 1, True, True),
    ("PENDING_PAYMENT", 48, False, False),
])
def test_refund_on_cancel(status, hours_before, by_manager, expected):
    assert booking.refund_on_cancel(status, NOW + hours_before * 3600, NOW, by_manager) is expected


@pytest.mark.parametrize("status,action", [
    ("PENDING_PAYMENT", "confirm"), ("EXPIRED", "refund"), ("CANCELLED", "refund"),
    ("CONFIRMED", "ignore"), ("COMPLETED", "ignore"),
])
def test_reaction_to_late_or_duplicate_payment(status, action):
    assert booking.reaction_to_payment(status) == action


# --- деньги и время -------------------------------------------------------------------------
def test_money_conversions():
    assert to_kopecks("700.5") == 70050
    assert to_kopecks(0.1 + 0.2) == 30
    assert to_rub(140000) == 1400.0
    assert to_provider_amount(200005) == "2000.05"


def test_time_conversions():
    assert to_iso(parse_iso("2026-11-20T18:00:00+03:00")) == "2026-11-20T15:00:00Z"
    start, end = day_bounds("2026-11-20", TZ)
    assert (to_iso(start), end - start) == ("2026-11-19T21:00:00Z", 86400)
    with pytest.raises(ValueError):
        parse_iso("2026-11-20T18:00:00")  # без часового пояса


# --- notification-service: тексты писем -----------------------------------------------------
def test_compose_cancel_letter():
    b = {"startsAt": "2026-11-22T15:00:00Z", "endsAt": "2026-11-22T17:00:00Z", "venueName": "Гараж 61",
         "roomName": "Комната «Красная»", "customer": {"name": "Илья"}, "totalPrice": 2000.0}
    subject, body = compose("BOOKING_CANCELLED", b, TZ, {"reason": "PAYMENT_FAILED"})
    assert subject == "Бронь отменена — Гараж 61, 22.11"
    assert "потому что оплата не прошла" in body and "22.11.2026, 18:00–20:00" in body
