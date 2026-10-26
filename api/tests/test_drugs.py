"""Тесты справочника препаратов (таблица drugs, источник — ГРЛС). Не ПДн."""
import pytest

from .conftest import auth_header, clinician_ctx, login, register

# Ожидаемый состав справочника (api/app/data/drugs.json): препараты первой линии
# при F32/F33/F41.1 — СИОЗС сертралин и эсциталопрам плюс анксиолитик Авиандр.
DRUG_IDS = {"aviandr", "zoloft", "escitalopram-canon"}


def test_drugs_list_requires_clinician(client):
    register(client, "p-drugs@test.ru")
    data = login(client, "p-drugs@test.ru")
    r = client.get("/api/v1/drugs", headers=auth_header(data["access_token"]))
    assert r.status_code == 403
    assert r.json()["code"] == "ROLE_MISMATCH"


def test_drugs_list_returns_whole_library(client):
    h, _ = clinician_ctx(client, "doc-drugs@test.ru")
    r = client.get("/api/v1/drugs", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert {d["drug_id"] for d in items} == DRUG_IDS
    # У каждого препарата заполнены клинические поля и провенанс источников.
    for d in items:
        assert d["dosage"] and d["indications"] and d["contraindications"]
        assert "ЛП-" in d["reg_certificate"]
        assert d["source_urls"], d["drug_id"]

    av = next(d for d in items if d["drug_id"] == "aviandr")
    assert av["trade_name"] == "АВИАНДР®"

    zoloft = next(d for d in items if d["drug_id"] == "zoloft")
    assert zoloft["trade_name"] == "ЗОЛОФТ®"
    assert zoloft["inn"] == "сертралин" and zoloft["inn_en"] == "sertraline"
    assert zoloft["reg_certificate"] == "ЛП-№(002851)-(РГ-RU)"
    assert zoloft["reg_date"] == "2023-07-24"

    esc = next(d for d in items if d["drug_id"] == "escitalopram-canon")
    assert esc["inn"] == "эсциталопрам" and esc["inn_en"] == "escitalopram"
    assert esc["reg_certificate"] == "ЛП-№(006068)-(РГ-RU)"
    assert esc["reg_date"] == "2024-06-28"

    # FR-27: кратность приёма разбирается из текста инструкции, сам текст не
    # переписывается — форму назначения врач заполняет дословной дозировкой.
    assert av["times_per_day"] == 2
    assert av["dosage"].startswith("Внутрь по 20 мг 2 раза/сут")
    assert zoloft["times_per_day"] == 1
    assert "1 раз/сут" in zoloft["dosage"]
    assert esc["times_per_day"] == 1
    assert "1 раз/сут" in esc["dosage"]


def test_drugs_filter_by_query(client):
    h, _ = clinician_ctx(client, "doc-drugs2@test.ru")
    r = client.get("/api/v1/drugs", params={"q": "маритупир"}, headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["drug_id"] == "aviandr"

    # Поиск по МНН находит каждый новый препарат.
    r2 = client.get("/api/v1/drugs", params={"q": "сертралин"}, headers=h)
    assert [d["drug_id"] for d in r2.json()["items"]] == ["zoloft"]
    r3 = client.get("/api/v1/drugs", params={"q": "эсциталопрам"}, headers=h)
    assert [d["drug_id"] for d in r3.json()["items"]] == ["escitalopram-canon"]
    # Поиск по торговому наименованию — тоже.
    r4 = client.get("/api/v1/drugs", params={"q": "ЗОЛОФТ"}, headers=h)
    assert [d["drug_id"] for d in r4.json()["items"]] == ["zoloft"]
    # Несовпадающий фильтр → пусто
    r5 = client.get("/api/v1/drugs", params={"q": "несуществующий"}, headers=h)
    assert r5.json()["items"] == []


def test_drug_detail_and_404(client):
    h, _ = clinician_ctx(client, "doc-drugs3@test.ru")
    r = client.get("/api/v1/drugs/aviandr", headers=h)
    assert r.status_code == 200
    assert r.json()["inn"] == "маритупирдин"

    r2 = client.get("/api/v1/drugs/zoloft", headers=h)
    assert r2.status_code == 200
    body = r2.json()
    assert body["inn_en"] == "sertraline"
    assert body["source_urls"] and all(
        u.startswith("https://") for u in body["source_urls"]
    )

    r3 = client.get("/api/v1/drugs/escitalopram-canon", headers=h)
    assert r3.status_code == 200
    assert "генерализованное тревожное расстройство" in r3.json()["indications"].lower()

    r4 = client.get("/api/v1/drugs/nope", headers=h)
    assert r4.status_code == 404


def test_seed_drugs_is_idempotent(db_session):
    """Повторный сид не дублирует строки и не пересоздаёт их (на drugs.drug_id
    ссылается adverse_events.drug_id, поэтому DELETE+INSERT недопустим)."""
    from sqlalchemy import select

    from app.models import Drug
    from app.seed import _seed_drugs

    def snapshot() -> dict[str, str]:
        return {
            d.drug_id: str(d.id)
            for d in db_session.execute(select(Drug)).scalars()
        }

    _seed_drugs(db_session)
    db_session.commit()
    first = snapshot()
    _seed_drugs(db_session)
    db_session.commit()
    second = snapshot()

    assert set(second) == DRUG_IDS
    assert second == first, "повторный сид должен обновлять строки, а не пересоздавать"


@pytest.mark.parametrize(
    ("dosage", "expected"),
    [
        ("2 раза/сут", 2),
        ("1 раз/сут", 1),
        ("по 20 мг 2 раза/сут", 2),
        ("3 раза в сутки", 3),
        ("1 раз в день", 1),
        # Диапазон → нижняя граница: знаменатель приверженности не должен
        # требовать больше приёмов, чем назначено минимум (см. docstring хелпера).
        ("1-2 раза в день", 1),
        ("2 р./сут", 2),
        # Доза в сутки — не частота: слова «раз» нет.
        ("50 мг/сут", None),
        ("максимум 200 мг/сут", None),
        # Разные частоты в одном тексте — неоднозначно, не угадываем.
        ("1 раз/сут или 2 раза/сут", None),
        # Вне диапазона формы (1..6) не подставляем, чтобы не исказить инструкцию.
        ("8 раз/сут", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_times_per_day(dosage, expected):
    from app.services.drug_dosage import parse_times_per_day

    assert parse_times_per_day(dosage) == expected
