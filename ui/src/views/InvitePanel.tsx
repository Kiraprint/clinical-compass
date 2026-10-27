import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { InviteCreated, InviteRow } from "../api";

/**
 * FR-49: врач заводит карту пациента до появления аккаунта и получает
 * одноразовую ссылку. Почта не нужна — ссылку передают любым каналом.
 * Ссылка показывается ровно один раз: в БД лежит только хеш токена, поэтому
 * «показать снова» заменено перевыпуском.
 */
export default function InvitePanel() {
  const [open, setOpen] = useState(false);
  const [fio, setFio] = useState("");
  const [phone, setPhone] = useState("");
  const [birthDate, setBirthDate] = useState("");
  const [email, setEmail] = useState("");
  const [created, setCreated] = useState<InviteCreated | null>(null);
  const [items, setItems] = useState<InviteRow[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = () => {
    api
      .clinicianInvites()
      .then((r) => setItems(r.items))
      .catch(() => setItems([]));
  };

  useEffect(() => {
    if (open) load();
  }, [open]);

  const link = created
    ? `${window.location.origin}${created.link}`
    : "";

  async function create() {
    setBusy(true);
    setError("");
    try {
      const invite = await api.createInvite({
        fio: fio.trim() || undefined,
        phone: phone.trim() || undefined,
        birth_date: birthDate || undefined,
        email: email.trim() || undefined,
      });
      setCreated(invite);
      setFio("");
      setPhone("");
      setBirthDate("");
      setEmail("");
      load();
    } catch (e) {
      const apiErr = e instanceof ApiError ? e : null;
      setError(
        apiErr?.code === "INVITE_LIMIT"
          ? "Слишком много непринятых приглашений — отзовите лишние."
          : apiErr?.code === "PHONE_INVALID" || apiErr?.code === "BIRTH_DATE_INVALID"
            ? "Проверьте телефон и дату рождения."
            : "Не удалось создать приглашение.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  async function reissue(id: string) {
    setError("");
    try {
      const invite = await api.reissueInvite(id);
      setCreated(invite);
      load();
    } catch {
      setError("Не удалось перевыпустить ссылку.");
    }
  }

  async function revoke(id: string) {
    setError("");
    try {
      await api.revokeInvite(id);
      if (created?.id === id) setCreated(null);
      load();
    } catch {
      setError("Не удалось отозвать приглашение.");
    }
  }

  return (
    <details
      className="card invite-panel"
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="collapse-summary">
        Пригласить пациента{items && items.length ? ` (ожидают: ${items.length})` : ""}
      </summary>
      <p className="muted small">
        Врач заводит карту, пациент открывает ссылку и задаёт пароль. Почта не нужна —
        передайте ссылку любым удобным каналом. Ссылка действует 7 дней и показывается
        один раз.
      </p>
      <div className="invite-form">
        <label>
          ФИО пациента
          <input aria-label="ФИО пациента" value={fio} onChange={(e) => setFio(e.target.value)} />
        </label>
        <label>
          Телефон
          <input
            aria-label="Телефон пациента"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+7 926 123-45-67"
          />
        </label>
        <label>
          Дата рождения
          <input
            aria-label="Дата рождения пациента"
            type="date"
            value={birthDate}
            onChange={(e) => setBirthDate(e.target.value)}
          />
        </label>
        <label>
          E-mail (если знаете)
          <input
            aria-label="E-mail пациента"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <button className="primary" onClick={create} disabled={busy}>
          {busy ? "Создаём…" : "Создать ссылку"}
        </button>
      </div>

      {created && (
        <div className="invite-link">
          <p className="small">
            Ссылка для <strong>{created.fio ?? "пациента"}</strong> — скопируйте сейчас:
            повторно её не показать.
          </p>
          <div className="invite-link-row">
            <input readOnly value={link} aria-label="Ссылка-приглашение" />
            <button className="ghost" onClick={copy}>
              {copied ? "Скопировано" : "Скопировать ссылку"}
            </button>
          </div>
        </div>
      )}

      {items && items.length > 0 && (
        <div className="invite-pending">
          <h3>Ожидают активации</h3>
          <ul className="plain">
            {items.map((row) => (
              <li key={row.id} className="invite-pending-row">
                <span>
                  {row.fio || "без ФИО"}
                  {row.email ? ` · ${row.email}` : ""} · до{" "}
                  {new Date(row.expires_at).toLocaleDateString("ru-RU")}
                </span>
                <span className="invite-pending-actions">
                  <button className="ghost" onClick={() => reissue(row.id)}>
                    Перевыпустить
                  </button>
                  <button className="ghost" onClick={() => revoke(row.id)}>
                    Отозвать
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {error && <p className="email-verify-err">{error}</p>}
    </details>
  );
}