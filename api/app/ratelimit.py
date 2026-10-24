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
from collections import deque

from .config import settings

_lock = threading.Lock()
_MAX_KEYS = 4096  # порог уборки: перебор пар (IP,email) не должен растить память
_hits: dict[tuple[str, str], deque[float]] = {}


def check_rate(key: tuple[str, str]) -> tuple[bool, int]:
    """Проверяет лимит без записи попытки. Возвращает (разрешено, retry_after_seconds)."""
    now = time.monotonic()
    window = settings.rate_window
    limit = settings.rate_max
    with _lock:
        # get, а не defaultdict-индексация: проверка не должна создавать запись
        # (иначе перебор пар «IP,email» растит память без единой неудачной попытки).
        dq = _hits.get(key)
        if dq is None:
            return True, 0
        while dq and now - dq[0] >= window:
            dq.popleft()
        if not dq:
            _hits.pop(key, None)
            return True, 0
        if len(dq) >= limit:
            retry = int(window - (now - dq[0])) + 1
            return False, retry
        return True, 0


def _prune_locked(now: float, window: int) -> None:
    """Уборка словаря: сначала протухшие окна, затем самые старые ключи.

    Жёсткий потолок нужен, потому что ключ — пара (IP, email): перебор
    произвольных email создаёт неограниченное число записей. Вытеснение
    старейших означает fail-open для вытесненного ключа — без Redis точный
    глобальный счётчик не построить (см. docstring модуля).
    """
    for k, dq in list(_hits.items()):
        while dq and now - dq[0] >= window:
            dq.popleft()
        if not dq:
            del _hits[k]
    while len(_hits) >= _MAX_KEYS:
        del _hits[next(iter(_hits))]


def record_rate_hit(key: tuple[str, str]) -> None:
    """Фиксирует неудачную попытку входа в скользящее окно."""
    now = time.monotonic()
    window = settings.rate_window
    with _lock:
        if len(_hits) >= _MAX_KEYS:
            _prune_locked(now, window)
        dq = _hits.setdefault(key, deque())
        while dq and now - dq[0] >= window:
            dq.popleft()
        dq.append(now)


def reset_rate_limits() -> None:
    """Тестовый хук: очищает окно попыток между тестами."""
    with _lock:
        _hits.clear()
