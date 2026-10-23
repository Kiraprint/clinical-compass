"""Тесты SPI эмбеддингов (docs/08 §4) и фабрики build_embedder.

Не требуют модели: используется HashEmbedder (детерминирован, офлайн).
Загрузка bge-m3 в тестах не выполняется — проверяются только конфигурационные
ветки (профили, ошибки зависимостей/устройства) и флаги стратегии поиска.
"""

import pytest

from app.config import settings
from app.services.embeddings import (
    PROFILES,
    BgeM3Embedder,
    EmbedderConfigError,
    HashEmbedder,
    build_embedder,
    get_embedder,
    reset_embedder,
)

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
        monkeypatch.setattr(settings, "embedder", "hash")
        assert isinstance(build_embedder(), HashEmbedder)

    def test_unknown_falls_back_to_hash(self, monkeypatch):
        # Любое другое значение конфига → hash (безопасный фолбэк).
        monkeypatch.setattr(settings, "embedder", "no-such-model")
        assert isinstance(build_embedder(), HashEmbedder)

    def test_explicit_profile_overrides_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "embedder", "no-such-model")
        assert isinstance(build_embedder(kind="hash"), HashEmbedder)

    def test_get_embedder_is_process_singleton(self, monkeypatch):
        monkeypatch.setattr(settings, "embedder", "hash")
        reset_embedder()
        try:
            first = get_embedder()
            assert first is get_embedder()
        finally:
            reset_embedder()

class TestProfiles:
    def test_profiles_match_expected_dense_sizes(self):
        # Размерность профиля = размерность коллекции Qdrant (docs/08 §3).
        assert PROFILES["hash"].dense_size == 512
        assert PROFILES["bge-m3"].dense_size == 1024
        assert PROFILES["bge-m3"].model_id == "BAAI/bge-m3"

    def test_search_strategy_flags(self):
        # hash: sparse — единственный точный сигнал; bge-m3: dense-only (RU-качество).
        assert HashEmbedder.use_sparse is True
        assert BgeM3Embedder.use_sparse is False

class TestBgeM3Config:
    def test_missing_optional_deps_raises_actionable_error(self, monkeypatch):
        # Нет FlagEmbedding → EmbedderConfigError с инструкцией, а не ImportError
        # в середине запроса (docs/08 §4).
        import sys

        monkeypatch.setitem(sys.modules, "FlagEmbedding", None)
        with pytest.raises(EmbedderConfigError) as exc:
            BgeM3Embedder()
        msg = str(exc.value)
        assert "embeddings" in msg
        assert "CC_EMBEDDER=hash" in msg

    def test_rejects_bad_device(self):
        with pytest.raises(EmbedderConfigError) as exc:
            BgeM3Embedder(device="tpu")
        assert "CC_EMBEDDER_DEVICE" in str(exc.value)

    def test_rejects_bad_batch_size(self):
        with pytest.raises(EmbedderConfigError) as exc:
            BgeM3Embedder(batch_size=0)
        assert "CC_EMBEDDER_BATCH_SIZE" in str(exc.value)
