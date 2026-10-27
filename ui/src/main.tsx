import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import LegalView, { legalPageFromPath } from "./views/LegalView";
import InviteAcceptView from "./views/InviteAcceptView";
import "./style.css";

// FR-35: правовые страницы выбираются по пути ДО рендера приложения — они
// статические и доступны без входа (никаких запросов к API на этом пути).
const legalPage = legalPageFromPath(window.location.pathname);
// FR-49: страница активации приглашения тоже публичная: токен — это и есть
// учётные данные, поэтому вход не требуется.
const inviteToken = /^\/invite\/([A-Za-z0-9_-]{8,128})\/?$/.exec(window.location.pathname)?.[1];

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {legalPage ? (
      <LegalView page={legalPage} />
    ) : inviteToken ? (
      <InviteAcceptView />
    ) : (
      <App />
    )}
  </React.StrictMode>
);