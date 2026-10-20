"""Ошибки API в формате RFC 7807 (application/problem+json)."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _problem(status: int, code: str, detail: str, instance: str | None = None) -> dict:
    body = {
        "type": "about:blank",
        "title": code,
        "status": status,
        "code": code,
        "detail": detail,
    }
    if instance:
        body["instance"] = instance
    return body


async def problem_handler(request: Request, exc: Problem) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content=_problem(exc.status, exc.code, exc.detail, str(request.url.path)),
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


def register_handlers(app) -> None:
    app.add_exception_handler(Problem, problem_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(ValueError, value_error_handler)