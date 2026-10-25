"""ИИ-агент врача (LangGraph). Гибридный поиск + ответ с цитатами (docs/07, docs/08).

Граф: retrieve (Qdrant hybrid) → answer (чистая сборка из ядра, red line).
LLM — за SPI; по умолчанию детерминированная сборка (docs/03 §1.3), офлайн.
Потребляет только открытый не-ПДн корпус: связи с пациентом нет.
"""

from __future__ import annotations

from typing import Sequence, TypedDict

import re
from langgraph.graph import END, START, StateGraph

from compass.agent import RAGChunk, build_rag_answer, detect_redline

from app.config import settings
from app.services.depersonalize import scrub_text

from app.services import kb as kb_store
from app.services.embeddings import get_embedder, profile_default_min_score
from app.services.reranker import get_reranker

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

class AgentState(TypedDict, total=False):
    query: str
    retrieval_query: str | None
    topic: str | None
    topics: tuple[str, ...]
    limit: int
    chunks: list[RAGChunk]
    exclude_ids: set[str]
    allowed_work_ids: set[str] | None
    patient_context: dict | None
    patient_contexts: list[dict] | None
    corpus_drugs: list[dict] | None
    answer: dict

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
        """
        if detect_redline(redline_query if redline_query is not None else query) is not None:
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
        result = build_rag_answer(
            state["query"],
            state.get("chunks", []),
            patient_context=state.get("patient_context"),
            patient_contexts=state.get("patient_contexts"),
        )
        return {
            "answer": {
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
        }

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
    ) -> dict:
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
            from app.services import llm_router  # lazy: config-зависимый импорт
            topic_list = llm_router.route_topics(search_query)
        topic = topic_list[0] if topic_list else None
        return self.build().invoke(
            {
                "query": query,
                "retrieval_query": search_query,
                "topic": topic,
                "topics": topic_list,
                "limit": limit,
                "patient_context": patient_context,
                "patient_contexts": patient_contexts,
                "corpus_drugs": corpus_drugs,
                "exclude_ids": exclude_ids or set(),
                # FR-36: None — фильтр не нужен, set() (в т.ч. пустой) — статьи
                # видны только из allow-list. Вызывающий клинический путь всегда
                # передаёт множество (возможно пустое).
                "allowed_work_ids": allowed_work_ids,
            }
        )["answer"]

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
