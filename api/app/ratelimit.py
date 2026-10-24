"""Простой in-process rate limiter (демо/пилот, один инстанс).

Защита от перебора пароля/2FA на /auth/login: скользящее окно по
(IP, email) плюс грубое окно по IP (CC_RATE_IP_MAX), чтобы перебор
произвольных email с одного адреса не обходил лимит и не рос в памяти.
Пороги и окно — CC_RATE_MAX / CC_RATE_IP_MAX / CC_RATE_WINDOW.

Считает только НЕУДАЧНЫЕ попытки (record_rate_hit вызывается на 401) — брутфорс
проявляется как серия ошибок, легитимные повторные успешные входы (e2e, повторный
вход пользователя) бюджет не исчерпывают.

Для MVP без Redis: допустимо, т.к. контейнер один; при горизонтальном
масштабировании окно умножается на число процессов — заменить на Redis.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .config import settings

_lock = threading.Lock()
_MAX_KEYS = 4096  # жёсткий потолок записей в каждом словаре
_EVICT_BATCH = 64  # сколько старейших ключей выкидываем за один раз на потолке

_hits: dict[tuple[str, str], deque[float]] = {}
_ip_hits: dict[str, deque[float]] = {}


def _fresh(dq: deque[float], now: float, window: int) -> deque[float]:
    while dq and now - dq[0] >= window:
        dq.popleft()
    return dq


def _evict_locked(store: dict, now: float, window: int) -> None:
    """Уборка: сначала протухшие, затем пачка старейших ключей.

    Полный проход по словарю на каждый запрос был бы O(n); на потолке
    освобождаем пачкой. Вытесненный ключ теряет счётчик (fail-open): точный
    глобальный учёт без внешнего хранилища не построить, а жёсткий потолок
    важнее — иначе перебор email растит память процесса без ограничений.
    """
    if len(store) < _MAX_KEYS:
        return
    for k, dq in list(store.items()):
        _fresh(dq, now, window)
        if not dq:
            del store[k]
    for k in list(store)[: max(0, len(store) - _MAX_KEYS + _EVICT_BATCH)]:
        del store[k]


def check_rate(key: tuple[str, str]) -> tuple[bool, int]:
    """Проверяет лимиты без записи попытки. Возвращает (разрешено, retry_after_seconds)."""
    now = time.monotonic()
    window = settings.rate_window
    ip = key[0]
    with _lock:
        # get, а не defaultdict-индексация: проверка не должна создавать запись.
        dq = _hits.get(key)
        if dq is not None:
            _fresh(dq, now, window)
            if not dq:
                _hits.pop(key, None)
            elif len(dq) >= settings.rate_max:
                return False, int(window - (now - dq[0])) + 1
        ip_dq = _ip_hits.get(ip)
        if ip_dq is not None:
            _fresh(ip_dq, now, window)
            if not ip_dq:
                _ip_hits.pop(ip, None)
            elif len(ip_dq) >= settings.rate_ip_max:
                return False, int(window - (now - ip_dq[0])) + 1
        return True, 0


def record_rate_hit(key: tuple[str, str]) -> None:
    """Фиксирует неудачную попытку входа в скользящие окна (пара и IP)."""
    now = time.monotonic()
    window = settings.rate_window
    with _lock:
        for store in (_hits, _ip_hits):
            if len(store) >= _MAX_KEYS:
                _evict_locked(store, now, window)
        _hits.setdefault(key, deque()).append(now)
        _ip_hits.setdefault(key[0], deque()).append(now)


def reset_rate_limits() -> None:
    """Тестовый хук: очищает окна попыток между тестами."""
    with _lock:
        _hits.clear()
        _ip_hits.clear()
