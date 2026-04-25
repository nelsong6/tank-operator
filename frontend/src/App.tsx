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

export function App() {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
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

  async function createSession() {
    setBusy(true);
    setError(null);
    try {
      const res = await authedFetch("/api/sessions", { method: "POST" });
      if (!res.ok) throw new Error(`create failed: ${res.status}`);
      const created: Session = await res.json();
      await refresh();
      // The Job is created Pending and stays so until the kubelet pulls the
      // image and starts the container. Opening the WS before then races
      // get_pod_name's readiness wait — surface a "starting" state and
      // poll until the backend reports Active.
      setActive(created.id);
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
      if (active === id) setActive(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  if (authError) {
    return (
      <div className="app">
        <pre className="error">auth error: {authError}</pre>
        <button onClick={() => location.reload()}>retry</button>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="app">
        <p style={{ color: "#777" }}>signing in…</p>
      </div>
    );
  }

  if (active) {
    const session = sessions.find((s) => s.id === active);
    return (
      <Terminal
        sessionId={active}
        status={session?.status ?? "Pending"}
        onClose={() => setActive(null)}
        onPoll={refresh}
      />
    );
  }

  return (
    <div className="app">
      <header>
        <h1>tank-operator</h1>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <span style={{ color: "#888", fontSize: "0.85rem" }}>{user.email}</span>
          <button onClick={createSession} disabled={busy}>+ new session</button>
          <button onClick={logout}>sign out</button>
        </div>
      </header>
      {error && <pre className="error">{error}</pre>}
      <ul className="sessions">
        {sessions.map((s) => (
          <li key={s.id}>
            <button className="open" onClick={() => setActive(s.id)} disabled={s.status !== "Active"}>
              <span className="id">{s.id}</span>
              <span className={`status status-${s.status.toLowerCase()}`}>{s.status}</span>
            </button>
            <button className="delete" onClick={() => deleteSession(s.id)}>x</button>
          </li>
        ))}
        {sessions.length === 0 && <li className="empty">no sessions</li>}
      </ul>
    </div>
  );
}
