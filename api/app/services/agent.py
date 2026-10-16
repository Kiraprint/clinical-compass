"""ИИ-агент врача (LangGraph). Гибридный поиск + ответ с цитатами (docs/07, docs/08).

Граф: retrieve (Qdrant hybrid) → answer (чистая сборка из ядра, red line).
LLM — за SPI; по умолчанию детерминированная сборка (docs/03 §1.3), офлайн.
Потребляет только открытый не-ПДн корпус: связи с пациентом нет.
"""

from __future__ import annotations

from typing import TypedDict

import re
from langgraph.graph import END, START, StateGraph

from compass.agent import RAGChunk, build_rag_answer, detect_redline

from app.services import kb as kb_store
from app.services.embeddings import HashEmbedder

_embedder = HashEmbedder()

# GATE: лексическое покрытие (docs/07 §5 — sparse ловит точные термины, dense —
# смысл, но для демо-эмбеддера минимальный порог на точное совпадение терминов).
# Слова длины <3 и стоп-слова не считаем значимыми.
_GATE_MIN_TOKENS = 1
_STOP = {"что", "как", "какой", "какая", "какое", "у", "в", "на", "и", "это", "для", "из"}


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zа-яё0-9]{3,}", text.lower()) if t not in _STOP}


def _lexical_overlap(query: str, text: str) -> int:
    """Число значимых токенов запроса, найденных в тексте чанка."""
    q = _content_tokens(query)
    t = _content_tokens(text)
    return len(q & t)


def _relevance_gate(query: str, hits: list[dict], limit: int) -> list[dict]:
    """Оставляет чанки, где покрыт хотя бы один значимый токен запроса.

    Если у запроса нет ни одного значимого токена (чистая стоп-фраза/шуточный
    запрос) — считаем нерелевантным, чтобы не отвечать наугад (поверхность
    базы знаний, а не симптоматика).
    """
    kept = [h for h in hits if _lexical_overlap(query, h.get("text", "")) >= _GATE_MIN_TOKENS]
    return kept[:limit]


class AgentState(TypedDict, total=False):
    query: str
    topic: str | None
    limit: int
    chunks: list[RAGChunk]
    answer: dict


class AgentFactory:
    def __init__(self, client) -> None:
        self._client = client

    def _retrieve(self, state: AgentState) -> AgentState:
        if detect_redline(state["query"]) is not None:
            return {"chunks": []}  # отказ формируется в answer, поиск не нужен
        hits = kb_store.hybrid_search(
            self._client,
            _embedder,
            state["query"],
            topic=state.get("topic"),
            limit=state.get("limit", 5),
        )
        hits = _relevance_gate(state["query"], hits, state.get("limit", 5))
        chunks = [RAGChunk(text=h["text"], source_title=h["source_title"], guideline_id=h["guideline_id"], topic=h["topic"], score=h["score"]) for h in hits]
        return {"chunks": chunks}

    def _answer(self, state: AgentState) -> AgentState:
        q = state["query"]
        chunks = state.get("chunks", [])
        result = build_rag_answer(q, chunks)
        return {
            "answer": {
                "answer": result.answer,
                "grounded": result.grounded,
                "refused": result.refused,
                "refusal_code": result.refusal_code,
                "citations": [
                    {"source_title": c.source_title, "guideline_id": c.guideline_id, "topic": c.topic, "score": c.score}
                    for c in result.citations
                ],
            }
        }

    def build(self):
        graph = StateGraph(AgentState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("answer", self._answer)
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", "answer")
        graph.add_edge("answer", END)
        return graph.compile()

    def invoke(self, query: str, *, topic: str | None = None, limit: int = 5) -> dict:
        return self.build().invoke({"query": query, "topic": topic, "limit": limit})["answer"]


__all__ = ["AgentFactory", "AgentState"]
