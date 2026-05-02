import { useEffect, useRef, useState } from "react";
import { Terminal, type TerminalHandle } from "./Terminal";
import { authedFetch, bootstrapAuth, logout } from "./auth";

type SessionMode = "api_key" | "subscription" | "config";

interface Session {
  id: string;
  pod_name: string | null;
  owner: string;
  status: string;
  mode: SessionMode;
  // User-set friendly name. Null when unset; UI falls back to the id slug.
  name: string | null;
}

const MODE_LABELS: Record<SessionMode, string> = {
  api_key: "API key",
  subscription: "Subscription",
  config: "Config sub",
};

const MODE_HINTS: Record<SessionMode, string> = {
  subscription: "Default — uses claude.ai login",
  api_key: "Billed via API",
  config: "Log in once · seeds KV for future sessions",
};

const MODE_ORDER: SessionMode[] = ["subscription", "api_key", "config"];

interface SessionUser {
  sub: string;
  email: string;
  name: string;
  // Profile fields from /api/auth/me. Null until the user completes the
  // GitHub App install. installation_id presence drives the onboarding
  // wall — null means show the install CTA, non-null means full app.
  github_login: string | null;
  installation_id: number | null;
}

// One-line summaries for the install_error reasons the backend may surface
// via redirect query param after a failed install callback. Anything not in
// the map renders as the raw reason — keeps unknown errors visible without
// hardcoding every variant.
const INSTALL_ERROR_HINTS: Record<string, string> = {
  missing_state: "Install link expired before you returned. Try again.",
  invalid_state: "Install link signature didn't validate. Try again.",
  missing_installation_id: "GitHub didn't send an installation id. Re-run the install.",
  pending_approval: "Your install needs an org admin's approval. Once they approve, log in again.",
  session_expired: "Your session expired during install. Sign in again then re-run the install.",
  session_invalid: "Your session token didn't validate. Sign in again.",
  email_mismatch: "The signed-in account doesn't match the install link's email.",
};

function readInstallError(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("install_error");
}

function clearInstallError(): void {
  const url = new URL(window.location.href);
  url.searchParams.delete("install_error");
  window.history.replaceState({}, "", url.toString());
}

const POLL_INTERVAL_MS = 1500;

function IconPlus() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none"
         stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <line x1="8" y1="3.5" x2="8" y2="12.5" />
      <line x1="3.5" y1="8" x2="12.5" y2="8" />
    </svg>
  );
}

function IconChevron() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" fill="none"
         stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4,6 8,10 12,6" />
    </svg>
  );
}

function IconKebab() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor">
      <circle cx="8" cy="3" r="1.3" />
      <circle cx="8" cy="8" r="1.3" />
      <circle cx="8" cy="13" r="1.3" />
    </svg>
  );
}

function IconClose() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none"
         stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <line x1="4" y1="4" x2="12" y2="12" />
      <line x1="12" y1="4" x2="4" y2="12" />
    </svg>
  );
}

function BrandMark() {
  return (
    <svg viewBox="0 0 64 64" width="18" height="18" fill="none"
         stroke="currentColor" strokeWidth="2.5"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="8" y="28" width="40" height="14" rx="3" />
      <circle cx="16" cy="46" r="5" />
      <circle cx="40" cy="46" r="5" />
      <line x1="48" y1="32" x2="58" y2="32" />
      <rect x="22" y="20" width="14" height="8" rx="1.5" />
    </svg>
  );
}

function initials(user: SessionUser): string {
  const source = (user.name || user.email || "?").trim();
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  const first = parts[0]?.[0] ?? source[0];
  const second = parts[1]?.[0] ?? "";
  return (first + second).toUpperCase().slice(0, 2);
}

function OnboardingWall({
  user,
  onLogout,
}: {
  user: SessionUser;
  onLogout: () => Promise<void>;
}) {
  const [installError, setInstallError] = useState<string | null>(readInstallError);

  function dismissError() {
    setInstallError(null);
    clearInstallError();
  }

  return (
    <div className="welcome">
      <div className="welcome-inner onboarding">
        <h2 className="welcome-title">Connect GitHub</h2>
        <p className="welcome-sub">
          tank-operator needs the <code>tank-operator</code> GitHub App installed on your account so
          your sessions can read and write your repos via mcp-github.
        </p>
        {installError && (
          <pre className="error onboarding-error" onClick={dismissError} title="dismiss">
            {INSTALL_ERROR_HINTS[installError] ?? installError}
          </pre>
        )}
        <a className="btn-primary onboarding-cta" href="/api/github/install/url">
          Install GitHub App
        </a>
        <p className="onboarding-meta">
          Signed in as <strong>{user.email}</strong>.{" "}
          <button className="link-button" onClick={onLogout}>
            sign out
          </button>
        </p>
      </div>
    </div>
  );
}

export function App() {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tabs, setTabs] = useState<string[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [modeMenuOpen, setModeMenuOpen] = useState(false);
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  // Inline rename state. `editingTab` is the session id whose tab label is
  // currently an <input>; `editingValue` holds the in-progress name. Reset
  // when commit or cancel fires.
  const [editingTab, setEditingTab] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  // One Terminal handle per open tab — populated by Terminal's forwardRef
  // callback. Used by the "Remote control" tab-bar button to inject the
  // /remote-control slash command into the live WS.
  const terminalRefs = useRef<Map<string, TerminalHandle>>(new Map());

  useEffect(() => {
    bootstrapAuth()
      .then(setUser)
      .catch((e) => setAuthError(String(e)));
  }, []);

  // Close any open dropdown on an outside click. Both menus use a `data-menu`
  // attribute so a single listener can route by which menu is open.
  useEffect(() => {
    if (!modeMenuOpen && !profileMenuOpen) return;
    const close = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      const root = target?.closest("[data-menu]") as HTMLElement | null;
      if (root?.dataset.menu === "mode") return;
      if (root?.dataset.menu === "profile") return;
      setModeMenuOpen(false);
      setProfileMenuOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [modeMenuOpen, profileMenuOpen]);

  async function refresh() {
    try {
      const res = await authedFetch("/api/sessions");
      if (!res.ok) throw new Error(`list failed: ${res.status}`);
      setSessions(await res.json());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    if (user) void refresh();
  }, [user]);

  useEffect(() => {
    if (!user) return;
    const hasPending = tabs.some((id) => {
      const s = sessions.find((x) => x.id === id);
      return !s || s.status !== "Active";
    });
    if (!hasPending) return;
    const t = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(t);
  }, [tabs, sessions, user]);

  useEffect(() => {
    setTabs((prev) => {
      const next = prev.filter((id) => sessions.some((s) => s.id === id));
      if (next.length === prev.length) return prev;
      if (active && !next.includes(active)) setActive(next[next.length - 1] ?? null);
      return next;
    });
  }, [sessions]);

  function openTab(id: string) {
    setTabs((prev) => (prev.includes(id) ? prev : [...prev, id]));
    setActive(id);
  }

  function closeTab(id: string) {
    setTabs((prev) => {
      const next = prev.filter((x) => x !== id);
      if (active === id) setActive(next[next.length - 1] ?? null);
      return next;
    });
  }

  async function createSession(mode: SessionMode = "subscription") {
    setBusy(true);
    setModeMenuOpen(false);
    setError(null);
    try {
      const res = await authedFetch("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!res.ok) throw new Error(`create failed: ${res.status}`);
      const created: Session = await res.json();
      await refresh();
      openTab(created.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function renameSession(id: string, nextName: string | null) {
    try {
      const res = await authedFetch(`/api/sessions/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nextName }),
      });
      if (!res.ok) throw new Error(`rename failed: ${res.status}`);
      const updated: Session = await res.json();
      setSessions((prev) =>
        prev.map((s) => (s.id === id ? { ...s, name: updated.name ?? null } : s))
      );
    } catch (e) {
      setError(String(e));
    }
  }

  function startEditing(id: string, current: string | null) {
    setEditingTab(id);
    setEditingValue(current ?? "");
  }

  function commitEditing() {
    if (editingTab) {
      const trimmed = editingValue.trim();
      void renameSession(editingTab, trimmed === "" ? null : trimmed);
    }
    setEditingTab(null);
    setEditingValue("");
  }

  function cancelEditing() {
    setEditingTab(null);
    setEditingValue("");
  }

  async function deleteSession(id: string) {
    try {
      const res = await authedFetch(`/api/sessions/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`delete failed: ${res.status}`);
      closeTab(id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  function startRemoteControl(id: string) {
    // \r is what the terminal would send for the Enter key, so claude
    // submits the line. Slash commands are evaluated client-side by the
    // claude TUI, so this needs no orchestrator round-trip.
    terminalRefs.current.get(id)?.sendInput("/remote-control\r");
  }

  async function saveCredentials(id: string) {
    setBusy(true);
    setError(null);
    try {
      const res = await authedFetch(`/api/sessions/${id}/save-credentials`, {
        method: "POST",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `save failed: ${res.status}`);
      }
      await deleteSession(id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (authError) {
    return (
      <div className="boot-state">
        <pre className="error">auth error: {authError}</pre>
        <button className="btn-secondary" onClick={() => location.reload()}>retry</button>
      </div>
    );
  }

  if (!user) {
    return <div className="boot-state"><span className="boot-text">signing in…</span></div>;
  }

  if (user.installation_id == null) {
    return <OnboardingWall user={user} onLogout={logout} />;
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <span className="brand-mark" aria-hidden="true"><BrandMark /></span>
          <h1>tank-operator</h1>
        </div>

        <div className="sidebar-section">
          <div className="new-row" data-menu="mode">
            <button
              className="new-row-main"
              onClick={() => createSession("subscription")}
              disabled={busy}
              title="new session (subscription)"
            >
              <span className="row-icon"><IconPlus /></span>
              <span className="row-label">New session</span>
            </button>
            <button
              className="new-row-toggle"
              onClick={() => setModeMenuOpen((v) => !v)}
              disabled={busy}
              aria-label="choose auth mode"
              aria-expanded={modeMenuOpen}
            >
              <IconChevron />
            </button>
            {modeMenuOpen && (
              <ul className="dropdown dropdown-mode" role="menu">
                {MODE_ORDER.map((m) => (
                  <li key={m}>
                    <button onClick={() => createSession(m)} disabled={busy}>
                      <span className="dropdown-title">{MODE_LABELS[m]}</span>
                      <span className="dropdown-hint">{MODE_HINTS[m]}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {error && <pre className="error">{error}</pre>}

        <div className="sidebar-list">
          <div className="sidebar-section-label">Sessions</div>
          <ul className="sessions">
            {sessions.length === 0 && <li className="sessions-empty">no sessions</li>}
            {sessions.map((s) => {
              const isOpen = tabs.includes(s.id);
              return (
                <li key={s.id} className={isOpen ? "is-open" : ""}>
                  <button className="session-open" onClick={() => openTab(s.id)} title={s.id}>
                    <span className="session-id">{s.name ?? s.id}</span>
                    <span className={`mode mode-${s.mode}`}>{MODE_LABELS[s.mode] ?? s.mode}</span>
                    <span className={`status status-${s.status.toLowerCase()}`}>{s.status}</span>
                  </button>
                  <button
                    className="session-delete"
                    onClick={() => deleteSession(s.id)}
                    title="delete session"
                    aria-label="delete session"
                  >
                    <IconClose />
                  </button>
                </li>
              );
            })}
          </ul>
        </div>

        <div className="sidebar-footer" data-menu="profile">
          <button
            className="profile"
            onClick={() => setProfileMenuOpen((v) => !v)}
            title={user.email}
          >
            <span className="avatar" aria-hidden="true">{initials(user)}</span>
            <span className="profile-text">
              <span className="profile-name">{user.name || user.email}</span>
            </span>
            <span className="profile-kebab"><IconKebab /></span>
          </button>
          {profileMenuOpen && (
            <ul className="dropdown dropdown-profile" role="menu">
              <li className="dropdown-meta">
                <span className="dropdown-meta-label">Signed in as</span>
                <span className="dropdown-meta-value">{user.email}</span>
              </li>
              <li className="dropdown-divider" role="separator" />
              <li>
                <button onClick={logout}>Sign out</button>
              </li>
            </ul>
          )}
        </div>
      </aside>

      <main className="workspace">
        {tabs.length === 0 ? (
          <div className="welcome">
            <div className="welcome-inner">
              <h2 className="welcome-title">tank-operator</h2>
              <p className="welcome-sub">Spin up a Claude Code session</p>
              <div className="welcome-cards" role="list">
                {MODE_ORDER.map((m) => (
                  <button
                    key={m}
                    className="welcome-card"
                    onClick={() => createSession(m)}
                    disabled={busy}
                    role="listitem"
                  >
                    <span className="welcome-card-title">{MODE_LABELS[m]}</span>
                    <span className="welcome-card-sub">{MODE_HINTS[m]}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <>
            <nav className="tab-bar">
              {tabs.map((id) => {
                const s = sessions.find((x) => x.id === id);
                const status = s?.status ?? "Pending";
                const isConfig = s?.mode === "config";
                const isSubscription = s?.mode === "subscription";
                const displayName = s?.name ?? id;
                const isEditing = editingTab === id;
                return (
                  <div key={id} className={`tab ${active === id ? "active" : ""}`}>
                    {isEditing ? (
                      <input
                        className="tab-name-input"
                        value={editingValue}
                        autoFocus
                        onChange={(e) => setEditingValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") commitEditing();
                          else if (e.key === "Escape") cancelEditing();
                        }}
                        onBlur={commitEditing}
                        placeholder={id}
                        maxLength={80}
                      />
                    ) : (
                      <button
                        className="tab-label"
                        onClick={() => setActive(id)}
                        onDoubleClick={() => startEditing(id, s?.name ?? null)}
                        title={s?.name ? `${id} — double-click to rename` : "double-click to rename"}
                      >
                        <span className="id">{displayName}</span>
                        <span className={`status status-${status.toLowerCase()}`}>{status}</span>
                      </button>
                    )}
                    {isConfig && (
                      <button
                        className="tab-action"
                        onClick={() => saveCredentials(id)}
                        disabled={busy || status !== "Active"}
                        title="capture ~/.claude/.credentials.json from this pod and write it to KV"
                      >
                        save credentials
                      </button>
                    )}
                    {isSubscription && (
                      <button
                        className="tab-action"
                        onClick={() => startRemoteControl(id)}
                        disabled={status !== "Active"}
                        title="type /remote-control into this session — claude will print a https://claude.ai/code/session_… URL you can open"
                      >
                        Remote control
                      </button>
                    )}
                    <button
                      className="tab-close"
                      onClick={() => closeTab(id)}
                      title="close tab (session keeps running)"
                      aria-label="close tab"
                    >
                      <IconClose />
                    </button>
                  </div>
                );
              })}
            </nav>
            <div className="terminals">
              {tabs.map((id) => {
                const s = sessions.find((x) => x.id === id);
                return (
                  <Terminal
                    key={id}
                    ref={(h) => {
                      if (h) terminalRefs.current.set(id, h);
                      else terminalRefs.current.delete(id);
                    }}
                    sessionId={id}
                    status={s?.status ?? "Pending"}
                    visible={active === id}
                  />
                );
              })}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
