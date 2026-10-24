"""HTTP-фасад ML-сайдкара (контракт — api/app/services/ml_client.py).

Эндпоинты (все, кроме /health, требуют X-CC-ML-Token; без заданного
CC_ML_TOKEN приложение не стартует, если явно не выставлен
CC_ML_ALLOW_ANONYMOUS=1):

- ``POST /v1/embeddings`` {texts, kind, profile} -> {profile, dense_size, vectors}
- ``POST /v1/rerank`` {query, documents, top_k} -> {scores}
- ``POST /v1/mask`` {text, names} -> {text, spans, model, degraded}
- ``GET  /health`` -> {status, device, models, error: bool}

``model`` в ответе маскирования — ``hybrid`` только когда NER реально
отработал; ``regex`` + ``degraded=true`` означают, что api обязан трактовать
ответ как отказ backend'а (docs/12).

Ошибки всегда структурированы: ``{"detail": {"code": ..., "message": ...}}``.
Пока модели грузятся — 503 ``model_loading`` (с Retry-After); упавшая
загрузка — 503 ``model_unavailable``. Вход ограничен по объёму (MAX_* в
models.py), а тело запроса — по Content-Length до парсинга JSON:
сайдкар не принимает «весь корпус» одной пачкой.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

import models
from models import (
    MAX_CHARS,
    MAX_DOCUMENTS,
    MAX_NAMES,
    MAX_TEXTS,
    ModelNotReady,
    Registry,
    UnknownProfile,
    public_model_name,
)

log = logging.getLogger("ml.app")


# Потолки тела по Content-Length — до парсинга JSON. С запасом покрывают
# валидные MAX_* (utf-8 до 4 байт/символ): embeddings 64×20k, rerank
# query+256×20k, mask текст+256 имён.
_BODY_LIMITS = {
    "/v1/embeddings": 6 * 1024 * 1024,
    "/v1/rerank": 24 * 1024 * 1024,
    "/v1/mask": 1 * 1024 * 1024,
}

class ApiError(Exception):
    """Ошибка с машинным кодом и человекочитаемым сообщением."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def _error(status_code: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers=headers,
    )


# --- схемы запросов -------------------------------------------------------


class EmbeddingsRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=MAX_TEXTS)
    kind: Literal["query", "passage"] = "query"
    profile: str = Field(default="", max_length=64)

    @field_validator("texts")
    @classmethod
    def _texts_size(cls, texts: list[str]) -> list[str]:
        for i, text in enumerate(texts):
            if len(text) > MAX_CHARS:
                raise ValueError(f"texts[{i}]: больше {MAX_CHARS} символов")
        return texts


class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_CHARS)
    documents: list[str] = Field(..., min_length=1, max_length=MAX_DOCUMENTS)
    top_k: int | None = Field(default=None, ge=1, le=MAX_DOCUMENTS)

    @field_validator("documents")
    @classmethod
    def _documents_size(cls, documents: list[str]) -> list[str]:
        for i, doc in enumerate(documents):
            if len(doc) > MAX_CHARS:
                raise ValueError(f"documents[{i}]: больше {MAX_CHARS} символов")
        return documents

    @model_validator(mode="after")
    def _top_k_fits(self) -> "RerankRequest":
        if self.top_k is not None and self.top_k > len(self.documents):
            raise ValueError("top_k больше числа documents")
        return self


class MaskRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_CHARS)
    names: list[str] = Field(default_factory=list, max_length=MAX_NAMES)

    @field_validator("names")
    @classmethod
    def _names_size(cls, names: list[str]) -> list[str]:
        for i, name in enumerate(names):
            if len(name) > 200:
                raise ValueError(f"names[{i}]: больше 200 символов")
        return names


# --- приложение -----------------------------------------------------------


def create_app(registry: Registry | None = None, *, start_warmup: bool = True) -> FastAPI:
    registry = registry or Registry()
    token = os.environ.get("CC_ML_TOKEN", "").strip()
    allow_anonymous = os.environ.get("CC_ML_ALLOW_ANONYMOUS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not token:
        # Fail-closed: через сайдкар идёт сырой клинический текст. Токен
        # генерирует tools/gen-secrets.sh; анонимный режим — только явный флаг
        # (локальная отладка, не прод).
        if not allow_anonymous:
            raise RuntimeError(
                "CC_ML_TOKEN не задан: сайдкар не стартует без токена. "
                "Сгенерируйте секреты (tools/gen-secrets.sh) или для локальной "
                "отладки выставьте CC_ML_ALLOW_ANONYMOUS=1"
            )
        log.warning(
            "CC_ML_TOKEN пуст, CC_ML_ALLOW_ANONYMOUS=1: запросы принимаются "
            "без аутентификации — только для локальной отладки"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001 — подпись FastAPI
        if start_warmup:
            registry.start()
        yield

    app = FastAPI(
        title="Clinical Compass ML sidecar",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.registry = registry
    app.state.start_warmup = start_warmup

    # --- обработчики ошибок ---

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:  # noqa: ARG001
        headers = {"Retry-After": "10"} if exc.status_code == 503 else None
        return _error(exc.status_code, exc.code, exc.message, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ARG001
        # Не эхоим вход (в теле могут быть ПДн): только локация и правило.
        parts = [
            f"{'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
            for err in exc.errors()[:5]
        ]
        return _error(422, "invalid_request", "; ".join(parts) or "некорректный запрос")

    @app.exception_handler(ModelNotReady)
    async def _not_ready(request: Request, exc: ModelNotReady) -> JSONResponse:  # noqa: ARG001
        status = registry.status()
        if status["status"] == "error":
            code = "model_unavailable"
            message = "модели не загрузились, подробности — в логах сайдкара"
        else:
            code = "model_loading"
            message = "модели загружаются"
        return _error(503, code, message, {"Retry-After": "10"})

    # --- авторизация и готовность ---

    def require_token(x_cc_ml_token: str | None = Header(default=None)) -> None:
        if not token:
            return
        if not x_cc_ml_token:
            raise ApiError(401, "unauthorized", "нужен заголовок X-CC-ML-Token")
        if not secrets.compare_digest(x_cc_ml_token, token):
            raise ApiError(403, "forbidden", "неверный X-CC-ML-Token")

    def require_ready() -> Registry:
        if not registry.ready:
            raise ModelNotReady()
        return registry

    @app.middleware("http")
    async def _limit_body(request: Request, call_next):  # noqa: ANN001 — сигнатура Starlette
        """Отсечь гигантское тело по Content-Length до парсинга JSON.

        Лимиты в разы ниже максимального валидного тела под MAX_* из
        models.py (64/256 текстов по 20 000 символов); token-проверка
        остаётся первой зависимостью эндпоинта.
        """
        limit = _BODY_LIMITS.get(request.url.path)
        if limit is not None and request.method == "POST":
            raw = request.headers.get("content-length")
            if raw is None:
                return _error(411, "length_required", "требуется заголовок Content-Length")
            try:
                size = int(raw)
            except ValueError:
                return _error(400, "invalid_request", "некорректный Content-Length")
            if size < 0:
                return _error(400, "invalid_request", "некорректный Content-Length")
            if size > limit:
                return _error(
                    413,
                    "payload_too_large",
                    f"тело запроса больше {limit} байт",
                )
        return await call_next(request)

    # --- эндпоинты ---

    @app.get("/health")
    def health() -> dict:
        return registry.status()

    @app.post("/v1/embeddings", dependencies=[Depends(require_token), Depends(require_ready)])
    def embeddings(body: EmbeddingsRequest) -> dict:
        try:
            profile, dense_size, vectors = registry.embed(body.texts, body.kind, body.profile)
        except UnknownProfile as exc:
            raise ApiError(
                404,
                "unknown_profile",
                f"профиль {exc.args[0]!r} не сконфигурирован",
            ) from exc
        return {"profile": profile, "dense_size": dense_size, "vectors": vectors}

    @app.post("/v1/rerank", dependencies=[Depends(require_token), Depends(require_ready)])
    def rerank(body: RerankRequest) -> dict:
        # top_k — подсказка вызывающему; ответ всегда выровнен с documents,
        # чтобы клиент не собирал индексы заново.
        return {"scores": registry.rerank(body.query, body.documents)}

    @app.post("/v1/mask", dependencies=[Depends(require_token), Depends(require_ready)])
    def mask(body: MaskRequest) -> dict:
        return registry.mask(body.text, body.names)

    @app.get("/v1/profiles", dependencies=[Depends(require_token)])
    def profiles() -> dict:
        """Список профилей — для деплой-проверки, не для бизнес-логики."""
        return {
            "default": registry.default_profile,
            "profiles": {
                key: {
                    "model": public_model_name(value.model_id),
                    "dense_size": value.dense_size,
                    "query_prefix": value.query_prefix,
                    "passage_prefix": value.passage_prefix,
                }
                for key, value in registry.profiles.items()
            },
        }

    return app


# uvicorn app:app — точка входа образа (ml/Dockerfile).
app = create_app()
