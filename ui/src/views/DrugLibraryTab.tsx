import { useEffect, useState } from "react";
import { api, Drug } from "../api";

/**
 * Библиотека препаратов (задача кейса 6, п. «библиотека» для врача).
 *
 * Источник — таблица drugs (ГРЛС, grls.rosminzdrav.ru), не ПДн. Поиск по
 * торговому названию/МНН на сервере (GET /drugs?q=). Раздел НЯ/противопоказаний
 * визуально выделен: предупреждения о безопасности терапии — обязательное условие
 * кейса. Научное обоснование НЯ — чанк «Фармаконадзор РФ 2025» в базе знаний
 * ассистента (docs/14).
 */

/** Задержка поиска по библиотеке препаратов, мс. */
const SEARCH_DEBOUNCE_MS = 300;

export default function DrugLibraryTab() {
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<Drug[]>([]);
  const [picked, setPicked] = useState<Drug | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    // Пауза перед запросом: не дёргаем сервер на каждое нажатие клавиши.
    const timer = window.setTimeout(() => {
      api
        .drugs(query.trim() || undefined)
        .then((r) => {
          if (!cancelled) {
            setItems(r.items);
            setError("");
          }
        })
        .catch((e: unknown) => {
          if (!cancelled) setError(e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          if (!cancelled) setBusy(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  if (picked) {
    return (
      <div className="card">
        <button onClick={() => setPicked(null)} style={{ marginBottom: 10 }}>
          ← К списку
        </button>
        <h2>{picked.trade_name}</h2>
        <p style={{ color: "#5a6478", fontSize: 13, marginTop: 0 }}>
          МНН: {picked.inn}
          {picked.inn_en ? ` (${picked.inn_en})` : ""} · {picked.form}
          <br />
          РУ: {picked.reg_certificate}
          {picked.reg_date ? ` от ${picked.reg_date}` : ""}
          {picked.revision_date ? `, ред. ${picked.revision_date}` : ""}
        </p>

        <h3>Показания</h3>
        <p>{picked.indications}</p>

        <h3>Способ применения и дозы</h3>
        <p>{picked.dosage}</p>

        <h3>Фармакология</h3>
        <p>{picked.pharmacology}</p>

        {/* Безопасность — визуально выделена (обязательное условие кейса). */}
        <div
          style={{
            background: "#fff4f2",
            border: "1px solid #f3b3a8",
            borderRadius: 8,
            padding: 12,
            marginTop: 14,
          }}
        >
          <h3 style={{ marginTop: 0, color: "#a32a18" }}>Нежелательные явления</h3>
          <p style={{ marginTop: 0 }}>{picked.adverse_events}</p>
        </div>
        <div
          style={{
            background: "#fffaf0",
            border: "1px solid #f0d59a",
            borderRadius: 8,
            padding: 12,
            marginTop: 10,
          }}
        >
          <h3 style={{ marginTop: 0, color: "#8a5a00" }}>Противопоказания</h3>
          <p style={{ marginTop: 0 }}>{picked.contraindications}</p>
        </div>

        {picked.special_notes && (
          <>
            <h3>Особые указания</h3>
            <p>{picked.special_notes}</p>
          </>
        )}

        {picked.source_urls.length > 0 && (
          <>
            {/* Провенанс: по какой записи ГРЛС и инструкции заполнены поля. */}
            <h3>Источники</h3>
            <ul style={{ fontSize: 13, paddingLeft: 18 }}>
              {picked.source_urls.map((url) => (
                <li key={url}>
                  <a href={url} target="_blank" rel="noreferrer">
                    {url}
                  </a>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Библиотека препаратов</h2>
      <p style={{ color: "#5a6478", fontSize: 13 }}>
        Справочник ГРЛС: показания, дозы, нежелательные явления и противопоказания.
      </p>
      <label>
        Поиск по названию или МНН
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Например: Авиандр или вортиоксетин"
        />
      </label>

      {error && <p style={{ color: "#b3261e" }}>{error}</p>}
      {busy && items.length === 0 && <p style={{ color: "#5a6478" }}>Загрузка…</p>}
      {!busy && !error && items.length === 0 && (
        <p style={{ color: "#5a6478" }}>Ничего не найдено.</p>
      )}

      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Торговое название</th>
                <th>МНН</th>
                <th>Форма</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((d) => (
                <tr key={d.drug_id}>
                  <td style={{ fontWeight: 600 }}>{d.trade_name}</td>
                  <td>{d.inn}</td>
                  <td>{d.form}</td>
                  <td>
                    <button onClick={() => setPicked(d)}>Открыть</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
