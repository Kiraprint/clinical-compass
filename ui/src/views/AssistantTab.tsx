import { useEffect, useState } from "react";
import { api, AssistantAnswer } from "../api";

const TOPICS = [
  { value: "", label: "Все" },
  { value: "ШКАЛЫ", label: "Шкалы" },
  { value: "ТРИАЖ", label: "Триаж" },
  { value: "РЕКОМЕНДАЦИИ", label: "Рекомендации" },
  { value: "ПРЕПАРАТЫ", label: "Препараты" },
] as const;

export default function AssistantTab() {
  const [query, setQuery] = useState("");
  const [topic, setTopic] = useState("");
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [points, setPoints] = useState<number | null>(null);

  useEffect(() => {
    api.assistantStatus().then((s) => setPoints(s.points)).catch(() => setPoints(null));
  }, []);

  const ask = async () => {
    if (!query.trim()) return;
    setBusy(true);
    setError("");
    setAnswer(null);
    try {
      setAnswer(await api.assistantAsk(query.trim(), topic || undefined));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <h2>Ассистент врача</h2>
      <p style={{ color: "#5a6478", fontSize: 13 }}>
        Справочный поиск по базе клинических рекомендаций и шкал.
        {points !== null && <> Корпус: {points} чанков.</>}
      </p>

      <div className="row">
        <label>
          Раздел
          <select value={topic} onChange={(e) => setTopic(e.target.value)}>
            {TOPICS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label>
        Вопрос
        <textarea
          rows={2}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Например: какой порог тяжести у шкалы PHQ-9?"
        />
      </label>

      <div className="row" style={{ marginTop: 10 }}>
        <button disabled={busy || !query.trim()} onClick={ask}>
          {busy ? "Поиск…" : "Спросить"}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {answer && (
        <div style={{ marginTop: 16 }}>
          <pre className="assistant-answer">{answer.answer}</pre>
          {answer.citations.length > 0 && (
            <div className="assistant-sources">
              <strong>Источники:</strong>
              <ul>
                {answer.citations.map((c, i) => (
                  <li key={i}>
                    «{c.source_title}» · {c.topic}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
