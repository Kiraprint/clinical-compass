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


class _SpyClient:
    """Фейк Qdrant: запоминает kwargs query_points, точек не возвращает."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def collection_exists(self, name) -> bool:  # как у настоящего QdrantClient
        return True

    def query_points(self, **kwargs):
        self.calls.append(kwargs)

        class _Res:
            points: list = []

        return _Res()


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


def test_off_path_is_byte_identical(monkeypatch):
    """CC_RERANKER=off и незаданный CC_RRF_K → ровно допираровская форма запроса.

    Проверяем сам объект, уходящий в Qdrant: `FusionQuery(Fusion.RRF)` — то, что
    слал main. `RrfQuery(k=60)` здесь означал бы молчаливую смену выдачи
    hash-профиля (P1 ревью), поэтому ассерт — точная форма, а не «новый дефолт
    равен самому себе».
    """
    from qdrant_client.http import models as qm

    client = _SpyClient()
    monkeypatch.setattr(settings, "rrf_k", None)
    kb.hybrid_search(
        client, HashEmbedder(), "порог тяжёлой депрессии PHQ-9", limit=2, candidates=None, reranker=None
    )
    sent = client.calls[0]["query"]
    assert isinstance(sent, qm.FusionQuery) and sent.fusion == qm.Fusion.RRF
    assert not isinstance(sent, qm.RrfQuery)

    # Явный профиль (CC_RRF_K=60) — единственный способ включить qm.RrfQuery.
    client.calls.clear()
    kb.hybrid_search(client, HashEmbedder(), "порог тяжёлой депрессии PHQ-9", limit=2, rrf_k=60)
    sent = client.calls[0]["query"]
    assert isinstance(sent, qm.RrfQuery) and sent.rrf.k == 60


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
    """search_kb передаёт порог профиля в hybrid_search; CC_KB_MIN_SCORE перекрывает.

    Сам порог применяется в hybrid_search ко всему пулу (тест ниже) — агент
    отвечает только за значение: дефолт профиля / явный override.
    """
    from app.services import agent as agent_mod

    class DenseOnly(HashEmbedder):
        use_sparse = False

    seen: dict = {}

    def _search(*args, **kwargs):
        seen.update(kwargs)
        return [
            {
                "guideline_id": "g-phq",
                "source_title": "Шкала PHQ-9",
                "topic": "ШКАЛЫ",
                "text": "Шкала PHQ-9, порог 20 баллов",
                "score": 0.5,
            }
        ]

    monkeypatch.setattr(
        agent_mod,
        "get_embedder",
        lambda: HashEmbedder() if settings.embedder == "hash" else DenseOnly(),
    )
    monkeypatch.setattr(agent_mod, "get_reranker", lambda: None)
    monkeypatch.setattr(agent_mod.kb_store, "hybrid_search", _search)
    monkeypatch.setattr(settings, "kb_min_score", None)
    factory = agent_mod.AgentFactory(client=None)

    monkeypatch.setattr(settings, "embedder", "bge-m3")  # профиль-дефолт 0.45
    assert factory.search_kb("шкала депрессии") != []
    assert seen["min_score"] == 0.45

    monkeypatch.setattr(settings, "embedder", "user-bge-m3")  # профиль-дефолт 0.55
    factory.search_kb("шкала депрессии")
    assert seen["min_score"] == 0.55

    monkeypatch.setattr(settings, "kb_min_score", 0.2)  # явный override
    factory.search_kb("шкала депрессии")
    assert seen["min_score"] == 0.2

    # hash-профиль гейтится лексически: dense-порог не передаётся.
    monkeypatch.setattr(settings, "embedder", "hash")
    factory.search_kb("шкала депрессии")
    assert seen["min_score"] is None


def test_search_kb_passes_multiple_topics_as_one_filter(monkeypatch):
    """FR-14: две темы уходят в hybrid_search одним вызовом (topics=...), не двумя."""
    from app.services import agent as agent_mod
    from app.services.embeddings import HashEmbedder

    calls: list[dict] = []

    def _search(*_a, **kw):
        calls.append(kw)
        return []

    monkeypatch.setattr(agent_mod, "get_embedder", lambda: HashEmbedder())
    monkeypatch.setattr(agent_mod.kb_store, "hybrid_search", _search)
    factory = agent_mod.AgentFactory(client=None)

    factory.search_kb("семаглутид и риск депрессии", topics=("ЛЕКАРСТВА", "НАУЧНЫЕ-СТАТЬИ"))

    assert len(calls) == 1, "должен быть один поиск, а не два"
    assert calls[0]["topics"] == ("ЛЕКАРСТВА", "НАУЧНЫЕ-СТАТЬИ")

def test_candidates_and_rrf_k_reach_qdrant(monkeypatch):
    captured: dict = {}

    class _Point:
        payload = {"guideline_id": "g", "source_title": "s", "topic": "t", "text": "x"}
        score = 0.9

    class _Res:
        points = [_Point()]

    class _Client:
        def collection_exists(self, name) -> bool:  # как у настоящего QdrantClient
            return True

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


def test_gate_uses_pool_best_not_reranked_top(monkeypatch):
    """Порог — по лучшему скору ПУЛА, а не по top-limit после реранка.

    Сценарий P2: лучший по первичному скору чанк (0.90 >= 0.45) выброшен
    реранком из top-5, у оставшихся retrieval_score 0.30–0.34 < порога. Гейт по
    top-limit включил бы ложный отказ «нет данных»; ответ обязан остаться
    обоснованным (есть цитаты).
    """
    from app.services import agent as agent_mod
    from compass.agent import build_rag_answer

    class DenseOnly(HashEmbedder):
        use_sparse = False

    class _Point:
        def __init__(self, gid: str, score: float) -> None:
            self.payload = {
                "guideline_id": gid,
                "source_title": f"Источник {gid}",
                "topic": "ШКАЛЫ",
                "text": f"Шкала PHQ-9: чанк {gid}",
            }
            self.score = score

    class _Client:
        def __init__(self, scores: list[float]) -> None:
            self._scores = scores

        def collection_exists(self, name) -> bool:  # как у настоящего QdrantClient
            return True

        def query_points(self, **kwargs):
            class _Res:
                pass

            res = _Res()
            res.points = [_Point(f"g-{i}", score) for i, score in enumerate(self._scores)]
            return res

    class _PushBestOut:
        """CE ставит пул в обратный порядок и режет top-limit: 0.90 уходит."""

        def rerank(self, query, hits, limit, *, exclude_ids=None):
            ranked = [
                {**h, "score": -1.0 - i, "retrieval_score": h["score"]}
                for i, h in enumerate(hits)
            ]
            return list(reversed(ranked))[:limit]

    monkeypatch.setattr(agent_mod, "get_embedder", lambda: DenseOnly())
    monkeypatch.setattr(agent_mod, "get_reranker", lambda: _PushBestOut())
    monkeypatch.setattr(settings, "embedder", "bge-m3")
    monkeypatch.setattr(settings, "kb_min_score", None)

    pool = [0.90, 0.34, 0.33, 0.32, 0.31, 0.30]
    chunks = agent_mod.AgentFactory(client=_Client(pool)).search_kb("шкала PHQ-9 порог", limit=5)
    assert chunks, "релевантный пул (0.90) не должен давать отказ"
    assert all(c.guideline_id != "g-0" for c in chunks)  # реранк реально отбросил лучший
    assert build_rag_answer("шкала PHQ-9 порог", chunks).grounded

    # Контроль: пул целиком ниже порога — отказ остаётся.
    off_topic = agent_mod.AgentFactory(
        client=_Client([0.40, 0.39, 0.38, 0.37, 0.36, 0.35])
    ).search_kb("шкала PHQ-9 порог", limit=5)
    assert off_topic == []
