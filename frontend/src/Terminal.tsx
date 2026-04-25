import { useEffect, useRef, useState } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

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

export function Terminal({ sessionId, status, visible }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [everActive, setEverActive] = useState(false);

  useEffect(() => {
    if (status === "Active") setEverActive(true);
  }, [status]);

  useEffect(() => {
    if (!everActive) return;
    if (!containerRef.current) return;

    const term = new XTerm({
      cursorBlink: true,
      fontFamily: 'ui-monospace, "Cascadia Code", "Consolas", monospace',
      fontSize: 13,
      theme: { background: "#0e0e10", foreground: "#e6e6e6" },
    });
    const fit = new FitAddon();
    fitRef.current = fit;
    term.loadAddon(fit);
    term.open(containerRef.current);
    if (visible) fit.fit();

    const wsUrl = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/sessions/${sessionId}/exec`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
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
      fitRef.current = null;
      wsRef.current = null;
    };
    // visible intentionally omitted — we don't tear down on hide.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, everActive]);

  // Re-fit whenever this tab becomes visible. xterm computes rows/cols from
  // the container's offsetWidth, which is 0 while display:none.
  useEffect(() => {
    if (visible && fitRef.current) {
      fitRef.current.fit();
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
}
