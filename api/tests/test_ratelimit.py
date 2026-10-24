"""Rate limiter логина: проверка не создаёт записи, словарь имеет жёсткий потолок.

Регресс: `defaultdict`-индексация в `check_rate` заводила запись на каждую пару
(IP, email) ещё до неудачной попытки, а `record_rate_hit` не убирал пустые
окна — перебор email растил память процесса без ограничений.
"""

from __future__ import annotations

import pytest

from app import ratelimit
from app.config import settings


@pytest.fixture(autouse=True)
def _clean():
    ratelimit.reset_rate_limits()
    yield
    ratelimit.reset_rate_limits()


def test_check_rate_does_not_create_entries():
    assert ratelimit.check_rate(("10.1.1.1", "probe@test.ru")) == (True, 0)
    assert ratelimit._hits == {}


def test_recorded_hits_are_bounded_by_max_keys():
    for i in range(ratelimit._MAX_KEYS + 50):
        ratelimit.record_rate_hit((f"10.9.{(i // 250) % 250}.{i % 250}", f"u{i}@test.ru"))
    assert len(ratelimit._hits) <= ratelimit._MAX_KEYS


def test_limit_still_blocks_after_hits():
    key = ("10.2.2.2", "doc@test.ru")
    for _ in range(settings.rate_max):
        ratelimit.record_rate_hit(key)
    allowed, retry_after = ratelimit.check_rate(key)
    assert allowed is False
    assert retry_after >= 1


def test_ip_budget_blocks_spray_of_distinct_emails():
    """Перебор разных email с одного IP упирается в IP-окно, а не только в пару."""
    for i in range(settings.rate_ip_max):
        ratelimit.record_rate_hit(("10.3.3.3", f"spray{i}@test.ru"))
    allowed, retry_after = ratelimit.check_rate(("10.3.3.3", "new@test.ru"))
    assert allowed is False
    assert retry_after >= 1
    # другой IP не страдает
    assert ratelimit.check_rate(("10.4.4.4", "new@test.ru")) == (True, 0)
