"""Коннектор ВОЗ (IRIS, DSpace 7 REST): гайдлайны по психиатрии.

Источник — только официальное хранилище ВОЗ iris.who.int. Белый список
курируется людьми (WHO_HANDLES): автоматический поиск по сайту ВОЗ не
используется — метаданные IRIS не дают надёжного тематического фильтра
(психиатрия vs «mental health» в широком смысле), а ошибка ингеста стоит
дороже ручного добавления одного handle.

Текст берём из bundle TEXT (готовый `.pdf.txt` от IRIS): PDF-парсинг не нужен,
сохраняется разбивка на разделы. Лицензия определяется по самому документу
(строка CC BY-NC-SA 3.0 IGO), а не по вере в белый список.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.enrichment.types import SourceDocument

MAX_BOOK_BYTES = 30 * 1024 * 1024


class SourceError(RuntimeError):
    """Внешний источник недоступен или отдал неожиданный формат."""


@dataclass(frozen=True)
class WHOHandle:
    handle: str  # IRIS handle, стабильный идентификатор
    uuid: str  # item UUID (проверен вручную 2026-10-21)
    title: str
    issued: str
    category: str  # mhgap | pharmacology | work | adolescents | stress | physical
    notes: str = ""


# Белый список проверенных гайдлайнов ВОЗ по психическому здоровью.
# Расширять только после ручной проверки: тема, редакция, лицензия.
WHO_HANDLES: tuple[WHOHandle, ...] = (
    WHOHandle(
        handle="10665/374250",
        uuid="d7727881-07ec-485f-95a8-6f2864aee9a9",
        title=(
            "Mental Health Gap Action Programme (mhGAP) guideline for mental, "
            "neurological and substance use disorders"
        ),
        issued="2023-11-20",
        category="mhgap",
        notes="Актуальная редакция рекомендаций mhGAP (заменяет 2015).",
    ),
    WHOHandle(
        handle="10665/377181",
        uuid="f67a4578-e1e4-41f5-a3c3-3dfb861e15ea",
        title=(
            "Pharmacological treatment of priority mental, neurological and substance use "
            "disorders in non-specialized health care: a job aid for supporting "
            "implementation of the mhGAP guidelines"
        ),
        issued="2024-06-01",
        category="pharmacology",
        notes="Фармакотерапия приоритетных расстройств (mhGAP job aid).",
    ),
    WHOHandle(
        handle="10665/363177",
        uuid="8efa3fa9-758c-484a-98d7-1de5199a294c",
        title="WHO guidelines on mental health at work",
        issued="2022-09-27",
        category="work",
    ),
    WHOHandle(
        handle="10665/336864",
        uuid="f8b95e8c-14ef-4c41-9cdb-c7a7f9c75620",
        title=(
            "Guidelines on mental health promotive and preventive interventions for "
            "adolescents: helping adolescents thrive"
        ),
        issued="2020-11-23",
        category="adolescents",
    ),
    WHOHandle(
        handle="10665/275718",
        uuid="63c8f3e2-30e6-4e7d-b7bf-ab31fcfdaa43",
        title=(
            "Management of physical health conditions in adults with severe mental "
            "disorders: WHO guidelines"
        ),
        issued="2018-11-02",
        category="physical",
    ),
    WHOHandle(
        handle="10665/85119",
        uuid="d8e6a2b6-5c74-49b5-9747-56a762b58c6d",
        title="Guidelines for the management of conditions that are specifically related to stress",
        issued="2013-06-17",
        category="stress",
        notes="Редакция старше 5 лет: помечается предупреждением, а не исключается.",
    ),
    WHOHandle(
        handle="10665/250239",
        uuid="9c42b21c-dfc9-4c2b-b32d-e1caa5befbbc",
        title=(
            "mhGAP intervention guide for mental, neurological and substance use disorders "
            "in non-specialized health settings"
        ),
        issued="2016-09-29",
        category="mhgap_ig",
        notes="Клинические протоколы mhGAP IG v2.0 (дополняет guideline 2023).",
    ),
)

_CC_OK = re.compile(
    r"Creative Commons Attribution-NonCommercial-ShareAlike 3\.0 IGO|CC BY-NC-SA 3\.0 IGO",
    re.I,
)
_PROPRIETARY = re.compile(r"All rights reserved|©\s*World Health Organization", re.I)
_OTHER_CC = re.compile(r"Creative Commons|\bCC BY\b", re.I)



def _request(cfg, url: str, *, accept: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"Accept": accept, "User-Agent": cfg.user_agent},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:  # noqa: S310 — фиксированный https-хост
            data = resp.read(MAX_BOOK_BYTES)
    except urllib.error.HTTPError as exc:  # pragma: no cover - сеть
        raise SourceError(f"WHO IRIS HTTP {exc.code} для {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover - сеть
        raise SourceError(f"WHO IRIS недоступен ({url}): {exc}") from exc
    return data


def _get_json(cfg, url: str) -> dict:
    raw = _request(cfg, url, accept="application/json")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceError(f"WHO IRIS вернул не JSON: {url}") from exc


def _bitstreams(cfg, item: dict) -> list[dict]:
    bundles = _get_json(cfg, item["_links"]["bundles"]["href"])["_embedded"]["bundles"]
    streams: list[dict] = []
    for bundle in bundles:
        if bundle.get("name") != "TEXT":
            continue
        payload = _get_json(cfg, bundle["_links"]["bitstreams"]["href"])
        for stream in payload["_embedded"]["bitstreams"]:
            stream["_bundle"] = bundle.get("name")
            streams.append(stream)
    return streams


def _pick_text_stream(streams: list[dict]) -> dict | None:
    texts = [s for s in streams if s["_bundle"] == "TEXT" and s.get("name", "").endswith(".pdf.txt")]
    if not texts:
        return None
    eng = [s for s in texts if "-eng." in s["name"] or s["name"].startswith("eng")]
    return (eng or texts)[0]


def detect_license(text: str) -> str:
    """Класс лицензии по тексту документа.

    Возвращает: "CC BY-NC-SA 3.0 IGO" (можно индексировать), "proprietary"
    (all rights reserved — публикация запрещена), "other-cc" (другая CC —
    требует ручного решения) или "" (не найдено).
    """
    head = text[:20000]
    if _CC_OK.search(head):
        return "CC BY-NC-SA 3.0 IGO"
    if _PROPRIETARY.search(head):
        return "proprietary"
    if _OTHER_CC.search(head):
        return "other-cc"
    return ""


def parse_iris_item(item: dict, text: str, handle_meta: WHOHandle) -> SourceDocument:
    """Чистая функция: метаданные item + текст bitstream → SourceDocument."""
    metadata = item.get("metadata", {})
    title = (metadata.get("dc.title") or [{"value": handle_meta.title}])[0]["value"].strip()
    issued = (metadata.get("dc.date.issued") or [{"value": handle_meta.issued}])[0]["value"]
    abstract = ""
    if metadata.get("dc.description.abstract"):
        abstract = metadata["dc.description.abstract"][0]["value"].strip()
    handle = item.get("handle") or handle_meta.handle
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return SourceDocument(
        source="who",
        kind="guideline",
        external_id=handle,
        guideline_id=f"who-{handle.replace('/', '-')}",
        title=title,
        revision=f"{issued}#{digest[:12]}",
        text=text,
        abstract=abstract,
        journal="World Health Organization",
        year=int(issued[:4]) if issued[:4].isdigit() else None,
        url=f"https://iris.who.int/handle/{handle}",
        fulltext_url=f"https://iris.who.int/handle/{handle}",
        license=detect_license(text),
        language="en",
        publication_types=("Guideline", "WHO guideline"),
        retraction="not_checked",  # у гайдлайнов нет механизма ретракции
        fetched_at="",
        raw={"handle": handle, "uuid": item.get("uuid"), "issued": issued, "content_sha256": digest},
    )


def fetch_documents(cfg) -> tuple[list[SourceDocument], dict, list[str]]:
    """Загружает белый список. Возвращает (документы, query_plan, ошибки).

    Ошибка одного документа не валит прогон: она попадает в errors, остальные
    документы обрабатываются (run.status=partial).
    """
    documents: list[SourceDocument] = []
    errors: list[str] = []
    plan = {
        "source": "who",
        "endpoint": f"{cfg.base_url}/core/items",
        "handles": [h.handle for h in cfg.handles],
        "license_gate": "CC BY-NC-SA 3.0 IGO в тексте документа",
    }
    for handle_meta in cfg.handles:
        try:
            item = _get_json(cfg, f"{cfg.base_url}/core/items/{handle_meta.uuid}")
            actual_handle = item.get("handle")
            if actual_handle != handle_meta.handle:
                # UUID в белом списке и handle в ответе разошлись: молча
                # индексировать чужой документ нельзя.
                raise SourceError(
                    f"IRIS вернул handle {actual_handle} вместо {handle_meta.handle}"
                )
            streams = _bitstreams(cfg, item)
            stream = _pick_text_stream(streams)
            if stream is None:
                raise SourceError(f"нет TEXT bitstream для {handle_meta.handle}")
            raw_text = _request(cfg, stream["_links"]["content"]["href"], accept="text/plain")
            text = raw_text.decode("utf-8", errors="replace")
            if len(text) > cfg.max_text_chars:
                raise SourceError(
                    f"текст {handle_meta.handle} больше лимита ({len(text)} > {cfg.max_text_chars})"
                )
            if len(text.strip()) < 2000:
                raise SourceError(f"подозрительно короткий текст {handle_meta.handle}")
            documents.append(parse_iris_item(item, text, handle_meta))
        except (SourceError, KeyError, IndexError) as exc:
            errors.append(f"who {handle_meta.handle}: {exc}")
    return documents, plan, errors
