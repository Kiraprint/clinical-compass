"""ИИ-агент врача (LangGraph). Гибридный поиск + ответ с цитатами (docs/07, docs/08).

Граф: retrieve (Qdrant hybrid) → answer (LLM по сниппетам либо чистая сборка
из ядра, red line). LLM — за SPI и только по найденным фрагментам: без
`CC_ASSISTANT_LLM_URL`, при сбое или без валидных ссылок отвечает
детерминированный `build_rag_answer` (docs/03 §1.3), офлайн. Отказные пути
(красная линия, нет фрагментов, только карта пациента) LLM не касаются.
Потребляет только открытый не-ПДн корпус: карта пациента в промпт не уходит.
"""

from __future__ import annotations

import logging
import re
from typing import AsyncIterator, Sequence, TypedDict

import anyio

from langgraph.graph import END, START, StateGraph

from compass.agent import Citation, RAGAnswer, RAGChunk, build_rag_answer, detect_redline

from app.config import settings
from app.services.depersonalize import scrub_text

from app.services import kb as kb_store
from app.services import llm_router
from app.services.embeddings import get_embedder, profile_default_min_score
from app.services.reranker import get_reranker

logger = logging.getLogger(__name__)

# GATE: лексическое покрытие (docs/07 §5 — sparse ловит точные термины, dense —
# смысл, но для демо-эмбеддера минимальный порог на точное совпадение терминов).
# Слова длины <3 и стоп-слова не считаем значимыми.
_GATE_MIN_TOKENS = 1
_STOP = {"что", "как", "какой", "какая", "какое", "у", "в", "на", "и", "это", "для", "из"}

def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zа-яё0-9]{3,}", text.lower()) if t not in _STOP}

def _lexical_overlap(query: str, text: str) -> int:
    """Число значимых токенов запроса, найденных в тексте чанка.

    Русский сильно флективен: «дозировка» vs «дозирования», «авиандра» vs «авиандр».
    Для токенов ≥4 символов принимаем совпадение по 4-символьному префиксу (грубый
    стеммер), иначе — точное равенство.
    """
    q = _content_tokens(query)
    t = _content_tokens(text)
    n = 0
    for qt in q:
        if len(qt) >= 4:
            pref = qt[:4]
            if any(tt[:4] == pref for tt in t):
                n += 1
        elif qt in t:
            n += 1
    return n

def _relevance_gate(query: str, hits: list[dict], limit: int) -> list[dict]:
    """Оставляет чанки, где покрыт хотя бы один значимый токен запроса.

    Если у запроса нет ни одного значимого токена (чистая стоп-фраза/шуточный
    запрос) — считаем нерелевантным, чтобы не отвечать наугад (поверхность
    базы знаний, а не симптоматика).
    """
    kept = [h for h in hits if _lexical_overlap(query, h.get("text", "")) >= _GATE_MIN_TOKENS]
    return kept[:limit]


def _hash_rerank(
    query: str,
    hits: list[dict],
    limit: int,
    *,
    topics: Sequence[str] = (),
    exclude_ids: set[str] | None,
    allowed_work_ids: set[str] | None = None,
    corpus_drugs: list[dict] | None = None,
) -> list[dict]:
    """Лексический гейт + keyword-fallback для hash-профиля (демо/тесты).

    Hash-dense шумит, поэтому гарантируем, что чанки с прямым лексическим
    совпадением (напр. «Авиандр») всплывают независимо от dense-ранжирования.

    Функция исполняется в рабочем потоке (offload агента), поэтому sync-сессию
    БД здесь открывать нельзя. Загрузку препаратов владеет вызывающий
    (`load_drug_docs` в async-сессии запроса); `corpus_drugs is None` означает
    «препаратов в корпусе нет», а не «сходи в БД».

    allowed_work_ids (FR-36): allow-list статей, одобренных врачом лично.
    Фильтр применяется и к хитам Qdrant, и к keyword-fallback (точки seed-корпуса
    work_id не имеют и видны всегда).
    """
    if allowed_work_ids is not None:
        hits = [h for h in hits if kb_store.is_visible(h, allowed_work_ids)]
    hits = _relevance_gate(query, hits, limit)
    if len(hits) >= limit:
        return hits
    from app.kb_seed import build_corpus  # lazy: избегаем циклического импорта
    seen = {h.get("guideline_id") for h in hits}
    kw = sorted(
        ((_lexical_overlap(query, d["text"]), d)
         for d in build_corpus(drug_docs=corpus_drugs or [])
         if (not topics or d.get("topic") in topics)
         and (not exclude_ids or d["guideline_id"] not in exclude_ids)
         and kb_store.is_visible(d, allowed_work_ids)),
        key=lambda x: x[0],
    )
    for ov, d in reversed(kw):
        if ov < _GATE_MIN_TOKENS or d["guideline_id"] in seen:
            continue
        d = {**d, "score": 0.5}
        hits.append(d)
        seen.add(d["guideline_id"])
        if len(hits) >= limit:
            break
    return hits

# Ссылка модели на сниппет: [1], [1, 2], [1][3]. Скобки без цифр игнорируем.
_CITATION_RE = re.compile(r"\[([^\[\]]{1,40})\]")
# Ссылкой считается только скобка из цифр и разделителей: «[10–14 баллов]» —
# клинический диапазон шкалы, а не источник.
_CITE_GROUP_RE = re.compile(r"^[\d\s,;]+$")
MAX_ANSWER_CITATIONS = 3  # как в build_rag_answer: больше трёх источников — шум


def _cited_indexes(text: str) -> list[int]:
    """Номера сниппетов, на которые сослался LLM, в порядке первого упоминания."""
    found: list[int] = []
    for group in _CITATION_RE.findall(text):
        if not _CITE_GROUP_RE.match(group):
            continue
        for token in re.findall(r"\d{1,3}", group):
            n = int(token)
            if n not in found:
                found.append(n)
    return found


def _renumber_citations(text: str, positions: dict[int, int]) -> str:
    """Ссылки модели → номера в списке citations (порядок источников в UI).

    positions — номер сниппета из промпта → 1-based позиция в citations. Маркер
    сниппета, который в citations не попал (дедуп по guideline_id, лимит
    MAX_ANSWER_CITATIONS), из текста убирается: видимый номер обязан указывать
    на тот источник, который лежит в citations под этим номером.
    """

    def repl(match: re.Match[str]) -> str:
        group = match.group(1)
        if not _CITE_GROUP_RE.match(group):
            return match.group(0)
        mapped: list[int] = []
        for token in re.findall(r"\d{1,3}", group):
            new = positions.get(int(token))
            if new is not None and new not in mapped:
                mapped.append(new)
        return f"[{', '.join(map(str, mapped))}]" if mapped else ""

    text = _CITATION_RE.sub(repl, text)
    text = re.sub(r"[ \t]+([.,;:!?)])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _compose_answer(
    query: str, history: list[dict], chunks: list[RAGChunk]
) -> RAGAnswer | None:
    """LLM-ответ по сниппетам с серверной проверкой ссылок (FR-17).

    None — LLM выключен/недоступен/вернул пусто ИЛИ ни одна ссылка [n] не
    указывает на фрагмент, который модель реально видела: тогда вызывающий
    берёт детерминированный build_rag_answer. Окно номеров — не len(chunks), а
    answer_snippet_window: при limit=20 в промпт попадают только первые
    ANSWER_SNIPPETS (и не больше, чем влезло в бюджет символов), ссылка за это
    окно — выдумка модели. Текст ответа модели в citations не попадает —
    цитаты собираются только из настоящих чанков (номер → chunks[n - 1]), а
    видимые маркеры перенумеровываются в порядок citations (дедуп по
    guideline_id и лимит сдвигают номера). Карта пациента сюда не передаётся:
    модель не видит ПДн.
    """
    payload = [{"text": c.text, "source_title": c.source_title} for c in chunks]
    text = llm_router.compose_answer(history, query, payload)
    return _finalize_llm_answer(text, chunks)


def _finalize_llm_answer(text: str | None, chunks: list[RAGChunk]) -> RAGAnswer | None:
    """Проверка ссылок модели и сборка citations; None — брать детерминированный."""
    if not text:
        return None
    payload = [{"text": c.text, "source_title": c.source_title} for c in chunks]
    window = llm_router.answer_snippet_window(payload)
    picked: list[RAGChunk] = []
    positions: dict[int, int] = {}  # номер сниппета → номер в citations
    by_guideline: dict[str, int] = {}
    for n in sorted(_cited_indexes(text)):
        if not 1 <= n <= window:
            continue
        chunk = chunks[n - 1]
        known = by_guideline.get(chunk.guideline_id)
        if known is not None:  # дубль гайдлайна — та же цитата
            positions[n] = known
            continue
        if len(picked) >= MAX_ANSWER_CITATIONS:
            continue
        picked.append(chunk)
        by_guideline[chunk.guideline_id] = len(picked)
        positions[n] = len(picked)
    if not picked:
        logger.info("LLM-ответ без валидных ссылок на сниппеты — детерминированный ответ")
        return None
    return RAGAnswer(
        answer=_renumber_citations(text, positions),
        citations=tuple(
            Citation(
                source_title=c.source_title,
                guideline_id=c.guideline_id,
                topic=c.topic,
                score=round(c.score, 4),
            )
            for c in picked
        ),
        grounded=True,
    )


class AgentState(TypedDict, total=False):
    query: str
    retrieval_query: str | None
    topic: str | None
    topics: tuple[str, ...]
    limit: int
    chunks: list[RAGChunk]
    # История треда (деперсонализированные реплики, oldest → newest) — контекст
    # для LLM-ответа; в retrieval/red line не участвует.
    history: list[dict]
    exclude_ids: set[str]
    allowed_work_ids: set[str] | None
    patient_context: dict | None
    patient_contexts: list[dict] | None
    corpus_drugs: list[dict] | None
    answer: dict


def _answer_dict(state: AgentState, result: RAGAnswer) -> dict:
    return {
        "answer": result.answer,
        "grounded": result.grounded,
        "refused": result.refused,
        "refusal_code": result.refusal_code,
        "topic": state.get("topic"),
        "citations": [
            {
                "source_title": c.source_title,
                "guideline_id": c.guideline_id,
                "topic": c.topic,
                "score": c.score,
            }
            for c in result.citations
        ],
    }


class AgentFactory:
    def __init__(self, client) -> None:
        self._client = client
        self._graph = None  # компиляция графа один раз, не на каждый запрос

    def search_kb(
        self,
        query: str,
        *,
        topic: str | None = None,
        topics: Sequence[str] | None = None,
        limit: int = 5,
        exclude_ids: set[str] | None = None,
        allowed_work_ids: set[str] | None = None,
        redline_query: str | None = None,
        corpus_drugs: list[dict] | None = None,
        allow_redline: bool = False,
    ) -> list[RAGChunk]:
        """Инструмент doctor-search: гибридный поиск по Qdrant (docs/07 §7).

        Отдельный переиспользуемый tool — ядро RAG-модуля. Redline-запрос
        (диагноз/терапия) поиска не делает; отказ формируется в _answer.

        redline_query: по какому тексту проверять red line. По умолчанию — сам
        query; при LLM-переформулировке уточняющего вопроса (FR-21) сюда
        передаётся исходный вопрос врача, чтобы redline-реплика из истории
        диалога не глушила поиск.

        allowed_work_ids (FR-36): статьи, которые врач одобрил лично; всё
        остальное из статей в выдачу не попадает (гайдлайны ВОЗ и seed — всегда).

        allow_redline (FR-43): обход red-line гейта. Гейт запрещает ПОИСК по
        запросу про диагноз/лечение, потому что публичный чат не должен
        подсказывать терапию. Черновик протокола сессии — врач-в-петле: он и
        так формирует диагноз/лечение, а источники нужны как обоснование
        (docs/18 §5.3). Врач/пациентский чат этот параметр не передают —
        гейт там остаётся включённым.
        """
        if not allow_redline and detect_redline(
            redline_query if redline_query is not None else query
        ) is not None:
            return []
        embedder = get_embedder()
        use_sparse = getattr(embedder, "use_sparse", True)
        # Реранк осмыслен только для нейросетевых профилей: hash-dense шумит, и
        # его порядок уже чинится лексическим гейтом/fallback (см. _hash_rerank).
        reranker = None if use_sparse else get_reranker()
        # Порог «нет релевантного ответа» применяет hybrid_search ко ВСЕМУ пулу
        # ДО реранка/отсечки: max по top-limit после реранка мог выбросить
        # лучший по первичному скору чанк и включить ложный отказ.
        # У bge-m3 косинус калиброван (релевантные >=0.57, офтоп <=0.43,
        # bench/REPORT-retrieval.md); шкала у профилей своя, поэтому дефолт —
        # из профиля, CC_KB_MIN_SCORE перекрывает.
        min_score = None
        if not use_sparse:
            min_score = (
                settings.kb_min_score
                if settings.kb_min_score is not None
                else profile_default_min_score(settings.embedder)
            )
        # topic_list — одна или две темы (FR-14): вопрос бывает и про препарат,
        # и про доказательства. Пусто — поиск по всем разделам.
        topic_list = tuple(topics or ()) or ((topic,) if topic else ())
        hits = kb_store.hybrid_search(
            self._client,
            embedder,
            query,
            topics=topic_list or None,
            limit=limit,
            exclude_ids=exclude_ids,
            work_ids=allowed_work_ids,
            candidates=settings.retrieval_candidates or None,
            reranker=reranker,
            min_score=min_score,
        )
        if use_sparse:
            hits = _hash_rerank(
                query,
                hits,
                limit,
                topics=topic_list,
                exclude_ids=exclude_ids,
                allowed_work_ids=allowed_work_ids,
                corpus_drugs=corpus_drugs,
            )
        return [
            RAGChunk(
                text=h["text"],
                source_title=h["source_title"],
                guideline_id=h["guideline_id"],
                topic=h["topic"],
                score=h["score"],
                chunk_id=h.get("id") or "",
                url=h.get("url") or "",
            )
            for h in hits
        ]

    def _retrieve(self, state: AgentState) -> AgentState:
        # Retrieval — по retrieval_query (LLM-переформулировка уточняющего
        # вопроса, FR-21), red line — строго по исходному вопросу врача:
        # redline-реплика из истории треда не глушит поиск следующих ходов.
        if detect_redline(state["query"]) is not None:
            return {"chunks": []}
        return {
            "chunks": self.search_kb(
                state.get("retrieval_query") or state["query"],
                topic=state.get("topic"),
                topics=state.get("topics"),
                limit=state.get("limit", 5),
                exclude_ids=state.get("exclude_ids"),
                allowed_work_ids=state.get("allowed_work_ids"),
                redline_query=state["query"],
                corpus_drugs=state.get("corpus_drugs"),
            )
        }

    def _answer(self, state: AgentState) -> AgentState:
        chunks = state.get("chunks") or []
        # FR-17: связный ответ по сниппетам пишет LLM. Всё, что похоже на
        # диагноз/терапию (red line), а также случаи «фрагментов нет» и «есть
        # только карта пациента» остаются детерминированными: карта и ПДн в
        # промпт не уходят, а отказ обязан быть воспроизводимым.
        result = None
        if chunks and detect_redline(state["query"]) is None:
            result = _compose_answer(
                state["query"], state.get("history") or [], chunks
            )
        if result is None:
            result = build_rag_answer(
                state["query"],
                chunks,
                patient_context=state.get("patient_context"),
                patient_contexts=state.get("patient_contexts"),
            )
        return {"answer": _answer_dict(state, result)}

    def build(self):
        if self._graph is None:
            graph = StateGraph(AgentState)
            graph.add_node("retrieve", self._retrieve)
            graph.add_node("answer", self._answer)
            graph.add_edge(START, "retrieve")
            graph.add_edge("retrieve", "answer")
            graph.add_edge("answer", END)
            self._graph = graph.compile()
        return self._graph

    def invoke(
        self,
        query: str,
        *,
        topic: str | None = None,
        topics: Sequence[str] | None = None,
        limit: int = 5,
        patient_context: dict | None = None,
        patient_contexts: list[dict] | None = None,
        exclude_ids: set[str] | None = None,
        allowed_work_ids: set[str] | None = None,
        retrieval_query: str | None = None,
        corpus_drugs: list[dict] | None = None,
        history: list[dict] | None = None,
    ) -> dict:
        return self.build().invoke(
            self._state(
                query,
                topic=topic,
                topics=topics,
                limit=limit,
                patient_context=patient_context,
                patient_contexts=patient_contexts,
                exclude_ids=exclude_ids,
                allowed_work_ids=allowed_work_ids,
                retrieval_query=retrieval_query,
                corpus_drugs=corpus_drugs,
                history=history,
            )
        )["answer"]

    def _state(
        self,
        query: str,
        *,
        topic: str | None = None,
        topics: Sequence[str] | None = None,
        limit: int = 5,
        patient_context: dict | None = None,
        patient_contexts: list[dict] | None = None,
        exclude_ids: set[str] | None = None,
        allowed_work_ids: set[str] | None = None,
        retrieval_query: str | None = None,
        corpus_drugs: list[dict] | None = None,
        history: list[dict] | None = None,
    ) -> AgentState:
        # Retrieval и роутинг раздела идут по retrieval_query (FR-21):
        # assistant_chat подставляет сюда LLM-переформулировку уточняющего
        # вопроса. Без неё (одиночный вопрос/LLM выключен) — сам query.
        search_query = (retrieval_query or "").strip() or query
        # LLM-роутинг раздела (docs/04 FR-14): если клиент явно не задал topic и
        # LLM-роутер включён — темы выбирает LLM (тулзы search_<раздел>).
        # Тем может быть две (препарат + доказательства), поэтому фильтр в
        # Qdrant — MatchAny; это один запрос, а не два поиска.
        # Redline-запросы LLM не трогаем: отказ детерминирован и дешевле.
        topic_list: tuple[str, ...] = tuple(topics or ())
        if topic is not None:
            topic_list = (topic,)
        elif not topic_list and detect_redline(query) is None:
            topic_list = llm_router.route_topics(search_query)
        topic = topic_list[0] if topic_list else None
        return {
            "query": query,
            "retrieval_query": search_query,
            "topic": topic,
            "topics": topic_list,
            "limit": limit,
            "patient_context": patient_context,
            "patient_contexts": patient_contexts,
            "corpus_drugs": corpus_drugs,
            # История треда (уже деперсонализированная) — контекст только
            # для LLM-ответа; retrieval/red line её не видят.
            "history": history or [],
            "exclude_ids": exclude_ids or set(),
            # FR-36: None — фильтр не нужен, set() (в т.ч. пустой) — статьи
            # видны только из allow-list. Вызывающий клинический путь всегда
            # передаёт множество (возможно пустое).
            "allowed_work_ids": allowed_work_ids,
        }

    async def stream(self, query: str, **kwargs) -> AsyncIterator[dict]:
        """Ответ по сниппетам токенами: retrieve, затем SSE-поток LLM.

        Отдаёт `{"delta": str}` по мере генерации и последний `{"answer": dict}`
        в формате invoke(). Любой сбой LLM после начала потока завершается
        детерминированным build_rag_answer: UI заменит текст финальным.
        """
        state = await anyio.to_thread.run_sync(lambda: self._state(query, **kwargs))
        state = {**state, **await anyio.to_thread.run_sync(self._retrieve, state)}
        chunks = state.get("chunks") or []
        payload = [{"text": c.text, "source_title": c.source_title} for c in chunks]
        result: RAGAnswer | None = None
        if chunks and detect_redline(query) is None:
            parts: list[str] = []
            try:
                async for piece in llm_router.compose_answer_stream(
                    state.get("history") or [], query, payload
                ):
                    parts.append(piece)
                    yield {"delta": piece}
                result = _finalize_llm_answer(llm_router.clean_answer("".join(parts)), chunks)
            except Exception as exc:  # транспорт/SSE/парсер — не ломаем ответ
                logger.warning(
                    "assistant LLM-поток прервался (%s: %s) — детерминированный ответ",
                    type(exc).__name__,
                    exc,
                )
        if result is None:
            result = build_rag_answer(
                query,
                chunks,
                patient_context=state.get("patient_context"),
                patient_contexts=state.get("patient_contexts"),
            )
        yield {"answer": _answer_dict(state, result)}


# --- Разделяемые клиенты процесса -------------------------------------------
# Один Qdrant-клиент и один скомпилированный граф на процесс: LangGraph
# компилируется лениво в build(), graph.invoke потокобезопасен.
_qdrant_client = None
_shared_factory: AgentFactory | None = None

def qdrant_client():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient

        _qdrant_client = QdrantClient(url=settings.qdrant_url)
    return _qdrant_client

def shared_factory() -> AgentFactory:
    global _shared_factory
    if _shared_factory is None:
        _shared_factory = AgentFactory(qdrant_client())
    return _shared_factory

__all__ = ["AgentFactory", "AgentState", "qdrant_client", "shared_factory"]

# --- Граница LLM: деперсонализация (docs/12) -------------------------------

def scrub_for_llm(
    query: str,
    patient_context: dict | None,
    names: list[str] | None = None,
) -> tuple[str, dict | None, dict]:
    """Маскирует ПДн в запросе и контексте пациента перед вызовом LLM.

    Возвращает (query, patient_context, отчёт). Отчёт содержит только категории
    и количество — исходные значения ПДн не покидают процесс.

    Маскируются: запрос врача целиком (он может содержать ФИО/телефон/адрес) и
    свободный текст карты (ФИО, дневниковые записи). Числовые поля (баллы,
    триаж, тренд) — не ПДн и остаются как есть.

    Текст проходит единый span-based путь services.depersonalize.scrub_text:
    структурные правила + контрольные числа + ML-backend (ФИО/адреса) +
    deny-list ростра. В CC_PROD_MODE при недоступном ML-backend поднимается
    DepersonalizationUnavailable → 503, сырой текст в LLM не уходит.
    """
    counts: dict[str, int] = {}

    def _add(found) -> None:
        for f in found:
            counts[f.kind] = counts.get(f.kind, 0) + 1

    masked_query = scrub_text(query, names=names)
    _add(masked_query.findings)

    masked_ctx = None
    if patient_context is not None:
        masked_ctx = dict(patient_context)
        fio = masked_ctx.get("fio")
        if fio:
            # ФИО пациента маскируется всегда, даже если его нет в deny-list
            # (врач мог открыть карту по id без явного упоминания).
            name_findings = scrub_text(str(fio), names=names or [str(fio)]).findings
            _add(name_findings)
            masked_ctx["fio"] = "[ПАЦИЕНТ]"
        diary = masked_ctx.get("recent_diary") or []
        masked_diary = []
        for entry in diary:
            content = entry.get("content")
            if content:
                scrubbed = scrub_text(str(content), names=names)
                _add(scrubbed.findings)
                masked_diary.append({**entry, "content": scrubbed.text})
            else:
                masked_diary.append(entry)
        if "recent_diary" in masked_ctx:
            masked_ctx["recent_diary"] = masked_diary

    report = {"applied": bool(counts), "masked": counts}
    return masked_query.text, masked_ctx, report
