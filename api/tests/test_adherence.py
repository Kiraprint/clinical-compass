"""FR-27: приверженность считается от ожидаемых приёмов по назначенному режиму.

Проверяем математику (знаменатель = times_per_day × дни активности в окне),
weighted-агрегат по препаратам, отсутствие расписания, валидацию отметки и
исключение исторических записей из знаменателя.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models import AdherenceRecord, PatientMedication

from .conftest import admin_ctx, clinician_ctx, patient_ctx

MOSCOW = ZoneInfo("Europe/Moscow")

def _today():
    return datetime.now(MOSCOW).date()

def _iso(days_ago: int = 0) -> str:
    return (_today() - timedelta(days=days_ago)).isoformat()

def _setup(client, email: str):
    """Пациент с согласием + врач в roster. Возвращает (patient_h, pid, clinician_h)."""
    ph, pid = patient_ctx(client, email)
    c, cid = clinician_ctx(client, f"c-{email}")
    a, _ = admin_ctx(client, f"a-{email}")
    resp = client.post(
        "/api/v1/admin/roster", json={"patient_id": pid, "clinician_id": cid}, headers=a,
    )
    assert resp.status_code == 201, resp.text
    return ph, pid, c

def _med(client, c, pid, **overrides) -> dict:
    body = {"title": "Сертралин", "started_on": _iso(), "times_per_day": 1, **overrides}
    resp = client.post(f"/api/v1/clinician/patients/{pid}/medications", json=body, headers=c)
    assert resp.status_code == 201, resp.text
    return resp.json()

def _mark(client, h, status: str, medication_id: str | None = None) -> dict:
    body = {"status": status}
    if medication_id is not None:
        body["medication_id"] = medication_id
    resp = client.post("/api/v1/me/adherence", json=body, headers=h)
    assert resp.status_code == 201, resp.text
    return resp.json()

def _stats(client, h, days: int = 7) -> dict:
    resp = client.get(f"/api/v1/me/adherence?days={days}", headers=h)
    assert resp.status_code == 200, resp.text
    return resp.json()

def test_percentage_from_expected_doses(client):
    """5 принятых при режиме 2×день за 7 дней → 5/14 = 36% (не 5/5 = 100%)."""
    ph, pid, c = _setup(client, "adh-math@test.ru")
    med = _med(client, c, pid, times_per_day=2, started_on=_iso(6))
    for _ in range(5):
        _mark(client, ph, "taken", med["id"])

    body = _stats(client, ph, days=7)
    assert body["summary"]["expected"] == 14
    assert body["summary"]["score"] == 5
    assert body["summary"]["pct"] == 36
    assert body["medications"][0]["pct"] == 36

def test_expected_is_calendar_days_not_weekdays(client):
    """Ожидание — все календарные дни активности, без исключения выходных."""
    ph, pid, c = _setup(client, "adh-week@test.ru")
    med = _med(client, c, pid, times_per_day=2, started_on=_iso(6))
    for days, expected in ((7, 14), (14, 14)):
        body = _stats(client, ph, days=days)
        assert body["medications"][0]["expected"] == expected
        assert body["window"]["to"] == _today().isoformat()

def test_ended_on_cuts_expected_days(client):
    """Назначение, закончившееся 3 дня назад: считаем только активные 4 дня окна."""
    ph, pid, c = _setup(client, "adh-ended@test.ru")
    med = _med(client, c, pid, times_per_day=1, started_on=_iso(9), ended_on=_iso(3))
    _mark(client, ph, "taken", med["id"])
    _mark(client, ph, "taken", med["id"])

    body = _stats(client, ph, days=7)
    assert body["medications"][0]["active_days"] == 4
    assert body["summary"]["expected"] == 4
    assert body["summary"]["pct"] == 50

def test_overall_pct_is_weighted_not_average(client):
    """Два препарата: 2/2 и 0/1 → общий 67%, а не среднее 100% и 0%."""
    ph, pid, c = _setup(client, "adh-weighted@test.ru")
    a = _med(client, c, pid, title="A", times_per_day=2, started_on=_iso())
    b = _med(client, c, pid, title="B", times_per_day=1, started_on=_iso())
    _mark(client, ph, "taken", a["id"])
    _mark(client, ph, "taken", a["id"])

    body = _stats(client, ph, days=7)
    assert body["summary"]["expected"] == 3
    assert body["summary"]["score"] == 2
    assert body["summary"]["pct"] == 67
    pcts = sorted(m["pct"] for m in body["medications"])
    assert pcts == [0, 100]

def test_partial_counts_half(client):
    """partial = 0.5 приёма: 3 partial из 2×в день за 1 день → 1.5/2 = 75%."""
    ph, pid, c = _setup(client, "adh-partial@test.ru")
    med = _med(client, c, pid, times_per_day=2, started_on=_iso())
    for _ in range(3):
        _mark(client, ph, "partial", med["id"])
    body = _stats(client, ph, days=7)
    assert body["summary"]["score"] == 1.5
    assert body["summary"]["pct"] == 75

def test_no_schedule_means_no_percent(client):
    """time per day = NULL (ЕМИАС): процент не считается, отметки вне знаменателя."""
    ph, pid, c = _setup(client, "adh-noschedule@test.ru")
    med = _med(client, c, pid, times_per_day=None, started_on=_iso())
    assert med["times_per_day"] is None
    _mark(client, ph, "taken", med["id"])

    body = _stats(client, ph, days=7)
    assert body["summary"]["pct"] is None
    assert body["summary"]["expected"] == 0
    assert body["medications"] == []
    assert body["excluded"]["count"] == 1
    assert body["excluded"]["items"][0]["medication_id"] == med["id"]

def test_no_medications_gives_empty_aggregate(client):
    ph, _pid, _c = _setup(client, "adh-empty@test.ru")
    body = _stats(client, ph)
    assert body["summary"] == {"pct": None, "score": 0, "expected": 0}
    assert body["medications"] == []
    assert body["items"] == []

def test_legacy_records_are_outside_denominator(client):
    """Отметка без medication_id (историческая) видна в списке, но не в %."""
    ph, pid, c = _setup(client, "adh-legacy@test.ru")
    med = _med(client, c, pid, times_per_day=1, started_on=_iso())
    _mark(client, ph, "taken", med["id"])
    _mark(client, ph, "missed")  # свободный текст/без FK

    body = _stats(client, ph, days=7)
    assert body["summary"]["expected"] == 1
    assert body["summary"]["pct"] == 100
    assert body["total"] == 2
    assert body["excluded"]["count"] == 1
    assert body["excluded"]["items"][0]["medication_id"] is None
    assert len(body["items"]) == 2

def test_post_foreign_or_stopped_medication_422(client):
    """Отметку нельзя привязать к чужому или остановленному назначению."""
    ph, pid, c = _setup(client, "adh-foreign@test.ru")
    _other_ph, other_pid = patient_ctx(client, "adh-other@test.ru")
    c2, cid2 = clinician_ctx(client, "c-adh-other@test.ru")
    a2, _ = admin_ctx(client, "a-adh-other@test.ru")
    assert client.post(
        "/api/v1/admin/roster", json={"patient_id": other_pid, "clinician_id": cid2}, headers=a2,
    ).status_code == 201
    foreign = _med(client, c2, other_pid, started_on=_iso())
    own = _med(client, c, pid, started_on=_iso())
    assert client.post(f"/api/v1/clinician/patients/{pid}/medications/{own['id']}/stop", headers=c).status_code == 200
    r = client.post("/api/v1/me/adherence", json={"medication_id": own["id"], "status": "taken"}, headers=ph)
    assert r.status_code == 422
    assert r.json()["code"] == "MEDICATION_NOT_ACTIVE"

def test_times_per_day_validation(client):
    """Режим вне 1..6 — 422 TIMES_PER_DAY_INVALID при создании и правке."""
    _ph, pid, c = _setup(client, "adh-tpd@test.ru")
    for bad in (0, 7):
        r = client.post(
            f"/api/v1/clinician/patients/{pid}/medications",
            json={"title": "X", "started_on": _iso(), "times_per_day": bad}, headers=c,
        )
        assert r.status_code == 422
        assert r.json()["code"] == "TIMES_PER_DAY_INVALID"

    med = _med(client, c, pid, times_per_day=2, started_on=_iso())
    r = client.patch(
        f"/api/v1/clinician/patients/{pid}/medications/{med['id']}",
        json={"times_per_day": 0}, headers=c,
    )
    assert r.status_code == 422
    assert r.json()["code"] == "TIMES_PER_DAY_INVALID"
    r = client.patch(
        f"/api/v1/clinician/patients/{pid}/medications/{med['id']}",
        json={"times_per_day": 4}, headers=c,
    )
    assert r.status_code == 200 and r.json()["times_per_day"] == 4

def test_consent_revoke_deletes_linked_records_and_medications(client, db_session):
    """Отзыв согласия уничтожает и назначения, и связанные отметки (F2)."""
    ph, pid, c = _setup(client, "adh-revoke@test.ru")
    med = _med(client, c, pid, started_on=_iso())
    _mark(client, ph, "taken", med["id"])

    assert client.post("/api/v1/consent/revoke", headers=ph).status_code == 204
    db_session.expire_all()
    assert db_session.execute(
        select(func.count()).select_from(AdherenceRecord).where(AdherenceRecord.patient_id == pid)
    ).scalar_one() == 0
    assert db_session.execute(
        select(func.count()).select_from(PatientMedication).where(PatientMedication.patient_id == pid)
    ).scalar_one() == 0

def test_missing_start_date_counts_from_creation(client):
    """Назначение без started_on: знаменатель с даты создания, а не за всё окно.

    Регресс: назначение, заведённое сегодня, требовало приёмов за все 7 дней
    (expected=14 вместо 2) и занижало процент.
    """
    ph, pid, c = _setup(client, "adh-nostart@test.ru")
    med = _med(client, c, pid, times_per_day=2, started_on=None)
    _mark(client, ph, "taken", med["id"])
    stats = _stats(client, ph, days=7)
    (row,) = stats["medications"]
    assert row["expected"] == 2
    assert row["pct"] == 50
    assert stats["summary"]["expected"] == 2
