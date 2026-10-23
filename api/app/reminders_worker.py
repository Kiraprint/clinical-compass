"""Воркер доставки напоминаний врача (FR-24).

Отдельный процесс по образцу kb-enrichment: `python -m app.reminders_worker`.
Период тика — CC_REMINDER_TICK_SECONDS (по умолчанию 60). На тике сервис
`services.reminders.deliver_due` под pg_try_advisory_lock создаёт по одному
in-app Notification на каждое просроченное-к-доставке напоминание и рассылает
best-effort push подпискам врача. Дубли исключены notified_at + unique
notifications.reminder_id; параллельные инстансы — advisory-локом.

Флаги: --once (один тик и выход), --max-runs N (не более N тиков) — для smoke/CI.
Падение одного тика не роняет процесс: ошибка логируется, тик повторяется.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app import db
from app.config import settings
from app.services import reminders

log = logging.getLogger("app.reminders_worker")

async def tick() -> int:
    """Один тик доставки. Возвращает число доставленных напоминаний."""
    async with db.AsyncSessionLocal() as session:
        try:
            return await reminders.deliver_due(session)
        except Exception:
            await session.rollback()
            raise

async def run(*, once: bool = False, max_runs: int | None = None) -> int:
    interval = max(1, int(settings.reminder_tick_seconds))
    runs = 0
    while True:
        try:
            count = await tick()
            log.info("reminders: тик %s, доставлено %s", runs + 1, count)
        except Exception:  # noqa: BLE001 — воркер не должен падать на одном тике
            log.exception("reminders: тик упал")
        runs += 1
        if once or (max_runs is not None and runs >= max_runs):
            return 0
        await asyncio.sleep(interval)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Доставка напоминаний врача (FR-24)")
    parser.add_argument("--once", action="store_true", help="один тик и выход")
    parser.add_argument("--max-runs", type=int, default=None, help="не более N тиков")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.setup()
    try:
        return asyncio.run(run(once=args.once, max_runs=args.max_runs))
    finally:
        db.sync_engine.dispose()

if __name__ == "__main__":
    raise SystemExit(main())
