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

    # Потолок сайдкара (ml/models.py MAX_TEXTS): больше — 4xx, клиент обязан резать.
    max_texts = 64

    def embed(self, texts, *, kind="query", profile=""):
        self._guard()
        self.calls.append(("embeddings", {"kind": kind, "profile": profile, "texts": list(texts)}))
        if len(texts) > self.max_texts:
            raise MlUnavailable(
                f"/v1/embeddings: не больше {self.max_texts} текстов, пришло {len(texts)}"
            )
        return [self._vector_for(t) for t in texts]

    def _vector_for(self, text: str) -> list[float]:
        """Вектор, кодирующий номер текста: так проверяется порядок ответа."""
        tail = text.rpartition("-")[2]
        lead = float(tail) if tail.isdigit() else 0.1
        return [lead] + [0.0] * (self.dense_size - 1)

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

    def __init__(self, docs: list[dict], scores: list[float] | None = None) -> None:
        self._docs = docs
        self._scores = scores
        self.last_kwargs: dict | None = None

    def query_points(self, **kwargs):
        self.last_kwargs = kwargs
        scores = self._scores or [round(0.9 - 0.1 * i, 3) for i in range(len(self._docs))]
        return _Response(
            [_Point({**d, "chunk_index": 0, "text_hash": "x"}, score=s) for d, s in zip(self._docs, scores)]
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


def test_remote_rerank_honours_window(monkeypatch):
    """CC_RERANK_WINDOW режет текст ДО отправки в сайдкар, как у локального CE."""
    docs = [
        {"guideline_id": "g-long", "source_title": "Длинный чанк", "topic": "ЛЕКАРСТВА",
         "text": "z" * 200 + " сертралин доза" + "z" * 100},
        {"guideline_id": "g-short", "source_title": "Короткий", "topic": "ЛЕКАРСТВА",
         "text": "сертралин"},
    ]
    ml = FakeMl()
    _patch(monkeypatch, ml)
    monkeypatch.setattr(settings, "rerank_window", 40)
    kb.hybrid_search(
        FakeQdrant(docs), _Hash(), "сертралин доза", limit=2, reranker=RemoteReranker()
    )
    sent = [call[1]["documents"] for call in ml.calls if call[0] == "rerank"][0]
    assert all(len(d) <= 40 for d in sent), "окно не применено к тексту для сайдкара"
    assert any("сертр" in d for d in sent), "окно должно содержать слово запроса"


def test_remote_embedder_batches_passages(fake_client):
    """Индексация шлёт пачку одним POST /v1/embeddings, не по запросу на чанк."""
    emb = RemoteEmbedder("qwen3-0.6b", expected_dense_size=1024)
    texts = ["чанк один", "чанк два", "чанк три"]
    vectors = emb.dense_batch(texts)
    assert len(vectors) == 3 and all(len(v) == 1024 for v in vectors)
    embed_calls = [c for c in fake_client.calls if c[0] == "embeddings"]
    assert len(embed_calls) == 1
    assert embed_calls[0][1]["texts"] == texts
    # Повторный вызов — из кеша, без сети.
    emb.dense_batch(texts)
    assert len([c for c in fake_client.calls if c[0] == "embeddings"]) == 1

def test_remote_embedder_splits_sidecar_batches(fake_client):
    """Пачки режет клиент: сайдкар отбивает >64 текстов (4xx не ретраится)."""
    emb = RemoteEmbedder("qwen3-0.6b", expected_dense_size=1024)
    texts = [f"чанк-{i}" for i in range(65)]
    vectors = emb.dense_batch(texts)
    assert len(vectors) == 65
    sizes = [len(c[1]["texts"]) for c in fake_client.calls if c[0] == "embeddings"]
    assert sizes == [64, 1]

def test_remote_embedder_more_texts_than_cache(fake_client):
    """300 текстов > FIFO-кеша (256): ответ не перечитывается из кеша, порядок цел."""
    emb = RemoteEmbedder("qwen3-0.6b", expected_dense_size=1024)
    texts = [f"чанк-{i}" for i in range(300)]
    vectors = emb.dense_batch(texts)
    assert [v[0] for v in vectors] == [float(i) for i in range(300)]
    sizes = [len(c[1]["texts"]) for c in fake_client.calls if c[0] == "embeddings"]
    assert sizes == [64, 64, 64, 64, 44]


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


def test_remote_rerank_gate_uses_pool_cosine_not_sidecar_scores(monkeypatch):
    """Порог профиля — по косинусу пула, а не по скору сайдкара (CE-логит)."""
    client = FakeQdrant(DOCS)
    ml = FakeMl(scores=[-5.0, 1.0, 2.0])
    _patch(monkeypatch, ml)
    hits = kb.hybrid_search(
        client, _Hash(), "шкала PHQ-9", limit=3, reranker=RemoteReranker(), min_score=0.45
    )
    # score — скор сайдкара, retrieval_score — косинус первичного поиска
    assert hits[0]["score"] == 2.0
    assert hits[0]["retrieval_score"] == 0.7

    # Пул целиком ниже порога — отказ, несмотря на положительные скоры сайдкара.
    off_topic = FakeQdrant(DOCS, scores=[0.40, 0.30, 0.20])
    assert (
        kb.hybrid_search(
            off_topic, _Hash(), "шкала PHQ-9", limit=3, reranker=RemoteReranker(), min_score=0.45
        )
        == []
    )


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
