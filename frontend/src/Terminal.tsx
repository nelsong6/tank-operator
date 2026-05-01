import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { registerWrappedLinks } from "./wrappedLinkProvider";
import "@xterm/xterm/css/xterm.css";
import "./fonts.css";

interface Props {
  sessionId: string;
  status: string;
  /**
   * When false the component stays mounted (preserving WS + scrollback) but
   * the DOM is hidden via CSS. On every transition to true we re-run fit() so
   * xterm picks up the now-visible viewport size.
   */
  visible: boolean;
}

/**
 * Imperative handle the parent uses to push input into the live WS — used by
 * the "Remote control" tab-bar button to send "/remote-control\r" without
 * the user having to focus the terminal and type. No-op if the WS isn't open
 * yet (the button is disabled until status === "Active" anyway).
 */
export interface TerminalHandle {
  sendInput: (s: string) => void;
}

export const Terminal = forwardRef<TerminalHandle, Props>(function Terminal(
  { sessionId, status, visible },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const termRef = useRef<XTerm | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [everActive, setEverActive] = useState(false);

  useEffect(() => {
    if (status === "Active") setEverActive(true);
  }, [status]);

  useImperativeHandle(ref, () => ({
    sendInput: (s: string) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(s);
    },
  }), []);

  useEffect(() => {
    if (!everActive) return;
    if (!containerRef.current) return;

    const term = new XTerm({
      cursorBlink: true,
      // "Symbols Nerd Font Mono" is loaded last as a fallback so private-use-area
      // glyphs (Powerline arrows, branded marks, status icons) render instead of
      // the U+FFFD replacement diamond. Latin still resolves to the earlier
      // monospace fonts via per-glyph CSS font fallback.
      fontFamily: 'ui-monospace, "Cascadia Code", "Consolas", "Symbols Nerd Font Mono", monospace',
      fontSize: 13,
      theme: { background: "#171717", foreground: "#e4e4e4" },
    });
    const fit = new FitAddon();
    fitRef.current = fit;
    term.loadAddon(fit);
    // URLs in claude's output become click-to-open. Custom provider (instead
    // of @xterm/addon-web-links) so links that wrap across terminal rows are
    // recognised as a single contiguous URL — the stock addon matches per
    // buffer line and would render a wrapped link as two broken halves.
    // Open in a new tab so the workspace session isn't navigated away from.
    registerWrappedLinks(term, (_event, uri) => {
      window.open(uri, "_blank", "noopener,noreferrer");
    });
    term.open(containerRef.current);
    termRef.current = term;
    if (visible) {
      fit.fit();
      // Without this, the user has to click into the terminal before keystrokes
      // land — xterm doesn't auto-focus on mount.
      term.focus();
    }

    const wsUrl = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/sessions/${sessionId}/exec`;

    let cancelled = false;
    let reconnectTimer: number | null = null;
    let pingTimer: number | null = null;
    let backoffMs = 500;
    let everConnected = false;
    const maxBackoffMs = 15_000;

    const sendIfOpen = (payload: string | Uint8Array) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(payload);
    };

    const sendResize = (cols: number, rows: number) => {
      sendIfOpen(JSON.stringify({ resize: [cols, rows] }));
    };

    const onWindowResize = () => {
      fit.fit();
      sendResize(term.cols, term.rows);
    };
    window.addEventListener("resize", onWindowResize);
    const onResizeDisp = term.onResize(({ cols, rows }) => sendResize(cols, rows));
    const onDataDisp = term.onData((data) => sendIfOpen(data));

    const stopPing = () => {
      if (pingTimer != null) {
        window.clearInterval(pingTimer);
        pingTimer = null;
      }
    };

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.binaryType = "arraybuffer";

      ws.onopen = () => {
        if (everConnected) {
          term.write("\r\n\x1b[32m[reconnected]\x1b[0m\r\n");
        }
        everConnected = true;
        backoffMs = 500;
        sendResize(term.cols, term.rows);
        // Heartbeat keeps Envoy's idle stream timeout (~5min default) from
        // cutting a quiet WS. Without it, the orchestrator's idle reaper
        // then deletes the pod 5min later — the session is gone for good.
        stopPing();
        pingTimer = window.setInterval(() => {
          sendIfOpen(JSON.stringify({ ping: 1 }));
        }, 30_000);
      };

      ws.onmessage = (e) => {
        if (typeof e.data === "string") {
          term.write(e.data);
        } else {
          term.write(new Uint8Array(e.data as ArrayBuffer));
        }
      };

      ws.onclose = (e) => {
        stopPing();
        // code 1006 = abnormal closure with no close frame; for those the
        // browser drops `reason`. Show the code so failures are diagnosable.
        const detail = e.reason || `code ${e.code}`;
        // 1008 = policy violation (auth / not the session owner) — retrying
        // won't fix it. Everything else gets exponential backoff.
        if (cancelled || e.code === 1008) {
          term.write(`\r\n\x1b[33m[disconnected: ${detail}]\x1b[0m\r\n`);
          return;
        }
        const delay = backoffMs;
        term.write(
          `\r\n\x1b[33m[disconnected: ${detail}; reconnecting in ${Math.round(delay / 1000)}s…]\x1b[0m\r\n`,
        );
        backoffMs = Math.min(backoffMs * 2, maxBackoffMs);
        reconnectTimer = window.setTimeout(connect, delay);
      };

      ws.onerror = () => {
        // onclose fires next and drives the reconnect; nothing to do here.
      };
    };

    connect();

    return () => {
      cancelled = true;
      stopPing();
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
      window.removeEventListener("resize", onWindowResize);
      onResizeDisp.dispose();
      onDataDisp.dispose();
      wsRef.current?.close();
      term.dispose();
      fitRef.current = null;
      termRef.current = null;
      wsRef.current = null;
    };
    // visible intentionally omitted — we don't tear down on hide.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, everActive]);

  // Re-fit and re-focus whenever this tab becomes visible. xterm computes
  // rows/cols from the container's offsetWidth (0 while display:none), and
  // focus is lost when another tab steals it — refocusing on every show
  // means tabbing back lets the user type immediately.
  useEffect(() => {
    if (visible) {
      fitRef.current?.fit();
      termRef.current?.focus();
    }
  }, [visible]);

  if (!everActive) {
    return (
      <div className="terminal-waiting" style={{ display: visible ? "flex" : "none" }}>
        waiting for pod to be ready… (status: {status})
      </div>
    );
  }
  return (
    <div
      ref={containerRef}
      className="terminal-body"
      style={{ display: visible ? "block" : "none" }}
    />
  );
});
