"""Uvicorn-entrypoint для нагрузочного стенда (не входит в прод-образ).

Опционально переопределяет размер пула SQLAlchemy через
CC_LOADTEST_POOL_SIZE/CC_LOADTEST_MAX_OVERFLOW — чтобы измерить, снимает ли
увеличение пула обрыв на высокой конкуренции.

`--workers N` поднимает N процессов uvicorn. Приложение обязано передаваться
строкой импорта (`app.main:app`): с объектом uvicorn отказывается работать в
многопроцессном режиме. Каждый воркер импортирует app.main заново, поэтому все
настройки (пул, WEB_CONCURRENCY) обязаны быть в env ДО старта — их читает
app.config при импорте, а бюджет соединений проверяет app.db.validate_pool_budget.
"""

from __future__ import annotations

import argparse
import os

import uvicorn


def _apply_pool_env() -> None:
    """CC_LOADTEST_POOL_SIZE (профиль пула) → CC_DB_POOL_SIZE/CC_DB_MAX_OVERFLOW.

    Явный CC_LOADTEST_MAX_OVERFLOW важнее производной pool//4: бюджет соединений
    считается как N × (pool + overflow + 2) и должен быть предсказуемым.
    """
    pool_size = os.environ.get("CC_LOADTEST_POOL_SIZE")
    if not pool_size:
        return
    size = int(pool_size)
    if not size:
        return
    os.environ["CC_DB_POOL_SIZE"] = str(size)
    overflow = os.environ.get("CC_LOADTEST_MAX_OVERFLOW")
    os.environ["CC_DB_MAX_OVERFLOW"] = (
        overflow if overflow is not None else str(max(1, size // 4))
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="API стенда нагрузочных замеров")
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("WEB_CONCURRENCY", "1")),
        help="число процессов uvicorn (по умолчанию WEB_CONCURRENCY)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CC_LOADTEST_PORT", "8011")))
    args = parser.parse_args(argv)

    _apply_pool_env()
    # WEB_CONCURRENCY читает app.db.worker_count(): без этой переменной каждый
    # воркер считал бы бюджет соединений как для одного процесса.
    os.environ["WEB_CONCURRENCY"] = str(max(1, args.workers))
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        workers=max(1, args.workers),
        log_level="warning",
        access_log=False,
    )


# Обязательный guard: multiprocessing spawn переисполняет главный модуль в
# дочернем процессе (как __mp_main__) — без него воркеры рекурсивно поднимали бы
# новые supervisor'ы.
if __name__ == "__main__":
    main()
