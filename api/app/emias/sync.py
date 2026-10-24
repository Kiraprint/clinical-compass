"""Синхронизация с ЕМИАС (FR-25): идемпотентный upsert + журнал прогонов.

Свойства, на которые опираются тесты и docs/06 §3:
  * повторный прогон не создаёт дублей: patient_medications.emias_id unique,
    visits.emias_id unique (в т.ч. для импортированных приёмов), emias_links.patient_id unique;
  * каждая сущность пишется в собственном SAVEPOINT — сбой одной не откатывает
    остальные и не оставляет «полусостояние»;
  * СНИЛС не хранится: только sha256(СНИЛС + перец) в emias_links.snils_hash;
  * в stats/логи не попадают тела ответов, ФИО, телефоны и токены.

Режимы:
  patients — обойти всех пациентов контура ЕМИАС, сопоставить с локальными и
             обновить связи/историю (patients без локального соответствия — skipped);
  one      — обновить одного пациента по patient_id (требует emias_links).
"""

from __future__ import annotations

import logging
import uuid

import anyio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Problem
from app.models import EmiasLink, EmiasSyncRun, PatientMedication, User, Visit

from . import mapping
from .client import EmiasClient, EmiasError

log = logging.getLogger(__name__)

@dataclass
class SyncStats:
    mode: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    by_entity: dict = field(default_factory=dict)
    details: list[str] = field(default_factory=list)

    def bump(self, entity: str, action: str) -> None:
        """action: created|updated|skipped|error. Только коды, без ПДн."""
        bucket = self.by_entity.setdefault(entity, {"created": 0, "updated": 0, "skipped": 0, "errors": 0})
        if action == "error":
            self.errors += 1
            bucket["errors"] += 1
        else:
            setattr(self, action, getattr(self, action) + 1)
            bucket[action] += 1
        self.details.append(f"{entity}:{action}")

    def status(self) -> str:
        if self.errors and (self.created or self.updated):
            return "partial"
        if self.errors:
            return "failed"
        return "ok"

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "by_entity": self.by_entity,
            "status": self.status(),
        }

# --- вспомогательное ---------------------------------------------------------

def _pepper(cfg) -> str:
    return cfg.snils_pepper

def _snils_hash_of(fhir_patient: dict, cfg) -> str | None:
    raw = mapping.extract_snils(fhir_patient)
    if not raw:
        return None
    try:
        return mapping.snils_hash(raw, _pepper(cfg))
    except ValueError:
        return None  # некорректный формат — ресурс не сопоставляем по СНИЛС

async def _upsert_link(db: AsyncSession, patient: User, fhir_patient: dict, cfg, stats: SyncStats) -> EmiasLink:
    emias_id = str(fhir_patient.get("id") or "")
    if not emias_id:
        raise ValueError("Patient без id")
    shash = _snils_hash_of(fhir_patient, cfg)
    link = await db.scalar(select(EmiasLink).where(EmiasLink.patient_id == patient.id))
    now = datetime.now(timezone.utc)
    if link is None:
        link = EmiasLink(
            patient_id=patient.id,
            emias_patient_id=emias_id,
            snils_hash=shash or "",
            synced_at=now,
        )
        db.add(link)
        stats.bump("link", "created")
    else:
        changed = link.emias_patient_id != emias_id or (shash is not None and link.snils_hash != shash)
        link.emias_patient_id = emias_id
        if shash is not None:
            link.snils_hash = shash
        link.synced_at = now
        stats.bump("link", "updated" if changed else "skipped")
    return link

async def _sync_encounters(db: AsyncSession, patient: User, emias_patient_id: str, client, stats: SyncStats) -> None:
    encs = await anyio.to_thread.run_sync(client.encounters, emias_patient_id)
    for encounter in encs:
        try:
            async with db.begin_nested():
                fields = mapping.encounter_visit_fields(encounter)
                visit = await db.scalar(select(Visit).where(Visit.emias_id == fields["emias_id"]))
                if visit is None:
                    db.add(Visit(patient_id=patient.id, clinician_id=None, **fields))
                    await db.flush()
                    stats.bump("encounter", "created")
                    continue
                changed = False
                for key, value in fields.items():
                    if key == "emias_id":
                        continue
                    if getattr(visit, key) != value:
                        setattr(visit, key, value)
                        changed = True
                await db.flush()
                stats.bump("encounter", "updated" if changed else "skipped")
        except EmiasError:
            raise
        except Exception as exc:  # SAVEPOINT откатился, прогон продолжается
            stats.bump("encounter", "error")
            log.warning("emias: encounter не сохранён: %s", type(exc).__name__)

async def _sync_medications(db: AsyncSession, patient: User, emias_patient_id: str, client, stats: SyncStats) -> None:
    meds = await anyio.to_thread.run_sync(client.medications, emias_patient_id)
    for request in meds:
        try:
            async with db.begin_nested():
                fields = mapping.medication_fields(request)
                med = await db.scalar(select(PatientMedication).where(PatientMedication.emias_id == fields["emias_id"]))
                if med is None:
                    db.add(PatientMedication(patient_id=patient.id, **fields))
                    await db.flush()
                    stats.bump("medication", "created")
                    continue
                changed = False
                for key, value in fields.items():
                    if key == "emias_id":
                        continue
                    if getattr(med, key) != value:
                        setattr(med, key, value)
                        changed = True
                await db.flush()
                stats.bump("medication", "updated" if changed else "skipped")
        except EmiasError:
            raise
        except Exception as exc:
            stats.bump("medication", "error")
            log.warning("emias: medication не сохранено: %s", type(exc).__name__)

async def _sync_one(db: AsyncSession, patient: User, fhir_patient: dict, client, stats: SyncStats) -> None:
    link = await _upsert_link(db, patient, fhir_patient, client.cfg, stats)
    await _sync_encounters(db, patient, link.emias_patient_id, client, stats)
    await _sync_medications(db, patient, link.emias_patient_id, client, stats)

# --- публичные операции ------------------------------------------------------

async def sync_patients(db: AsyncSession, *, client: EmiasClient | None = None) -> SyncStats:
    """Полный обход контура ЕМИАС. Сопоставление — mapping.find_local_patient."""
    client = client or EmiasClient()
    stats = SyncStats(mode="patients")
    resources = await anyio.to_thread.run_sync(client.list_patients)
    stats.fetched = len(resources)
    for fhir_patient in resources:
        try:
            async with db.begin_nested():
                local, via = await mapping.find_local_patient(
                    db,
                    snils_hash_value=_snils_hash_of(fhir_patient, client.cfg),
                    fio=mapping.patient_display_name(fhir_patient),
                )
                if local is None:
                    stats.bump("patient", "skipped")
                    continue
                await _sync_one(db, local, fhir_patient, client, stats)
                stats.details.append(f"patient:matched_by:{via or 'unknown'}")
        except EmiasError:
            raise
        except Exception as exc:
            stats.bump("patient", "error")
            log.warning("emias: пациент не синхронизирован: %s", type(exc).__name__)
    await db.commit()
    return stats

async def sync_patient(db: AsyncSession, patient_id, *, client: EmiasClient | None = None) -> SyncStats:
    """Обновление одного пациента. Без связи — Problem 404 EMIAS_NOT_LINKED."""
    client = client or EmiasClient()
    link = await db.scalar(select(EmiasLink).where(EmiasLink.patient_id == patient_id))
    if link is None:
        raise Problem(404, "EMIAS_NOT_LINKED", "Пациент не связан с ЕМИАС")
    patient = await db.get(User, patient_id)
    if patient is None:
        raise Problem(404, "NOT_FOUND", "Пациент не найден")
    stats = SyncStats(mode="one")
    fhir_patient = await anyio.to_thread.run_sync(client.get_patient, link.emias_patient_id)
    stats.fetched = 1
    await _sync_one(db, patient, fhir_patient, client, stats)
    await db.commit()
    return stats

async def link_patient(db: AsyncSession, patient_id, snils: str, *, client: EmiasClient | None = None) -> dict:
    """Связать локального пациента с ЕМИАС по СНИЛС (поиск Patient по identifier)."""
    client = client or EmiasClient()
    patient = await db.get(User, patient_id)
    if patient is None:
        raise Problem(404, "NOT_FOUND", "Пациент не найден")
    try:
        shash = mapping.snils_hash(snils, client.cfg.snils_pepper)
    except ValueError:
        raise Problem(422, "EMIAS_BAD_SNILS", "СНИЛС должен содержать 11 цифр") from None
    found = await anyio.to_thread.run_sync(client.search_patients, mapping.normalize_snils(snils))
    if not found:
        raise Problem(404, "EMIAS_PATIENT_NOT_FOUND", "Пациент с таким СНИЛС не найден в ЕМИАС")
    if len(found) > 1:
        raise Problem(409, "EMIAS_AMBIGUOUS_PATIENT", "ЕМИАС вернул более одного пациента")
    # Не доверяем фильтру ЕМИАС: связка должна указывать на пациента с тем же
    # СНИЛС, иначе один СНИЛС уводил бы карту чужого пациента (P3 ревью).
    matched_snils = mapping.extract_snils(found[0]) or ""
    try:
        matched_norm = mapping.normalize_snils(matched_snils)
    except ValueError:
        matched_norm = ""
    if matched_norm != mapping.normalize_snils(snils):
        raise Problem(409, "EMIAS_SNILS_MISMATCH", "ЕМИАС вернул пациента с другим СНИЛС")
    emias_id = str(found[0].get("id") or "")
    if not emias_id:
        raise Problem(502, "EMIAS_UPSTREAM", "ЕМИАС вернул пациента без идентификатора")
    link = await db.scalar(select(EmiasLink).where(EmiasLink.patient_id == patient.id))
    now = datetime.now(timezone.utc)
    if link is None:
        link = EmiasLink(patient_id=patient.id, emias_patient_id=emias_id, snils_hash=shash, synced_at=now)
        db.add(link)
        created = True
    else:
        link.emias_patient_id = emias_id
        link.snils_hash = shash
        link.synced_at = now
        created = False
    try:
        await db.commit()
    except IntegrityError:
        # emias_patient_id unique: одна карта ЕМИАС не может быть привязана к двум
        # локальным пациентам (иначе двойные визиты и путаница карт).
        await db.rollback()
        raise Problem(409, "EMIAS_ALREADY_LINKED", "Этот пациент ЕМИАС уже связан с другой картой") from None
    return {"patient_id": str(patient.id), "emias_patient_id": emias_id, "created": created}

async def patient_summary(db: AsyncSession, patient_id) -> dict:
    """Витрина «ЕМИАС» в карточке врача: связка + назначения + число приёмов."""
    link = await db.scalar(select(EmiasLink).where(EmiasLink.patient_id == patient_id))
    meds = (await db.scalars(
        select(PatientMedication)
        .where(PatientMedication.patient_id == patient_id, PatientMedication.source == "emias")
        .order_by(PatientMedication.started_on.desc().nullslast(), PatientMedication.created_at.desc())
    )).all()
    encounters = await db.scalar(
        select(func.count())
        .select_from(Visit)
        .where(Visit.patient_id == patient_id, Visit.source == "emias")
    )
    return {
        "linked": link is not None,
        "last_sync": link.synced_at.isoformat() if link and link.synced_at else None,
        "encounters": int(encounters or 0),
        "medications": [mapping.medication_dict(m) for m in meds],
    }

async def execute_sync(db: AsyncSession, *, mode: str, patient_id=None, trigger: str = "manual") -> EmiasSyncRun:
    """Прогон синхронизации с журналом emias_sync_runs (вызывают роутер и CLI)."""
    run = EmiasSyncRun(trigger=trigger, mode=mode, status="running", stats={})
    db.add(run)
    await db.commit()
    try:
        if mode == "one":
            if patient_id is None:
                raise Problem(422, "EMIAS_PATIENT_REQUIRED", "Для mode=one нужен patient_id")
            stats = await sync_patient(db, patient_id)
        else:
            stats = await sync_patients(db)
        run.stats = stats.as_dict()
        run.status = stats.status()
    except EmiasError as exc:
        await db.rollback()
        run.stats = {"mode": mode, "errors": 1, "error_code": exc.code}
        run.status = "failed"
    except Problem as exc:
        await db.rollback()
        run.stats = {"mode": mode, "errors": 1, "error_code": exc.code}
        run.status = "failed"
    except Exception as exc:  # непредвиденный сбой: журнал важнее трейса
        await db.rollback()
        log.error("emias: прогон упал: %s", type(exc).__name__)
        run.stats = {"mode": mode, "errors": 1, "error_code": "internal_error"}
        run.status = "failed"
    run.finished_at = datetime.now(timezone.utc)
    db.add(run)
    await db.commit()
    return run
