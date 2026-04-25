import { useEffect, useState } from "react";
import { Terminal } from "./Terminal";

interface Session {
  id: string;
  pod_name: string | null;
  owner: string;
  status: string;
}

export function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [active, setActive] = useState<string | null>(null);

  async function refresh() {
    try {
      const res = await fetch("/api/sessions");
      if (!res.ok) throw new Error(`list failed: ${res.status}`);
      setSessions(await res.json());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function createSession() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/sessions", { method: "POST" });
      if (!res.ok) throw new Error(`create failed: ${res.status}`);
      const created: Session = await res.json();
      await refresh();
      setActive(created.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSession(id: string) {
    try {
      const res = await fetch(`/api/sessions/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`delete failed: ${res.status}`);
      if (active === id) setActive(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  if (active) {
    return <Terminal sessionId={active} onClose={() => setActive(null)} />;
  }

  return (
    <div className="app">
      <header>
        <h1>tank-operator</h1>
        <button onClick={createSession} disabled={busy}>
          + new session
        </button>
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
