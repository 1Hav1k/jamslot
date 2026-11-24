"""Время в системе хранится как Unix-время в секундах (UTC), в API — ISO 8601."""
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

UTC = timezone.utc


class SystemClock:
    """Источник текущего времени. В тестах подменяется на управляемые часы."""

    def now(self) -> int:
        return int(time.time())


class ManualClock:
    """Часы для тестов: время двигается только вручную."""

    def __init__(self, start: int | None = None):
        self._now = int(time.time()) if start is None else start

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += int(seconds)


def to_iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_datetime(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("время должно содержать часовой пояс")
    return int(dt.timestamp())


def parse_iso(value: str) -> int:
    return from_datetime(datetime.fromisoformat(value))


def local_parts(epoch: int, tz: str) -> datetime:
    return datetime.fromtimestamp(epoch, tz=ZoneInfo(tz))


def day_bounds(date_str: str, tz: str) -> tuple[int, int]:
    """Начало и конец календарного дня в часовом поясе площадки."""
    zone = ZoneInfo(tz)
    start = datetime.fromisoformat(date_str).replace(tzinfo=zone)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())
