"""Тесты справочника препаратов (таблица drugs, источник — ГРЛС). Не ПДн."""
from .conftest import auth_header, clinician_ctx, login, register


def test_drugs_list_requires_clinician(client):
    register(client, "p-drugs@test.ru")
    data = login(client, "p-drugs@test.ru")
    r = client.get("/api/v1/drugs", headers=auth_header(data["access_token"]))
    assert r.status_code == 403
    assert r.json()["code"] == "ROLE_MISMATCH"


def test_drugs_list_returns_aviandr(client):
    h, _ = clinician_ctx(client, "doc-drugs@test.ru")
    r = client.get("/api/v1/drugs", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(d["drug_id"] == "aviandr" for d in items)
    av = next(d for d in items if d["drug_id"] == "aviandr")
    assert av["trade_name"] == "АВИАНДР®"
    assert "ЛП-" in av["reg_certificate"]
    assert av["dosage"]  # не пусто


def test_drugs_filter_by_query(client):
    h, _ = clinician_ctx(client, "doc-drugs2@test.ru")
    r = client.get("/api/v1/drugs", params={"q": "маритупир"}, headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["drug_id"] == "aviandr"
    # Несовпадающий фильтр → пусто
    r2 = client.get("/api/v1/drugs", params={"q": "несуществующий"}, headers=h)
    assert r2.json()["items"] == []


def test_drug_detail_and_404(client):
    h, _ = clinician_ctx(client, "doc-drugs3@test.ru")
    r = client.get("/api/v1/drugs/aviandr", headers=h)
    assert r.status_code == 200
    assert r.json()["inn"] == "маритупирдин"
    r2 = client.get("/api/v1/drugs/nope", headers=h)
    assert r2.status_code == 404
