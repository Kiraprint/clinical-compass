"""Тесты ML-сайдкара в stub-режиме: без torch и без GPU.

Запуск: ``python -m pytest ml/tests -q`` из корня репозитория.
Реальные модели проверяются отдельным прогоном (REPORT-ml-sidecar.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ML = Path(__file__).resolve().parents[1]
_ROOT = _ML.parent
for path in (_ML, _ROOT / "core"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# До импорта app: реестр по умолчанию (app = create_app()) должен быть stub.
import os  # noqa: E402
# Тесты работают без токена: явный анонимный режим (в проде приложение без
# CC_ML_TOKEN не стартует — см. test_startup_requires_token).
os.environ.setdefault("CC_ML_ALLOW_ANONYMOUS", "1")
os.environ.setdefault("CC_ML_STUB", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app import create_app  # noqa: E402
from models import Registry  # noqa: E402


@pytest.fixture
def registry() -> Registry:
    return Registry(stub=True)


@pytest.fixture
def client(registry: Registry):
    with TestClient(create_app(registry, start_warmup=False)) as test_client:
        yield test_client


@pytest.fixture
def ready(client, registry: Registry):
    """Клиент с загруженными (stub) моделями."""
    registry.warmup()
    return client
