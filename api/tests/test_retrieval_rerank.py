"""Реранк выдачи КБ и per-profile порог (docs/08 §4, FR-17).

Проверяем ровно контракт wiring-а, без скачивания моделей:
- CC_RERANKER=off → реранкера нет, выдача байт-в-байт как до изменения;
- исключённые врачом источники не воскресают после реранка (фильтр в реранкере
  и повторный фильтр в kb.hybrid_search);
- порог «нет релевантного ответа» берётся из профиля эмбеддера, если
  CC_KB_MIN_SCORE не задан явно;
- CC_RRF_K / CC_RETRIEVAL_CANDIDATES доезжают до запроса в Qdrant.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import kb
from app.services.embeddings import HashEmbedder, profile_default_min_score
from app.services.reranker import CrossEncoderReranker, build_reranker, get_reranker, reset_reranker


def _qdrant_available() -> bool:
    try:
        from qdrant_client import QdrantClient

        QdrantClient(url=settings.qdrant_url, timeout=2).get_collections()
        return True
    except Exception:  # noqa: BLE001 — Qdrant не поднят (локальный прогон без стека)
        return False


needs_qdrant = pytest.mark.skipif(not _qdrant_available(), reason="Qdrant недоступен (CC_QDRANT_URL)")

DOCS = [
    {
        "guideline_id": "g-phq",
        "source_title": "Шкала PHQ-9",
        "topic": "ШКАЛЫ",
        "text": "Шкала PHQ-9, порог тяжёлой депрессии 20 баллов, суицидальный пункт 9",
    },
    {
        "guideline_id": "g-gad",
        "source_title": "Шкала GAD-7",
        "topic": "ШКАЛЫ",
        "text": "Шкала GAD-7 измеряет генерализованное тревожное расстройство за две недели",
    },
]


class _StubCrossEncoder:
    """Заглушка CrossEncoder: детерминированный скор по пересечению слов."""

    def predict(self, pairs, **kwargs):
        out = []
        for query, text in pairs:
            q = {w for w in query.lower().split() if len(w) > 3}
            t = {w for w in text.lower().split() if len(w) > 3}
            out.append(float(len(q & t)))
        return out


class _Reranker:
    name = "bge-reranker-v2-m3"

    def __init__(self) -> None:
        self.seen: set[str] | None = None

    def rerank(self, query, hits, limit, *, exclude_ids=None):
        self.seen = exclude_ids
        return list(reversed(hits))[:limit]


# --------------------------------------------------------------- off-path
def test_reranker_off_is_none(monkeypatch):
    """CC_RERANKER=off (дефолт) — реранкера нет, модель не грузится."""
    monkeypatch.setattr(settings, "reranker", "off")
    reset_reranker()
    try:
        assert build_reranker("off") is None
        assert build_reranker("") is None
        assert build_reranker("none") is None
        assert get_reranker() is None
    finally:
        reset_reranker()


@needs_qdrant
def test_off_path_is_byte_identical(monkeypatch):
    """Без реранкера выдача та же, что при явных исторических параметрах."""
    from qdrant_client import QdrantClient

    probe = "kb_test_rerank_off"
    client = QdrantClient(url=settings.qdrant_url, timeout=10)
    if client.collection_exists(probe):
        client.delete_collection(probe)
    monkeypatch.setattr(kb, "COLLECTION", probe)
    try:
        kb.upsert_documents(client, HashEmbedder(), DOCS)
        legacy = kb.hybrid_search(client, HashEmbedder(), "порог тяжёлой депрессии PHQ-9", limit=2)
        explicit = kb.hybrid_search(
            client,
            HashEmbedder(),
            "порог тяжёлой депрессии PHQ-9",
            limit=2,
            candidates=None,
            reranker=None,
            rrf_k=settings.rrf_k,
        )
        assert legacy == explicit
        assert legacy and legacy[0]["guideline_id"] == "g-phq"
    finally:
        monkeypatch.undo()
        client.delete_collection(probe)


# ------------------------------------------------------------- exclude_ids
def test_cross_encoder_reranker_drops_excluded_before_scoring():
    """Реранкер фильтрует исключённые источники сам (до скоринга)."""
    rr = CrossEncoderReranker.__new__(CrossEncoderReranker)  # без загрузки весов
    rr.model_id = "stub"
    rr._m = _StubCrossEncoder()
    rr._batch = 8
    hits = [
        {"guideline_id": "g-gad", "text": "GAD тревога беспокойство", "score": 0.9},
        {"guideline_id": "g-phq", "text": "PHQ депрессия порог", "score": 0.5},
    ]
    out = rr.rerank("депрессия порог", hits, 2, exclude_ids={"g-gad"})
    assert [h["guideline_id"] for h in out] == ["g-phq"]


@needs_qdrant
def test_excluded_ids_never_returned_after_rerank(monkeypatch):
    """Исключённый врачом источник не возвращается даже «доброжелательным» реранком."""
    from qdrant_client import QdrantClient

    probe = "kb_test_rerank_exclude"
    client = QdrantClient(url=settings.qdrant_url, timeout=10)
    if client.collection_exists(probe):
        client.delete_collection(probe)
    monkeypatch.setattr(kb, "COLLECTION", probe)
    reranker = _Reranker()
    try:
        kb.upsert_documents(client, HashEmbedder(), DOCS)
        hits = kb.hybrid_search(
            client,
            HashEmbedder(),
            "шкала депрессии PHQ-9",
            limit=2,
            exclude_ids={"g-gad"},
            reranker=reranker,
        )
        assert reranker.seen == {"g-gad"}
        assert all(h["guideline_id"] != "g-gad" for h in hits)

        # Реренкер-нарушитель: возвращает исключённое — kb фильтрует повторно.
        class _Violator(_Reranker):
            def rerank(self, query, hits, limit, *, exclude_ids=None):
                return [{"guideline_id": "g-gad", "text": "GAD", "score": 1.0}, *hits][:limit]

        hits = kb.hybrid_search(
            client, HashEmbedder(), "шкала депрессии PHQ-9", limit=2,
            exclude_ids={"g-gad"}, reranker=_Violator(),
        )
        assert all(h["guideline_id"] != "g-gad" for h in hits)
    finally:
        monkeypatch.undo()
        client.delete_collection(probe)


# ----------------------------------------------------------- profile gate
def test_profile_default_scores_are_per_profile(monkeypatch):
    assert profile_default_min_score("bge-m3") == 0.45
    assert profile_default_min_score("user-bge-m3") != profile_default_min_score("bge-m3")
    # неизвестный профиль — консервативный дефолт, без исключения
    assert profile_default_min_score("nope") == 0.45


def test_agent_uses_profile_gate_and_explicit_override(monkeypatch):
    """search_kb режет выдачу порогом профиля; CC_KB_MIN_SCORE перекрывает его."""
    from app.services import agent as agent_mod

    class DenseOnly(HashEmbedder):
        use_sparse = False

    def _hits(score: float) -> list[dict]:
        return [
            {
                "guideline_id": "g-phq",
                "source_title": "Шкала PHQ-9",
                "topic": "ШКАЛЫ",
                "text": "порог тяжёлой депрессии 20 баллов",
                "score": score,
            }
        ]

    monkeypatch.setattr(agent_mod, "get_embedder", lambda: DenseOnly())
    monkeypatch.setattr(agent_mod, "get_reranker", lambda: None)
    monkeypatch.setattr(agent_mod.kb_store, "hybrid_search", lambda *a, **kw: _hits(0.5))
    monkeypatch.setattr(settings, "kb_min_score", None)
    factory = agent_mod.AgentFactory(client=None)

    monkeypatch.setattr(settings, "embedder", "bge-m3")  # профиль-дефолт 0.45
    assert factory.search_kb("шкала депрессии") != []

    monkeypatch.setattr(settings, "embedder", "user-bge-m3")  # 0.55 > 0.5
    assert factory.search_kb("шкала депрессии") == []

    monkeypatch.setattr(settings, "kb_min_score", 0.2)  # явный CC_KB_MIN_SCORE
    assert factory.search_kb("шкала депрессии") != []


# ----------------------------------------------------- candidates / rrf_k
def test_candidates_and_rrf_k_reach_qdrant(monkeypatch):
    captured: dict = {}

    class _Point:
        payload = {"guideline_id": "g", "source_title": "s", "topic": "t", "text": "x"}
        score = 0.9

    class _Res:
        points = [_Point()]

    class _Client:
        def query_points(self, **kwargs):
            captured.update(kwargs)
            return _Res()

    class DenseOnly(HashEmbedder):
        use_sparse = False

    monkeypatch.setattr(settings, "rrf_k", 30)
    kb.hybrid_search(_Client(), HashEmbedder(), "rrf", limit=5, candidates=100, rrf_k=None)
    assert captured["limit"] == 100
    assert captured["query"].rrf.k == 30
    assert [p.limit for p in captured["prefetch"]] == [100, 100]

    # dense-only профиль: глубина пула — candidates, но sparse не запрашивается
    captured.clear()
    kb.hybrid_search(_Client(), DenseOnly(), "rrf", limit=5, candidates=40)
    assert captured["limit"] == 40 and captured["using"] == kb.DENSE_NAME
    assert "prefetch" not in captured


def test_st_profile_without_extra_gives_actionable_error():
    """Профиль qwen3-0.6b без sentence-transformers → понятная ошибка, не 500."""
    from app.services.embeddings import EmbedderConfigError, build_embedder

    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        with pytest.raises(EmbedderConfigError) as exc:
            build_embedder(kind="qwen3-0.6b")
        assert "extra `embeddings`" in str(exc.value)
    else:  # окружение с моделью: проверяем только размерность профиля
        pytest.skip("sentence-transformers установлен — тест про отсутствие extra")


def test_gate_uses_retrieval_score_after_rerank():
    """Порог профиля сравнивается со скором первичного поиска, не с logit-ом реранка."""
    from app.services.agent import _dense_top_gate

    reranked = [
        {"guideline_id": "a", "text": "x", "score": -7.5, "retrieval_score": 0.71},
        {"guideline_id": "b", "text": "y", "score": -9.1, "retrieval_score": 0.52},
    ]
    assert _dense_top_gate(reranked, 0.45) == reranked
    off_topic = [{"guideline_id": "a", "text": "x", "score": 3.2, "retrieval_score": 0.41}]
    assert _dense_top_gate(off_topic, 0.45) == []
    assert _dense_top_gate([], 0.45) == []
