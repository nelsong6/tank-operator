import { useEffect, useState } from "react";

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
      await refresh();
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
      await refresh();
    } catch (e) {
      setError(String(e));
    }
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
            <span className="id">{s.id}</span>
            <span className={`status status-${s.status.toLowerCase()}`}>{s.status}</span>
            <button onClick={() => deleteSession(s.id)}>x</button>
          </li>
        ))}
        {sessions.length === 0 && <li className="empty">no sessions</li>}
      </ul>
    </div>
  );
}
