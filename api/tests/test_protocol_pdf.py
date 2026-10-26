"""FR-40 (docs/18 §4.5, §7): печатная форма протокола — PDF с отметкой «ПОДПИСАНО».

Проверяется: 409 до подписи, кириллический текст в PDF (извлекается pypdf),
блоки подписи обоих подписантов, кэширование артефакта (одинаковые байты на двух
GET) и аудит `SESSION_PROTOCOL_PDF`; рендер идёт в потоке anyio, не в event loop.
"""

from __future__ import annotations

import threading
from io import BytesIO

from pypdf import PdfReader
from sqlalchemy import text

from app.config import settings
from app.services import artifacts

from . import protocol_signing as ps
from .protocol_signing import BASE


def _pdf(client, context, session):
    return client.get(
        f"{BASE}/sessions/{session['id']}/protocol/pdf", headers=context["doctor"]
    )


def _text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _signed(client, context, session) -> dict:
    resp = client.post(
        f"{BASE}/sessions/{session['id']}/protocol/sign", headers=context["doctor"]
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_pdf_requires_signature(client):
    context = ps.ctx(client, "pdf-draft")
    session = ps.approved(client, context)

    resp = _pdf(client, context, session)
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "PROTOCOL_NOT_SIGNED"


def test_pdf_contains_russian_text_and_stamps(client):
    context = ps.ctx(client, "pdf-ok")
    session = ps.approved(client, context)
    signed = _signed(client, context, session)

    resp = _pdf(client, context, session)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"] == 'attachment; filename="protocol-1.pdf"'

    text = _text(resp.content)
    assert "ПОДПИСАНО" in text
    assert "Страница 1 из" in text
    # Содержательные разделы §5.1 и печатная форма (не пустая болванка).
    assert "тревога, бессонница" in text
    assert "F41.1" in text
    assert "сертралин" in text
    # Отметка о демонстрационном характере подписи (209н + честность демо).
    assert "УКЭП аккредитованного УЦ не выдавалась" in text

    signatures = {item["role"]: item for item in signed["signature"]["signatures"]}
    assert signatures["mo"]["fio"] == settings.signing_mo_fio
    for role in ("clinician", "mo"):
        assert signatures[role]["fio"] in text
        assert signatures[role]["cert_serial"] in text
        assert signatures[role]["cert_thumbprint"][:16] in text


def test_pdf_is_cached(client, db_session):
    context = ps.ctx(client, "pdf-cache")
    session = ps.approved(client, context)
    protocol_id = _signed(client, context, session)["id"]

    first = _pdf(client, context, session)
    second = _pdf(client, context, session)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content

    rows = db_session.execute(
        text("select id, sha256 from document_artifacts where kind = 'protocol_pdf'")
    ).all()
    assert len(rows) == 1  # второй GET не создал второй артефакт
    artifact = ps.artifact_row(db_session, "protocol_pdf", protocol_id)
    assert artifact is not None
    assert artifact.sha256 == rows[0][1]
    assert artifacts.get_bytes(artifact) == first.content


def test_pdf_audit_row(client, db_session):
    context = ps.ctx(client, "pdf-audit")
    session = ps.approved(client, context)
    _signed(client, context, session)
    assert _pdf(client, context, session).status_code == 200

    rows = db_session.execute(
        text("select object_id from audit_log where action = 'SESSION_PROTOCOL_PDF'")
    ).all()
    assert len(rows) == 1
    assert rows[0][0] == f"{session['id']}:v1"


def test_cached_pdf_read_is_audited(client, db_session):
    """Каждый GET подписанного документа — событие доступа, даже из кэша."""
    context = ps.ctx(client, "pdf-audit-cache")
    session = ps.approved(client, context)
    _signed(client, context, session)
    assert _pdf(client, context, session).status_code == 200
    assert _pdf(client, context, session).status_code == 200

    rows = db_session.execute(
        text("select object_id from audit_log where action = 'SESSION_PROTOCOL_PDF'")
    ).all()
    assert len(rows) == 2
    assert {row[0] for row in rows} == {f"{session['id']}:v1"}


def test_render_pdf_runs_off_the_event_loop(client, monkeypatch):
    """WeasyPrint синхронный и CPU-тяжёлый: рендер уходит в поток anyio.

    Иначе GET PDF замораживает event loop процесса на время рендера и все
    остальные запросы этого воркера ждут (docs/03 §12).
    """
    from app.services import documents

    render_threads: list[str] = []
    real = documents.render_pdf

    def spy(**kwargs):
        render_threads.append(threading.current_thread().name)
        return real(**kwargs)

    monkeypatch.setattr(documents, "render_pdf", spy)

    context = ps.ctx(client, "pdf-thread")
    session = ps.approved(client, context)
    _signed(client, context, session)

    assert _pdf(client, context, session).status_code == 200
    assert render_threads, "render_pdf не вызывался"
    assert render_threads[0].startswith("AnyIO worker"), render_threads[0]
