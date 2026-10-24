"""Uvicorn-entrypoint для нагрузочного стенда (не входит в прод-образ).

Опционально переопределяет размер пула SQLAlchemy через
CC_LOADTEST_POOL_SIZE — чтобы измерить, снимает ли увеличение пула обрыв на
высокой конкуренции.
"""

from __future__ import annotations

import os

import uvicorn

_pool_size = int(os.environ.get("CC_LOADTEST_POOL_SIZE", "0"))
if _pool_size:
    # Настройки читаются app.config при импорте app.main, поэтому пул задаётся
    # env-переменными до старта uvicorn. Приложение само создаёт async-engine.
    os.environ["CC_DB_POOL_SIZE"] = str(_pool_size)
    os.environ["CC_DB_MAX_OVERFLOW"] = str(max(1, _pool_size // 4))

uvicorn.run(
    "app.main:app",
    host="127.0.0.1",
    port=int(os.environ.get("CC_LOADTEST_PORT", "8011")),
    log_level="warning",
    access_log=False,
)
