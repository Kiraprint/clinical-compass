"""Remote-backend деперсонализации (CC_DEPERSONALIZE_NER=remote, docs/12).

Проверяем HTTP-контур без GPU: фейковый sidecar отдаёт спаны в контракте
POST /v1/mask, а api-путь маскирует их теми же правилами/спанами, что hybrid.
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import Settings, settings
from app.services import llm_router
from app.services.depersonalize import (
    DepersonalizationUnavailable,
    ner_backend,
    scrub_text,
)
from compass.depersonalize import (
    ADDRESS,
    AGE,
    DOC,
    DOB,
    EMAIL,
    FIO,
    PHONE,
    structural_spans,
)

# Капитализированные ФИО 2–3 слова — «руберт» фейкового sidecar'а.

_CAP_FIO = re.compile(
    r"[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2}(?:\s+[А-ЯЁ]\.(?:[А-ЯЁ]\.)?)?"
    r"|[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.(?:\s?[А-ЯЁ]\.)?"
)

_CATEGORY = {
    FIO: "FIO",
    ADDRESS: "ADDRESS",
    PHONE: "PHONE",
    EMAIL: "EMAIL",
    DOB: "DOB",
    AGE: "AGE",
}

def _fake_spans(text: str, names: list[str]) -> list[dict]:
    """Спаны как у hybrid: структурные правила + ФИО/deny-list, без torch."""
    found: list[tuple[str, int, int, str | None]] = []
    for span in structural_spans(text):
        if span.kind == DOC:
            found.append((DOC, span.start, span.end, span.category or "DOC"))
        else:
            found.append(
                (span.kind, span.start, span.end, _CATEGORY.get(span.kind))
            )
    for m in _CAP_FIO.finditer(text):
        found.append((FIO, m.start(), m.end(), "FIO"))
    for name in names:
        low_text, low_name = text.lower(), name.lower()
        pos = low_text.find(low_name)
        while pos >= 0:
            found.append((FIO, pos, pos + len(name), "FIO"))
            pos = low_text.find(low_name, pos + 1)
    return [
        {"category": category or kind, "start": start, "end": end}
        for kind, start, end, category in found
    ]

class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-ml/1"

    def do_POST(self):  # noqa: N802 (stdlib API)
        if self.path != "/v1/mask":
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw or b"{}")
        self.server.requests.append(
            {"headers": {k.lower(): v for k, v in self.headers.items()},
             "body": body}
        )
        text = str(body.get("text", ""))
        names = [str(n) for n in body.get("names") or []]
        payload = {
            "text": text,
            "spans": _fake_spans(text, names),
            "model": "fake-rubert",
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # тишина в stderr
        pass

@pytest.fixture
def fake_ml():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
@pytest.fixture(autouse=True)
def _restore_settings():
    saved = (
        settings.prod_mode,
        settings.depersonalize_ner,
        settings.ml_url,
        settings.ml_token,
        settings.ml_timeout,
    )
    yield
    (
        settings.prod_mode,
        settings.depersonalize_ner,
        settings.ml_url,
        settings.ml_token,
        settings.ml_timeout,
    ) = saved

def _use(fake_ml, token: str = "") -> None:
    settings.depersonalize_ner = "remote"
    settings.ml_url = f"http://127.0.0.1:{fake_ml.server_port}"
    settings.ml_token = token
    settings.ml_timeout = 2.0

def _closed_port_url() -> str:
    """Порт, который заведомо никто не слушает (только локально)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_port
    server.server_close()
    return f"http://127.0.0.1:{port}"

def test_remote_backend_selected():
    settings.depersonalize_ner = "remote"
    assert ner_backend() == "remote"
    settings.depersonalize_ner = "hybrid"
    assert ner_backend() == "hybrid"

def test_remote_masks_structural_formats(fake_ml):
    _use(fake_ml)
    text = (
        "Добрый день! Меня зовут Иванов Пётр Сергеевич, телефон 8 (926) 123-45-67, "
        "почта ivanov@example.com, СНИЛС 112-233-445 95, полис 1234 5678 9012 3454, "
        "медкарта № 77-004521, дата рождения 12.03.1954, "
        "адрес: г. Москва, ул. Арбат, д. 21, кв. 8"
    )
    out = scrub_text(text).text
    assert "[ФИО]" in out
    assert "[ТЕЛЕФОН]" in out
    assert "[EMAIL]" in out
    assert "[ДОКУМЕНТ]" in out
    assert "[ДАТА РОЖДЕНИЯ]" in out
    assert "[АДРЕС]" in out
    for leaked in (
        "Иванов",
        "123-45-67",
        "ivanov@example.com",
        "112-233-445",
        "77-004521",
        "12.03.1954",
        "Арбат",
    ):
        assert leaked not in out

def test_remote_masks_lowercase_fio_by_core_heuristic(fake_ml):
    """Строчное ФИО закрывает правило ядра, даже если sidecar его не видит.

    Бывший остаточный риск docs/10 R25: NER не размечает «сидорова галина».
    Теперь морфология фамилии + имя из списка частых срабатывают до NER.
    """
    _use(fake_ml)
    text = "Меня зовут сидорова галина, беспокоит тревога"
    result = scrub_text(text)
    assert result.text == "Меня зовут [ФИО], беспокоит тревога"
    assert [f.kind for f in result.findings] == [FIO]

def test_remote_passes_names_and_token(fake_ml):
    _use(fake_ml, token="secret-ml-token")
    text = "Пациентка Петрова Анна, 64 года"
    result = scrub_text(text, names=["Петрова Анна Ивановна"])
    assert "[ФИО]" in result.text
    assert result.text.endswith("[ВОЗРАСТ]")
    req = fake_ml.requests[0]
    assert req["body"]["names"] == ["Петрова Анна Ивановна"]
    assert req["headers"].get("x-cc-ml-token") == "secret-ml-token"

def test_remote_prod_unreachable_fail_closed_before_llm(monkeypatch):
    """prod + sidecar недоступен → 503, и ни одного вызова LLM."""
    calls: list[object] = []
    monkeypatch.setattr(
        llm_router,
        "chat_completion",
        lambda *a, **kw: calls.append((a, kw)) or "{}",
    )
    settings.prod_mode = True
    settings.depersonalize_ner = "remote"
    settings.ml_url = _closed_port_url()
    settings.ml_timeout = 0.5

    with pytest.raises(DepersonalizationUnavailable) as err:
        scrub_text("Меня зовут Иванов Пётр Сергеевич, звоните +79261234567")
    assert err.value.status == 503
    assert err.value.code == "DEPERSONALIZATION_UNAVAILABLE"
    assert calls == []

def test_remote_nonprod_unreachable_degrades_to_regex():
    settings.prod_mode = False
    settings.depersonalize_ner = "remote"
    settings.ml_url = _closed_port_url()
    settings.ml_timeout = 0.5
    out = scrub_text("Звоните +79261234567, почта a@b.ru").text
    assert "[ТЕЛЕФОН]" in out and "[EMAIL]" in out

def _prod_settings(**kwargs) -> Settings:
    base = dict(
        _env_file=None,
        prod_mode=True,
        allow_demo_secrets=False,
        require_otp=True,
        jwt_secret="x" * 48,
        data_key="y" * 48,
        assistant_llm_url="http://llm.invalid/v1",
    )
    base.update(kwargs)
    return Settings(**base)

def test_prod_settings_accept_remote_with_url():
    s = _prod_settings(depersonalize_ner="remote", ml_url="http://ml:8080")
    assert s.ml_url.endswith("8080")

def test_prod_settings_reject_remote_without_url():
    with pytest.raises(ValueError, match="CC_ML_URL"):
        _prod_settings(depersonalize_ner="remote", ml_url="")

def test_prod_settings_reject_off_with_egress():
    with pytest.raises(ValueError, match="remote"):
        _prod_settings(depersonalize_ner="off")

def test_prod_settings_accept_hybrid():
    _prod_settings(depersonalize_ner="hybrid")


_CORPUS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "bench" / "ner" / "clinical_contexts" / "corpus.jsonl"
)


def _corpus_rows() -> list[dict]:
    if not _CORPUS.exists():  # корпус мог не попасть в установку пакета
        pytest.skip("нет bench/ner/clinical_contexts/corpus.jsonl")
    return [
        json.loads(line) for line in _CORPUS.read_text("utf-8").splitlines()
        if line.strip()
    ]


def test_remote_matches_gold_on_clinical_corpus(fake_ml):
    """Клинический корпус: всё, что hybrid маскирует, маскирует и remote."""
    _use(fake_ml)
    rows = _corpus_rows()
    assert len(rows) >= 150
    for row in rows:
        out = scrub_text(row["text"]).text
        for gold in row["gold"]:
            if gold["kind"] == "AGE":
                continue  # возраст без ФИО рядом — клинические данные
            leaked = gold["value"]
            if gold["kind"] == "FIO" and leaked == leaked.lower():
                continue  # docs/10 R25: ФИО целиком строчными
            assert leaked not in out, (row["id"], gold, out)


def test_remote_lowercase_fio_masked_without_ner(fake_ml):
    """Бывшие 8 промахов hybrid/remote (docs/10 R25) закрыты правилом ядра.

    rubert внутри sidecar'а строчные ФИО не размечает; морфология фамилии +
    имя из списка частых маскирует их структурно, в том числе в remote-пути.
    """
    _use(fake_ml)
    low = [
        row for row in _corpus_rows()
        if any(
            g["kind"] == "FIO" and g["value"] == g["value"].lower()
            for g in row["gold"]
        )
    ]
    assert len(low) >= 8, [row["id"] for row in low]
    for row in low:
        out = scrub_text(row["text"]).text
        for gold in row["gold"]:
            assert gold["value"] not in out, (row["id"], gold, out)

@pytest.mark.parametrize(
    "spans",
    [
        [{"category": "FIO", "start": "0", "end": 6}],       # строковые смещения
        [{"category": "FIO", "start": 0, "end": 9999}],      # вне текста
        [{"category": "UNKNOWN", "start": 0, "end": 6}],     # чужая категория
        [{"category": "FIO", "start": 4, "end": 2}],         # перевёрнутый диапазон
        ["не объект"],                                        # мусор в списке
    ],
)
def test_malformed_sidecar_spans_fail_closed_in_prod(monkeypatch, spans):
    """Битый ответ сайдкара не должен давать «успешное» немаскирование.

    Раньше такие элементы молча выбрасывались, и текст уходил в LLM как есть.
    """
    from app.services import ml_client

    monkeypatch.setattr(
        ml_client, "mask",
        lambda text, names=None: {"text": text, "spans": spans, "model": "fake"},
    )
    settings.prod_mode = True
    settings.depersonalize_ner = "remote"
    settings.ml_url = "http://ml.invalid"
    with pytest.raises(DepersonalizationUnavailable) as err:
        scrub_text("Иванов Пётр, звоните +79261234567")
    assert err.value.status == 503

def test_malformed_sidecar_response_degrades_outside_prod(monkeypatch):
    from app.services import ml_client

    monkeypatch.setattr(
        ml_client, "mask",
        lambda text, names=None: {"text": text, "spans": [{"category": "NOPE", "start": 0, "end": 3}]},
    )
    settings.prod_mode = False
    settings.depersonalize_ner = "remote"
    settings.ml_url = "http://ml.invalid"
    out = scrub_text("Звоните +79261234567").text
    assert "[ТЕЛЕФОН]" in out  # regex-путь продолжает работать

def test_deny_list_capped_before_sidecar(monkeypatch):
    """Ростер на 200 ФИО не раздувает egress-запрос: клиент режет до 128."""
    from app.services import ml_client

    seen: list[int] = []

    def fake_post(path, payload):
        seen.append(len(payload["names"]))
        return {"text": payload["text"], "spans": [], "model": "fake"}

    monkeypatch.setattr(ml_client, "_post", fake_post)
    ml_client.mask("текст", [f"Фамилия{i} Имя Отчество" for i in range(200)])
    assert seen == [128]

def test_degraded_sidecar_fail_closed_in_prod(monkeypatch):
    """degraded=true (NER упал/не запускался) — это отказ backend'а в prod."""
    from app.services import ml_client

    monkeypatch.setattr(
        ml_client, "mask",
        lambda text, names=None: {
            "text": text, "spans": [], "model": "regex", "degraded": True,
        },
    )
    settings.prod_mode = True
    settings.depersonalize_ner = "remote"
    settings.ml_url = "http://ml.invalid"
    with pytest.raises(DepersonalizationUnavailable) as err:
        scrub_text("Иванов Пётр, звоните +79261234567")
    assert err.value.status == 503

def test_degraded_sidecar_degrades_to_regex_outside_prod(monkeypatch):
    from app.services import ml_client

    monkeypatch.setattr(
        ml_client, "mask",
        lambda text, names=None: {
            "text": text, "spans": [], "model": "regex", "degraded": True,
        },
    )
    settings.prod_mode = False
    settings.depersonalize_ner = "remote"
    settings.ml_url = "http://ml.invalid"
    out = scrub_text("Звоните +79261234567").text
    assert "[ТЕЛЕФОН]" in out  # regex-деградация, не сырой текст

def test_degraded_must_be_bool(monkeypatch):
    """Небулев degraded — тоже несоответствие контракту (strict parsing)."""
    from app.services import ml_client

    monkeypatch.setattr(
        ml_client, "mask",
        lambda text, names=None: {
            "text": text, "spans": [], "model": "hybrid", "degraded": "false",
        },
    )
    settings.prod_mode = True
    settings.depersonalize_ner = "remote"
    settings.ml_url = "http://ml.invalid"
    with pytest.raises(DepersonalizationUnavailable):
        scrub_text("Иванов Пётр, звоните +79261234567")

def test_split_fio_spans_are_merged_before_plausibility(monkeypatch):
    """Сайдкар режет «Иванов Пётр» + «Сергеевич» — отчество не должно утечь."""
    from app.services import ml_client

    text = "Меня зовут Иванов Пётр Сергеевич, перезвоните"
    monkeypatch.setattr(
        ml_client, "mask",
        lambda text, names=None: {
            "text": text,
            "spans": [
                {"category": "FIO", "start": 11, "end": 22},
                {"category": "FIO", "start": 22, "end": 32},
            ],
            "model": "fake",
        },
    )
    settings.prod_mode = False
    settings.depersonalize_ner = "remote"
    settings.ml_url = "http://ml.invalid"
    out = scrub_text(text).text
    assert "Иванов" not in out and "Пётр" not in out and "Сергеевич" not in out, out
    assert out.count("[ФИО]") == 1
