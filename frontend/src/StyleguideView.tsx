// Visual catalog of the components shipped by tank-operator's frontend.
// Mounted by App.tsx at /_styleguide. Reviewers + the screenshot pass
// get one URL to scan instead of synthesizing a feel from a diff.
//
// Contract: nelsong6/glimmung/docs/styleguide-contract.md.
//
// When you change a component (button voice, status dot, mode chip,
// session row layout), update its entry here in the same PR. There's
// no automated drift check — the env-prep phase's /_styleguide curl
// is the floor that catches "the route doesn't even render anymore",
// not "the styleguide drifted from the live UI."

import { useState } from "react";

const MODES = ["subscription", "api_key", "config"] as const;
const MODE_LABELS: Record<(typeof MODES)[number], string> = {
  subscription: "sub",
  api_key: "api",
  config: "config",
};
const STATUSES = ["active", "pending", "error"] as const;

const sectionStyle: React.CSSProperties = {
  padding: "32px 0 24px",
  borderTop: "1px solid var(--border-subtle)",
};
const headStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  textTransform: "uppercase",
  letterSpacing: "1.2px",
  color: "var(--text-muted)",
  margin: "0 0 4px 0",
};
const captionStyle: React.CSSProperties = {
  fontSize: "var(--text-xs)",
  color: "var(--text-faint)",
  margin: "0 0 14px 0",
  maxWidth: "60ch",
};
const rowStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: "12px",
  alignItems: "center",
};

export function StyleguideView() {
  const [dropdownOpen, setDropdownOpen] = useState(true);

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "var(--bg-app)",
        color: "var(--text-body)",
        fontFamily: "var(--font-primary)",
        padding: "32px 48px 64px",
      }}
    >
      <div style={{ maxWidth: 880 }}>
        <h1
          style={{
            fontSize: "var(--text-xs)",
            fontWeight: 400,
            color: "var(--text-muted)",
            textTransform: "uppercase",
            letterSpacing: "1.4px",
            margin: "0 0 4px 0",
          }}
        >
          tank-operator — styleguide
        </h1>
        <p style={{ ...captionStyle, marginBottom: 24 }}>
          Visual catalog of components shipped by tank-operator's React app.
          Reviewers get this URL alongside every PR; agents update it whenever
          a component changes. See <code>docs/styleguide-contract.md</code> in
          the glimmung repo for the contract.
        </p>

        {/* === buttons === */}
        <section style={sectionStyle}>
          <h2 style={headStyle}>buttons</h2>
          <p style={captionStyle}>
            Three voices: <code>btn-primary</code> for the dominant action on a
            screen (sign-in, install CTA), <code>btn-secondary</code> for
            recoveries (retry, cancel), <code>link-button</code> for inline
            text affordances. Disabled state is muted opacity, not a separate
            class.
          </p>
          <div style={rowStyle}>
            <button className="btn-primary">Sign in</button>
            <button className="btn-primary" disabled>Sign in</button>
            <button className="btn-secondary">retry</button>
            <button className="btn-secondary" disabled>retry</button>
            <button className="link-button">Sign out</button>
          </div>
        </section>

        {/* === status dots === */}
        <section style={sectionStyle}>
          <h2 style={headStyle}>status dot</h2>
          <p style={captionStyle}>
            Replaces the old text pill ("Active" / "Pending" / "Failed") in
            the session row. Color carries the status; shape stays neutral so
            the row's dominant visual belongs to the session name.
          </p>
          <div style={rowStyle}>
            {STATUSES.map((s) => (
              <div key={s} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span className={`status-dot status-${s}`} aria-label={`status ${s}`} />
                <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>{s}</span>
              </div>
            ))}
          </div>
        </section>

        {/* === mode chips === */}
        <section style={sectionStyle}>
          <h2 style={headStyle}>mode chip</h2>
          <p style={captionStyle}>
            Surfaces the auth mode (subscription / api / config) on the
            session row. Each rides its own tinted background — not bordered
            pills — so the row is calm at rest but legible at a glance.
          </p>
          <div style={rowStyle}>
            {MODES.map((m) => (
              <span key={m} className={`mode mode-${m}`}>{MODE_LABELS[m]}</span>
            ))}
          </div>
        </section>

        {/* === session row === */}
        <section style={sectionStyle}>
          <h2 style={headStyle}>session row</h2>
          <p style={captionStyle}>
            One row per session in the sidebar list. Top: status dot + session
            name + delete affordance. Bottom: mode chip + optional inline
            actions (remote-control, save-credentials). Active row gets the
            <code>is-open</code> class; not styled here for brevity.
          </p>
          <ul className="sessions" style={{ maxWidth: 360, listStyle: "none", padding: 0, margin: 0 }}>
            <li>
              <div className="session-row-top">
                <span className="status-dot status-active" aria-label="status active" />
                <button className="session-open" type="button">
                  <span className="session-id">my-session</span>
                </button>
                <button className="session-delete" aria-label="delete session" type="button">
                  ×
                </button>
              </div>
              <div className="session-row-bottom">
                <span className="mode mode-subscription">sub</span>
              </div>
            </li>
            <li>
              <div className="session-row-top">
                <span className="status-dot status-pending" aria-label="status pending" />
                <button className="session-open" type="button">
                  <span className="session-id">starting…</span>
                </button>
                <button className="session-delete" aria-label="delete session" type="button">
                  ×
                </button>
              </div>
              <div className="session-row-bottom">
                <span className="mode mode-api_key">api</span>
              </div>
            </li>
          </ul>
        </section>

        {/* === dropdown === */}
        <section style={sectionStyle}>
          <h2 style={headStyle}>mode dropdown</h2>
          <p style={captionStyle}>
            Appears under the "New session" CTA when the chevron toggles. Each
            entry pairs a title with a one-line hint that frames the
            tradeoff — auth mode is the only setting on session creation, so
            the dropdown carries the explanation.
          </p>
          <div style={{ position: "relative", maxWidth: 280 }}>
            <button
              className="btn-secondary"
              type="button"
              onClick={() => setDropdownOpen((v) => !v)}
              style={{ width: "100%", justifyContent: "flex-start" }}
            >
              + New session ▾
            </button>
            {dropdownOpen && (
              <ul className="dropdown dropdown-mode" role="menu" style={{ position: "static", marginTop: 8 }}>
                <li>
                  <button type="button">
                    <span className="dropdown-title">Subscription</span>
                    <span className="dropdown-hint">Default — uses claude.ai login</span>
                  </button>
                </li>
                <li>
                  <button type="button">
                    <span className="dropdown-title">API key</span>
                    <span className="dropdown-hint">Billed via API</span>
                  </button>
                </li>
                <li>
                  <button type="button">
                    <span className="dropdown-title">Config sub</span>
                    <span className="dropdown-hint">Log in once · seeds KV for future sessions</span>
                  </button>
                </li>
              </ul>
            )}
          </div>
        </section>

        {/* === welcome card === */}
        <section style={sectionStyle}>
          <h2 style={headStyle}>welcome card</h2>
          <p style={captionStyle}>
            The shape used for boot states (sign-in, GitHub install onboarding,
            auth error). Centered card with a primary CTA and an inline
            click-to-dismiss error pill rendered above when set.
          </p>
          <div style={{ background: "var(--bg-elevated)", border: "1px solid var(--border-strong)", borderRadius: "var(--radius-lg)", padding: 24, maxWidth: 480 }}>
            <div className="welcome-inner onboarding">
              <h2 className="welcome-title">Connect GitHub</h2>
              <p className="welcome-sub">
                tank-operator needs the App installed so it can run sessions
                against your repos. The next page is GitHub's; come back here
                after.
              </p>
              <a className="btn-primary onboarding-cta" href="#">Install</a>
              <p className="onboarding-meta">
                signed in as <code>you@example.com</code> ·{" "}
                <button className="link-button" type="button">Sign out</button>
              </p>
            </div>
          </div>
        </section>

        {/* === error === */}
        <section style={sectionStyle}>
          <h2 style={headStyle}>error pill</h2>
          <p style={captionStyle}>
            Inline error surface, click-to-dismiss. Used for transient failures
            (lost socket, save failed, install error) above the active card or
            in the sidebar.
          </p>
          <div style={rowStyle}>
            <pre className="error" style={{ margin: 0 }}>save failed: 500</pre>
          </div>
        </section>

        {/* === boot states === */}
        <section style={sectionStyle}>
          <h2 style={headStyle}>boot state</h2>
          <p style={captionStyle}>
            Full-screen states for app lifecycle (loading, sign-in needed, auth
            error). Renders in <code>div.boot-state</code> — single centered
            element + minimal chrome.
          </p>
          <div className="boot-state" style={{ minHeight: 80, padding: 16, border: "1px dashed var(--border-strong)", borderRadius: "var(--radius-md)" }}>
            <span className="boot-text">loading…</span>
          </div>
        </section>
      </div>
    </div>
  );
}
