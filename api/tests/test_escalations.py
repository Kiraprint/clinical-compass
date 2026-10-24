# Контракт: docs/04 §3.1 (GET/POST /escalations), docs/06 §3 (escalations_*).
"""Фильтр статуса очереди эскалаций и контракт ошибок закрытия сигнала."""

from __future__ import annotations

from .conftest import admin_ctx, clinician_ctx, finish_screening, patient_ctx

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}

def _escalation(client, email: str = "esc@test.ru") -> tuple[dict, str, str]:
    """RED-скрининг пациента → открытая эскалация SUICIDAL.

    Возвращает (заголовки пациента, patient_id, escalation_id).
    """
    h, pid = patient_ctx(client, email)
    assessment = finish_screening(client, h, dict(PHQ_ALL, q9=1))
    assert assessment["escalation"]["kind"] == "SUICIDAL"
    return h, pid, assessment["escalation"]["id"]


def test_queue_status_filter_lists_resolved_history(client):
    _, pid, escalation_id = _escalation(client)
    ch, _ = clinician_ctx(client, "esc_doc@test.ru")

    opened = client.get("/api/v1/escalations", headers=ch).json()["items"]
    assert [i["id"] for i in opened] == [escalation_id]
    assert opened[0]["resolved_at"] is None
    assert opened[0]["resolution"] is None
    assert opened[0]["patient_id"] == pid
    assert opened[0]["risk_answers"] == {}

    resolution = "осмотрен, госпитализация не требуется"
    resolved = client.post(
        f"/api/v1/escalations/{escalation_id}/resolve",
        json={"resolution": resolution},
        headers=ch,
    )
    assert resolved.status_code == 200, resolved.text
    resolved_at = resolved.json()["resolved_at"]
    assert resolved.json()["resolution"] == resolution

    # open по умолчанию — как раньше: закрытый сигнал из очереди уходит.
    assert client.get("/api/v1/escalations", headers=ch).json()["items"] == []
    assert client.get("/api/v1/escalations?status=open", headers=ch).json()["items"] == []

    history = client.get("/api/v1/escalations?status=resolved", headers=ch).json()["items"]
    assert len(history) == 1
    assert history[0]["id"] == escalation_id
    assert history[0]["kind"] == "SUICIDAL"
    assert history[0]["resolved_at"] == resolved_at
    assert history[0]["resolution"] == resolution
    assert history[0]["in_roster"] is False  # ростера нет

    everything = client.get("/api/v1/escalations?status=all", headers=ch).json()["items"]
    assert [i["id"] for i in everything] == [escalation_id]
    assert everything[0]["resolution"] == resolution


def test_queue_status_all_shows_open_and_resolved(client):
    _, _, resolved_id = _escalation(client, "esc_all_a@test.ru")
    _, _, open_id = _escalation(client, "esc_all_b@test.ru")
    ch, _ = clinician_ctx(client, "esc_all_doc@test.ru")
    assert client.post(
        f"/api/v1/escalations/{resolved_id}/resolve", json={}, headers=ch
    ).status_code == 200

    items = client.get("/api/v1/escalations?status=all", headers=ch).json()["items"]
    by_id = {i["id"]: i for i in items}
    assert set(by_id) == {resolved_id, open_id}
    assert by_id[resolved_id]["resolved_at"] is not None
    assert by_id[open_id]["resolved_at"] is None


def test_queue_invalid_status_is_validation_error(client):
    ch, _ = clinician_ctx(client, "esc_status_doc@test.ru")
    resp = client.get("/api/v1/escalations?status=bogus", headers=ch)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION"


def test_resolve_unknown_uuid_404(client):
    ch, _ = clinician_ctx(client, "esc_missing_doc@test.ru")
    resp = client.post("/api/v1/escalations/00000000-0000-0000-0000-000000000000/resolve", json={}, headers=ch)
    assert resp.status_code == 404
    assert resp.json()["code"] == "ESCALATION_NOT_FOUND"


def test_resolve_malformed_id_404_not_validation(client):
    _, _, escalation_id = _escalation(client, "esc_bad_id@test.ru")
    ch, _ = clinician_ctx(client, "esc_bad_id_doc@test.ru")

    resp = client.post("/api/v1/escalations/not-a-uuid/resolve", json={}, headers=ch)
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "ESCALATION_NOT_FOUND"

    # Кривой id не должен закрывать настоящую эскалацию.
    assert [i["id"] for i in client.get("/api/v1/escalations", headers=ch).json()["items"]] == [escalation_id]


def test_resolve_resolution_length_uses_documented_code(client):
    _, _, escalation_id = _escalation(client, "esc_long@test.ru")
    ch, _ = clinician_ctx(client, "esc_long_doc@test.ru")

    too_long = client.post(
        f"/api/v1/escalations/{escalation_id}/resolve",
        json={"resolution": "я" * 1001},
        headers=ch,
    )
    assert too_long.status_code == 422, too_long.text
    assert too_long.json()["code"] == "RESOLUTION_TOO_LONG"
    # Отклонённый комментарий сигнал не закрывает.
    assert client.get("/api/v1/escalations", headers=ch).json()["items"][0]["id"] == escalation_id

    exact = "я" * 1000
    ok = client.post(
        f"/api/v1/escalations/{escalation_id}/resolve",
        json={"resolution": exact},
        headers=ch,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["resolution"] == exact


def test_resolve_twice_conflict_and_patient_forbidden(client):
    patient_h, _, escalation_id = _escalation(client, "esc_conflict@test.ru")
    ch, _ = clinician_ctx(client, "esc_conflict_doc@test.ru")

    assert client.post(
        f"/api/v1/escalations/{escalation_id}/resolve", json={}, headers=ch
    ).status_code == 200
    again = client.post(f"/api/v1/escalations/{escalation_id}/resolve", json={}, headers=ch)
    assert again.status_code == 409
    assert again.json()["code"] == "ESCALATION_ALREADY_RESOLVED"

    denied = client.post(
        f"/api/v1/escalations/{escalation_id}/resolve", json={}, headers=patient_h
    )
    assert denied.status_code == 403

    # Админ видит историю так же, как врач.
    ah, _ = admin_ctx(client, "esc_conflict_admin@test.ru")
    items = client.get("/api/v1/escalations?status=resolved", headers=ah).json()["items"]
    assert [i["id"] for i in items] == [escalation_id]
    assert items[0]["in_roster"] is False
