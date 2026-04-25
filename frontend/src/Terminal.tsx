import { useEffect, useRef } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

interface Props {
  sessionId: string;
  status: string;
  onClose: () => void;
  onPoll: () => void;
}

const POLL_INTERVAL_MS = 1500;

export function Terminal({ sessionId, status, onClose, onPoll }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Poll the parent's session list while the Job is still Pending so we can
  // open the WS the moment the pod transitions to Active.
  useEffect(() => {
    if (status === "Active") return;
    const t = setInterval(onPoll, POLL_INTERVAL_MS);
    return () => clearInterval(t);
  }, [status, onPoll]);

  useEffect(() => {
    if (status !== "Active") return;
    if (!containerRef.current) return;

    const term = new XTerm({
      cursorBlink: true,
      fontFamily: 'ui-monospace, "Cascadia Code", "Consolas", monospace',
      fontSize: 13,
      theme: { background: "#0e0e10", foreground: "#e6e6e6" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(containerRef.current);
    fit.fit();

    const wsUrl = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/sessions/${sessionId}/exec`;
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";

    let cleanup: (() => void) | null = null;

    ws.onopen = () => {
      const sendResize = (cols: number, rows: number) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ resize: [cols, rows] }));
        }
      };

      const onWindowResize = () => {
        fit.fit();
        sendResize(term.cols, term.rows);
      };

      sendResize(term.cols, term.rows);
      window.addEventListener("resize", onWindowResize);

      const onResizeDisp = term.onResize(({ cols, rows }) => sendResize(cols, rows));
      const onDataDisp = term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(data);
      });

      cleanup = () => {
        window.removeEventListener("resize", onWindowResize);
        onResizeDisp.dispose();
        onDataDisp.dispose();
      };
    };

    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        term.write(e.data);
      } else {
        term.write(new Uint8Array(e.data as ArrayBuffer));
      }
    };

    ws.onclose = (e) => {
      // code 1006 = abnormal closure with no close frame; for those the
      // browser drops `reason`. Show the code so failures are diagnosable.
      const detail = e.reason || `code ${e.code}`;
      term.write(`\r\n\x1b[33m[disconnected: ${detail}]\x1b[0m\r\n`);
    };

    ws.onerror = () => {
      term.write("\r\n\x1b[31m[connection error]\x1b[0m\r\n");
    };

    return () => {
      cleanup?.();
      ws.close();
      term.dispose();
    };
  }, [sessionId, status]);

  return (
    <div className="terminal-pane">
      <header className="terminal-header">
        <span className="terminal-id">{sessionId}</span>
        <span className={`status status-${status.toLowerCase()}`}>{status}</span>
        <button onClick={onClose}>back</button>
      </header>
      {status === "Active" ? (
        <div ref={containerRef} className="terminal-body" />
      ) : (
        <div className="terminal-waiting">
          waiting for pod to be ready… (status: {status})
        </div>
      )}
    </div>
  );
}
