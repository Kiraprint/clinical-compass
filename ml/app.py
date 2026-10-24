"""HTTP-фасад ML-сайдкара (контракт — api/app/services/ml_client.py).

Эндпоинты (все, кроме /health, требуют X-CC-ML-Token при заданном
CC_ML_TOKEN):

- ``POST /v1/embeddings`` {texts, kind, profile} -> {profile, dense_size, vectors}
- ``POST /v1/rerank`` {query, documents, top_k} -> {scores}
- ``POST /v1/mask`` {text, names} -> {text, spans, model}
- ``GET  /health`` -> {status, device, models}

Ошибки всегда структурированы: ``{"detail": {"code": ..., "message": ...}}``.
Пока модели грузятся — 503 ``model_loading`` (с Retry-After); упавшая
загрузка — 503 ``model_unavailable``. Вход ограничен по объёму (MAX_* в
models.py): сайдкар не принимает «весь корпус» одной пачкой.
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
)

log = logging.getLogger("ml.app")


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
        code = "model_unavailable" if status["status"] == "error" else "model_loading"
        return _error(503, code, str(exc), {"Retry-After": "10"})

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
            raise ModelNotReady(registry.status()["error"] or "модели загружаются")
        return registry

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
                    "model": value.model_id,
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
