# E5: дневник (FR-9), чат (FR-10), таймлайн (FR-7), каталог шкал (FR-2). Контракт: docs/04 §3.1; docs/06 §3.
from .conftest import finish_screening, patient_ctx

PHQ_ALL = {f"q{i}": 0 for i in range(1, 10)}

def test_questionnaire_catalog_serves_scales(client):
    # FR-2: каталог шкал из core — тексты не дублируются в UI
    h, _ = patient_ctx(client, "cat@test.ru")
    resp = client.get("/api/v1/questionnaires", headers=h)
    assert resp.status_code == 200
    codes = [q["code"] for q in resp.json()]
    assert codes == ["PHQ-9", "GAD-7", "HADS-A", "HADS-D"]
    phq = next(q for q in resp.json() if q["code"] == "PHQ-9")
    assert len(phq["items"]) == 9
    assert phq["items"][0]["key"] == "q1"
    assert phq["items"][0]["max_value"] == 3


def test_diary_create_and_list(client):
    h, _ = patient_ctx(client, "d@test.ru")
    resp = client.post(
        "/api/v1/diary", json={"content": "Сегодня получше", "mood": 7, "sleep_h": 6.5, "tags": ["сон"]}, headers=h
    )
    assert resp.status_code == 201
    items = client.get("/api/v1/diary", headers=h).json()["items"]
    assert items[0]["content"] == "Сегодня получше"
    assert items[0]["mood"] == 7


def test_diary_mood_out_of_range_422(client):
    h, _ = patient_ctx(client, "dm@test.ru")
    resp = client.post("/api/v1/diary", json={"content": "x", "mood": 11}, headers=h)
    assert resp.status_code == 422


def test_chat_hotline_on_red(client):
    h, _ = patient_ctx(client, "ch@test.ru")
    finish_screening(client, h, dict(PHQ_ALL, q9=1))  # RED
    resp = client.post("/api/v1/chat/thread", json={"content": "Мне очень плохо"}, headers=h)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reply"]["kind"] == "HOTLINE"
    assert body["reply"]["help_resources"]


def test_chat_faq_for_green(client):
    h, _ = patient_ctx(client, "cg@test.ru")
    finish_screening(client, h, PHQ_ALL)  # GREEN
    resp = client.post("/api/v1/chat/thread", json={"content": "Как вести дневник?"}, headers=h)
    assert resp.status_code == 201
    body = resp.json()
    assert body["reply"]["kind"] == "FAQ"
    assert body["reply"]["help_resources"] == []


def test_chat_thread_contains_both_sides(client):
    h, _ = patient_ctx(client, "ct@test.ru")
    client.post("/api/v1/chat/thread", json={"content": "Привет"}, headers=h)
    items = client.get("/api/v1/chat/thread", headers=h).json()["items"]
    assert [m["sender"] for m in items] == ["PATIENT", "SYSTEM"]


def test_timeline_returns_assessments_and_trend(client):
    h, _ = patient_ctx(client, "tl@test.ru")
    finish_screening(client, h, PHQ_ALL)  # 0
    finish_screening(client, h, dict(PHQ_ALL, q1=1, q2=1, q3=1))  # 3
    resp = client.get("/api/v1/me/timeline?days=90", headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert {r["total_score"] for r in body["runs"]} == {0, 3}
    assert len(body["trend"]["moving_avg"]) == 2