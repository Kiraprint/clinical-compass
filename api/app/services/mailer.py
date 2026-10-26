"""FR-48: канал e-mail для кодов подтверждения (docs/03 §15).

Только stdlib `smtplib`: новая зависимость ради одного письма не нужна.
Отправка блокирует поток до таймаута SMTP, поэтому вызывается через
`anyio.to_thread.run_sync` — event loop API не ждёт сеть.

Ни код, ни адрес не попадают в логи: в журнал уходит только факт
«канал не настроен» один раз на процесс.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import anyio

from app.config import settings

log = logging.getLogger(__name__)

_WARNING_SHOWN = False


class MailUnavailable(Exception):
    """Письмо не отправлено: канал не настроен или SMTP отказал."""


def configured() -> bool:
    """Канал настроен: есть хост и адрес отправителя."""
    return bool(settings.smtp_host and settings.smtp_from)


def warn_unconfigured() -> None:
    """Один WARNING на процесс: без SMTP коды некому отправить."""
    global _WARNING_SHOWN
    if not _WARNING_SHOWN:
        _WARNING_SHOWN = True
        log.warning("SMTP не настроен (CC_SMTP_HOST/CC_SMTP_FROM): коды e-mail не отправляются")


def _send_sync(to: str, subject: str, text: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        if settings.smtp_tls:
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)


async def send_code(to: str, subject: str, code: str) -> None:
    """Отправить код подтверждения. Ошибка — `MailUnavailable` (роут → 503)."""
    if not configured():
        warn_unconfigured()
        raise MailUnavailable("smtp not configured")
    text = f"Ваш код подтверждения: {code}\n\nКод действует 10 минут, попыток — 5."
    try:
        await anyio.to_thread.run_sync(_send_sync, to, subject, text)
    except Exception as exc:  # noqa: BLE001 — SMTP-сбой не должен раскрывать детали
        log.warning("Отправка письма не удалась: %s", type(exc).__name__)
        raise MailUnavailable(type(exc).__name__) from exc
