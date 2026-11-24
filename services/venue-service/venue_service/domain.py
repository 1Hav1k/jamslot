"""Бизнес-правила каталога: проверка интервала и расчёт стоимости комнаты."""
from jamslot_common.problems import ApiError, business
from jamslot_common.timeutil import local_parts

MAX_BOOKING_HOURS = 12


def _hour(hhmm: str) -> int:
    return int(hhmm[:2])


def check_interval(starts: int, ends: int, now: int, opens_at: str, closes_at: str,
                   min_hours: int, tz: str) -> int:
    """Вернуть длительность в часах или выбросить ошибку по контракту (400 или 422)."""
    if ends <= starts:
        raise ApiError(400, "VALIDATION_ERROR", "Конец интервала должен быть позже начала",
                       [{"field": "endsAt", "message": "должно быть больше startsAt"}])
    if starts % 3600 or ends % 3600:
        raise business("Начало и конец интервала должны быть кратны часу")
    if starts <= now:
        raise business("Нельзя забронировать время в прошлом")
    hours = (ends - starts) // 3600
    if hours < min_hours:
        raise business(f"Минимальная длительность брони этой комнаты — {min_hours} ч")
    if hours > MAX_BOOKING_HOURS:
        raise business(f"Максимальная длительность брони — {MAX_BOOKING_HOURS} ч")
    start_l, end_l = local_parts(starts, tz), local_parts(ends, tz)
    end_hour = end_l.hour
    if end_l.date() != start_l.date():
        # допускается только окончание ровно в полночь
        if (end_l.date() - start_l.date()).days == 1 and end_l.hour == 0:
            end_hour = 24
        else:
            raise business("Интервал должен укладываться в один день")
    opens, closes = _hour(opens_at), _hour(closes_at) or 24
    if start_l.hour < opens or end_hour > closes:
        raise business(f"Интервал выходит за часы работы площадки ({opens_at}–{closes_at})")
    return hours


def room_price(price_per_hour_kop: int, hours: int) -> int:
    return price_per_hour_kop * hours
