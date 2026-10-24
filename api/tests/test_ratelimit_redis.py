"""Rate limiter логина на общем Redis-счётчике (docs/03 §12, docs/10 R24).

Redis для тестов не нужен: ``_FakeServer`` моделирует общее хранилище (INCR
атомарен под локом), ``_FakeClient`` — процесс, а ``advance()`` — истечение
TTL. Проверяем поведение, а не библиотеку:

- бюджет (IP, email) и IP-бюджет общие для «двух процессов»;
- INCR не теряет попытки при параллельной записи;
- счётчики умирают по TTL;
- мёртвый Redis не вешает логин (fail-open в memory + WARNING + redis_errors);
- выбор бэкенда на старте (auto/memory/redis, нет пакета/нет URL);
- сброс тестового хука чистит именно активное хранилище.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import pytest

from app import ratelimit
from app.config import settings


# --- фейковый Redis ----------------------------------------------------------

class _FakeServer:
    """Общее хранилище: словарь + TTL по собственным часам."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict[str, int] = {}
        self.expires: dict[str, float] = {}
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds

    def _alive(self, key: str) -> bool:
        exp = self.expires.get(key)
        if exp is not None and self.now >= exp:
            self.data.pop(key, None)
            self.expires.pop(key, None)
            return False
        return key in self.data

    def incr(self, key: str) -> int:
        with self.lock:
            self._alive(key)
            self.data[key] = self.data.get(key, 0) + 1
            return self.data[key]

    def get(self, key: str) -> str | None:
        with self.lock:
            return str(self.data[key]) if self._alive(key) else None

    def expire(self, key: str, ttl: int) -> int:
        with self.lock:
            if key in self.data:
                self.expires[key] = self.now + ttl
                return 1
            return 0

    def delete(self, *keys: str) -> int:
        with self.lock:
            removed = 0
            for key in keys:
                removed += 1 if self.data.pop(key, None) is not None else 0
                self.expires.pop(key, None)
            return removed

    def count(self, key: str) -> int:
        with self.lock:
            return self.data.get(key, 0)


class _FakePipeline:
    def __init__(self, client: "_FakeClient") -> None:
        self._client = client
        self._cmds: list[tuple[str, tuple]] = []

    def get(self, key: str):
        self._cmds.append(("get", (key,)))
        return self

    def incr(self, key: str):
        self._cmds.append(("incr", (key,)))
        return self

    def expire(self, key: str, ttl: int):
        self._cmds.append(("expire", (key, ttl)))
        return self

    def execute(self):
        self._client._check()
        server = self._client.server
        return [getattr(server, name)(*args) for name, args in self._cmds]


class _FakeClient:
    """Один «процесс»: свой объект клиента поверх общего сервера."""

    def __init__(self, server: _FakeServer) -> None:
        self.server = server
        self.calls = 0
        self.fail_with: BaseException | None = None

    def _check(self) -> None:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        return _FakePipeline(self)

    def scan_iter(self, match: str):
        prefix = match.rstrip("*")
        return iter(sorted(k for k in list(self.server.data) if k.startswith(prefix)))

    def delete(self, *keys: str) -> int:
        return self.server.delete(*keys)


class _DeadClient:
    """Redis, который отвечает ошибкой (сокет уже истёк)."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0

    def pipeline(self, transaction: bool = False):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        raise ConnectionError("redis: connection refused")


@pytest.fixture
def clean():
    ratelimit.reset_rate_limits()
    yield
    ratelimit.reset_rate_limits()


@pytest.fixture
def redis_mode(monkeypatch, clean):
    server = _FakeServer()
    client = _FakeClient(server)
    monkeypatch.setattr(ratelimit, "_backend", "redis")
    monkeypatch.setattr(ratelimit, "_client", client)
    return server, client


# --- общий бюджет ------------------------------------------------------------

def test_pair_budget_shared_between_processes(redis_mode, monkeypatch):
    """rate_max суммарных неудач по двум процессам → блокировка на обоих."""
    server, first = redis_mode
    second = _FakeClient(server)
    key = ("10.20.0.1", "shared@test.ru")
    assert settings.rate_max >= 2

    monkeypatch.setattr(ratelimit, "_client", first)
    for _ in range(2):
        ratelimit.record_rate_hit(key)
    monkeypatch.setattr(ratelimit, "_client", second)
    for _ in range(settings.rate_max - 2):
        ratelimit.record_rate_hit(key)

    # Проверка «из второго процесса» видит общий счётчик: бюджет не удвоился.
    allowed, retry_after = ratelimit.check_rate(key)
    assert allowed is False
    assert retry_after >= 1


def test_ip_budget_shared_between_processes(redis_mode, monkeypatch):
    """Перебор разных email с одного IP упирается в общий IP-счётчик."""
    server, first = redis_mode
    second = _FakeClient(server)
    ip = "10.21.0.1"
    for i in range(settings.rate_ip_max):
        monkeypatch.setattr(ratelimit, "_client", first if i % 2 else second)
        ratelimit.record_rate_hit((ip, f"spray{i}@test.ru"))
    monkeypatch.setattr(ratelimit, "_client", second)
    allowed, _ = ratelimit.check_rate((ip, "fresh@test.ru"))
    assert allowed is False


def test_increments_are_atomic_across_processes(redis_mode):
    """INCR, а не read-modify-write: параллельные записи двух процессов не теряются."""
    server, first = redis_mode
    second = _FakeClient(server)
    key = ("10.22.0.1", "atomic@test.ru")
    hits_per_thread = 25
    threads_n = 8
    clients = [first, second]

    def worker(idx: int) -> None:
        client = clients[idx % 2]
        for _ in range(hits_per_thread):
            assert ratelimit._redis_record(key, client=client)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = threads_n * hits_per_thread
    pair_key, ip_key = next(
        k for k in server.data if k.startswith(f"{ratelimit._REDIS_PREFIX}:pair:")
    ), next(k for k in server.data if k.startswith(f"{ratelimit._REDIS_PREFIX}:ip:"))
    assert server.count(pair_key) == total
    assert server.count(ip_key) == total
    # TTL выставлен на каждом INCR: ключи не растут без срока жизни.
    assert server.expires[pair_key] == server.now + 2 * settings.rate_window


def test_window_expires_by_ttl(redis_mode):
    server, client = redis_mode
    key = ("10.23.0.1", "ttl@test.ru")
    for _ in range(settings.rate_max):
        ratelimit.record_rate_hit(key)
    assert ratelimit.check_rate(key)[0] is False

    server.advance(2 * settings.rate_window + 1)  # TTL = 2 окна
    assert ratelimit.check_rate(key) == (True, 0)
    assert server.data == {}


def test_check_does_not_create_keys(redis_mode):
    server, _ = redis_mode
    assert ratelimit.check_rate(("10.24.0.1", "probe@test.ru")) == (True, 0)
    assert server.data == {}


# --- отказ Redis -------------------------------------------------------------

def test_redis_failure_falls_back_to_memory_and_warns_once(monkeypatch, caplog):
    ratelimit.reset_rate_limits()
    dead = _DeadClient()
    monkeypatch.setattr(ratelimit, "_backend", "redis")
    monkeypatch.setattr(ratelimit, "_client", dead)
    key = ("10.25.0.1", "dead@test.ru")

    with caplog.at_level(logging.WARNING, logger="app.ratelimit"):
        started = time.monotonic()
        assert ratelimit.check_rate(key) == (True, 0)  # fail-open
        elapsed = time.monotonic() - started

    assert elapsed < settings.rate_redis_timeout + 0.2
    assert ratelimit._backend == "memory"
    assert ratelimit.redis_errors == 1
    assert sum("ошибка Redis" in rec.message for rec in caplog.records) == 1

    # Дальше Redis не дёргаем: локальный лимит продолжает считать и блокирует.
    for _ in range(settings.rate_max):
        ratelimit.record_rate_hit(key)
    assert ratelimit.check_rate(key)[0] is False
    assert dead.calls == 1


def test_dead_redis_does_not_delay_login_beyond_timeout(monkeypatch):
    """Клиент ограничен socket_timeout: исчерпание не превращается в ожидание."""
    ratelimit.reset_rate_limits()
    socket_timeout = 0.05
    monkeypatch.setattr(settings, "rate_redis_timeout", socket_timeout)
    dead = _DeadClient(delay=socket_timeout)
    monkeypatch.setattr(ratelimit, "_backend", "redis")
    monkeypatch.setattr(ratelimit, "_client", dead)

    started = time.monotonic()
    assert ratelimit.check_rate(("10.26.0.1", "slow@test.ru")) == (True, 0)
    elapsed = time.monotonic() - started
    assert elapsed < socket_timeout + 0.2


# --- выбор хранилища на старте ----------------------------------------------

def test_auto_without_url_is_memory(monkeypatch):
    monkeypatch.setattr(settings, "rate_backend", "auto")
    monkeypatch.setattr(settings, "redis_url", "")
    assert ratelimit.init_backend() == "memory"
    assert ratelimit._client is None


def test_explicit_redis_without_url_is_memory(monkeypatch, caplog):
    monkeypatch.setattr(settings, "rate_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", "")
    with caplog.at_level(logging.WARNING, logger="app.ratelimit"):
        assert ratelimit.init_backend() == "memory"
    assert any("без CC_REDIS_URL" in rec.message for rec in caplog.records)


def test_auto_uses_redis_when_ping_succeeds(monkeypatch):
    server = _FakeServer()
    client = _FakeClient(server)
    monkeypatch.setattr(settings, "rate_backend", "auto")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(ratelimit, "_connect", lambda: client)
    assert ratelimit.init_backend() == "redis"
    assert ratelimit._client is client


def test_unreachable_redis_at_startup_degrades_with_warning(monkeypatch, caplog):
    pytest.importorskip("redis")
    monkeypatch.setattr(settings, "rate_backend", "auto")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "rate_redis_timeout", 0.1)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.ratelimit"):
        assert ratelimit.init_backend() == "memory"
    assert time.monotonic() - started < 1.0
    assert any("Redis недоступен" in rec.message for rec in caplog.records)
    assert ratelimit._client is None


def test_missing_redis_package_degrades(monkeypatch, caplog):
    """Без extra api[redis] модуль обязан работать (ImportError → memory)."""
    monkeypatch.setattr(settings, "rate_backend", "redis")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6379/0")
    monkeypatch.setitem(sys.modules, "redis", None)  # `from redis import ...` → ImportError
    with caplog.at_level(logging.WARNING, logger="app.ratelimit"):
        assert ratelimit.init_backend() == "memory"
    assert any("пакет redis не установлен" in rec.message for rec in caplog.records)


def test_connect_passes_timeout_and_disables_retries(monkeypatch):
    """Мёртвый Redis не должен умножать socket_timeout на ретраи redis-py."""
    redis = pytest.importorskip("redis")
    captured: dict = {}

    class _Pinging:
        def ping(self) -> bool:
            return True

    def fake_from_url(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Pinging()

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(fake_from_url))
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(settings, "rate_redis_timeout", 0.25)
    assert ratelimit._connect() is not None
    assert captured["socket_timeout"] == 0.25
    assert captured["socket_connect_timeout"] == 0.25
    retry = captured["retry"]
    retries = retry.get_retries() if hasattr(retry, "get_retries") else retry._retries
    assert retries == 0  # redis-py 6+ по умолчанию ретраит connection errors


# --- тестовый сброс ----------------------------------------------------------

def test_reset_clears_redis_store(redis_mode):
    server, _ = redis_mode
    for _ in range(settings.rate_max):
        ratelimit.record_rate_hit(("10.27.0.1", "reset@test.ru"))
    assert server.data

    ratelimit.reset_rate_limits()
    assert server.data == {}
    assert ratelimit._client is None and ratelimit._backend is None
    # После сброса выбор хранилища разрешается заново (в тестах без URL — memory).
    assert ratelimit.check_rate(("10.27.0.1", "reset@test.ru")) == (True, 0)


def test_reset_clears_memory_store(monkeypatch):
    monkeypatch.setattr(settings, "rate_backend", "memory")
    key = ("10.28.0.1", "memreset@test.ru")
    for _ in range(settings.rate_max):
        ratelimit.record_rate_hit(key)
    assert ratelimit._hits
    ratelimit.reset_rate_limits()
    assert ratelimit._hits == {} and ratelimit._ip_hits == {}
    assert ratelimit.check_rate(key) == (True, 0)
