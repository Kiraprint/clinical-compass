"""FR-25: синхронизация — идемпотентность, savepoint'ы, связка по СНИЛС."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.emias import mapping, sync as emias_sync
from app.emias.config import EmiasConfig
from app.errors import Problem
from app.models import EmiasLink, PatientMedication, User, Visit

from .conftest import patient_ctx

PEPPER = "unit-test-pepper"
SNILS = "112-233-445 95"

def _patient_resource(patient_id: str = "emias-1001", fio=("Иванова", ["Мария", "Петровна"]), snils: str | None = SNILS) -> dict:
    identifiers = [{"system": mapping.SNILS_SYSTEM, "type": {"text": "SNILS"}, "value": snils}] if snils else []
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": identifiers,
        "name": [{"family": fio[0], "given": fio[1]}],
    }

def _encounter(enc_id: str = "enc-1", start: str = "2026-01-19T14:10:00+03:00", reason: str = "Приём") -> dict:
    return {
        "resourceType": "Encounter",
        "id": enc_id,
        "status": "finished",
        "class": {"code": "AMB"},
        "period": {"start": start, "end": "2026-01-19T14:40:00+03:00"},
        "serviceProvider": {"display": "Поликлиника № 1"},
        "reasonCode": [{"text": reason}],
        "participant": [{"individual": {"display": "Терапевт"}}],
    }

def _medication(med_id: str = "med-1", title: str = "Сертралин", status: str = "active") -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": med_id,
        "status": status,
        "medicationCodeableConcept": {"text": title},
        "dosageInstruction": [{"text": "50 мг вечером"}],
        "authoredOn": "2026-01-19",
    }

class FakeClient:
    """Тот же контракт, что EmiasClient, но без сети."""

    def __init__(self, patients, encounters=None, medications=None):
        self.cfg = EmiasConfig(
            enabled=True,
            base_url="http://127.0.0.1:8090",
            client_id="compass",
            client_secret="secret",
            allow_insecure=True,
            snils_pepper=PEPPER,
        )
        self._patients = patients
        self._encounters = encounters or {}
        self._medications = medications or {}

    def list_patients(self, **_kwargs):
        return list(self._patients)

    def search_patients(self, snils: str):
        normalized = mapping.normalize_snils(snils)
        return [p for p in self._patients
                if mapping.normalize_snils(mapping.extract_snils(p) or "") == normalized]

    def get_patient(self, patient_id: str):
        for patient in self._patients:
            if patient["id"] == patient_id:
                return patient
        raise AssertionError("нет такого пациента в фикстуре")

    def encounters(self, patient_id: str, **_kwargs):
        return list(self._encounters.get(patient_id, []))

    def medications(self, patient_id: str):
        return list(self._medications.get(patient_id, []))

def _local_patient(client, db_session, email: str, fio: str) -> str:
    _headers, user_id = patient_ctx(client, email)
    user = db_session.get(User, uuid.UUID(user_id))
    user.fio = fio
    db_session.commit()
    return user_id

def _client_data(**overrides):
    return FakeClient(
        patients=overrides.get("patients", [_patient_resource()]),
        encounters=overrides.get("encounters", {"emias-1001": [_encounter(), _encounter("enc-2", reason="Повтор")]}),
        medications=overrides.get("medications", {"emias-1001": [_medication(), _medication("med-2", "Гидроксизин")]}),
    )

def test_sync_patients_creates_then_is_idempotent(client, db_session):
    user_id = _local_patient(client, db_session, "p-emias-1@test.ru", "Иванова Мария Петровна")
    fake = _client_data()

    first = emias_sync.sync_patients(db_session, client=fake)
    assert first.fetched == 1
    assert first.errors == 0
    assert first.created == 5  # link + 2 приёма + 2 назначения
    assert first.status() == "ok"

    second = emias_sync.sync_patients(db_session, client=fake)
    assert second.fetched == 1
    assert second.created == 0
    assert second.errors == 0
    assert second.skipped >= 5

    assert db_session.scalar(select(EmiasLink).where(EmiasLink.patient_id == uuid.UUID(user_id)))
    assert db_session.scalar(
        select(Visit).where(Visit.patient_id == uuid.UUID(user_id), Visit.emias_id == "enc-1")
    ) is not None
    assert len(db_session.scalars(select(PatientMedication).where(
        PatientMedication.patient_id == uuid.UUID(user_id))).all()) == 2

def test_snils_stored_only_as_salted_hash(client, db_session):
    user_id = _local_patient(client, db_session, "p-emias-2@test.ru", "Иванова Мария Петровна")
    emias_sync.sync_patients(db_session, client=_client_data())

    link = db_session.scalar(select(EmiasLink).where(EmiasLink.patient_id == uuid.UUID(user_id)))
    assert link.snils_hash == mapping.snils_hash(SNILS, PEPPER)
    assert "11223344595" not in link.snils_hash
    # в БД не остаётся самого СНИЛС
    raw = db_session.execute(
        select(EmiasLink.snils_hash).where(EmiasLink.patient_id == uuid.UUID(user_id))
    ).scalar_one()
    assert raw == mapping.snils_hash("11223344595", PEPPER)

def test_match_by_snils_wins_over_fio(client, db_session):
    user_id = _local_patient(client, db_session, "p-emias-3@test.ru", "Совсем Другое Имя")
    db_session.add(EmiasLink(
        patient_id=uuid.UUID(user_id),
        emias_patient_id="emias-1001",
        snils_hash=mapping.snils_hash(SNILS, PEPPER),
    ))
    db_session.commit()

    stats = emias_sync.sync_patients(db_session, client=_client_data())
    assert stats.errors == 0
    assert stats.created == 4  # 2 приёма + 2 назначения (связка уже была)
    link = db_session.scalar(select(EmiasLink).where(EmiasLink.patient_id == uuid.UUID(user_id)))
    assert link.emias_patient_id == "emias-1001"

def test_unmatched_patient_is_skipped(client, db_session):
    _local_patient(client, db_session, "p-emias-4@test.ru", "Петров Алексей Игоревич")
    stats = emias_sync.sync_patients(db_session, client=_client_data())
    assert stats.skipped == 1
    assert stats.created == 0
    assert db_session.scalar(select(EmiasLink)) is None

def test_savepoint_isolation_keeps_good_entities(client, db_session):
    user_id = _local_patient(client, db_session, "p-emias-5@test.ru", "Иванова Мария Петровна")
    broken = {"resourceType": "Encounter", "id": "enc-bad", "class": {"code": "AMB"},
              "period": {}, "reasonCode": [{"text": "без даты"}]}
    fake = FakeClient(
        patients=[_patient_resource()],
        encounters={"emias-1001": [broken, _encounter()]},
        medications={"emias-1001": [_medication()]},
    )

    stats = emias_sync.sync_patients(db_session, client=fake)
    assert stats.errors == 1
    assert stats.created == 3  # link + 1 приём + 1 назначение
    assert stats.status() == "partial"
    assert db_session.scalar(select(Visit).where(Visit.emias_id == "enc-1")) is not None
    assert db_session.scalar(select(Visit).where(Visit.emias_id == "enc-bad")) is None

def test_duplicate_emias_id_updates_instead_of_creating(client, db_session):
    _local_patient(client, db_session, "p-emias-6@test.ru", "Иванова Мария Петровна")
    duplicate = _encounter(reason="Другая причина")
    fake = FakeClient(
        patients=[_patient_resource()],
        encounters={"emias-1001": [_encounter(), duplicate]},
        medications={"emias-1001": []},
    )

    stats = emias_sync.sync_patients(db_session, client=fake)
    assert stats.created == 2  # link + один приём
    assert stats.errors == 0
    visits = db_session.scalars(select(Visit).where(Visit.emias_id == "enc-1")).all()
    assert len(visits) == 1
    assert "Другая причина" in visits[0].notes

def test_sync_patient_requires_link(client, db_session):
    _headers, user_id = patient_ctx(client, "p-emias-7@test.ru")
    with pytest.raises(Problem) as exc:
        emias_sync.sync_patient(db_session, uuid.UUID(user_id), client=_client_data())
    assert exc.value.code == "EMIAS_NOT_LINKED"

def test_sync_patient_refreshes_history(client, db_session):
    user_id = _local_patient(client, db_session, "p-emias-8@test.ru", "Иванова Мария Петровна")
    emias_sync.sync_patients(db_session, client=_client_data())

    fake = FakeClient(
        patients=[_patient_resource()],
        encounters={"emias-1001": [_encounter(reason="Обновлённая причина")]},
        medications={"emias-1001": [_medication(title="Сертралин 100 мг")]},
    )
    stats = emias_sync.sync_patient(db_session, uuid.UUID(user_id), client=fake)
    assert stats.mode == "one"
    assert stats.errors == 0
    visit = db_session.scalar(select(Visit).where(Visit.emias_id == "enc-1"))
    assert "Обновлённая причина" in visit.notes
    med = db_session.scalar(select(PatientMedication).where(PatientMedication.emias_id == "med-1"))
    assert med.title == "Сертралин 100 мг"

def test_link_patient_by_snils_and_summary(client, db_session):
    _headers, user_id = patient_ctx(client, "p-emias-9@test.ru")
    result = emias_sync.link_patient(db_session, uuid.UUID(user_id), SNILS, client=_client_data())
    assert result["emias_patient_id"] == "emias-1001"

    summary = emias_sync.patient_summary(db_session, uuid.UUID(user_id))
    assert summary["linked"] is True
    assert summary["encounters"] == 0  # история ещё не синхронизирована
    assert summary["medications"] == []

    emias_sync.sync_patient(db_session, uuid.UUID(user_id), client=_client_data())
    summary = emias_sync.patient_summary(db_session, uuid.UUID(user_id))
    assert summary["encounters"] == 2
    assert len(summary["medications"]) == 2
    assert summary["last_sync"] is not None

def test_consent_revocation_destroys_emias_pdn(client, db_session):
    """F2/P1: отзыв согласия уничтожает производные ЕМИАС (ПДн спецкатегории)."""
    headers, user_id = patient_ctx(client, "p-emias-revoke@test.ru")
    pid = uuid.UUID(user_id)
    user = db_session.get(User, pid)
    user.fio = "Иванова Мария Петровна"
    db_session.commit()

    emias_sync.sync_patients(db_session, client=_client_data())
    assert db_session.scalar(select(EmiasLink).where(EmiasLink.patient_id == pid)) is not None
    assert db_session.scalars(select(PatientMedication).where(PatientMedication.patient_id == pid)).all()
    assert db_session.scalars(
        select(Visit).where(Visit.patient_id == pid, Visit.source == "emias")
    ).all()

    assert client.post("/api/v1/consent/revoke", headers=headers).status_code == 204
    db_session.expire_all()

    assert db_session.scalar(select(EmiasLink).where(EmiasLink.patient_id == pid)) is None
    assert db_session.scalars(select(PatientMedication).where(PatientMedication.patient_id == pid)).all() == []
    assert db_session.scalars(
        select(Visit).where(Visit.patient_id == pid, Visit.source == "emias")
    ).all() == []

def test_link_patient_rejects_foreign_snils(client, db_session):
    """P3: связка проверяет СНИЛС в ответе — чужой пациент не привязывается."""
    _headers, user_id = patient_ctx(client, "p-emias-mismatch@test.ru")
    other = _patient_resource(snils="112-233-446 96")
    fake = _client_data()
    fake.search_patients = lambda snils: [other]  # сервер игнорирует фильтр
    with pytest.raises(Problem) as exc:
        emias_sync.link_patient(db_session, uuid.UUID(user_id), SNILS, client=fake)
    assert exc.value.code == "EMIAS_SNILS_MISMATCH"
    assert db_session.scalar(select(EmiasLink).where(EmiasLink.patient_id == uuid.UUID(user_id))) is None
