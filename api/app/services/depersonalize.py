"""NER-расширение деперсонализации (docs/12): ФИО/адреса сверх deny-list.

SPI: NameExtractor.extract_spans(text) -> [(вид, start, end)] — спаны для
объединения со структурными правилами ядра; extract(text) — только ФИО
(совместимость с прежним deny-list API).

  hybrid             — rubert + структурные правила + валидаторы (локальный путь);
  remote             — GPU-sidecar по HTTP: POST {CC_ML_URL}/v1/mask (прод-путь,
                       api-образ без torch; docs/12).

Fail-closed (docs/10): в CC_PROD_MODE=true настроенный ML-backend обязан
загрузиться. Если модель/зависимость недоступны, egress-путь НЕ отправляет
сырой текст в LLM — поднимается DepersonalizationUnavailable (503
DEPERSONALIZATION_UNAVAILABLE). Вне prod сохраняется деградация до regex с
логом. Сбой инференса на отдельном фрагменте в prod — тоже 503.

Модели — опциональные зависимости (extra "ner"/"ner-rubert"), импорт ленивый:
`uv sync` без extra и docker-сборка не ломаются.
"""

from __future__ import annotations

import logging
from typing import Protocol

from compass.depersonalize import (
    ADDRESS,
    AGE,
    DOC,
    DOB,
    EMAIL,
    FIO,
    PHONE,
    Depersonalized,
    Span,
    depersonalize as core_depersonalize,
    is_plausible_person_span,
)

from app.config import settings
from app.errors import Problem

logger = logging.getLogger(__name__)


class DepersonalizationUnavailable(Problem):
    """Prod fail-closed: ML-деперсонализация не работает — egress запрещён."""

    def __init__(self) -> None:
        super().__init__(
            503,
            "DEPERSONALIZATION_UNAVAILABLE",
            "Деперсонализация недоступна — запрос к LLM заблокирован",
        )


def _backend_unavailable(backend: str, exc: BaseException) -> None:
    """Единая политика сбоя ML-backend'а: prod — 503, иначе regex-деградация."""
    if settings.prod_mode:
        logger.error(
            "depersonalize_ner=%s недоступен в prod (%s: %s) — LLM-egress заблокирован",
            backend, type(exc).__name__, exc,
        )
        raise DepersonalizationUnavailable() from exc
    logger.warning(
        "depersonalize_ner=%s недоступен (%s: %s) — деградация до regex/deny-list",
        backend, type(exc).__name__, exc,
    )


class NameExtractor(Protocol):
    def extract(self, text: str) -> list[str]:
        """Имена (в исходном написании) для маскирования в text."""
        ...

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        """(вид, start, end) найденных сущностей: FIO/ADDRESS/DOC/PHONE/EMAIL."""
        ...


class NullNameExtractor:
    """NER выключен: только deny-list + regex (детерминированное демо)."""

    def extract(self, text: str) -> list[str]:
        return []

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        return []


class NatashaNameExtractor:
    """natasha NewsNERTagger: теги PER → ФИО, LOC → адрес.

    Конвейер строится лениво при первом вызове; при недоступности библиотеки
    или моделей — лог + деградация до Null (вне prod) либо 503 (prod).
    """

    backend = "natasha"

    def __init__(self) -> None:
        self._pipeline = None  # None = ещё не инициализирован/недоступен

    def _build(self):
        """(lazy) сегментер + NER-таггер natasha. Бросает ImportError/OSError.

        MorphVocab (pymorphy2) опционален: на Python 3.14 его словари через
        pkg_resources-энтрипойнты не грузятся — нормализацию спана пропускаем,
        маскируем исходное написание (для deny-list этого достаточно).
        """
        from natasha import NewsEmbedding, NewsNERTagger, Segmenter

        seg = Segmenter()
        ner = NewsNERTagger(NewsEmbedding())
        morph = None
        try:
            from natasha import MorphVocab

            morph = MorphVocab()
        except Exception:  # pymorphy2 может отсутствовать/быть сломан — не критично
            pass
        return seg, morph, ner

    def _ensure(self) -> tuple | bool:
        if self._pipeline is None:
            try:
                self._pipeline = self._build()
            except Exception as exc:  # ImportError, OSError (модели), любые сбои
                _backend_unavailable(self.backend, exc)
                self._pipeline = False  # больше не пробуем в этом процессе
        return self._pipeline

    def extract(self, text: str) -> list[str]:
        return [text[s:e] for kind, s, e in self.extract_spans(text) if kind == FIO]

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        pipeline = self._ensure()
        if pipeline is False or not text:
            return []
        seg, morph, ner = pipeline

        from natasha import Doc

        doc = Doc(text)
        doc.segment(seg)
        try:
            doc.tag_ner(ner)
        except Exception as exc:
            _backend_unavailable(self.backend, exc)
            return []
        out: list[tuple[str, int, int]] = []
        for span in doc.spans:
            kind = _NATASHA_KIND.get(getattr(span, "type", ""))
            if kind is None:
                continue
            start, stop = span.start, span.stop
            if stop > start:
                out.append((kind, start, stop))
        return out


# natasha-типы: PER -> ФИО, LOC -> адрес (грубо; бенчмарк bench/ner это измеряет).
_NATASHA_KIND = {"PER": "FIO", "LOC": "ADDRESS"}


class RubertPiiNameExtractor:
    """redmadrobot-rnd/rubert-base-pii-ner: RU-PII token-classification.

    Apache-2.0, обучен на русских ПДн (ФИО/адреса/документы/контакты).
    На redmadrobot-rnd/pii_benchmark FIO F1 0.933 exact / 0.947 overlap;
    на клиническом корпусе (bench/ner/clinical_contexts) hybrid — P/R 1.000/
    0.975 по ФИО, см. bench/ner/README.md.

    Зависимости опциональны (extra "ner-rubert"): torch + transformers.
    При их отсутствии/сбое модели — 503 в prod, regex-деградация вне prod.
    """

    backend = "rubert"

    def __init__(self) -> None:
        self._pipe = None

    def _build(self):
        import torch
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        return pipeline(
            "token-classification",
            model=settings.ner_model,
            device=device,
            aggregation_strategy="first",
        )

    def _ensure(self):
        if self._pipe is None:
            try:
                self._pipe = self._build()
            except Exception as exc:  # ImportError/OSError/любой сбой инициализации
                _backend_unavailable(self.backend, exc)
                self._pipe = False
        return self._pipe

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        pipe = self._ensure()
        if pipe is False or not text:
            return []
        try:
            ents = pipe(text)
        except Exception as exc:
            _backend_unavailable(self.backend, exc)
            return []
        out: list[tuple[str, int, int]] = []
        for e in ents:
            label = e.get("entity_group") or e.get("entity")
            kind = _RUBERT_KIND.get(label)
            if kind and int(e["end"]) > int(e["start"]):
                out.append((kind, int(e["start"]), int(e["end"])))
        return out

    def extract(self, text: str) -> list[str]:
        return [text[s:e] for kind, s, e in self.extract_spans(text) if kind == FIO]


# Категории rubert-base-pii-ner -> виды нашего гварда.
_RUBERT_KIND = {
    "NAME": FIO, "FIRST_NAME": FIO, "LAST_NAME": FIO, "MIDDLE_NAME": FIO,
    "STREET": ADDRESS, "CITY": ADDRESS, "REGION": ADDRESS,
    "COUNTRY": ADDRESS, "HOUSE": ADDRESS, "DISTRICT": ADDRESS,
    "PASSPORT": "DOC", "SNILS": "DOC", "INN": "DOC", "OMS": "DOC",
    "DRIVER_LICENSE": "DOC", "MILITARY_ID": "DOC",
    "BIRTH_CERTIFICATE": "DOC", "CREDIT_CARD": "DOC",
    "PHONE": "PHONE", "EMAIL": "EMAIL",
}


class HybridNameExtractor(RubertPiiNameExtractor):
    """Прод-путь: rubert только для ФИО/адресов, остальное — правила ядра.

    Документы/телефоны/email не берём из модели: структурные правила с
    контрольными числами точнее (см. bench/ner/README.md), а дубли спанов
    разрешает ядро. Мусорные однословные «ФИО» со строчной отсеиваются.
    """

    backend = "hybrid"

    def extract_spans(self, text: str) -> list[tuple[str, int, int]]:
        out: list[tuple[str, int, int]] = []
        for kind, start, end in super().extract_spans(text):
            if kind not in (FIO, ADDRESS):
                continue
            if not is_plausible_person_span(text, Span(kind, start, end)):
                continue
            out.append((kind, start, end))
        return out



# Категории sidecar'а (/v1/mask) -> виды ядра. Документы сводятся к DOC с
# точной категорией в Span.category (так же, как локальный парсер).
_REMOTE_KIND: dict[str, tuple[str, str | None]] = {
    "FIO": (FIO, None),
    "PERSON": (FIO, None),
    "NAME": (FIO, None),
    "ФИО": (FIO, None),
    "ADDRESS": (ADDRESS, None),
    "LOCATION": (ADDRESS, None),
    "АДРЕС": (ADDRESS, None),
    "PHONE": (PHONE, None),
    "ТЕЛЕФОН": (PHONE, None),
    "EMAIL": (EMAIL, None),
    "DOB": (DOB, None),
    "BIRTHDATE": (DOB, None),
    "ДАТА РОЖДЕНИЯ": (DOB, None),
    "AGE": (AGE, None),
    "ВОЗРАСТ": (AGE, None),
    "SNILS": (DOC, "SNILS"),
    "СНИЛС": (DOC, "SNILS"),
    "INN": (DOC, "INN"),
    "ИНН": (DOC, "INN"),
    "OMS": (DOC, "OMS"),
    "POLICY": (DOC, "OMS"),
    "PASSPORT": (DOC, "PASSPORT"),
    "MEDCARD": (DOC, "MEDCARD"),
    "DOC": (DOC, None),
    "ДОКУМЕНТ": (DOC, None),
}



def _spans_from_remote(text: str, payload: object) -> list[Span]:
    """Ответ sidecar'а -> Span'ы ядра. Строго: мусор = ошибка, а не пропуск.

    Раньше битые элементы молча выбрасывались, и в прод-режиме сайдкар,
    вернувший, например, строковые смещения, давал пустой набор спанов —
    текст уходил в LLM немаскированным при «успешном» ответе. Теперь любое
    несоответствие контракту — исключение, и `_remote_spans` переводит его в
    политику `_backend_unavailable` (prod: 503 до вызова LLM).
    """
    if not isinstance(payload, dict):
        raise TypeError(f"неожиданный ответ /v1/mask: {type(payload).__name__}")
    # Новый сайдкар честно сообщает о деградации NER. Для egress-пути это
    # равносильно отказу backend'а: regex-спаны без NER не гарантируют
    # отсутствие ФИО/адресов (docs/12).
    if "degraded" in payload:
        degraded = payload["degraded"]
        if not isinstance(degraded, bool):
            raise TypeError("ответ /v1/mask: degraded не bool")
        if degraded:
            raise RuntimeError("сайдкар деградировал (NER недоступен)")
    raw_spans = payload.get("spans")
    if not isinstance(raw_spans, list):
        raise TypeError("ответ /v1/mask без списка spans")
    out: list[Span] = []
    for index, item in enumerate(raw_spans):
        if not isinstance(item, dict):
            raise TypeError(f"span[{index}] не объект")
        start, end = item.get("start"), item.get("end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise TypeError(f"span[{index}] без целых start/end")
        if not 0 <= start < end <= len(text):
            raise ValueError(f"span[{index}] вне диапазона текста")
        mapped = _REMOTE_KIND.get(str(item.get("category", "")).strip().upper())
        if mapped is None:
            raise ValueError(f"span[{index}] с неизвестной категорией")
        kind, category = mapped
        span = Span(kind, start, end, category)
        if is_plausible_person_span(text, span):
            out.append(span)
    return out


def _remote_spans(text: str, names: list[str] | None = None) -> list[Span]:
    """Маскирование на GPU-sidecar'е (POST {CC_ML_URL}/v1/mask).

    Клиент общий (`app.services.ml_client`), api-образ остаётся без torch: один
    повтор на сетевой сбой/5xx и таймауты (2 с connect / 5 с read) живут в
    клиенте (`CC_ML_TIMEOUT`). Любой сбой уходит в единую политику
    `_backend_unavailable`: prod — 503 до вызова LLM, вне prod — деградация до
    regex/deny-list.
    """
    from app.services import ml_client  # ленивый импорт: remote без sidecar'а

    try:
        payload = ml_client.mask(text, list(names or []))
        return _spans_from_remote(text, payload)
    except Exception as exc:
        _backend_unavailable("remote", exc)
        return []


class RemoteNameExtractor:
    """GPU-sidecar по HTTP (CC_ML_URL): маскирование вне api-процесса.

    Поведение совпадает с hybrid: спаны маппятся в те же виды/категории и
    проходят те же фильтры (`is_plausible_person_span`).
    """

    backend = "remote"

    def extract_spans(
        self, text: str, names: list[str] | None = None
    ) -> list[tuple[str, int, int]]:
        return [(s.kind, s.start, s.end) for s in _remote_spans(text, names)]

    def extract(self, text: str) -> list[str]:
        return [
            text[s.start:s.end] for s in _remote_spans(text, []) if s.kind == FIO
        ]



_NER_OFF = {"", "off", "false", "0", "none", "no"}


def ner_backend() -> str:
    """Нормализованное имя backend'а: off | natasha | rubert | hybrid | remote."""
    raw = getattr(settings, "depersonalize_ner", "off")
    value = str(raw).strip().lower()
    if value in _NER_OFF or raw is False:
        return "off"
    if value in ("true", "1", "yes", "on"):
        return "natasha"  # обратная совместимость флага
    if value == "hybrid":
        return "hybrid"
    if value == "rubert":
        return "rubert"
    if value == "natasha":
        return "natasha"
    if value == "remote":
        return "remote"
    logger.warning("Неизвестный CC_DEPERSONALIZE_NER=%r — используется off", raw)
    return "off"


_extractor = None


def build_name_extractor() -> NameExtractor:
    """Один экземпляр на процесс (модели грузятся один раз)."""
    global _extractor
    if _extractor is None:
        backend = ner_backend()
        if backend == "hybrid":
            _extractor = HybridNameExtractor()
        elif backend == "rubert":
            _extractor = RubertPiiNameExtractor()
        elif backend == "natasha":
            _extractor = NatashaNameExtractor()
        elif backend == "remote":
            _extractor = RemoteNameExtractor()
        else:
            _extractor = NullNameExtractor()
        logger.info("depersonalize_ner=%s", backend)
    return _extractor


def extract_pii_spans(
    text: str, names: list[str] | None = None
) -> list[Span]:
    """Спаны ML-детекции для объединения со структурными правилами ядра.

    Для backend=remote names (deny-list ростра) уходят в sidecar вместе с
    текстом: он маскирует известные ФИО даже без NER.
    """
    if not text or ner_backend() == "off":
        return []
    if ner_backend() == "remote":
        return _remote_spans(text, names)
    spans: list[Span] = []
    for kind, start, end in build_name_extractor().extract_spans(text):
        if 0 <= start < end <= len(text):
            span = Span(kind, start, end)
            if is_plausible_person_span(text, span):
                spans.append(span)
    return spans


def scrub_text(text: str, names: list[str] | None = None, *,
               mode: str = "replace") -> Depersonalized:
    """Единая точка деперсонализации перед LLM (regex + ML + deny-list)."""
    return core_depersonalize(
        text, names=names, mode=mode, extra_spans=extract_pii_spans(text, names)
    )

async def deny_names(db, clinician_id, *user_ids) -> list[str]:
    """Deny-list для egress: ростер врача + ФИО самого врача и пациентов.

    Общий хелпер для всех LLM-путей: раньше `chat_insights` добавлял ФИО
    пациента и врача, а `assistant_chat` — только ростер, поэтому ФИО самого
    врача в его же запросе уходило в LLM открытым (оставаясь на месте только
    за счёт ML-детекции, т.е. зависело от backend'а).
    """
    from sqlalchemy import select

    from app.models import User

    from .clinician import roster_names

    names = await roster_names(db, clinician_id)
    for uid in user_ids:
        if uid is None:
            continue
        fio = await db.scalar(select(User.fio).where(User.id == uid))
        if fio and fio.strip() and fio not in names:
            names.append(fio.strip())
    return names
