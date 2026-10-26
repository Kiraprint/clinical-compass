"""Диаризация (FR-42, docs/18 §4.5): маппинг кластеров на сегменты — без GPU.

Настоящие веса (sherpa-onnx) в CI не грузятся: здесь проверяются чистые
функции маппинга, stub-бэкенд и честная деградация при отсутствии весов.
Качество модели измерено отдельно — REPORT-diarization.md.
"""

from __future__ import annotations

import importlib.util

import pytest

import models
from models import (
    DiarizeUnavailable,
    SherpaDiarizer,
    StubDiarizer,
    assign_speakers,
    diarize_labels,
    label_clusters,
)


def _turns(*spans) -> list[dict]:
    return [
        {"start": start, "end": end, "speaker": speaker} for start, end, speaker in spans
    ]


def _segments(*spans) -> list[dict]:
    return [
        {"start": start, "end": end, "speaker": None, "text": f"t{index}"}
        for index, (start, end) in enumerate(spans)
    ]


def test_diarize_labels_are_latin_letters():
    assert diarize_labels(0) == []
    assert diarize_labels(2) == ["A", "B"]
    assert diarize_labels(3) == ["A", "B", "C"]


def test_label_clusters_follow_first_appearance():
    """A — тот, кто заговорил первым: api назначает ему роль врача."""
    turns = _turns((5.0, 7.0, 1), (0.0, 2.0, 0), (9.0, 11.0, 1))
    assert label_clusters(turns) == {0: "A", 1: "B"}


def test_assign_speakers_uses_max_overlap():
    turns = _turns((0.0, 4.0, 5), (4.0, 10.0, 7))
    # Граница ровно на 4.0: перекрытие с первым — 1.5 с, со вторым — 3.5 с.
    segments = _segments((0.5, 2.0), (2.5, 7.5))
    assert assign_speakers(segments, turns) == [5, 7]


def test_assign_speakers_without_overlap_is_none():
    """Сегмент в тишине не получает «соседнего» спикера — это была бы выдумка."""
    turns = _turns((0.0, 2.0, 1), (6.0, 8.0, 2))
    assert assign_speakers(_segments((3.0, 5.0)), turns) == [None]


def test_assign_speakers_empty_turns():
    assert assign_speakers(_segments((0.0, 1.0), (1.0, 2.0)), []) == [None, None]


def test_stub_diarizer_alternates_every_five_seconds():
    turns = StubDiarizer().diarize(None, 12.0)
    assert [(turn["start"], turn["end"], turn["speaker"]) for turn in turns] == [
        (0.0, 5.0, 0),
        (5.0, 10.0, 1),
        (10.0, 12.0, 0),
    ]


def test_stub_diarizer_ignores_empty_audio():
    assert StubDiarizer().diarize(None, 0.0) == []


def test_sherpa_diarizer_without_weights_raises():
    """Пустой каталог = понятная причина деградации, а не невнятный ImportError.

    В образе ml стоят веса и sherpa-onnx; в CI без колеса тем же путём
    деградации остаётся ModuleNotFoundError — оба варианта реестр ловит.
    """
    with pytest.raises((DiarizeUnavailable, ModuleNotFoundError)):
        SherpaDiarizer("/nonexistent/diarize")


def test_registry_diarizer_caches_failure(monkeypatch):
    """Липкая ошибка: повторные запросы не пытаются грузить веса заново."""
    registry = models.Registry(stub=True)
    registry.stub = False
    registry.diarize_model_dir = "/nonexistent/diarize"
    assert registry.diarizer() is None
    assert registry._diarizer_status == "error"
    attempts = []
    monkeypatch.setattr(
        models, "SherpaDiarizer", lambda *a, **kw: attempts.append(a)
    )
    assert registry.diarizer() is None
    assert attempts == []


def test_unknown_backend_degrades_with_code():
    """Опечатка в CC_ML_DIARIZE_BACKEND — деградация с понятным кодом, не 500."""
    registry = models.Registry(stub=True)
    registry.stub = False
    registry.diarize_backend = "pyannote"
    assert registry.diarizer() is None
    assert registry._diarizer_status == "error"
    assert "diarize_backend_unavailable" in (registry._diarizer_error or "")


def test_sortformer_without_nemo_degrades():
    """Бэкенд sortformer без nemo_toolkit: diarized=false, причина — в логе."""
    if importlib.util.find_spec("nemo") is not None:
        pytest.skip("nemo_toolkit установлен: ветка деградации не воспроизводится")
    registry = models.Registry(stub=True)
    registry.stub = False
    registry.diarize_backend = "sortformer"
    assert registry.diarizer() is None
    assert "diarize_backend_unavailable" in (registry._diarizer_error or "")


def test_default_backend_is_sherpa(monkeypatch):
    """Дефолт — sherpa (ONNX, CPU): sortformer требует отдельного образа."""
    monkeypatch.delenv("CC_ML_DIARIZE_BACKEND", raising=False)
    assert models.Registry(stub=True).diarize_backend == "sherpa"
    monkeypatch.setenv("CC_ML_DIARIZE_BACKEND", "SortFormer")
    assert models.Registry(stub=True).diarize_backend == "sortformer"
