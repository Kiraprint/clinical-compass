"""Офлайн-прогрев весов в том ml-models (docker compose ... run --rm ml-warmup).

Сеть у сервиса ``ml`` внутренняя (без egress), поэтому веса скачиваются
отдельным одноразовым контейнером, у которого egress есть. Здесь только
huggingface_hub: torch не импортируется, модели не загружаются в память.

Скачиваются все профили эмбеддингов из CC_ML_PROFILES (встроенные + override),
реранкер CC_ML_RERANK_MODEL и NER-модель CC_ML_NER_MODEL — ровно те, что
сайдкар потом откроет с HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1.
"""

from __future__ import annotations

import os
import sys

# Контейнер warmup'а — единственный, кому разрешён egress к HF.
os.environ["HF_HUB_OFFLINE"] = "0"
os.environ["TRANSFORMERS_OFFLINE"] = "0"

from huggingface_hub import snapshot_download  # noqa: E402 — после сброса offline

from models import (  # noqa: E402
    DEFAULT_PROFILE,
    _NER_MODEL,
    _RERANK_MODEL,
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
    seen: set[str] = set()
    return [model_id for model_id in ids if not (model_id in seen or seen.add(model_id))]

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
    if failed:
        print("[warmup] не все веса скачаны; ml не сможет стартовать офлайн", flush=True)
        return 1
    print("[warmup] готово: сервис ml можно поднимать с HF_HUB_OFFLINE=1", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
