"""Бизнес-правила бронирования, не зависящие от БД и HTTP."""

ACTIVE_STATUSES = ("PENDING_PAYMENT", "CONFIRMED")
CANCELLABLE = ("PENDING_PAYMENT", "CONFIRMED")
FREE_CANCELLATION_SECONDS = 24 * 3600
EQUIPMENT_HOLD_GRACE = 5 * 60

# Переходы статусов брони: из какого статуса в какой можно перейти
TRANSITIONS = {
    "PENDING_PAYMENT": {"CONFIRMED", "CANCELLED", "EXPIRED"},
    "CONFIRMED": {"CANCELLED", "COMPLETED"},
    "CANCELLED": set(),
    "EXPIRED": set(),
    "COMPLETED": set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Полуинтервалы [start, end) пересекаются."""
    return a_start < b_end and b_start < a_end


def refund_on_cancel(status: str, starts_at: int, now: int, by_manager: bool) -> bool:
    """Нужно ли возвращать деньги при отмене.

    Неоплаченная бронь — возвращать нечего. Отмена менеджером площадки — всегда полный возврат.
    Отмена клиентом — полный возврат, если до начала не меньше 24 часов, иначе без возврата.
    """
    if status != "CONFIRMED":
        return False
    if by_manager:
        return True
    return starts_at - now >= FREE_CANCELLATION_SECONDS


def reaction_to_payment(status: str) -> str:
    """Что делать с бронью при событии payment.completed.

    confirm — подтвердить; refund — оплата пришла по уже закрытой брони, вернуть деньги;
    ignore — повтор или поздний дубль.
    """
    if status == "PENDING_PAYMENT":
        return "confirm"
    if status in ("EXPIRED", "CANCELLED"):
        return "refund"
    return "ignore"

