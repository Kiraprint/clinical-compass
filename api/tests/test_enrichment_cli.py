"""CLI обогащения: команда reindex (перепубликация после сброса коллекции).

Qdrant подменяется фейковым модулем index (сеть не нужна), реестр — реальный
Postgres: проверяется, что публикуются только approved-работы, сбой одной
работы не отменяет остальные, а dry-run ничего не пишет.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.enrichment import CRITERIA_VERSION, cli, index
from app.models import KBWork

pytest.importorskip("qdrant_client")


def _args(*argv: str):
    return cli.build_parser().parse_args(["reindex", *argv])


def _work(
    db_session,
    *,
    status: str = "approved",
    guideline_id: str | None = None,
    criteria_version: str = CRITERIA_VERSION,
) -> KBWork:
    guideline = guideline_id or f"who-test-{uuid.uuid4().hex[:10]}"
    work = KBWork(
        kind="guideline",
        source="who",
        external_id=guideline,
        guideline_id=guideline,
        revision="2024#abcdef123456",
        content_hash="a" * 64,
        status=status,
        title="WHO guideline for depression",
        full_text="Recommendation text for depression and anxiety. " * 50,
        criteria={"version": criteria_version, "verdicts": {}},
        meta={
            "license": "CC BY-NC-SA 3.0 IGO",
            "url": f"https://iris.who.int/handle/{guideline}",
            "language": "en",
        },
    )
    db_session.add(work)
    db_session.commit()
    return work


@pytest.fixture()
def published(monkeypatch) -> list[str]:
    """Фейковый index.index_work: пишет вызовы, на *-bad падает."""
    calls: list[str] = []

    def fake_index_work(client, embedder, work, *, chunk_chars=1800, chunk_overlap=120):
        calls.append(work.guideline_id)
        if work.guideline_id.endswith("-bad"):
            raise RuntimeError("Qdrant недоступен")
        return 7

    monkeypatch.setattr(index, "index_work", fake_index_work)
    monkeypatch.setattr(cli, "_client", lambda: object())
    monkeypatch.setattr("app.services.embeddings.get_embedder", lambda: object())
    return calls


def test_reindex_republishes_only_approved(db_session, published, capsys):
    approved = _work(db_session)
    candidate = _work(db_session, status="candidate")

    code = asyncio.run(cli._cmd_reindex(_args()))

    assert code == 0
    assert published == [approved.guideline_id]
    assert "переиндексировано 1, пропущено 0, ошибок 0" in capsys.readouterr().out

    db_session.expire_all()
    row = db_session.get(KBWork, approved.id)
    assert row.chunk_count == 7
    assert row.indexed_hash == row.content_hash
    assert row.published_at is not None
    assert db_session.get(KBWork, candidate.id).chunk_count == 0


def test_reindex_failure_does_not_abort_the_rest(db_session, published, capsys):
    good = _work(db_session)
    bad = _work(db_session, guideline_id="who-test-bad")

    code = asyncio.run(cli._cmd_reindex(_args()))

    assert code == 1
    assert set(published) == {good.guideline_id, bad.guideline_id}
    captured = capsys.readouterr()
    assert "переиндексировано 1, пропущено 0, ошибок 1" in captured.out
    assert "who-test-bad" in captured.err

    db_session.expire_all()
    assert db_session.get(KBWork, good.id).chunk_count == 7
    assert db_session.get(KBWork, bad.id).chunk_count == 0


def test_reindex_dry_run_writes_nothing(db_session, published, capsys):
    work = _work(db_session)

    code = asyncio.run(cli._cmd_reindex(_args("--dry-run")))

    assert code == 0
    assert published == []
    out = capsys.readouterr().out
    assert "dry-run" in out and work.guideline_id in out
    db_session.expire_all()
    assert db_session.get(KBWork, work.id).chunk_count == 0


def test_reindex_work_id_skips_non_approved(db_session, published, capsys):
    work = _work(db_session, status="candidate")

    code = asyncio.run(cli._cmd_reindex(_args("--work-id", str(work.id))))

    assert code == 0
    assert published == []
    assert "переиндексировано 0, пропущено 1, ошибок 0" in capsys.readouterr().out


def test_reindex_work_id_targets_single_work(db_session, published, capsys):
    first = _work(db_session)
    second = _work(db_session)

    code = asyncio.run(cli._cmd_reindex(_args("--work-id", str(second.id))))

    assert code == 0
    assert published == [second.guideline_id]
    db_session.expire_all()
    assert db_session.get(KBWork, second.id).chunk_count == 7
    assert db_session.get(KBWork, first.id).chunk_count == 0


def test_cli_exposes_reindex_command(capsys):
    assert _args().func is cli._cmd_reindex

    with pytest.raises(SystemExit) as top:
        cli.main(["--help"])
    assert top.value.code == 0
    assert "reindex" in capsys.readouterr().out

    with pytest.raises(SystemExit) as sub:
        cli.main(["reindex", "--help"])
    assert sub.value.code == 0
    assert "--work-id" in capsys.readouterr().out

def test_reindex_skips_works_with_stale_criteria_version(db_session, published, capsys):
    """Работу, одобренную под старые критерии, вслепую не переопубликовываем."""
    work = _work(db_session, criteria_version="0.9.0")

    code = asyncio.run(cli._cmd_reindex(_args("--work-id", str(work.id))))

    assert code == 0
    assert published == []
    captured = capsys.readouterr()
    assert "переиндексировано 0, пропущено 1" in captured.out
    assert "критерии устарели" in captured.err


def test_reindex_rejects_malformed_work_id():
    with pytest.raises(SystemExit):
        _args("--work-id", "not-a-uuid")
