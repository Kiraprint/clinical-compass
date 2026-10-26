"""CLI передачи СЭМД в РЭМД (FR-41): `python -m app.remd.cli <команда>`.

Команды:
  health                        флаг интеграции, схема URL, LU настроек, OAuth
  submit <session_id> --clinician <uuid>
                                отправить подписанный протокол сессии

Формат вывода — JSON (как у `app.emias.cli`); СНИЛС, ФИО, содержимое СЭМД,
секреты и токены не печатаются.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from app import db as db_module
from app.config import settings


def _session():
    if db_module.engine is None:
        db_module.setup(settings.database_url)
    return db_module.AsyncSessionLocal()


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _disabled() -> int:
    _print({"error": "remd_disabled", "hint": "CC_REMD_ENABLED=true"})
    return 2


async def _cmd_health(_args) -> int:
    from app.remd.client import RemdClient
    from app.remd.config import RemdConfig

    cfg = RemdConfig.from_settings()
    payload = {
        "enabled": cfg.enabled,
        "base_url_scheme": cfg.base_url.split(":", 1)[0],
        "lpu_uid": cfg.lpu_uid,
        "document_kind": cfg.document_kind,
        "callback_url_set": bool(cfg.callback_url),
        "authenticated": False,
    }
    if cfg.enabled:
        try:
            RemdClient(cfg).health()
            payload["authenticated"] = True
        except Exception as exc:  # noqa: BLE001 — health-команда не падает трейсбеком
            payload["error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
            _print(payload)
            return 1
    _print(payload)
    return 0


async def _cmd_submit(args) -> int:
    if not settings.remd_enabled:
        return _disabled()
    from app.remd import service

    async with _session() as session:
        status, payload = await service.submit(
            session, uuid.UUID(args.clinician), uuid.UUID(args.session_id), ip=None
        )
    _print({"http_status": status, "delivery": payload})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.remd.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    submit = sub.add_parser("submit")
    submit.add_argument("session_id")
    submit.add_argument("--clinician", required=True, help="uuid врача — автор сессии")
    args = parser.parse_args(argv)
    handler = {"health": _cmd_health, "submit": _cmd_submit}[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
