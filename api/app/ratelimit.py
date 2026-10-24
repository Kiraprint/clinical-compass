"""Rate limiter логина: окна в памяти процесса или общий счётчик в Redis.

Защита от перебора пароля/2FA на /auth/login: окно по (IP, email) плюс грубое
окно по IP (CC_RATE_IP_MAX), чтобы перебор произвольных email с одного адреса
не обходил лимит и не рос в памяти. Пороги и окно — CC_RATE_MAX /
CC_RATE_IP_MAX / CC_RATE_WINDOW.

Считает только НЕУДАЧНЫЕ попытки (record_rate_hit вызывается на 401) — брутфорс
проявляется как серия ошибок, легитимные повторные успешные входы (e2e, повторный
вход пользователя) бюджет не исчерпывают.

Два хранилища (CC_RATE_BACKEND + CC_REDIS_URL, docs/03 §12):

- ``memory`` (дефолт) — словари процесса. Бюджет умножается на число процессов
  (``uvicorn --workers N``) и реплик, не переживает рестарт. Только демо/один
  процесс: с ``N>1`` атакующий получает до ``N × CC_RATE_MAX`` попыток.
- ``redis`` — атомарные фиксированные окна в общем хранилище: ключ содержит
  номер окна, запись — ``INCR`` + ``EXPIRE`` в одном pipeline, поэтому все
  процессы/реплики тратят ОДИН бюджет ``CC_RATE_MAX``. Нужны extra
  ``api[redis]`` и ``CC_REDIS_URL``.
- ``auto`` — redis, если ``CC_REDIS_URL`` задан и Redis ответил на ``PING`` при
  старте, иначе memory с однократным WARNING (graceful degradation).

Окно в Redis фиксированное, в памяти — скользящее: на границе окна Redis
допускает всплеск до ``2 × CC_RATE_MAX`` (ключи соседних окон не связаны).
TTL ключа — два окна, поэтому счётчики живут в хранилище ограниченное время;
в ключах нет открытых ПДн — только SHA-256 от (IP, email) или IP.

Недоступность Redis не должна вешать логин: socket/connect timeout короткий
(``CC_RATE_REDIS_TIMEOUT``), повторов нет. При ошибке бэкенд до рестарта
переключается на memory: общий бюджет ослабляется (fail-open), но локальный
лимит продолжает работать; факт пишется в WARNING и в счётчик
``redis_errors``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections import deque

import anyio
from .config import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()
_MAX_KEYS = 4096  # жёсткий потолок записей в каждом словаре
_EVICT_BATCH = 64  # сколько старейших ключей выкидываем за один раз на потолке
_REDIS_PREFIX = "cc:rl"  # namespace ключей лимитера в общем Redis

_hits: dict[tuple[str, str], deque[float]] = {}
_ip_hits: dict[str, deque[float]] = {}

# Выбор хранилища разрешается один раз (на старте API, app.main.lifespan).
_backend: str | None = None  # None → ещё не разрешён; затем "memory" | "redis"
_client = None  # Redis-клиент или None; redis-py импортируется лениво
_backend_lock = threading.Lock()
redis_errors = 0  # счётчик ошибок Redis (fail-open); для логов/наблюдаемости
_redis_warned = False
_warned: set[str] = set()

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

# --- память процесса ---------------------------------------------------------

def _memory_check(key: tuple[str, str]) -> tuple[bool, int]:
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

def _memory_record(key: tuple[str, str]) -> None:
    now = time.monotonic()
    window = settings.rate_window
    with _lock:
        for store in (_hits, _ip_hits):
            if len(store) >= _MAX_KEYS:
                _evict_locked(store, now, window)
        _hits.setdefault(key, deque()).append(now)
        _ip_hits.setdefault(key[0], deque()).append(now)

# --- Redis -------------------------------------------------------------------

def _warn_once(tag: str, msg: str) -> None:
    if tag in _warned:
        return
    _warned.add(tag)
    log.warning(msg)

def _redis_failed(exc: BaseException) -> None:
    """Redis сломался в рантайме: до рестарта считаем в памяти (fail-open)."""
    global _client, _backend, redis_errors, _redis_warned
    redis_errors += 1
    _client = None
    _backend = "memory"
    if not _redis_warned:
        _redis_warned = True
        log.warning(
            "Rate limiter: ошибка Redis (%r) — переключение на memory до рестарта; "
            "общий бюджет логинов ослаблен (fail-open), локальный лимит работает",
            exc,
        )

def _connect():
    """Создаёт и проверяет Redis-клиент. None — extra нет или Redis недоступен.

    Импорт redis-py ленивый: без extra ``api[redis]`` модуль обязан работать.
    Повторы (``Retry(NoBackoff(), 0)``) выключены намеренно: redis-py 6 по
    умолчанию ретраит connection errors и умножил бы короткий timeout на 3,
    а мёртвый Redis не должен задерживать логин.
    """
    try:
        from redis import Redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:  # extra api[redis] не установлен
        _warn_once(
            "no-redis-pkg",
            "Rate limiter: CC_REDIS_URL задан, но пакет redis не установлен "
            '(`pip install -e "api[redis]"`) — режим memory',
        )
        return None
    timeout = settings.rate_redis_timeout
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            retry=Retry(NoBackoff(), 0),
            decode_responses=True,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 — любая сетевая ошибка = деградация
        _warn_once(
            "redis-down",
            f"Rate limiter: Redis недоступен ({exc!r}) — режим memory "
            "(бюджет логинов на процесс, fail-open по общему счёту)",
        )
        return None
    return client

def _resolve_backend() -> str:
    """Фиксирует хранилище при первом обращении (в проде — на старте API)."""
    global _backend, _client
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        mode = (settings.rate_backend or "auto").strip().lower()
        if mode not in ("auto", "memory", "redis"):
            _warn_once(
                "bad-mode",
                f"Rate limiter: неизвестный CC_RATE_BACKEND={settings.rate_backend!r} — memory",
            )
            mode = "memory"
        if mode != "memory" and settings.redis_url:
            client = _connect()
            if client is not None:
                _client = client
                _backend = "redis"
                log.info("Rate limiter: общий счётчик в Redis (CC_REDIS_URL)")
                return _backend
        elif mode == "redis":
            _warn_once(
                "redis-no-url",
                "Rate limiter: CC_RATE_BACKEND=redis без CC_REDIS_URL — режим memory",
            )
        _backend = "memory"
        log.info("Rate limiter: in-process memory (бюджет на процесс)")
        return _backend

def init_backend() -> str:
    """Разрешает хранилище на старте (PING Redis) и возвращает режим."""
    return _resolve_backend()

def _window_id(now: float, window: int) -> int:
    return int(now // window)

def _retry_after(now: float, window: int) -> int:
    return int(window - (now % window)) + 1

def _redis_key(kind: str, ident: str, now: float, window: int) -> str:
    """Ключ окна: namespace + вид + номер окна + HMAC-SHA256 идентификатора.

    HMAC с серверным ключом (`CC_DATA_KEY`), а не голый хеш: пространство IP
    перебираемо за минуты, поэтому SHA-256(ip) в Redis — фактически список
    адресов клиентов. HMAC оставляет ключи фиксированной длины и стабильными
    в пределах инсталляции, но без ключа не восстанавливается (docs/02 §5).
    """
    secret = settings.data_key.encode("utf-8")
    digest = hmac.new(secret, ident.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{_REDIS_PREFIX}:{kind}:{_window_id(now, window)}:{digest}"

def _redis_keys(key: tuple[str, str], now: float, window: int) -> tuple[str, str]:
    return (
        _redis_key("pair", f"{key[0]}\x00{key[1]}", now, window),
        _redis_key("ip", key[0], now, window),
    )

def _redis_check(key: tuple[str, str], client=None) -> tuple[bool, int] | None:
    """Читает счётчики текущего окна. None — ошибка Redis (уже fail-open).

    ``client`` — для тестов «несколько процессов»: тот же код, другой клиент
    поверх общего фейкового хранилища.
    """
    client = client if client is not None else _client
    if client is None:
        return None
    now = time.time()  # wall clock: окна общие для всех процессов и реплик
    window = settings.rate_window
    pair_key, ip_key = _redis_keys(key, now, window)
    try:
        pipe = client.pipeline(transaction=False)
        pipe.get(pair_key)
        pipe.get(ip_key)
        pair_raw, ip_raw = pipe.execute()
    except Exception as exc:  # noqa: BLE001 — лимитер не имеет права вернуть 500
        _redis_failed(exc)
        return None
    if int(pair_raw or 0) >= settings.rate_max:
        return False, _retry_after(now, window)
    if int(ip_raw or 0) >= settings.rate_ip_max:
        return False, _retry_after(now, window)
    return True, 0

def _redis_record(key: tuple[str, str], client=None) -> bool:
    """Атомарно (INCR на ключ) увеличивает оба счётчика окна; TTL = 2 окна."""
    client = client if client is not None else _client
    if client is None:
        return False
    now = time.time()
    window = settings.rate_window
    ttl = window * 2
    try:
        # transaction=True → MULTI/EXEC: INCR и EXPIRE применяются вместе,
        # поэтому счётчик не может остаться в Redis без TTL (иначе окно
        # никогда не истечёт и ключ утечёт в память навсегда).
        pipe = client.pipeline(transaction=True)
        for k in _redis_keys(key, now, window):
            pipe.incr(k)
            pipe.expire(k, ttl)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001
        _redis_failed(exc)
        return False
    return True

# --- публичный контракт ------------------------------------------------------

def check_rate(key: tuple[str, str]) -> tuple[bool, int]:
    """Проверяет лимиты без записи попытки. Возвращает (разрешено, retry_after_seconds)."""
    if _resolve_backend() == "redis":
        verdict = _redis_check(key)
        if verdict is not None:
            return verdict
        # Redis умер в этом запросе — _redis_failed уже переключил на memory.
    return _memory_check(key)

def record_rate_hit(key: tuple[str, str]) -> None:
    """Фиксирует неудачную попытку входа в окна (пара и IP)."""
    if _resolve_backend() == "redis" and _redis_record(key):
        return
    # memory-режим или отказ Redis: локальный счётчик продолжает работать.
    _memory_record(key)

def reset_rate_limits() -> None:
    """Тестовый хук: чистит окна в активном хранилище и сбрасывает выбор бэкенда."""
    global _backend, _client, redis_errors, _redis_warned
    client = _client if _backend == "redis" else None
    if client is not None:
        try:
            keys = list(client.scan_iter(match=f"{_REDIS_PREFIX}:*"))
            if keys:
                client.delete(*keys)
        except Exception as exc:  # noqa: BLE001 — хук тестов, не бизнес-путь
            log.warning("Rate limiter: не удалось очистить Redis (%r)", exc)
    with _lock:
        _hits.clear()
        _ip_hits.clear()
    _backend = None
    _client = None
    redis_errors = 0
    _redis_warned = False
    _warned.clear()

async def acheck_rate(key: tuple[str, str]) -> tuple[bool, int]:
    """Async-обёртка над `check_rate` для роутов.

    Redis-режим делает сетевой ввод-вывод (до `CC_RATE_REDIS_TIMEOUT` на сокет):
    в event loop FastAPI его пускать нельзя, поэтому уходим в рабочий поток —
    ровно как для argon2 (`routers/auth.py`). Memory-режим остаётся быстрым
    и выполняется на месте.
    """
    if _resolve_backend() == "memory":
        return check_rate(key)
    return await anyio.to_thread.run_sync(check_rate, key)

async def arecord_rate_hit(key: tuple[str, str]) -> None:
    """Async-обёртка над `record_rate_hit`; см. `acheck_rate`."""
    if _resolve_backend() == "memory":
        record_rate_hit(key)
        return
    await anyio.to_thread.run_sync(record_rate_hit, key)

def backend_status() -> dict[str, object]:
    """Активное хранилище и счётчик отказов Redis — для /health (docs/03 §12).

    Счётчик нужен, потому что после первого сбоя Redis режим навсегда
    переключается на memory: без статуса оператор не узнает, что общий бюджет
    деградировал до per-process (единственный WARNING уходит в лог один раз).
    """
    return {"backend": _backend or "unresolved", "redis_errors": redis_errors}
