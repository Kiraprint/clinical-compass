import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import LegalView, { legalPageFromPath } from "./views/LegalView";
import "./style.css";

// FR-35: правовые страницы выбираются по пути ДО рендера приложения — они
// статические и доступны без входа (никаких запросов к API на этом пути).
const legalPage = legalPageFromPath(window.location.pathname);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {legalPage ? <LegalView page={legalPage} /> : <App />}
  </React.StrictMode>
);
