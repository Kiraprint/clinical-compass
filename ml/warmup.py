"""Офлайн-прогрев весов в том ml-models (docker compose ... run --rm ml-warmup).

Сеть у сервиса ``ml`` внутренняя (без egress), поэтому веса скачиваются
отдельным одноразовым контейнером, у которого egress есть. Здесь только
huggingface_hub: torch не импортируется, модели не загружаются в память.

Скачиваются все профили эмбеддингов из CC_ML_PROFILES (встроенные + override),
реранкер CC_ML_RERANK_MODEL и NER-модель CC_ML_NER_MODEL, а при CC_ML_ASR=1 —
ещё веса ASR CC_ML_ASR_MODEL: ровно те, что сайдкар потом откроет с
HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1. Диаризация (FR-42, CC_ML_DIARIZE)
качается не с HF, а из релизов k2-fsa на GitHub — в каталог
CC_ML_DIARIZE_MODEL_DIR тома ml-models; файлы сверяются по sha256.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

# Контейнер warmup'а — единственный, кому разрешён egress к HF.
os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["TRANSFORMERS_OFFLINE"] = "0"

from huggingface_hub import snapshot_download  # noqa: E402 — после сброса offline

from models import (  # noqa: E402
    DIARIZE_EMBEDDING_FILE,
    DIARIZE_EMBEDDING_SHA256,
    DIARIZE_EMBEDDING_URL,
    DIARIZE_MODEL_DIR,
    DIARIZE_SEGMENTATION_ARCHIVE_SHA256,
    DIARIZE_SEGMENTATION_FILE,
    DIARIZE_SEGMENTATION_SHA256,
    DIARIZE_SEGMENTATION_URL,
    DEFAULT_PROFILE,
    _ASR_MODEL,
    _NER_MODEL,
    _RERANK_MODEL,
    _env_bool,
    load_profiles,
)

def model_ids() -> list[str]:
    """Все модели, которые нужны сайдкару; порядок стабилен, дубли убраны."""
    profiles = load_profiles()
    default = os.environ.get("CC_ML_DEFAULT_PROFILE", "").strip() or DEFAULT_PROFILE
    if default not in profiles:
        raise SystemExit(
            f"CC_ML_DEFAULT_PROFILE={default!r} отсутствует в профилях "
            f"({', '.join(sorted(profiles))})"
        )
    ids = [profiles[default].model_id]
    ids += [profile.model_id for profile in profiles.values()]
    ids.append(os.environ.get("CC_ML_RERANK_MODEL", "").strip() or _RERANK_MODEL)
    ids.append(os.environ.get("CC_ML_NER_MODEL", "").strip() or _NER_MODEL)
    # Веса ASR качаем только при включённой транскрипции: 1.6 ГБ в томе
    # ml-models не нужны стеку, который её не использует.
    if _env_bool("CC_ML_ASR"):
        ids.append(os.environ.get("CC_ML_ASR_MODEL", "").strip() or _ASR_MODEL)
    seen: set[str] = set()
    return [model_id for model_id in ids if not (model_id in seen or seen.add(model_id))]


def diarize_dir() -> str:
    """Каталог весов диаризации: CC_ML_DIARIZE_MODEL_DIR (том ml-models)."""
    return os.environ.get("CC_ML_DIARIZE_MODEL_DIR", "").strip() or DIARIZE_MODEL_DIR


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch(url: str, dest: str, expected_sha256: str) -> None:
    """Скачать файл в dest и сверить sha256 (иначе — исключение)."""
    with urllib.request.urlopen(url, timeout=120) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)
    actual = _sha256(dest)
    if actual != expected_sha256:
        os.unlink(dest)
        raise RuntimeError(f"sha256 {url}: ожидался {expected_sha256}, получен {actual}")


def fetch_diarize(target_dir: str) -> list[str]:
    """Веса диаризации в target_dir; возвращает список пропущенных файлов.

    Идемпотентно: уже скачанные файлы с верным хэшем не перекачиваются
    (34 МБ из GitHub на каждом прогреве не нужны).
    """
    os.makedirs(target_dir, exist_ok=True)
    segmentation = os.path.join(target_dir, DIARIZE_SEGMENTATION_FILE)
    embedding = os.path.join(target_dir, DIARIZE_EMBEDDING_FILE)
    skipped: list[str] = []

    if os.path.isfile(segmentation) and _sha256(segmentation) == DIARIZE_SEGMENTATION_SHA256:
        skipped.append(segmentation)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "segmentation.tar.bz2")
            _fetch(DIARIZE_SEGMENTATION_URL, archive, DIARIZE_SEGMENTATION_ARCHIVE_SHA256)
            with tarfile.open(archive, "r:bz2") as tar:
                # В архиве model.onnx и model.int8.onnx: берём fp32 — int8 на
                # тестовом диалоге схлопывал два голоса в один кластер.
                member = next(
                    item for item in tar.getmembers() if item.name.endswith("/model.onnx")
                )
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise RuntimeError(f"{DIARIZE_SEGMENTATION_URL}: пустой model.onnx")
                with open(segmentation, "wb") as out:
                    shutil.copyfileobj(extracted, out)
        actual = _sha256(segmentation)
        if actual != DIARIZE_SEGMENTATION_SHA256:
            os.unlink(segmentation)
            raise RuntimeError(
                f"{DIARIZE_SEGMENTATION_URL}: sha256 model.onnx {actual}"
            )

    if os.path.isfile(embedding) and _sha256(embedding) == DIARIZE_EMBEDDING_SHA256:
        skipped.append(embedding)
    else:
        _fetch(DIARIZE_EMBEDDING_URL, embedding, DIARIZE_EMBEDDING_SHA256)
    return skipped

def main() -> int:
    target = os.environ.get("HF_HOME", "/models")
    print(f"[warmup] HF_HOME={target}; offline после прогрева", flush=True)
    failed = False
    for model_id in model_ids():
        try:
            path = snapshot_download(model_id)
        except Exception as exc:  # noqa: BLE001 — показываем все проблемные модели
            failed = True
            print(f"[warmup] FAIL {model_id}: {type(exc).__name__}: {exc}", flush=True)
            continue
        print(f"[warmup] ok {model_id} -> {path}", flush=True)
    # Диаризация (FR-42): 34 МБ ONNX из релизов k2-fsa. Без весов сайдкар
    # работает, но diarize=true деградирует, поэтому сбой — предупреждение,
    # а не провал прогрева.
    if _env_bool("CC_ML_DIARIZE", True):
        try:
            skipped = fetch_diarize(diarize_dir())
        except Exception as exc:  # noqa: BLE001 — сеть/GitHub мог лежать
            print(
                f"[warmup] WARN диаризация не скачана ({type(exc).__name__}: {exc}); "
                "diarize=true будет деградировать",
                flush=True,
            )
        else:
            print(
                f"[warmup] ok диаризация -> {diarize_dir()} "
                f"({'уже было' if len(skipped) == 2 else 'скачано'})",
                flush=True,
            )
    if failed:
        print("[warmup] не все веса скачаны; ml не сможет стартовать офлайн", flush=True)
        return 1
    print("[warmup] готово: сервис ml можно поднимать с HF_HUB_OFFLINE=1", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
