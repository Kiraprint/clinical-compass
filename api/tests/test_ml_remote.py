"""Клиент ML-сайдкара: remote-эмбеддер и remote-реранк (docs/08 §4.7).

Проверяем контракт без GPU и без сайдкара: функции клиента
(`app.services.ml_client.ping/embed/rerank/enabled`) подменяются фейками
рядом с самим модулем, Qdrant — фейковым
`query_points`. Покрытие:
- размерность из сайдкара должна совпадать с профилем (иначе громкая ошибка);
- `remote`-профиль передаёт id профиля и kind=query|passage;
- сайдкар упал → выдача в порядке RRF, без исключения наверх;
- исключённые врачом источники не воскресают remote-реранком;
- порог профиля считается по скору первичного поиска, а не по скору сайдкара.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import kb, ml_client
from app.services.embeddings import EmbedderConfigError, RemoteEmbedder
from app.services.ml_client import MlUnavailable
from app.services.reranker import RemoteReranker

DOCS = [
    {"guideline_id": "g-a", "source_title": "Шкала PHQ-9", "topic": "ШКАЛЫ", "text": "PHQ-9 порог 20 баллов"},
    {"guideline_id": "g-b", "source_title": "Шкала GAD-7", "topic": "ШКАЛЫ", "text": "GAD-7 тревога две недели"},
    {"guideline_id": "g-c", "source_title": "Сертралин", "topic": "ЛЕКАРСТВА", "text": "сертралин доза 50 мг"},
]


class FakeMl:
    """Фейк функций контракта сайдкара: те же имена, что в ml_client."""

    def __init__(self, *, dense_size: int = 1024, scores: list[float] | None = None, down: bool = False):
        self.base_url = "http://ml-test:8080"
        self.dense_size = dense_size
        self._scores = scores
        self.down = down
        self.calls: list[tuple[str, dict]] = []

    def _guard(self) -> None:
        if self.down:
            raise MlUnavailable("connection refused")

    def ping(self):
        self._guard()
        return {"status": "ok", "device": "cuda", "models": {"qwen3-0.6b": {"dense_size": self.dense_size}}}

    def embed(self, texts, *, kind="query", profile=""):
        self._guard()
        self.calls.append(("embeddings", {"kind": kind, "profile": profile, "texts": list(texts)}))
        return [[0.1] * self.dense_size for _ in texts]

    def rerank(self, query, documents, *, top_k=None):
        self._guard()
        self.calls.append(("rerank", {"query": query, "documents": list(documents), "top_k": top_k}))
        if self._scores is not None:
            return self._scores
        return [float(len(documents) - i) for i in range(len(documents))]


class _Point:
    def __init__(self, payload: dict, score: float) -> None:
        self.payload = payload
        self.score = score


class _Response:
    def __init__(self, points: list[_Point]) -> None:
        self.points = points


class FakeQdrant:
    """Фейк QdrantClient: `query_points` возвращает заранее заданные точки."""

    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs
        self.last_kwargs: dict | None = None

    def query_points(self, **kwargs):
        self.last_kwargs = kwargs
        return _Response(
            [_Point({**d, "chunk_index": 0, "text_hash": "x"}, score=round(0.9 - 0.1 * i, 3)) for i, d in enumerate(self._docs)]
        )


def _patch(monkeypatch, fake: "FakeMl") -> None:
    """Подменить функции клиента фейком (контракт тот же, сети нет)."""
    monkeypatch.setattr(ml_client, "enabled", lambda: True)
    monkeypatch.setattr(ml_client, "ping", fake.ping)
    monkeypatch.setattr(ml_client, "embed", fake.embed)
    monkeypatch.setattr(ml_client, "rerank", fake.rerank)
    monkeypatch.setattr(settings, "ml_url", fake.base_url)


@pytest.fixture
def fake_client(monkeypatch):
    fake = FakeMl()
    monkeypatch.setattr(ml_client, "enabled", lambda: True)
    monkeypatch.setattr(ml_client, "ping", fake.ping)
    monkeypatch.setattr(ml_client, "embed", fake.embed)
    monkeypatch.setattr(ml_client, "rerank", fake.rerank)
    monkeypatch.setattr(settings, "ml_url", fake.base_url)
    return fake


def test_remote_embedder_dimension_mismatch_raises(fake_client):
    fake_client.dense_size = 768  # сайдкар отдал другую модель
    with pytest.raises(EmbedderConfigError) as exc:
        RemoteEmbedder("qwen3-0.6b", expected_dense_size=1024)
    assert "768" in str(exc.value) and "1024" in str(exc.value)


def test_remote_embedder_without_url_raises(monkeypatch):
    monkeypatch.setattr(settings, "ml_url", "")
    monkeypatch.setattr(ml_client, "enabled", lambda: False)
    with pytest.raises(EmbedderConfigError) as exc:
        RemoteEmbedder("qwen3-0.6b", expected_dense_size=1024)
    assert "CC_ML_URL" in str(exc.value)


def test_remote_embedder_passes_profile_and_kind(fake_client):
    emb = RemoteEmbedder("qwen3-0.6b", expected_dense_size=1024)
    assert emb.dense_size == 1024
    assert len(emb.dense("пассаж")) == 1024
    assert len(emb.query_dense("запрос")) == 1024
    kinds = [(c[1]["kind"], c[1]["profile"]) for c in fake_client.calls]
    assert ("passage", "qwen3-0.6b") in kinds
    assert ("query", "qwen3-0.6b") in kinds


def test_sidecar_down_degrades_to_rrf_order(monkeypatch):
    """Падение сайдкара не роняет поиск: выдача совпадает с путём без реранка."""
    client = FakeQdrant(DOCS)
    planned = kb.hybrid_search(client, _Hash(), "шкала PHQ-9", limit=3)
    down = FakeMl(down=True)
    _patch(monkeypatch, down)
    reranker = RemoteReranker()
    assert [h["guideline_id"] for h in kb.hybrid_search(client, _Hash(), "шкала PHQ-9", limit=3, reranker=reranker)] == [
        h["guideline_id"] for h in planned
    ]


def test_remote_rerank_never_resurrects_excluded(monkeypatch):
    client = FakeQdrant(DOCS)
    # сайдкар ставит исключённый документ первым — фильтр всё равно обязан его убрать
    scores = [1.0, 2.0, 3.0]
    ml = FakeMl(scores=scores)
    _patch(monkeypatch, ml)
    hits = kb.hybrid_search(
        client, _Hash(), "шкала тревоги", limit=2, exclude_ids={"g-c"}, reranker=RemoteReranker()
    )
    assert all(h["guideline_id"] != "g-c" for h in hits)
    assert all("g-c" not in [d for d in call[1]["documents"]] for call in ml.calls if call[0] == "rerank")


def test_remote_rerank_uses_retrieval_score_for_gate(monkeypatch):
    client = FakeQdrant(DOCS)
    ml = FakeMl(scores=[-5.0, 1.0, 2.0])
    _patch(monkeypatch, ml)
    hits = kb.hybrid_search(client, _Hash(), "шкала PHQ-9", limit=3, reranker=RemoteReranker())
    from app.services.agent import _dense_top_gate

    # score — скор сайдкара, retrieval_score — косинус первичного поиска
    assert hits[0]["score"] == 2.0
    assert hits[0]["retrieval_score"] == 0.7
    assert _dense_top_gate(hits, 0.45)  # порог смотрит на retrieval_score, а не на logit


def test_ml_client_requires_url(monkeypatch):
    """Без CC_ML_URL клиент падает MlUnavailable — второй клиент не заводим."""
    monkeypatch.setattr(settings, "ml_url", "")
    assert ml_client.enabled() is False
    with pytest.raises(MlUnavailable):
        ml_client.embed(["x"])


class _Hash:
    """Лёгкий эмбеддер-заглушка (для hybrid_search нужен только dense-вектор)."""

    dense_size = 8
    use_sparse = False

    def dense(self, text: str):
        return [0.1] * self.dense_size

    def sparse(self, text: str):
        return [], []
