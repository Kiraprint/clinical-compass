import { useCallback, useEffect, useState } from "react";
import { api, ApiError, ClinicianKbWork, KB_DESIGN_LABELS, KbVerdictStatus } from "../api";
import DrugLibraryTab from "./DrugLibraryTab";

/**
 * «Библиотека» врача: статьи базы знаний + справочник препаратов.
 *
 * FR-36 (docs/04, docs/15 §12): публикация статьи в индекс — решение админа
 * (общая КБ), а видимость в ответах ассистента — персональная. Врач видит
 * только статьи, которые одобрил сам; гайдлайны ВОЗ доступны всегда.
 */

type LibrarySection = "articles" | "drugs";
type StatusFilter = "new" | "approved" | "rejected" | "all";

const SECTIONS: { id: LibrarySection; label: string }[] = [
  { id: "articles", label: "Статьи" },
  { id: "drugs", label: "Препараты" },
];

const STATUS_FILTERS: { value: StatusFilter; label: string }[] = [
  { value: "new", label: "Новые" },
  { value: "approved", label: "Одобрено вами" },
  { value: "rejected", label: "Отклонено вами" },
  { value: "all", label: "Все" },
];

const VERDICT: Record<KbVerdictStatus, { label: string; tone: string }> = {
  met: { label: "выполнено", tone: "GREEN" },
  not_met: { label: "не выполнено", tone: "RED" },
  insufficient_info: { label: "нет данных", tone: "YELLOW" },
  not_checked: { label: "не проверялось", tone: "" },
};

const SOURCE_LABELS: Record<string, string> = { pubmed: "PubMed", who: "ВОЗ" };

function fmtDate(value: string | null): string {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("ru-RU");
}

function verdictBadge(status: KbVerdictStatus, label: string) {
  const meta = VERDICT[status] ?? { label: status, tone: "" };
  return (
    <span
      key={label}
      className={`badge ${meta.tone}`}
      title={`${label}: ${meta.label}`}
      style={meta.tone ? undefined : { background: "#eef1f6", color: "#5a6478" }}
    >
      {label}
    </span>
  );
}

function ArticlesLibrary() {
  const [status, setStatus] = useState<StatusFilter>("new");
  const [items, setItems] = useState<ClinicianKbWork[]>([]);
  const [busy, setBusy] = useState(true);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const r = await api.clinicianKbWorks({ status, kind: "article" });
      setItems(r.works);
      setError("");
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [status]);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(work: ClinicianKbWork, decision: "approved" | "rejected") {
    setPending(work.work_id);
    setError("");
    try {
      const updated = await api.clinicianKbDecision(work.work_id, decision);
      // Решение уже в ответе: статья либо остаётся в текущем фильтре (обновлённая),
      // либо уходит из него — без повторного GET списка.
      const stays =
        status === "all" ||
        (status === "new" ? updated.decision === null : updated.decision === status);
      setItems((cur) =>
        stays
          ? cur.map((w) => (w.work_id === updated.work_id ? updated : w))
          : cur.filter((w) => w.work_id !== updated.work_id),
      );
    } catch (e: unknown) {
      setError(e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="card">
      <h2>Статьи базы знаний</h2>
      <p className="small" style={{ color: "#5a6478" }}>
        Ассистент опирается только на статьи, которые вы одобрили сами. Гайдлайны ВОЗ
        доступны всегда; отклонённые вами статьи в ответах не появляются.
      </p>

      <div className="tabs" role="group" aria-label="Фильтр по моему решению">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.value}
            className={status === f.value ? "active" : ""}
            onClick={() => setStatus(f.value)}
          >
            {f.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="alert-danger" role="alert">
          {error}{" "}
          <button className="ghost" onClick={() => void load()}>
            Повторить
          </button>
        </div>
      )}
      {busy && <p style={{ color: "#5a6478" }}>Загрузка…</p>}
      {!busy && !error && items.length === 0 && (
        <p style={{ color: "#5a6478" }}>
          {status === "new"
            ? "Новых статей нет: всё опубликованное вы уже просмотрели."
            : "Ничего не найдено."}
        </p>
      )}

      {items.length > 0 && (
        <p className="muted small" style={{ margin: "0 0 8px" }}>
          В ответах ассистента используется аннотация статьи; полный текст
          открывается по названию или ссылке «Полный текст».
        </p>
      )}
      {items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Статья</th>
                <th>Источник</th>
                <th>Дизайн</th>
                <th>Критерии</th>
                <th>Ваше решение</th>
              </tr>
            </thead>
            <tbody>
              {items.map((w) => {
                const blocked = w.criteria.verdicts.filter((v) => v.status === "not_met");
                return (
                  <tr key={w.work_id}>
                    <td style={{ whiteSpace: "normal", minWidth: 260 }}>
                      {w.source_url ? (
                        <a href={w.source_url} target="_blank" rel="noreferrer">
                          {w.title}
                        </a>
                      ) : (
                        w.title
                      )}
                      {w.fulltext_url && w.fulltext_url !== w.source_url && (
                        <div style={{ marginTop: 4 }}>
                          <a
                            href={w.fulltext_url}
                            target="_blank"
                            rel="noreferrer"
                            style={{ fontSize: 12 }}
                            title="В ответах ассистента используется аннотация; полный текст — по этой ссылке"
                          >
                            Полный текст
                          </a>
                        </div>
                      )}
                    </td>
                    <td style={{ whiteSpace: "normal" }}>
                      {SOURCE_LABELS[w.source] ?? w.source}
                      {w.year ? ` · ${w.year}` : ""}
                    </td>
                    <td style={{ whiteSpace: "normal", fontSize: 12, color: "#5a6478" }}>
                      {KB_DESIGN_LABELS[w.design?.design] ?? w.design?.design ?? "—"}
                    </td>
                    <td style={{ whiteSpace: "normal" }}>
                      <span
                        className={`badge ${w.criteria.decision === "exclude" ? "RED" : "GREEN"}`}
                        title="Решение детерминированных критериев отбора (docs/13)"
                      >
                        {w.criteria.decision === "exclude" ? "не прошла" : "прошла"}
                      </span>{" "}
                      {blocked.map((v) => verdictBadge(v.status, v.label))}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <div style={{ marginBottom: 6 }}>
                        {w.decision === "approved" && (
                          <>
                            <span className="badge GREEN">Одобрено вами</span>
                            {w.reviewed_at && (
                              <span className="small" style={{ color: "#5a6478" }}>
                                {" "}
                                {fmtDate(w.reviewed_at)}
                              </span>
                            )}
                          </>
                        )}
                        {w.decision === "rejected" && (
                          <>
                            <span className="badge RED">Отклонено вами</span>
                            {w.reviewed_at && (
                              <span className="small" style={{ color: "#5a6478" }}>
                                {" "}
                                {fmtDate(w.reviewed_at)}
                              </span>
                            )}
                          </>
                        )}
                        {w.decision === null && (
                          <span
                            className="badge"
                            style={{ background: "#eef1f6", color: "#5a6478" }}
                          >
                            Не решено
                          </span>
                        )}
                      </div>
                      <div style={{ display: "flex", gap: 6 }}>
                        {w.decision !== "approved" && (
                          <button
                            disabled={pending === w.work_id}
                            onClick={() => void decide(w, "approved")}
                          >
                            Одобрить
                          </button>
                        )}
                        {w.decision !== "rejected" && (
                          <button
                            className="ghost"
                            disabled={pending === w.work_id}
                            onClick={() => void decide(w, "rejected")}
                          >
                            Отклонить
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function LibraryView() {
  const [section, setSection] = useState<LibrarySection>("articles");

  return (
    <div>
      <div className="tabs" role="tablist" aria-label="Разделы библиотеки">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            role="tab"
            aria-selected={section === s.id}
            className={section === s.id ? "active" : ""}
            onClick={() => setSection(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>
      {section === "drugs" ? <DrugLibraryTab /> : <ArticlesLibrary />}
    </div>
  );
}
