import { useEffect, useState } from "react";
import { api, getToken, setToken, setRefreshToken, setOnAuthFail, ApiError } from "./api";
import PatientView from "./views/PatientView";
import ClinicianView from "./views/ClinicianView";
import AdminView from "./views/AdminView";
import NotificationsBell from "./NotificationsBell";
import { initPush } from "./push";

type Session = { role: string; email: string };

export default function App() {
  const [session, setSession] = useState<Session | null>(() => {
    const role = localStorage.getItem("cc_role");
    const email = localStorage.getItem("cc_email");
    return role && email ? { role, email } : null;
  });
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [role, setRole] = useState("PATIENT");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (session && !getToken()) return null; // защита от неконсистентного состояния

  const logout = () => {
    setToken(null);
    setRefreshToken(null);
    localStorage.removeItem("cc_role");
    localStorage.removeItem("cc_email");
    setSession(null);
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

  const login = async () => {
    setBusy(true);
    setError("");
    try {
      const body = { email, password, ...(otp ? { otp } : {}) };
      const data = await api.login(body);
      setToken(data.access_token);
      setRefreshToken(data.refresh_token);
      localStorage.setItem("cc_role", data.role);
      localStorage.setItem("cc_email", email);
      setSession({ role: data.role, email });
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const register = async () => {
    setBusy(true);
    setError("");
    try {
      // Клиники/админы создаются через seed; из UI — только пациенты
      await api.register({ email, password, role });
      await login();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!session) {
    return (
      <div style={{ maxWidth: 420, margin: "60px auto" }}>
        <div className="card">
          <h2>Клинический компас</h2>
          <p style={{ color: "#5a6478", fontSize: 14 }}>
            Инструмент диагностики и ведения пациентов с тревогой и депрессией
          </p>
          <div className="tabs">
            <button className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>
              Вход
            </button>
            <button className={mode === "register" ? "active" : ""} onClick={() => setMode("register")}>
              Регистрация
            </button>
          </div>
          <label>
            Email
            <input value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label>
            Пароль
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>
          <label>
            Код 2FA (для врача и администратора)
            <input value={otp} onChange={(e) => setOtp(e.target.value)} placeholder="6 цифр TOTP" />
          </label>
          {mode === "register" && (
            <>
              <label>Роль</label>
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="PATIENT">Пациент</option>
              </select>
            </>
          )}
          {error && <p className="error">{error}</p>}
          <div className="row" style={{ marginTop: 14 }}>
            <button disabled={busy} onClick={mode === "login" ? login : register}>
              {busy ? "…" : mode === "login" ? "Войти" : "Создать аккаунт"}
            </button>
          </div>
          <p style={{ marginTop: 16, fontSize: 12, color: "#8a94a8" }}>
            Демо-доступы (из симулятора): врач doc@compass.demo / Demo-Pass-1 (2FA TOTP),
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
        {session.role === "PATIENT" && <PatientView />}
        {session.role === "CLINICIAN" && <ClinicianView />}
        {session.role === "ADMIN" && <AdminView />}
      </main>
    </>
  );
}