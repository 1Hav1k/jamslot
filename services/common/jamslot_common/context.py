"""Сквозной идентификатор сценария (X-Correlation-Id) и настройка логов."""
import contextvars
import logging
import os
import sys
import uuid

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None):
    return _correlation_id.set(value)


def reset_correlation_id(token) -> None:
    _correlation_id.reset(token)


def ensure_correlation_id() -> str:
    cid = _correlation_id.get()
    if not cid:
        cid = str(uuid.uuid4())
        _correlation_id.set(cid)
    return cid


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get() or "-"
        return True


def setup_logging(service: str, level: str | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_jamslot_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_CorrelationFilter())
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s %(levelname)-5s {service} [%(correlation_id)s] %(name)s: %(message)s"))
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger("pika").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    root._jamslot_configured = True
