"""FR-25: чистый маппинг FHIR → локальные модели (без сети и БД)."""

from __future__ import annotations

import hashlib

import pytest

from app.emias import mapping

PATIENT = {
    "resourceType": "Patient",
    "id": "emias-1001",
    "identifier": [
        {"system": "urn:oid:1.2.643.100.3", "type": {"text": "SNILS"}, "value": "112-233-445 95"},
    ],
    "name": [{"family": "Иванова", "given": ["Мария", "Петровна"]}],
}

ENCOUNTER = {
    "resourceType": "Encounter",
    "id": "enc-1",
    "status": "finished",
    "class": {"code": "VR"},
    "period": {"start": "2026-01-19T14:10:00+03:00", "end": "2026-01-19T14:40:00+03:00"},
    "serviceProvider": {"display": "Поликлиника № 1"},
    "reasonCode": [{"text": "Контрольный приём"}],
    "participant": [{"individual": {"display": "Терапевт Ковалёва Н.С."}}],
}

MEDICATION = {
    "resourceType": "MedicationRequest",
    "id": "med-1",
    "status": "stopped",
    "medicationCodeableConcept": {"text": "Сертралин"},
    "dosageInstruction": [{"text": "50 мг вечером"}],
    "authoredOn": "2026-01-19",
}

def test_normalize_snils_accepts_formats():
    assert mapping.normalize_snils("112-233-445 95") == "11223344595"
    assert mapping.normalize_snils("11223344595") == "11223344595"
    with pytest.raises(ValueError):
        mapping.normalize_snils("123")
    with pytest.raises(ValueError):
        mapping.normalize_snils(None)

def test_snils_hash_is_salted_and_irreversible():
    digest = mapping.snils_hash("112-233-445 95", "pepper-1")
    assert digest == hashlib.sha256(b"11223344595:pepper-1").hexdigest()
    assert digest == mapping.snils_hash("11223344595", "pepper-1")
    assert mapping.snils_hash("112-233-445 95", "pepper-2") != digest
    assert "11223344595" not in digest and "112-233-445" not in digest

def test_extract_snils_by_oid_and_type():
    assert mapping.extract_snils(PATIENT) == "112-233-445 95"
    by_type = {"identifier": [{"type": {"text": "SNILS"}, "value": "11223344595"}]}
    assert mapping.extract_snils(by_type) == "11223344595"
    assert mapping.extract_snils({"identifier": [{"system": "urn:oid:1.2.3", "value": "x"}]}) is None

def test_patient_display_name_and_fio_normalization():
    assert mapping.patient_display_name(PATIENT) == "Иванова Мария Петровна"
    assert mapping.normalize_fio("  Иванова   Мария ") == "иванова мария"
    assert mapping.patient_display_name({}) == ""

def test_encounter_maps_to_visit_fields():
    fields = mapping.encounter_visit_fields(ENCOUNTER)
    assert fields["emias_id"] == "enc-1"
    assert fields["visit_type"] == "online"
    assert fields["duration_min"] == 30
    assert fields["source"] == "emias"
    assert fields["visited_at"].isoformat().startswith("2026-01-19T14:10:00")
    assert "Контрольный приём" in fields["notes"]
    assert "Ковалёва" in fields["notes"]

def test_encounter_requires_start():
    with pytest.raises(ValueError):
        mapping.encounter_visit_fields({"id": "e", "class": {"code": "AMB"}, "period": {}})

def test_encounter_type_fallbacks():
    base = {"id": "e1", "period": {"start": "2026-01-01T10:00:00+03:00"}}
    assert mapping.encounter_visit_type({**base, "class": {"code": "AMB"}}) == "in_person"
    assert mapping.encounter_visit_type({**base, "class": {"code": "PHONE"}}) == "phone"
    assert mapping.encounter_visit_type(base) == "in_person"

def test_medication_maps_status_and_date():
    fields = mapping.medication_fields(MEDICATION)
    assert fields == {
        "emias_id": "med-1",
        "title": "Сертралин",
        "dosage": "50 мг вечером",
        "started_on": mapping._parse_date("2026-01-19"),
        "status": "stopped",
        "source": "emias",
    }
    assert mapping.medication_fields({**MEDICATION, "status": "on-hold"})["status"] == "unknown"

def test_medication_without_title_rejected():
    with pytest.raises(ValueError):
        mapping.medication_fields({"id": "m", "status": "active", "medicationCodeableConcept": {}})

def test_encounter_summary_has_no_free_text_notes():
    summary = mapping.encounter_summary(ENCOUNTER)
    assert summary["emias_id"] == "enc-1"
    assert summary["reason"] == "Контрольный приём"
    assert "notes" not in summary
