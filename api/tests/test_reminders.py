"""FR-24: напоминания врача по пациентам. Контракт: docs/04 FR-24; docs/06 §3.

Покрытие: roster/IDOR-гейты, создание/список/фильтры срока (время заморожено),
переходы done/cancel, шифрование note в покое, тик воркера (ровно одно
уведомление, идемпотентность), advisory-лок против дублей, уничтожение
напоминаний при отзыве согласия (F2).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app import db
from app.services import reminders

from .conftest import admin_ctx, clinician_ctx, patient_ctx

NOW = datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc)  # 12:00 МСК
PAST = "2026-05-09T09:00:00+00:00"
TODAY = "2026-05-10T20:00:00+00:00"  # 23:00 МСК того же дня
SOON = "2026-05-14T09:00:00+00:00"
LATER = "2026-06-20T09:00:00+00:00"

def _grant(client, admin_h, patient_id, clinician_id) -> None:
    resp = client.post(
        "/api/v1/admin/roster",
        json={"patient_id": patient_id, "clinician_id": clinician_id},
        headers=admin_h,
    )
    assert resp.status_code == 201, resp.text

def _setup(client, patient_email="rp@test.ru", clinician_email="rc@test.ru"):
    h, pid = patient_ctx(client, patient_email)
    c, cid = clinician_ctx(client, clinician_email)
    a, _ = admin_ctx(client, "ra@test.ru")
    _grant(client, a, pid, cid)
    return h, pid, c, cid

def _create(client, headers, patient_id, due_at=PAST, kind="call", note=None) -> dict:
    resp = client.post(
        "/api/v1/clinician/reminders",
        json={"patient_id": str(patient_id), "kind": kind, "note": note, "due_at": due_at},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()

@pytest.fixture()
def frozen(monkeypatch):
    monkeypatch.setattr(reminders, "_now", lambda: NOW)
    return NOW

def test_create_and_list_with_due_states(client, frozen):
    _, pid, c, _ = _setup(client)
    overdue = _create(client, c, pid, due_at=PAST, note="позвонить повторно")
    today = _create(client, c, pid, due_at=TODAY, kind="write")
    soon = _create(client, c, pid, due_at=SOON, kind="appointment")
    later = _create(client, c, pid, due_at=LATER, kind="custom")

    assert overdue["due_state"] == "overdue"
    assert overdue["patient_fio"] is not None
    assert overdue["kind_label"] == "Позвонить"

    all_items = client.get("/api/v1/clinician/reminders", headers=c).json()
    assert all_items["overdue"] == 1
    assert [i["id"] for i in all_items["items"]] == [
        overdue["id"], today["id"], soon["id"], later["id"]
    ]
    states = {i["id"]: i["due_state"] for i in all_items["items"]}
    assert states[today["id"]] == "today"
    assert states[soon["id"]] == "soon"
    assert states[later["id"]] == "later"

    ids = lambda due: [i["id"] for i in client.get(f"/api/v1/clinician/reminders?due={due}", headers=c).json()["items"]]
    assert ids("overdue") == [overdue["id"]]
    # Границы «сегодня» — календарный день МСК, 20:00 UTC = 23:00 МСК того же дня.
    assert ids("today") == [today["id"]]
    assert ids("week") == [overdue["id"], today["id"], soon["id"]]

def test_unrostered_patient_forbidden(client, frozen):
    _, _, c, _ = _setup(client, "rpx@test.ru", "rcx@test.ru")
    _, stranger = patient_ctx(client, "stranger@test.ru")
    resp = client.post(
        "/api/v1/clinician/reminders",
        json={"patient_id": stranger, "kind": "call", "due_at": PAST},
        headers=c,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "NO_ROSTER"
    assert client.get(f"/api/v1/clinician/patients/{stranger}/reminders", headers=c).status_code == 403

def test_idor_other_clinician_cannot_see_or_touch(client, frozen):
    h, pid, c, _ = _setup(client, "rip@test.ru", "ric@test.ru")
    reminder = _create(client, c, pid)
    other, _ = clinician_ctx(client, "other-c@test.ru")

    assert client.get("/api/v1/clinician/reminders", headers=other).json()["items"] == []
    assert client.patch(
        f"/api/v1/clinician/reminders/{reminder['id']}", json={"note": "x"}, headers=other
    ).status_code == 404
    assert client.post(f"/api/v1/clinician/reminders/{reminder['id']}/done", headers=other).status_code == 404
    assert client.post(f"/api/v1/clinician/reminders/{reminder['id']}/cancel", headers=other).status_code == 404
    # Пациент не имеет доступа к клиницистским роутам.
    assert client.get("/api/v1/clinician/reminders", headers=h).status_code == 403

def test_patch_and_transitions(client, frozen):
    _, pid, c, _ = _setup(client, "rtp@test.ru", "rtc@test.ru")
    reminder = _create(client, c, pid, note="черновик")

    patched = client.patch(
        f"/api/v1/clinician/reminders/{reminder['id']}",
        json={"note": "уточнить дозу", "kind": "write", "due_at": SOON},
        headers=c,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["note"] == "уточнить дозу"
    assert patched.json()["kind"] == "write"
    assert patched.json()["due_state"] == "soon"

    done = client.post(f"/api/v1/clinician/reminders/{reminder['id']}/done", headers=c)
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["done_at"] is not None
    assert done.json()["due_state"] is None
    # Повторное закрытие — 409, правка закрытого — 409.
    assert client.post(f"/api/v1/clinician/reminders/{reminder['id']}/done", headers=c).status_code == 409
    assert client.patch(
        f"/api/v1/clinician/reminders/{reminder['id']}", json={"note": "y"}, headers=c
    ).status_code == 409

    second = _create(client, c, pid, kind="custom")
    cancelled = client.post(f"/api/v1/clinician/reminders/{second['id']}/cancel", headers=c)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.get("/api/v1/clinician/reminders?status=all", headers=c).json()["overdue"] == 0

def test_note_encrypted_at_rest(client, frozen, db_session):
    _, pid, c, _ = _setup(client, "rep@test.ru", "rec@test.ru")
    secret = "Иванов И.И. — суицидальные мысли"
    reminder = _create(client, c, pid, note=secret)

    raw = db_session.execute(
        text("SELECT note FROM clinician_reminders WHERE id = :id"), {"id": reminder["id"]}
    ).scalar()
    assert isinstance(raw, (bytes, bytearray, memoryview))
    assert secret.encode() not in bytes(raw)
    # Через API текст расшифрован (EncryptedText).
    listed = client.get("/api/v1/clinician/reminders", headers=c).json()["items"][0]
    assert listed["note"] == secret

def test_worker_tick_creates_one_notification_idempotent(client, frozen):
    _, pid, c, _ = _setup(client, "rwp@test.ru", "rwc@test.ru")
    reminder = _create(client, c, pid, due_at=PAST, kind="write", note="Иванов И.И. — состояние")

    with db.SessionLocal() as s:
        assert reminders.deliver_due(s, now=NOW) == 1
        # Второй тик не создаёт дубль (notified_at + unique reminder_id).
        assert reminders.deliver_due(s, now=NOW) == 0

    items = client.get("/api/v1/me/notifications", headers=c).json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "REMINDER"
    assert items[0]["patient_id"] == str(pid)
    # note (свободный текст ПДн) в канал доставки не попадает.
    assert "Иванов" not in items[0]["body"]

    with db.SessionLocal() as s:
        notified = s.execute(
            text("SELECT notified_at, (SELECT count(*) FROM notifications WHERE reminder_id = :id) "
                 "FROM clinician_reminders WHERE id = :id"),
            {"id": reminder["id"]},
        ).one()
    assert notified[0] is not None
    assert notified[1] == 1

def test_worker_respects_advisory_lock(client, frozen, engine):
    _, pid, c, _ = _setup(client, "rlock@test.ru", "rlockc@test.ru")
    _create(client, c, pid, due_at=PAST)

    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(hashtext('clinician-reminders'))"))
        try:
            with db.SessionLocal() as s:
                assert reminders.deliver_due(s, now=NOW) == 0
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(hashtext('clinician-reminders'))"))

    assert client.get("/api/v1/me/notifications", headers=c).json()["items"] == []
    with db.SessionLocal() as s:
        assert reminders.deliver_due(s, now=NOW) == 1

def test_failed_tick_releases_advisory_lock(client, frozen, engine, monkeypatch):
    _, pid, c, _ = _setup(client, "rfail@test.ru", "rfailc@test.ru")
    _create(client, c, pid, due_at=PAST)

    def boom(*_args, **_kwargs):
        raise RuntimeError("tick failed")

    monkeypatch.setattr(reminders, "_patient_name", boom)
    with db.SessionLocal() as s:
        with pytest.raises(RuntimeError):
            reminders.deliver_due(s, now=NOW)

    # Лок обязан освободиться: иначе утечка на пул-соединение заблокирует все
    # последующие тики этого инстанса (и маскирует исходную ошибку).
    with engine.connect() as conn:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": reminders.DELIVERY_LOCK_KEY}
        ).scalar()
        try:
            assert acquired is True
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": reminders.DELIVERY_LOCK_KEY}
            )
            conn.commit()

def test_reminder_delete_cascades_notification(client, frozen):
    _, pid, c, _ = _setup(client, "rcd@test.ru", "rcdc@test.ru")
    reminder = _create(client, c, pid, due_at=PAST)
    with db.SessionLocal() as s:
        assert reminders.deliver_due(s, now=NOW) == 1

    # FK notifications.reminder_id ON DELETE CASCADE: удаление напоминания
    # (в т.ч. гонка с отзывом согласия) не должно упираться в FK.
    with db.SessionLocal() as s:
        s.execute(text("DELETE FROM clinician_reminders WHERE id = :id"), {"id": reminder["id"]})
        s.commit()
        left = s.execute(
            text("SELECT count(*) FROM notifications WHERE reminder_id = :id"), {"id": reminder["id"]}
        ).scalar()
    assert left == 0

def test_consent_revocation_destroys_reminders(client, frozen):
    h, pid, c, _ = _setup(client, "rrev@test.ru", "rrevc@test.ru")
    _create(client, c, pid, due_at=PAST, note="ФИО и состояние")
    with db.SessionLocal() as s:
        assert reminders.deliver_due(s, now=NOW) == 1

    assert client.post("/api/v1/consent/revoke", headers=h).status_code == 204
    with db.SessionLocal() as s:
        left = s.execute(
            text("SELECT count(*) FROM clinician_reminders WHERE patient_id = :p"), {"p": pid}
        ).scalar()
        pushed = s.execute(
            text("SELECT count(*) FROM notifications WHERE patient_id = :p"), {"p": pid}
        ).scalar()
    assert left == 0
    assert pushed == 0
