import { useCallback, useEffect, useState } from "react";
import {
  api,
  AuditEvent,
  KB_DESIGN_LABELS,
  KbAppraisal,
  KbCriteria,
  KbHealth,
  KbRun,
  KbVerdictStatus,
  KbWork,
} from "../api";

type KbFilter = "candidate" | "approved" | "rejected" | "";

const FILTERS: { value: KbFilter; label: string }[] = [
  { value: "candidate", label: "Кандидаты" },
  { value: "approved", label: "Одобренные" },
  { value: "rejected", label: "Отклонённые" },
  { value: "", label: "Все" },
];

const WORK_STATUS: Record<string, { label: string; tone: string }> = {
  candidate: { label: "Кандидат", tone: "YELLOW" },
  approved: { label: "Одобрено", tone: "GREEN" },
  rejected: { label: "Отклонено", tone: "RED" },
  excluded: { label: "Исключено критериями", tone: "RED" },
  superseded: { label: "Заменено новой ревизией", tone: "YELLOW" },
};

const RUN_STATUS: Record<string, { label: string; tone: string }> = {
  running: { label: "идёт", tone: "YELLOW" },
  ok: { label: "успешно", tone: "GREEN" },
  partial: { label: "частично", tone: "YELLOW" },
  failed: { label: "ошибка", tone: "RED" },
};

const VERDICT: Record<KbVerdictStatus, { label: string; tone: string }> = {
  met: { label: "выполнено", tone: "GREEN" },
  not_met: { label: "не выполнено", tone: "RED" },
  insufficient_info: { label: "недостаточно данных", tone: "YELLOW" },
  not_checked: { label: "не проверялось", tone: "" },
};

const OVERALL: Record<string, string> = {
  high: "высокая уверенность",
  moderate: "умеренная уверенность",
  low: "низкая уверенность",
  critically_low: "критически низкая уверенность",
  not_assessed: "не оценивалось",
};

function StatusBadge({ status }: { status: KbVerdictStatus }) {
  const meta = VERDICT[status] ?? { label: status, tone: "" };
  return (
    <span
      className={`badge ${meta.tone}`}
      style={meta.tone ? undefined : { background: "#eef1f6", color: "#5a6478" }}
    >
      {meta.label}
    </span>
  );
}

function fmtTime(value: string | null): string {
  if (!value) return "—";
  return value.slice(0, 16).replace("T", " ");
}

function designLabel(code: string | undefined): string {
  if (!code) return "дизайн не определён";
  return KB_DESIGN_LABELS[code] ?? code;
}

function EvidenceBlock({ evidence }: { evidence: KbWork["criteria"]["verdicts"][number]["evidence"] }) {
  if (!evidence) return null;
  return (
    <div style={{ marginTop: 4, fontSize: 12, color: "#3d4658" }}>
      <blockquote
        style={{
          margin: "4px 0",
          padding: "4px 10px",
          borderLeft: "3px solid #cfd6e2",
          color: "#5a6478",
        }}
      >
        «{evidence.quote}»
      </blockquote>
      <span style={{ color: "#8a94a8" }}>
        {evidence.locator}
        {evidence.source_url && (
          <>
            {" · "}
            <a href={evidence.source_url} target="_blank" rel="noreferrer">
              источник
            </a>
          </>
        )}
      </span>
    </div>
  );
}

function CriteriaBlock({ criteria }: { criteria: KbCriteria }) {
  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ fontSize: 13 }}>
        <strong>Критерии отбора</strong>
        <span className="badge" style={{ background: "#eef1f6", color: "#3d4658" }}>
          {criteria.decision === "include" ? "включить" : "исключить"}
        </span>
        <span style={{ color: "#8a94a8" }}>версия {criteria.version}</span>
        <span style={{ color: "#8a94a8" }}>
          релевантность {(criteria.relevance_score ?? 0).toFixed(2)}
        </span>
      </div>
      {(criteria.reasons ?? []).length > 0 && (
        <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: 13, color: "#3d4658" }}>
          {(criteria.reasons ?? []).map((reason, i) => (
            <li key={i}>{reason}</li>
          ))}
        </ul>
      )}
      <div style={{ marginTop: 6 }}>
        {(criteria.verdicts ?? []).map((v) => (
          <div key={v.key} style={{ padding: "4px 0", borderTop: "1px solid #f0f2f7" }}>
            <div className="row" style={{ gap: 6 }}>
              <StatusBadge status={v.status} />
              <span style={{ fontSize: 13, fontWeight: 600 }}>{v.label}</span>
              {v.manual_review && (
                <span style={{ fontSize: 12, color: "#96680b" }}>
                  нужно решение врача{v.manual_review_reason ? `: ${v.manual_review_reason}` : ""}
                </span>
              )}
            </div>
            <div style={{ fontSize: 12, color: "#3d4658" }}>{v.explanation}</div>
            <EvidenceBlock evidence={v.evidence} />
          </div>
        ))}
      </div>
    </div>
  );
}

function AppraisalBlock({ appraisal }: { appraisal: KbAppraisal }) {
  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ fontSize: 13 }}>
        <strong>Оценка качества: {appraisal.tool_label || appraisal.tool}</strong>
        <span className="badge" style={{ background: "#eef1f6", color: "#3d4658" }}>
          {OVERALL[appraisal.overall] ?? appraisal.overall}
        </span>
        {appraisal.experimental && (
          <span style={{ fontSize: 12, color: "#96680b" }}>экспериментальная оценка</span>
        )}
      </div>
      {!appraisal.applicable && (
        <p style={{ margin: "4px 0", fontSize: 12, color: "#5a6478" }}>{appraisal.reason}</p>
      )}
      {appraisal.summary && (
        <p style={{ margin: "4px 0", fontSize: 13, color: "#3d4658" }}>{appraisal.summary}</p>
      )}
      {(appraisal.items ?? []).map((item) => (
        <div key={item.key} style={{ padding: "4px 0", borderTop: "1px solid #f0f2f7" }}>
          <div className="row" style={{ gap: 6 }}>
            <StatusBadge status={item.status} />
            <span style={{ fontSize: 13 }}>{item.label}</span>
          </div>
          {item.explanation && (
            <div style={{ fontSize: 12, color: "#3d4658" }}>{item.explanation}</div>
          )}
          <EvidenceBlock evidence={item.evidence} />
        </div>
      ))}
    </div>
  );
}

function WorkRow({
  work,
  busy,
  rejecting,
  rejectReason,
  onApprove,
  onRejectStart,
  onRejectCancel,
  onRejectReason,
  onRejectSubmit,
}: {
  work: KbWork;
  busy: boolean;
  rejecting: boolean;
  rejectReason: string;
  onApprove: () => void;
  onRejectStart: () => void;
  onRejectCancel: () => void;
  onRejectReason: (value: string) => void;
  onRejectSubmit: () => void;
}) {
  const status = WORK_STATUS[work.status] ?? { label: work.status, tone: "" };
  return (
    <div style={{ borderTop: "1px solid #e2e6ee", padding: "10px 0" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div style={{ flex: "1 1 320px" }}>
          <div style={{ fontWeight: 600 }}>{work.title}</div>
          <div style={{ fontSize: 12, color: "#5a6478" }}>
            {work.source === "pubmed" ? "PubMed" : "ВОЗ"} · {work.external_id}
            {work.year ? ` · ${work.year}` : ""} · {designLabel(work.design?.design)}
            {work.journal ? ` · ${work.journal}` : ""}
            {work.revision ? ` · ревизия ${work.revision}` : ""}
          </div>
        </div>
        <span
          className={`badge ${status.tone}`}
          style={status.tone ? undefined : { background: "#eef1f6", color: "#5a6478" }}
        >
          {status.label}
        </span>
      </div>

      <div className="row" style={{ marginTop: 4, fontSize: 12, color: "#5a6478" }}>
        <span>
          Решение критериев: {work.criteria?.decision === "include" ? "включить" : "исключить"}
        </span>
        {(work.criteria?.reasons ?? []).length > 0 && (
          <span>Причины: {(work.criteria.reasons ?? []).join("; ")}</span>
        )}
        {work.retraction !== "none" && <span>Ретракция: {work.retraction}</span>}
      </div>

      <details style={{ marginTop: 6 }}>
        <summary style={{ cursor: "pointer", fontSize: 13, color: "#2f6fed" }}>Подробнее</summary>
        {work.reject_reason && (
          <p style={{ fontSize: 13, color: "#b3261e" }}>
            Причина отклонения: {work.reject_reason}
          </p>
        )}
        {work.criteria && <CriteriaBlock criteria={work.criteria} />}
        {work.appraisal && <AppraisalBlock appraisal={work.appraisal} />}
        {work.abstract && (
          <div style={{ marginTop: 8, fontSize: 13, color: "#3d4658" }}>
            <strong style={{ fontSize: 13 }}>Аннотация</strong>
            <p style={{ margin: "4px 0" }}>{work.abstract}</p>
            {work.results && <p style={{ margin: "4px 0" }}>{work.results}</p>}
          </div>
        )}
        <div className="row" style={{ marginTop: 8, fontSize: 13 }}>
          {work.pmid && (
            <a
              href={`https://pubmed.ncbi.nlm.nih.gov/${work.pmid}/`}
              target="_blank"
              rel="noreferrer"
            >
              PubMed
            </a>
          )}
          {work.doi && (
            <a href={`https://doi.org/${work.doi}`} target="_blank" rel="noreferrer">
              DOI
            </a>
          )}
          {work.fulltext_url && (
            <a href={work.fulltext_url} target="_blank" rel="noreferrer">
              Полный текст
            </a>
          )}
          {!work.fulltext_url && work.url && (
            <a href={work.url} target="_blank" rel="noreferrer">
              Ссылка на источник
            </a>
          )}
          <span style={{ color: "#8a94a8" }}>
            {work.indexed ? `в индексе, чанков: ${work.chunk_count}` : "не индексирован"}
            {work.last_seen_at ? ` · найден ${fmtTime(work.last_seen_at)}` : ""}
          </span>
        </div>
      </details>

      {work.status === "candidate" && (
        <div style={{ marginTop: 8 }}>
          {!rejecting ? (
            <div className="row">
              <button onClick={onApprove} disabled={busy}>
                Принять
              </button>
              <button className="danger" onClick={onRejectStart} disabled={busy}>
                Отклонить
              </button>
            </div>
          ) : (
            <div className="row" style={{ alignItems: "flex-start" }}>
              <input
                value={rejectReason}
                onChange={(e) => onRejectReason(e.target.value)}
                placeholder="Причина отклонения (не короче 3 символов)"
                style={{ flex: "1 1 260px" }}
                maxLength={2000}
              />
              <button
                className="danger"
                onClick={onRejectSubmit}
                disabled={busy || rejectReason.trim().length < 3}
              >
                Подтвердить
              </button>
              <button className="ghost" onClick={onRejectCancel} disabled={busy}>
                Отмена
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function runStats(run: KbRun): string {
  const entries = Object.entries(run.stats ?? {});
  if (entries.length === 0) return "нет статистики";
  return entries
    .map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
    .join(" · ");
}

function KbSection() {
  const [health, setHealth] = useState<KbHealth | null>(null);
  const [runs, setRuns] = useState<KbRun[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [works, setWorks] = useState<KbWork[]>([]);
  const [filter, setFilter] = useState<KbFilter>("candidate");
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rejectId, setRejectId] = useState<string | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  const load = useCallback(async (status: KbFilter) => {
    setLoadError("");
    try {
      const [h, w, r] = await Promise.all([
        api.adminKbHealth(),
        api.adminKbWorks({ status, limit: 100 }),
        api.adminKbRuns(5),
      ]);
      setHealth(h);
      setCounts(w.counts ?? h.counts ?? {});
      setWorks(w.works);
      setRuns(r.runs);
    } catch (e: unknown) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load(filter);
  }, [filter, load]);

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusyId(id);
    setActionError("");
    try {
      await fn();
      setRejectId(null);
      setRejectReason("");
      await load(filter);
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const lastRun = health?.last_run ?? null;

  return (
    <div className="card">
      <h2>База знаний</h2>

      <div className="row" style={{ fontSize: 13, color: "#5a6478" }}>
        {lastRun ? (
          <>
            <span>Последний прогон: {fmtTime(lastRun.started_at)}</span>
            <span
              className={`badge ${RUN_STATUS[lastRun.status]?.tone ?? ""}`}
              style={
                RUN_STATUS[lastRun.status]?.tone
                  ? undefined
                  : { background: "#eef1f6", color: "#5a6478" }
              }
            >
              {RUN_STATUS[lastRun.status]?.label ?? lastRun.status}
            </span>
            <span>триггер: {lastRun.trigger}</span>
          </>
        ) : (
          <span>Прогонов обогащения ещё не было.</span>
        )}
      </div>

      <div className="row" style={{ marginTop: 6, fontSize: 13 }}>
        <span>Кандидаты: {counts.candidate ?? 0}</span>
        <span>Одобренные: {counts.approved ?? 0}</span>
        <span>Исключённые: {counts.excluded ?? 0}</span>
        <span>Отклонённые: {counts.rejected ?? 0}</span>
      </div>

      {lastRun && (
        <p style={{ fontSize: 12, color: "#8a94a8", margin: "6px 0 0" }}>{runStats(lastRun)}</p>
      )}

      {runs.length > 0 && runs.some((r) => r.errors.length > 0) && (
        <div className="alert-warn" style={{ marginTop: 10 }}>
          <div style={{ fontWeight: 600, fontSize: 13 }}>Ошибки прогонов</div>
          {runs
            .filter((r) => r.errors.length > 0)
            .map((r) => (
              <div key={r.id} style={{ fontSize: 12 }}>
                {fmtTime(r.started_at)}:
                <ul style={{ margin: "2px 0 4px", paddingLeft: 18 }}>
                  {r.errors.map((err, i) => (
                    <li key={i}>{err}</li>
                  ))}
                </ul>
              </div>
            ))}
        </div>
      )}

      <div className="row" style={{ margin: "10px 0 4px" }}>
        {FILTERS.map((f) => (
          <button
            key={f.value || "all"}
            className={filter === f.value ? "" : "ghost"}
            onClick={() => setFilter(f.value)}
            disabled={busyId !== null}
          >
            {f.label}
          </button>
        ))}
      </div>

      {!loaded && <p style={{ color: "#5a6478" }}>Загрузка…</p>}
      {loadError && <p className="error">Не удалось загрузить базу знаний: {loadError}</p>}
      {actionError && <p className="error">Действие не выполнено: {actionError}</p>}

      {loaded && !loadError && works.length === 0 && (
        <p style={{ color: "#5a6478" }}>
          {filter === "candidate"
            ? "Очередь кандидатов пуста."
            : "Нет источников для выбранного фильтра."}
        </p>
      )}

      {works.map((work) => (
        <WorkRow
          key={work.id}
          work={work}
          busy={busyId === work.id}
          rejecting={rejectId === work.id}
          rejectReason={rejectReason}
          onApprove={() => void act(work.id, () => api.adminKbApprove(work.id))}
          onRejectStart={() => {
            setRejectId(work.id);
            setRejectReason("");
            setActionError("");
          }}
          onRejectCancel={() => {
            setRejectId(null);
            setRejectReason("");
          }}
          onRejectReason={setRejectReason}
          onRejectSubmit={() =>
            void act(work.id, () => api.adminKbReject(work.id, rejectReason.trim()))
          }
        />
      ))}

      <p style={{ color: "#8a94a8", fontSize: 12, marginTop: 10 }}>
        Статья попадает в индекс только после ручного принятия; гайдлайны ВОЗ публикуются при
        подтверждённой лицензии. Оценка качества на аннотации неполная — решает врач.
      </p>
    </div>
  );
}

export default function AdminView() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .adminAudit()
      .then((data) => {
        setEvents(data.items);
        setLoaded(true);
      })
      .catch((e: unknown) => {
        setLoaded(true);
        setError(e instanceof Error ? e.message : String(e));
      });
  }, []);

  return (
    <>
      <KbSection />

      <div className="card">
        <h2>Журнал аудита (append-only)</h2>
        {!loaded && <p style={{ color: "#5a6478" }}>Загрузка…</p>}
        {error && <p className="error">Не удалось загрузить журнал: {error}</p>}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Время</th>
                <th>Действие</th>
                <th>Объект</th>
                <th>Актор</th>
                <th>IP</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td>{e.occurred_at.slice(0, 19).replace("T", " ")}</td>
                  <td>{e.action}</td>
                  <td>
                    {e.object_type} {e.object_id ?? ""}
                  </td>
                  <td>{e.actor_id ? e.actor_id.slice(0, 8) : "—"}</td>
                  <td>{e.ip ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p style={{ color: "#8a94a8", fontSize: 12 }}>
          Журнал защищён от правки на уровне БД (триггер append-only). Привязка врач→пациент
          выполняется через симулятор/API.
        </p>
      </div>

      {/* FR-35: правовые документы — статические страницы, открываются без входа. */}
      <div className="card">
        <h2>Правовые документы</h2>
        <p style={{ color: "#5a6478", fontSize: 13, margin: "0 0 8px" }}>
          Шаблоны для юридической проверки; реквизиты оператора не заполнены.
        </p>
        <ul style={{ margin: 0, paddingLeft: 20, fontSize: 14 }}>
          <li>
            <a href="/legal/terms">Пользовательское соглашение</a>
          </li>
          <li>
            <a href="/legal/consent">Согласие на обработку персональных данных</a>
          </li>
        </ul>
      </div>
    </>
  );
}
