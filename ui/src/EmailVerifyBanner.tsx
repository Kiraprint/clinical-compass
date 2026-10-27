import { useEffect, useState } from "react";
import { api, ApiError } from "./api";

/**
 * FR-48 (v2): мягкое подтверждение адреса.
 *
 * Баннер, а не блокировка: при опечатке в адресе человек не теряет доступ
 * (решение продукта — «доступ не закрываем»). Показывается только когда канал
 * почты настроен (`/auth/config` → `mail`), иначе на стенде без SMTP висел бы
 * у всех и предлагал действие, которое физически не работает.
 */
export default function EmailVerifyBanner() {
  const [visible, setVisible] = useState(false);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState("");
  const [demoCode, setDemoCode] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.all([api.authConfig(), api.authMe()])
      .then(([cfg, me]) => {
        if (!alive) return;
        setEmail(me.email);
        setVisible(Boolean(cfg.mail) && !me.email_verified);
      })
      .catch(() => {
        /* баннер — не критичный элемент: молча не показываем */
      });
    return () => {
      alive = false;
    };
  }, []);

  async function send() {
    setBusy(true);
    setErr("");
    try {
      const r = await api.requestEmailVerify();
      setDemoCode(r.demo_code ?? "");
      setSent(r.sent ? `Письмо отправлено на ${email}` : "Отправка писем не настроена");
    } catch (e) {
      const apiErr = e instanceof ApiError ? e : null;
      setErr(
        apiErr?.code === "CODE_COOLDOWN"
          ? "Письмо уже отправлено — подождите минуту"
          : "Не удалось отправить письмо, попробуйте позже",
      );
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    setBusy(true);
    setErr("");
    try {
      await api.verifyEmail({ code: code.trim() });
      setDone(true);
      setVisible(false);
    } catch (e) {
      const apiErr = e instanceof ApiError ? e : null;
      setErr(
        apiErr?.code === "CODE_INVALID"
          ? "Код не подошёл или истёк — запросите новый"
          : apiErr?.code === "EMAIL_ALREADY_VERIFIED"
            ? "Адрес уже подтверждён"
            : "Не удалось подтвердить адрес",
      );
    } finally {
      setBusy(false);
    }
  }

  if (!visible) return null;

  return (
    <div className="email-verify" role="status">
      <div className="email-verify-text">
        <strong>Подтвердите адрес почты.</strong> На {email} нужен код: без него
        не восстановить пароль самостоятельно. Доступ к сервису это не
        ограничивает.
      </div>
      <div className="email-verify-form">
        <input
          aria-label="Код из письма"
          inputMode="numeric"
          autoComplete="one-time-code"
          placeholder="Код из письма"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          maxLength={12}
        />
        <button className="primary" onClick={confirm} disabled={busy || code.trim().length < 4}>
          Подтвердить
        </button>
        <button className="ghost" onClick={send} disabled={busy}>
          Отправить код
        </button>
      </div>
      {demoCode && <div className="email-verify-hint">Демо-стенд, код: {demoCode}</div>}
      {sent && !demoCode && <div className="email-verify-hint">{sent}</div>}
      {err && <div className="email-verify-err">{err}</div>}
      {done && <div className="email-verify-hint">Адрес подтверждён.</div>}
    </div>
  );
}
