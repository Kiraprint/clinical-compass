import { useEffect, useState } from "react";
import { api, setToken, setRefreshToken, setOnAuthFail, ApiError } from "./api";
import type { LoginResp } from "./api";
import PatientView from "./views/PatientView";
import ClinicianView from "./views/ClinicianView";
import AdminView from "./views/AdminView";
import NotificationsBell from "./NotificationsBell";
import { initPush } from "./push";

type Session = { role: string; email: string };
// FR-48 (E17): экран входа — это цепочка шагов. Enter в каждом шаге отправляет
// свою форму (`<form onSubmit>`), первой пустой поле получает автофокус.
type Step = "login" | "register" | "otp" | "reset-email" | "reset-code";

export default function App() {
  const [session, setSession] = useState<Session | null>(() => {
    const role = localStorage.getItem("cc_role");
    const email = localStorage.getItem("cc_email");
    return role && email ? { role, email } : null;
  });
  const [step, setStep] = useState<Step>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [code, setCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [challenge, setChallenge] = useState("");
  const [demoCode, setDemoCode] = useState("");  // только демо-стенд без SMTP
  const [notice, setNotice] = useState("");
  const [role, setRole] = useState("PATIENT");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // CC_REQUIRE_OTP: если false — поле 2FA не показываем (проверка отключена
  // сервером), но totp_secret всё равно хранится в БД. Дефолт true — конфиг
  // недоступен → показываем поле (безопаснее).
  const [requireOtp, setRequireOtp] = useState(true);

  // Публичный конфиг входа — UI показывает/скрывает поле 2FA.
  useEffect(() => {
    api
      .authConfig()
      .then((cfg) => setRequireOtp(cfg.require_otp))
      .catch(() => {});
  }, []);

  // F9: web-режим использует httpOnly cookies — getToken() возвращает null,
  // но сессия валидна (cookie отправляется автоматически). localStorage нужен
  // только для UI-состояния (role/email), не для токенов.

  const logout = () => {
    api.logout(); // F4: отзыв refresh + очистка cookies на сервере
    setToken(null);      // F9: no-op в web (cookies), очищает localStorage в native
    setRefreshToken(null);
    localStorage.removeItem("cc_role");
    localStorage.removeItem("cc_email");
    setSession(null);
    // FR-48: возвращаемся на первый шаг, а не на OTP/сброс прошлой сессии.
    setStep("login");
    setOtp("");
    setCode("");
    setChallenge("");
    setNotice("");
    setError("");
  };

  // При неудачном refresh (истёкший refresh-токен) — принудительный логаут.
  useEffect(() => {
    setOnAuthFail(logout);
    return () => setOnAuthFail(null);
  }, []);

  // Web Push для CLINICIAN/ADMIN после входа.
  useEffect(() => {
    if (session && session.role !== "PATIENT") initPush(session.role);
  }, [session]);

  // Общая обёртка: busy + текст ошибки сервера как есть (ApiError.detail).
  const guard = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const completeLogin = (data: LoginResp) => {
    setToken(data.access_token);
    setRefreshToken(data.refresh_token);
    localStorage.setItem("cc_role", data.role);
    localStorage.setItem("cc_email", email);
    setSession({ role: data.role, email });
  };

  const login = async () => {
    const body = { email, password, ...(otp ? { otp } : {}) };
    const data = await api.login(body);
    if ("otp_required" in data) {
      // FR-48: пароль верный, но нужен код из письма.
      setChallenge(data.challenge);
      setDemoCode(data.demo_code ?? "");
      setCode("");
      setNotice("Код отправлен на e-mail. Введите его, чтобы войти.");
      setStep("otp");
      return;
    }
    completeLogin(data);
  };

  const submitOtp = async () => {
    completeLogin(await api.loginOtp({ challenge, code }));
  };

  const register = async () => {
    // Клиники/админы создаются через seed; из UI — только пациенты
    await api.register({ email, password, role });
    await login();
  };

  const requestReset = async () => {
    const data = await api.requestPasswordReset({ email });
    setDemoCode(data.demo_code ?? "");
    setCode("");
    setNewPassword("");
    setNotice("Если адрес зарегистрирован, код отправлен. Введите код и новый пароль.");
    setStep("reset-code");
  };

  const submitReset = async () => {
    await api.resetPassword({ email, code, password: newPassword });
    setPassword("");
    setCode("");
    setDemoCode("");
    setNotice("Пароль изменён. Войдите с новым паролем.");
    setStep("login");
  };

  if (!session) {
    // Первая пустая форма получает фокус — Enter сразу работает.
    const focusEmail = !email;
    const focusPassword = !!email && !password;

    const tabs = (
      <div className="tabs">
        <button
          type="button"
          className={step === "login" ? "active" : ""}
          onClick={() => {
            setStep("login");
            setError("");
          }}
        >
          Вход
        </button>
        <button
          type="button"
          className={step === "register" ? "active" : ""}
          onClick={() => {
            setStep("register");
            setError("");
          }}
        >
          Регистрация
        </button>
      </div>
    );

    return (
      <div style={{ maxWidth: 420, margin: "60px auto" }}>
        <div className="card">
          <h2>Клинический компас</h2>
          <p style={{ color: "#5a6478", fontSize: 14 }}>
            Инструмент диагностики и ведения пациентов с тревогой и депрессией
          </p>
          {(step === "login" || step === "register") && tabs}

          {step === "login" && (
            <form
              className="auth-form"
              onSubmit={(e) => {
                e.preventDefault();
                void guard(login);
              }}
            >
              <label>
                Email
                <input
                  value={email}
                  autoComplete="email"
                  autoFocus={focusEmail}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              <label>
                Пароль
                <input
                  type="password"
                  value={password}
                  autoComplete="current-password"
                  autoFocus={focusPassword}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </label>
              {requireOtp && (
                <label>
                  Код 2FA (для врача и администратора)
                  <input
                    value={otp}
                    autoComplete="one-time-code"
                    inputMode="numeric"
                    onChange={(e) => setOtp(e.target.value)}
                    placeholder="6 цифр TOTP"
                  />
                </label>
              )}
              {error && <p className="error">{error}</p>}
              {notice && <p className="auth-hint">{notice}</p>}
              <div className="row" style={{ marginTop: 14 }}>
                <button type="submit" disabled={busy}>
                  {busy ? "…" : "Войти"}
                </button>
                <button
                  type="button"
                  className="auth-link"
                  onClick={() => {
                    setStep("reset-email");
                    setError("");
                    setNotice("");
                  }}
                >
                  Забыли пароль?
                </button>
              </div>
            </form>
          )}

          {step === "register" && (
            <form
              className="auth-form"
              onSubmit={(e) => {
                e.preventDefault();
                void guard(register);
              }}
            >
              <label>
                Email
                <input
                  value={email}
                  autoComplete="email"
                  autoFocus={focusEmail}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              <label>
                Пароль
                <input
                  type="password"
                  value={password}
                  autoComplete="new-password"
                  autoFocus={focusPassword}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </label>
              <label>
                Роль
                <select value={role} onChange={(e) => setRole(e.target.value)}>
                  <option value="PATIENT">Пациент</option>
                </select>
              </label>
              {error && <p className="error">{error}</p>}
              <div className="row" style={{ marginTop: 14 }}>
                <button type="submit" disabled={busy}>
                  {busy ? "…" : "Создать аккаунт"}
                </button>
              </div>
            </form>
          )}

          {step === "otp" && (
            <form
              className="auth-form"
              onSubmit={(e) => {
                e.preventDefault();
                void guard(submitOtp);
              }}
            >
              <p className="auth-hint">Код отправлен на {email}</p>
              <label>
                Код из письма
                <input
                  value={code}
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  autoFocus
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="6 цифр"
                />
              </label>
              {demoCode && <p className="auth-hint">Демо-стенд без SMTP: код {demoCode}</p>}
              {notice && <p className="auth-hint">{notice}</p>}
              {error && <p className="error">{error}</p>}
              <div className="row" style={{ marginTop: 14 }}>
                <button type="submit" disabled={busy}>
                  {busy ? "…" : "Подтвердить"}
                </button>
                <button
                  type="button"
                  className="auth-link"
                  onClick={() => {
                    setStep("login");
                    setError("");
                  }}
                >
                  Назад ко входу
                </button>
              </div>
            </form>
          )}

          {step === "reset-email" && (
            <form
              className="auth-form"
              onSubmit={(e) => {
                e.preventDefault();
                void guard(requestReset);
              }}
            >
              <p className="auth-hint">Укажите e-mail — пришлём код сброса пароля.</p>
              <label>
                Email
                <input
                  value={email}
                  autoComplete="email"
                  autoFocus={focusEmail}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              {error && <p className="error">{error}</p>}
              <div className="row" style={{ marginTop: 14 }}>
                <button type="submit" disabled={busy}>
                  {busy ? "…" : "Отправить код"}
                </button>
                <button
                  type="button"
                  className="auth-link"
                  onClick={() => {
                    setStep("login");
                    setError("");
                  }}
                >
                  Назад ко входу
                </button>
              </div>
            </form>
          )}

          {step === "reset-code" && (
            <form
              className="auth-form"
              onSubmit={(e) => {
                e.preventDefault();
                void guard(submitReset);
              }}
            >
              <label>
                Код из письма
                <input
                  value={code}
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  autoFocus
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="6 цифр"
                />
              </label>
              <label>
                Новый пароль
                <input
                  type="password"
                  value={newPassword}
                  autoComplete="new-password"
                  onChange={(e) => setNewPassword(e.target.value)}
                />
              </label>
              {demoCode && <p className="auth-hint">Демо-стенд без SMTP: код {demoCode}</p>}
              {notice && <p className="auth-hint">{notice}</p>}
              {error && <p className="error">{error}</p>}
              <div className="row" style={{ marginTop: 14 }}>
                <button type="submit" disabled={busy}>
                  {busy ? "…" : "Сохранить пароль"}
                </button>
                <button
                  type="button"
                  className="auth-link"
                  onClick={() => {
                    setStep("login");
                    setError("");
                  }}
                >
                  Назад ко входу
                </button>
              </div>
            </form>
          )}

          <p style={{ marginTop: 16, fontSize: 12, color: "#8a94a8" }}>
            Демо-доступы (из симулятора): врач doc@compass.demo / Demo-Pass-1{requireOtp ? " (2FA TOTP)" : ""},
            пациент p1@compass.demo / Demo-Pass-1
          </p>
        </div>
      </div>
    );
  }

  return (
    <>
      <header className="top">
        <h1>Клинический компас</h1>
        <div className="row">
          {session.role !== "PATIENT" && <NotificationsBell />}
          <span style={{ fontSize: 13, opacity: 0.85 }}>
            {session.email} · {session.role}
          </span>
          <button className="ghost" onClick={logout}>
            Выйти
          </button>
        </div>
      </header>
      <main>
        {session.role === "PATIENT" && <PatientView onLogout={logout} />}
        {session.role === "CLINICIAN" && <ClinicianView />}
        {session.role === "ADMIN" && <AdminView />}
      </main>
    </>
  );
}
