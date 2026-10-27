import { useEffect, useState } from "react";
import { api, setToken, setRefreshToken, ApiError } from "../api";
/**
 * FR-49: публичная страница активации приглашения.
 * Открывается по `/invite/<token>` БЕЗ входа: врач передал ссылку пациенту
 * любым каналом. После принятия — аккаунт, ростер и сессия.
 */
export default function InviteAcceptView() {
  const token = window.location.pathname.split("/").pop() ?? "";
  const [info, setInfo] = useState<{ fio: string | null; email: string | null } | null>(null);
  const [error, setError] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .authInvite(token)
      .then((data) => {
        if (!alive) return;
        setInfo(data);
        if (data.email) setEmail(data.email);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        const code = e instanceof ApiError ? e.code : "";
        setError(
          code === "INVITE_EXPIRED" || code === "INVITE_USED"
            ? "Ссылка уже не действует: попросите врача выдать новую."
            : "Ссылка не найдена или недействительна.",
        );
      });
    return () => {
      alive = false;
    };
  }, [token]);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      const data = await api.authInviteAccept(token, {
        email: email.trim() || undefined,
        password,
      });
      api.invalidate();
      setToken(data.access_token);
      setRefreshToken(data.refresh_token);
      localStorage.setItem("cc_role", data.role);
      localStorage.setItem("cc_email", email.trim() || info?.email || "");
      window.location.href = "/";
    } catch (e) {
      const apiErr = e instanceof ApiError ? e : null;
      setError(
        apiErr?.code === "EMAIL_TAKEN"
          ? "Этот адрес уже занят — войдите в свою учётную запись."
          : apiErr?.code === "INVITE_EXPIRED" || apiErr?.code === "INVITE_USED"
            ? "Ссылка уже не действует: попросите врача выдать новую."
            : "Не удалось активировать карту. Проверьте поля и попробуйте ещё раз.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (!info && !error) return <div className="invite-view">Загружаем приглашение…</div>;

  return (
    <div className="invite-view">
      <div className="invite-card card">
        <h1>Приглашение от клиники</h1>
        {error ? (
          <p className="email-verify-err">{error}</p>
        ) : (
          <>
            <p>
              {info?.fio ? <strong>{info.fio}</strong> : "Пациент"} — врач уже завёл вашу карту.
              Осталось придумать пароль и получить доступ.
            </p>
            <label>
              E-mail (логин)
              <input
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                // Поле только для ввода недостающего адреса; у врача адрес уже есть.
                disabled={Boolean(info?.email)}
              />
            </label>
            <label>
              Пароль
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-label="Пароль"
              />
            </label>
            <label>
              Пароль ещё раз
              <input
                type="password"
                value={repeat}
                onChange={(e) => setRepeat(e.target.value)}
                aria-label="Повторите пароль"
              />
            </label>
            <button
              className="primary"
              disabled={busy || password.length < 8 || password !== repeat}
              onClick={submit}
            >
              {busy ? "Создаём аккаунт…" : "Активировать карту"}
            </button>
            {password && password !== repeat && (
              <p className="email-verify-err">Пароли не совпадают.</p>
            )}
          </>
        )}
      </div>
    </div>
  );
}