"""Единая модель ошибок — Problem Details (RFC 9457 / RFC 7807)."""

TITLES = {
    "VALIDATION_ERROR": "Некорректный запрос",
    "IDEMPOTENCY_KEY_REQUIRED": "Не передан ключ идемпотентности",
    "UNAUTHORIZED": "Требуется авторизация",
    "FORBIDDEN": "Доступ запрещён",
    "NOT_FOUND": "Ресурс не найден",
    "METHOD_NOT_ALLOWED": "Метод не поддерживается",
    "BUSINESS_RULE_VIOLATION": "Нарушено бизнес-правило",
    "SLOT_ALREADY_TAKEN": "Интервал уже занят",
    "EQUIPMENT_UNAVAILABLE": "Оборудование недоступно",
    "BOOKING_NOT_CANCELLABLE": "Бронь нельзя отменить",
    "PAYMENT_ALREADY_EXISTS": "Платёж уже существует",
    "IDEMPOTENCY_KEY_REUSED": "Ключ идемпотентности уже использован с другим запросом",
    "IDEMPOTENCY_REQUEST_IN_PROGRESS": "Запрос с этим ключом ещё выполняется",
    "RATE_LIMITED": "Слишком много запросов",
    "PAYMENT_PROVIDER_ERROR": "Ошибка платёжного шлюза",
    "SERVICE_UNAVAILABLE": "Сервис временно недоступен",
    "INTERNAL_ERROR": "Внутренняя ошибка сервиса",
}

SLUGS = {code: code.lower().replace("_", "-") for code in TITLES}


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str | None = None,
                 errors: list[dict] | None = None, headers: dict | None = None,
                 title: str | None = None):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.errors = errors
        self.headers = headers or {}
        self.title = title

    def to_problem(self, instance: str | None = None, trace_id: str | None = None) -> dict:
        body = {
            "type": f"urn:jamslot:problem:{SLUGS.get(self.code, self.code.lower().replace('_', '-'))}",
            "title": self.title or TITLES.get(self.code, self.code),
            "status": self.status,
            "code": self.code,
        }
        if self.detail:
            body["detail"] = self.detail
        if instance:
            body["instance"] = instance
        if trace_id:
            body["traceId"] = trace_id
        if self.errors:
            body["errors"] = self.errors
        return body

    @classmethod
    def from_problem(cls, status: int, problem: dict | None) -> "ApiError":
        """Пробросить ошибку соседнего сервиса клиенту с тем же кодом и описанием."""
        problem = problem or {}
        return cls(status, problem.get("code") or ("NOT_FOUND" if status == 404 else "INTERNAL_ERROR"),
                   problem.get("detail"), problem.get("errors"), title=problem.get("title"))


def not_found(detail: str = "Объект с указанным идентификатором не существует") -> ApiError:
    return ApiError(404, "NOT_FOUND", detail)


def forbidden(detail: str = "Недостаточно прав для операции") -> ApiError:
    return ApiError(403, "FORBIDDEN", detail)


def business(detail: str) -> ApiError:
    return ApiError(422, "BUSINESS_RULE_VIOLATION", detail)


def bad_request(detail: str, errors: list[dict] | None = None) -> ApiError:
    return ApiError(400, "VALIDATION_ERROR", detail, errors)
