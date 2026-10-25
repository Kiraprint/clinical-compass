"""Safety-gate нагрузочного стенда: имена целей и запрет разрушающих операций.

Этот файл живёт вне `api/tests`, потому что проверяет только `loadtest/` и не
требует Postgres/Qdrant: DDL и пересоздание коллекции — самая опасная часть
стенда, и регрессия здесь означает «стенд снёс не ту базу».

Запуск из корня репо:

    PYTHONPATH=api:core python -m pytest loadtest/test_safety.py -q
"""

from __future__ import annotations

import pytest

from loadtest import common, provision


@pytest.mark.parametrize(
    "value",
    [
        "compass",  # прод-БД
        "compass_dev",
        'compass_load"; DROP DATABASE compass; --',  # инъекция в DDL
        "compass_loadtest-2",  # дефис не проходит charset
        "compass_load test",
        "Compass_loadtest",  # верхний регистр
    ],
)
def test_db_names_outside_allowlist_are_refused(value, monkeypatch):
    monkeypatch.setenv("CC_LOADTEST_ALLOW", "1")
    with pytest.raises(common.SafetyRefusal):
        common.assert_destructive(db=value, collection="kb_loadtest")


@pytest.mark.parametrize("value", ["kb", "kb_test", "kb_load-collection", "kb_load/../kb"])
def test_collection_names_outside_allowlist_are_refused(value, monkeypatch):
    monkeypatch.setenv("CC_LOADTEST_ALLOW", "1")
    with pytest.raises(common.SafetyRefusal):
        common.assert_destructive(db="compass_loadtest", collection=value)


def test_destructive_ops_require_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("CC_LOADTEST_ALLOW", raising=False)
    with pytest.raises(common.SafetyRefusal) as exc:
        common.assert_destructive(db="compass_loadtest", collection="kb_loadtest")
    assert "CC_LOADTEST_ALLOW" in str(exc.value)

    monkeypatch.setenv("CC_LOADTEST_ALLOW", "1")
    assert common.assert_destructive(
        db="compass_loadtest", collection="kb_loadtest"
    ) == ("compass_loadtest", "kb_loadtest")


def test_seed_qdrant_validates_its_argument_as_a_collection(monkeypatch):
    """Регресс: коллекция уходила в параметр db и provision падал на любом имени.

    Проверяется до импорта qdrant_client, поэтому окружение не нужно.
    """
    monkeypatch.setenv("CC_LOADTEST_ALLOW", "1")
    with pytest.raises(common.SafetyRefusal) as exc:
        provision.seed_qdrant("compass_loadtest")
    assert "Коллекц" in str(exc.value)


def test_database_url_override_is_allowlisted(monkeypatch):
    """Полный URL не обходит allowlist: имя БД из URL проверяется."""
    monkeypatch.setenv("CC_LOADTEST_ALLOW", "1")
    monkeypatch.delenv("CC_LOADTEST_DB", raising=False)
    monkeypatch.delenv("CC_LOADTEST_COLLECTION", raising=False)
    monkeypatch.setenv(
        "CC_LOADTEST_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/compass"
    )
    with pytest.raises(common.SafetyRefusal):
        common.assert_destructive()

    monkeypatch.setenv(
        "CC_LOADTEST_DATABASE_URL",
        "postgresql+psycopg://u:p@127.0.0.1:5432/compass_load_mw",
    )
    assert common.assert_destructive() == ("compass_load_mw", "kb_loadtest")
    assert common.db_url().endswith("/compass_load_mw")
    assert ":p@" not in common.redacted_db_url()
    assert "dbname=postgres" in common.pg_dsn()
    assert "dbname=compass_load_mw" in common.pg_dsn("compass_load_mw")


@pytest.mark.parametrize(
    "workers,pool,overflow,demand",
    [(1, 20, 20, 42), (2, 20, 20, 84), (4, 8, 8, 72)],
)
def test_pool_budget_matches_app_formula(workers, pool, overflow, demand):
    """Бюджет стенда = N × (pool + overflow + 2), как в db.validate_pool_budget."""
    budget = common.pool_budget(workers, pool, overflow, max_connections=100)
    assert budget["demand"] == demand
    assert budget["fits"] is True
    tight = common.pool_budget(workers, pool, overflow, max_connections=demand - 1)
    assert tight["fits"] is False
