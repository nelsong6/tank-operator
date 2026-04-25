import { useEffect, useState } from "react";
import { Terminal } from "./Terminal";
import { authedFetch, bootstrapAuth, logout } from "./auth";

interface Session {
  id: string;
  pod_name: string | null;
  owner: string;
  status: string;
}

interface SessionUser {
  sub: string;
  email: string;
  name: string;
}

const POLL_INTERVAL_MS = 1500;

export function App() {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Open tabs are session IDs in insertion order. Tabs survive sidebar
  // re-renders so switching tabs doesn't tear down the WebSocket.
  const [tabs, setTabs] = useState<string[]>([]);
  const [active, setActive] = useState<string | null>(null);

  useEffect(() => {
    bootstrapAuth()
      .then(setUser)
      .catch((e) => setAuthError(String(e)));
  }, []);

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

  // While any open tab is still Pending, poll the list so the Terminal can
  // transition out of the "waiting" placeholder once its pod is Active.
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

  // Close tabs whose sessions disappeared (idle reaper, manual delete from
  // another browser, etc.).
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

  async function createSession() {
    setBusy(true);
    setError(null);
    try {
      const res = await authedFetch("/api/sessions", { method: "POST" });
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

  if (authError) {
    return (
      <div className="empty-state">
        <pre className="error">auth error: {authError}</pre>
        <button onClick={() => location.reload()}>retry</button>
      </div>
    );
  }

  if (!user) {
    return <div className="empty-state">signing in…</div>;
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <header className="sidebar-header">
          <h1>tank-operator</h1>
          <button onClick={createSession} disabled={busy}>
            + new
          </button>
        </header>
        {error && <pre className="error">{error}</pre>}
        <ul className="sessions">
          {sessions.length === 0 && <li className="empty">no sessions</li>}
          {sessions.map((s) => {
            const isOpen = tabs.includes(s.id);
            return (
              <li key={s.id} className={isOpen ? "is-open" : ""}>
                <button className="open" onClick={() => openTab(s.id)}>
                  <span className="id">{s.id}</span>
                  <span className={`status status-${s.status.toLowerCase()}`}>{s.status}</span>
                </button>
                <button
                  className="delete"
                  onClick={() => deleteSession(s.id)}
                  title="delete session"
                >
                  x
                </button>
              </li>
            );
          })}
        </ul>
        <footer className="sidebar-footer">
          <span className="email">{user.email}</span>
          <button onClick={logout}>sign out</button>
        </footer>
      </aside>
      <main className="workspace">
        {tabs.length === 0 ? (
          <div className="empty-state">click <code>+ new</code> or pick a session</div>
        ) : (
          <>
            <nav className="tab-bar">
              {tabs.map((id) => {
                const s = sessions.find((x) => x.id === id);
                const status = s?.status ?? "Pending";
                return (
                  <div key={id} className={`tab ${active === id ? "active" : ""}`}>
                    <button className="tab-label" onClick={() => setActive(id)}>
                      <span className="id">{id}</span>
                      <span className={`status status-${status.toLowerCase()}`}>{status}</span>
                    </button>
                    <button
                      className="tab-close"
                      onClick={() => closeTab(id)}
                      title="close tab (session keeps running)"
                    >
                      ×
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
