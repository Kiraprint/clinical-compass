"""Тесты SPI эмбеддингов (docs/08 §4) и фабрики build_embedder.

Не требуют модели: используется HashEmbedder (детерминирован, офлайн).
"""

import pytest

from app.services.embeddings import HashEmbedder, build_embedder


class TestHashEmbedder:
    def test_dense_deterministic_and_normalized(self):
        h = HashEmbedder()
        a = h.dense("повышенная тревожность и ГАД")
        b = h.dense("повышенная тревожность и ГАД")
        assert a == b
        norm = sum(x * x for x in a) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-6)

    def test_dense_size_matches(self):
        assert HashEmbedder().dense_size == 512
        assert len(HashEmbedder().dense("тест")) == 512

    def test_sparse_returns_indices_and_values(self):
        idx, val = HashEmbedder().sparse("шкала PHQ-9")
        assert isinstance(idx, list) and isinstance(val, list)
        assert len(idx) == len(val)
        assert len(idx) > 0

    def test_dense_empty_string_is_zero(self):
        v = HashEmbedder().dense("")
        assert all(x == 0.0 for x in v)

    def test_semantic_trigram_helps(self):
        # 3-граммы слов дают ненулевое сходство при разных падежах.
        h = HashEmbedder()
        a = h.dense("повышенная тревожность")
        b = h.dense("повышенная тревожность симптом")
        dot = sum(x * y for x, y in zip(a, b))
        assert dot > 0.0


class TestBuildEmbedder:
    def test_default_is_hash(self, monkeypatch):
        # CC_EMBEDDER = hash (дефолт) → офлайн-эмбеддер для демо/тестов.
        from app.config import settings

        monkeypatch.setattr(settings, "embedder", "hash")
        assert isinstance(build_embedder(), HashEmbedder)

    def test_unknown_falls_back_to_hash(self, monkeypatch):
        # Любое другое значение конфига → hash (безопасный фолбэк).
        from app.config import settings

        monkeypatch.setattr(settings, "embedder", "no-such-model")
        assert isinstance(build_embedder(), HashEmbedder)
