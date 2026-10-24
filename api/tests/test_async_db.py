"""Async-БД: атомарность мульти-записи и отсутствие утечки соединений в пул.

Контракт docs/06 §3: сбой посреди доменной операции не оставляет частичных строк;
после ответа клиенту (в т.ч. при 200 одновременных запросах) в pg_stat_activity
не остаётся соединений `idle in transaction`.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import db
from app.config import Settings
from app.models import Assessment, AuditLog, ClinicianRoster, Escalation, Notification, Run

from .conftest import TEST_URL, admin_ctx, clinician_ctx, patient_ctx, register

# total 27 → SEVERE → RED: финиш обязан создать Assessment + Escalation +
# Notification ростерным врачам в одной транзакции.
PHQ_SEVERE = {f"q{i}": 3 for i in range(1, 10)}


def _counts() -> dict[str, int]:
    with db.SyncSessionLocal() as s:
        return {
            "assessments": s.scalar(select(func.count()).select_from(Assessment)),
            "escalations": s.scalar(select(func.count()).select_from(Escalation)),
            "notifications": s.scalar(select(func.count()).select_from(Notification)),
            "audit": s.scalar(select(func.count()).select_from(AuditLog)),
        }


def test_finish_rolls_back_whole_unit(client, monkeypatch):
    """Сбой аудита на финише скрининга → ни Assessment, ни Escalation, ни Notification."""
    h, _ = patient_ctx(client, "atomic@test.ru")
    run_id = client.post("/api/v1/screenings", json={}, headers=h).json()["run_id"]
    assert client.post(
        f"/api/v1/runs/{run_id}/answers", json={"answers": PHQ_SEVERE}, headers=h
    ).status_code == 204

    from app.services import screening as screening_service

    async def boom(*_args, **_kwargs):
        raise RuntimeError("injected failure inside finish transaction")

    # audit_record вызывается последним — после Assessment, Escalation и Notification.
    monkeypatch.setattr(screening_service, "audit_record", boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        client.post(f"/api/v1/runs/{run_id}/finish", headers=h)

    assert _counts() == {"assessments": 0, "escalations": 0, "notifications": 0, "audit": 0}
    # Прогон остался открытым: транзакция откатилась целиком, а не наполовину.
    with db.SyncSessionLocal() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.finished_at is None


def test_audit_and_business_write_are_atomic(client, monkeypatch):
    """Сбой аудита при выдаче roster → строки clinician_roster не остаётся."""
    admin_h, _ = admin_ctx(client, "atomic-admin@test.ru")
    patient = register(client, "atomic-patient@test.ru")
    clinician_h, clinician_id = clinician_ctx(client, "atomic-doc@test.ru")
    assert clinician_h  # клиницист зарегистрирован

    from app.services import audit as audit_service

    async def boom(*_args, **_kwargs):
        raise RuntimeError("injected failure inside audit append")

    monkeypatch.setattr(audit_service, "record", boom)
    with pytest.raises(RuntimeError, match="injected failure"):
        client.post(
            "/api/v1/admin/roster",
            json={"patient_id": patient["id"], "clinician_id": clinician_id},
            headers=admin_h,
        )

    with db.SyncSessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ClinicianRoster)) == 0


def test_no_idle_in_transaction_after_concurrent_requests(client):
    """200 параллельных запросов через пул → 0 сессий `idle in transaction`."""
    h, _ = patient_ctx(client, "leak@test.ru")
    token = h["Authorization"]  # уже "Bearer <jwt>"
    app = client.app

    async def burst() -> list[int]:
        # Свой пул (5+5) внутри loop: тестовый engine — NullPool, а нам нужно
        # проверить именно возврат соединений в пул после каждого ответа.
        engine = create_async_engine(TEST_URL, pool_size=5, max_overflow=5, pool_timeout=10)
        maker = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        original = db.AsyncSessionLocal
        db.AsyncSessionLocal = maker
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                async def one(_index: int) -> int:
                    resp = await ac.get(
                        "/api/v1/diary", headers={"Authorization": token}
                    )
                    return resp.status_code

                return await asyncio.gather(*[one(i) for i in range(200)])
        finally:
            db.AsyncSessionLocal = original
            await engine.dispose()

    codes = asyncio.run(burst())
    assert set(codes) == {200}, set(codes)

    with db.sync_engine.connect() as conn:
        idle_tx = conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database() AND state = 'idle in transaction'"
        )).scalar_one()
    assert idle_tx == 0


def test_pool_budget_counts_api_workers_and_background_services():
    """Бюджет считает и воркеры API, и фоновые сервисы (+1 служебное соединение).

    Регресс: раньше формула учитывала только `workers × (pool+overflow)`, хотя
    reminders-worker и kb-enrichment держат такие же пулы — проверка проходила,
    а Postgres потом отбивал соединения.
    """
    settings = Settings()
    per_worker = settings.db_pool_size + settings.db_max_overflow + 1
    per_bg = settings.db_bg_pool_size + settings.db_bg_max_overflow + 1
    max_workers = max(1, (settings.db_max_connections - per_bg) // per_worker)

    db.validate_pool_budget(max_workers, background=1)  # впритык — допустимо
    with pytest.raises(RuntimeError, match="Пул БД не влезает"):
        db.validate_pool_budget(max_workers + 1, background=1)
    with pytest.raises(RuntimeError, match="Пул БД не влезает"):
        # фоновые процессы входят в тот же бюджет
        db.validate_pool_budget(1, background=settings.db_max_connections)


def test_background_profile_has_small_pool_and_sync_engine_is_nullpool():
    """Фоновый профиль не занимает полный пул API; sync-движок не держит соединений."""
    from sqlalchemy.pool import NullPool

    assert db.pool_capacity("background") < db.pool_capacity()
    assert isinstance(db.sync_engine.pool, NullPool)
