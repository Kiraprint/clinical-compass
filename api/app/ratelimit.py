"""Простой in-process rate limiter (демо/пилот, один инстанс).

Защита от перебора пароля/2FA на /auth/login: скользящее окно по
(IP, email). Порог и окно — конфигурируются через env (CC_RATE_MAX, CC_RATE_WINDOW).

Считает только НЕУДАЧНЫЕ попытки (record_rate_hit вызывается на 401) — брутфорс
проявляется как серия ошибок, а легитимные повторные успешные входы (e2e, повторный
вход пользователя) не должны исчерпывать бюджет. Успешный вход атакой не является.

Для MVP без redis: допустимо, т.к. контейнер один; при горизонтальном
масштабировании заменить на Redis/сторонний rate limiter.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from .config import settings

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def check_rate(key: tuple[str, str]) -> tuple[bool, int]:
    """Проверяет лимит без записи попытки. Возвращает (разрешено, retry_after_seconds)."""
    now = time.monotonic()
    window = settings.rate_window
    limit = settings.rate_max
    with _lock:
        dq = _hits[key]
        while dq and now - dq[0] >= window:
            dq.popleft()
        if len(dq) >= limit:
            retry = int(window - (now - dq[0])) + 1
            return False, retry
        return True, 0


def record_rate_hit(key: tuple[str, str]) -> None:
    """Фиксирует неудачную попытку входа в скользящее окно."""
    now = time.monotonic()
    window = settings.rate_window
    with _lock:
        dq = _hits[key]
        while dq and now - dq[0] >= window:
            dq.popleft()
        dq.append(now)


def reset_rate_limits() -> None:
    """Тестовый хук: очищает окно попыток между тестами."""
    with _lock:
        _hits.clear()
