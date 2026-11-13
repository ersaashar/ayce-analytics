import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "@fontsource/plus-jakarta-sans/latin-400.css";
import "@fontsource/plus-jakarta-sans/latin-500.css";
import "@fontsource/plus-jakarta-sans/latin-600.css";

import { parseBootstrap } from "./api";
import { Management } from "./components/admin/Management";
import { parseManagementBootstrap } from "./management-api";
import { Workspace } from "./Workspace";
import "./style.css";

const BOOTSTRAP_ERROR = "Could not load this page. Refresh and try again.";

function reportBootstrapFailure(): void {
  const notice = document.getElementById("bootstrap-error");
  if (notice === null) return;
  notice.textContent = BOOTSTRAP_ERROR;
  notice.hidden = false;
}

function readBootstrap(id: string): unknown | null {
  const element = document.getElementById(id);
  if (element === null) return null;
  try {
    return JSON.parse(element.textContent ?? "");
  } catch {
    return null;
  }
}

function mountChat(): boolean {
  const root = document.getElementById("chat-root");
  if (root === null) return false;
  const payload = readBootstrap("chat-bootstrap");
  const parsed = payload === null ? null : parseBootstrap(payload);
  if (parsed === null) {
    reportBootstrapFailure();
    return true;
  }
  createRoot(root).render(
    <StrictMode>
      <Workspace bootstrap={parsed} />
    </StrictMode>,
  );
  return true;
}

function mountManagement(): boolean {
  const root = document.getElementById("management-root");
  if (root === null) return false;
  const payload = readBootstrap("management-bootstrap");
  const parsed = payload === null ? null : parseManagementBootstrap(payload);
  if (parsed === null) {
    reportBootstrapFailure();
    return true;
  }
  createRoot(root).render(
    <StrictMode>
      <Management bootstrap={parsed} />
    </StrictMode>,
  );
  return true;
}

function mount(): void {
  if (mountChat()) return;
  if (mountManagement()) return;
  reportBootstrapFailure();
}

// A page restored from the back/forward cache can predate a logout, so ask the
// server for the current session instead of showing stale private HTML.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});

mount();
