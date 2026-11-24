"""Общая обвязка Flask-приложения сервиса: Problem Details, X-Correlation-Id, логирование,
пагинация, разбор тела, текущий пользователь, идемпотентные эндпоинты, фоновые задачи."""
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import unquote

from flask import Flask, Response, g, request
from pydantic import BaseModel, ValidationError
from werkzeug.exceptions import HTTPException

from . import context
from .idempotency import request_hash, require_key, run_idempotent
from .problems import ApiError, bad_request

log = logging.getLogger(__name__)

_HTTP_CODES = {400: "VALIDATION_ERROR", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
               405: "METHOD_NOT_ALLOWED", 415: "VALIDATION_ERROR", 429: "RATE_LIMITED"}


def json_response(body, status: int = 200, headers: dict | None = None) -> Response:
    resp = Response(json.dumps(body, ensure_ascii=False), status=status, mimetype="application/json")
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def problem_response(err: ApiError) -> Response:
    body = err.to_problem(instance=request.path, trace_id=context.get_correlation_id())
    resp = Response(json.dumps(body, ensure_ascii=False), status=err.status, mimetype="application/problem+json")
    for k, v in err.headers.items():
        resp.headers[k] = v
    return resp


def create_base_app(service: str) -> Flask:
    app = Flask(service)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    @app.before_request
    def _correlation():
        g.started = time.perf_counter()
        g.cid_token = context.set_correlation_id(request.headers.get("X-Correlation-Id") or None)
        context.ensure_correlation_id()

    @app.after_request
    def _log(resp):
        resp.headers["X-Correlation-Id"] = context.get_correlation_id() or ""
        if request.path != "/health":
            ms = (time.perf_counter() - g.get("started", time.perf_counter())) * 1000
            log.info("%s %s → %d (%.0f мс)", request.method, request.full_path.rstrip("?"), resp.status_code, ms)
        return resp

    @app.teardown_request
    def _reset(_exc):
        token = g.pop("cid_token", None)
        if token is not None:
            context.reset_correlation_id(token)

    @app.errorhandler(ApiError)
    def _api_error(err: ApiError):
        if err.status >= 500:
            log.error("%s: %s", err.code, err.detail)
        return problem_response(err)

    @app.errorhandler(ValidationError)
    def _validation(err: ValidationError):
        return problem_response(validation_problem(err))

    @app.errorhandler(HTTPException)
    def _http(err: HTTPException):
        code = _HTTP_CODES.get(err.code, "INTERNAL_ERROR")
        detail = "Маршрут не найден" if err.code == 404 else (err.description or None)
        return problem_response(ApiError(err.code, code, detail))

    @app.errorhandler(Exception)
    def _unexpected(err: Exception):
        log.exception("Необработанная ошибка")
        return problem_response(ApiError(500, "INTERNAL_ERROR", "Внутренняя ошибка сервиса"))

    return app


def validation_problem(err: ValidationError) -> ApiError:
    errors = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"] if p != "__root__") or "body"
        errors.append({"field": loc, "message": e["msg"]})
    return bad_request("Одно или несколько полей заполнены неверно", errors)


def parse_body(model: type[BaseModel], required: bool = True):
    data = request.get_json(silent=True)
    if data is None:
        if not required and not request.get_data():
            data = {}
        else:
            raise bad_request("Тело запроса должно быть JSON-объектом")
    if not isinstance(data, dict):
        raise bad_request("Тело запроса должно быть JSON-объектом")
    return model.model_validate(data)


def page_params() -> tuple[int, int]:
    errors = []

    def num(name, default, lo, hi):
        raw = request.args.get(name)
        if raw is None:
            return default
        try:
            v = int(raw)
        except ValueError:
            errors.append({"field": name, "message": "должно быть целым числом"})
            return default
        if v < lo or (hi is not None and v > hi):
            errors.append({"field": name, "message": f"должно быть от {lo}" + (f" до {hi}" if hi else "")})
        return v
    page, size = num("page", 0, 0, None), num("size", 20, 1, 100)
    if errors:
        raise bad_request("Некорректные параметры пагинации", errors)
    return page, size


def page_body(items: list, page: int, size: int, total: int) -> dict:
    return {"items": items, "page": {"page": page, "size": size, "totalElements": total,
                                     "totalPages": (total + size - 1) // size}}


def query_enum(name: str, allowed) -> str | None:
    v = request.args.get(name)
    if v is not None and v not in allowed:
        raise bad_request("Некорректный параметр запроса",
                          [{"field": name, "message": f"допустимые значения: {', '.join(sorted(allowed))}"}])
    return v


def query_uuid(name: str, required: bool = False) -> str | None:
    import uuid
    v = request.args.get(name)
    if v is None:
        if required:
            raise bad_request("Не передан обязательный параметр", [{"field": name, "message": "обязательный параметр"}])
        return None
    try:
        return str(uuid.UUID(v))
    except ValueError:
        raise bad_request("Некорректный параметр запроса", [{"field": name, "message": "ожидается UUID"}])


def path_uuid(value: str) -> str:
    import uuid
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "Объект с указанным идентификатором не существует")


# --- пользователь ---------------------------------------------------------------------
@dataclass
class User:
    id: str
    name: str = ""
    email: str = ""
    roles: set = field(default_factory=set)
    venue_ids: set = field(default_factory=set)

    def has_role(self, *roles) -> bool:
        return bool(self.roles & set(roles))

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


def current_user(required: bool = True) -> User | None:
    """Пользователь из заголовков, которые проставил API Gateway после проверки JWT.
    Gateway удаляет такие заголовки из входящего запроса, поэтому подделать их снаружи нельзя."""
    uid = request.headers.get("X-User-Id")
    if not uid:
        if required:
            raise ApiError(401, "UNAUTHORIZED", "Access-токен отсутствует или просрочен")
        return None

    def split(h):
        return {x for x in (request.headers.get(h) or "").split(",") if x}
    return User(id=uid, name=unquote(request.headers.get("X-User-Name", "")),
                email=unquote(request.headers.get("X-User-Email", "")),
                roles=split("X-User-Roles"), venue_ids=split("X-User-Venue-Ids"))


# --- идемпотентные эндпоинты ----------------------------------------------------------
def idempotent(db, clock, scope_owner: str, fn) -> Response:
    """Обернуть обработчик POST: fn() → (status, body)."""
    key = require_key(request.headers.get("Idempotency-Key"))
    body = request.get_json(silent=True)
    scope = f"{scope_owner}:{request.method} {request.url_rule.rule if request.url_rule else request.path}"
    status, result, replay = run_idempotent(db, clock, scope, key, request_hash(request.method, request.path, body), fn)
    if isinstance(result, dict) and "__problem__" in result:
        problem = dict(result["__problem__"], instance=request.path, traceId=context.get_correlation_id())
        resp = Response(json.dumps(problem, ensure_ascii=False), status=status, mimetype="application/problem+json")
        for k, v in (result.get("headers") or {}).items():
            resp.headers[k] = v
    else:
        resp = json_response(result, status)
    if replay:
        resp.headers["Idempotent-Replayed"] = "true"
    return resp


# --- фоновые задачи -------------------------------------------------------------------
class PeriodicWorker(threading.Thread):
    def __init__(self, name: str, interval: float, fn):
        super().__init__(name=name, daemon=True)
        self.interval, self.fn = interval, fn
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.wait(self.interval):
            try:
                self.fn()
            except Exception:
                logging.getLogger(self.name).exception("Ошибка фоновой задачи")

    def stop(self):
        self._stop_event.set()


def health_route(app: Flask, db) -> None:
    @app.get("/health")
    def health():
        ok = db.ping()
        return json_response({"status": "UP" if ok else "DOWN"}, 200 if ok else 503)
