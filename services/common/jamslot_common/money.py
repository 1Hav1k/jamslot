"""Деньги хранятся в копейках (целое число), в API — число рублей с двумя знаками."""
from decimal import Decimal, ROUND_HALF_UP


def to_kopecks(value) -> int:
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_rub(kopecks: int | None) -> float | None:
    if kopecks is None:
        return None
    return round(kopecks / 100, 2)


def to_provider_amount(kopecks: int) -> str:
    """Формат суммы ЮKassa: строка «2000.00»."""
    return f"{kopecks // 100}.{kopecks % 100:02d}"
