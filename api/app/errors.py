"""Ошибки API в формате RFC 7807 (application/problem+json)."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse


class Problem(Exception):
    """RFC 7807-ошибка домена. `headers` — для 503 (Retry-After) и подобных.

    `fields` — привязка ошибки к полю ввода (`{"diagnosis.icd10": "…"}`): клиент
    показывает текст рядом с самим полем, а не одной плашкой в начале страницы.
    Ключ — путь поля в документе, значение — короткая причина без префикса пути.
    """

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        headers: dict[str, str] | None = None,
        fields: dict[str, str] | None = None,
    ):
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers or {}
        self.fields = fields or {}
        super().__init__(detail)


def _problem(
    status: int,
    code: str,
    detail: str,
    instance: str | None = None,
    fields: dict[str, str] | None = None,
) -> dict:
    body = {
        "type": "about:blank",
        "title": code,
        "status": status,
        "code": code,
        "detail": detail,
    }
    if instance:
        body["instance"] = instance
    if fields:
        body["fields"] = dict(fields)
    return body


async def problem_handler(request: Request, exc: Problem) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content=_problem(
            exc.status, exc.code, exc.detail, str(request.url.path), exc.fields
        ),
        headers=exc.headers or None,
    )


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # F5 (ГОСТ 56939/152-ФЗ): pydantic echo'ит поле input — там могут быть пароль
    # или PHI (текст дневника/чата). Вырезаем input и ctx перед сериализацией.
    safe = []
    for e in exc.errors()[:3]:
        e = {k: v for k, v in e.items() if k not in ("input", "ctx")}
        safe.append(e)
    return JSONResponse(
        status_code=422,
        content=_problem(422, "VALIDATION", str(safe), str(request.url.path)),
    )


async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_problem(422, "VALIDATION", str(exc), str(request.url.path)),
    )



async def pool_timeout_handler(request: Request, exc: Exception) -> JSONResponse:
    """Пул соединений исчерпан (CC_DB_POOL_TIMEOUT) → 503 + Retry-After.

    Без явного маппинга SQLAlchemy TimeoutError уходил в общий 500: клиент
    не понимал, что это временная перегрузка, и не мог ретраить с задержкой.
    """
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "2"},
        content=_problem(
            503, "DB_UNAVAILABLE", "Нет свободного соединения с БД, повторите запрос",
            str(request.url.path),
        ),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """HTTP-исключения Starlette/FastAPI — в тот же Problem-конверт.

    Без явного обработчика `{"detail": ...}` (битый multipart, несовпадение
    метода, 404 роутера) ломал единый формат ошибок для клиента.
    """
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE"}.get(
        exc.status_code, "HTTP_ERROR"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_problem(exc.status_code, code, str(exc.detail), str(request.url.path)),
        headers=getattr(exc, "headers", None) or None,
    )


def register_handlers(app) -> None:
    app.add_exception_handler(Problem, problem_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(ValueError, value_error_handler)
    # sqlalchemy.exc.TimeoutError — исчерпан пул (см. CC_DB_POOL_TIMEOUT).
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    app.add_exception_handler(PoolTimeoutError, pool_timeout_handler)