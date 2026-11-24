"""Синхронные вызовы соседних сервисов: таймаут, один повтор, circuit breaker,
проброс X-Correlation-Id и Idempotency-Key."""
import logging
import threading
import time

import requests

from . import context

log = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Сосед ответил ошибкой 4xx — это ответ по контракту, а не сбой."""

    def __init__(self, service: str, status: int, problem: dict | None):
        super().__init__(f"{service} ответил {status}")
        self.service, self.status, self.problem = service, status, problem or {}


class UpstreamUnavailable(Exception):
    """Сосед недоступен: нет соединения, таймаут, 5xx или открыт circuit breaker."""

    def __init__(self, service: str, reason: str):
        super().__init__(f"{service} недоступен: {reason}")
        self.service, self.reason = service, reason


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown: float = 30.0):
        self.threshold, self.cooldown = threshold, cooldown
        self.failures = 0
        self.open_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            return time.monotonic() >= self.open_until

    def success(self):
        with self._lock:
            self.failures = 0
            self.open_until = 0.0

    def failure(self):
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = time.monotonic() + self.cooldown
                self.failures = 0


class ServiceClient:
    def __init__(self, name: str, base_url: str, session=None, timeout: float = 2.0, retries: int = 1,
                 caller: str = "", auth: tuple[str, str] | None = None, extra_headers: dict | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.caller = caller
        self.auth = auth
        self.extra_headers = extra_headers or {}
        self.breaker = CircuitBreaker()

    def call(self, method: str, path: str, json=None, params=None, idempotency_key: str | None = None,
             headers: dict | None = None, retry: bool | None = None):
        """Вернуть (status, body) для 2xx, иначе UpstreamError / UpstreamUnavailable."""
        if not self.breaker.allow():
            raise UpstreamUnavailable(self.name, "circuit breaker открыт")
        hdrs = {"Accept": "application/json", "X-Correlation-Id": context.ensure_correlation_id(), **self.extra_headers}
        if self.caller:
            hdrs["X-Service-Name"] = self.caller
        if idempotency_key:
            hdrs["Idempotency-Key"] = idempotency_key
        hdrs.update(headers or {})
        # Повторять безопасно только GET и запросы с ключом идемпотентности
        safe = retry if retry is not None else (method == "GET" or bool(idempotency_key))
        attempts = 1 + (self.retries if safe else 0)
        url = f"{self.base_url}{path}"
        reason = ""
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(method, url, json=json, params=params, headers=hdrs,
                                            timeout=self.timeout, auth=self.auth)
            except requests.RequestException as exc:
                reason = exc.__class__.__name__
                log.warning("%s %s → %s (попытка %d/%d)", method, url, reason, attempt, attempts)
                continue
            if resp.status_code >= 500:
                reason = f"HTTP {resp.status_code}"
                log.warning("%s %s → %s (попытка %d/%d)", method, url, reason, attempt, attempts)
                continue
            self.breaker.success()
            body = _json(resp)
            log.info("%s %s → %d", method, url, resp.status_code)
            if 200 <= resp.status_code < 300:
                return resp.status_code, body
            raise UpstreamError(self.name, resp.status_code, body if isinstance(body, dict) else None)
        self.breaker.failure()
        raise UpstreamUnavailable(self.name, reason)


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return None
