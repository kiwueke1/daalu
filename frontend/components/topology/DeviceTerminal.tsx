"use client";

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import type { Terminal as XTerminal } from "@xterm/xterm";
import { api } from "@/lib/api";

type Status = "connecting" | "connected" | "closed" | "error";

// An in-browser SSH shell into a fabric device. xterm.js ⇆ the backend's
// /ws/device-terminal PTY bridge. Loaded only when opened (dynamic import), so
// the graph page carries no terminal weight until you click "SSH".
export function DeviceTerminal({
  device,
  env,
  onClose,
}: {
  device: { id: string; name: string };
  /** NV-CM environment slug — the shell must resolve the device in the same
   *  Nautobot the graph came from, not the tenant's primary one. */
  env?: string;
  onClose: () => void;
}) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const [status, setStatus] = useState<Status>("connecting");

  useEffect(() => {
    let disposed = false;
    let ws: WebSocket | null = null;
    let term: XTerminal | null = null;
    let ro: ResizeObserver | null = null;

    (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      // xterm's stylesheet — required for correct glyph metrics.
      await import("@xterm/xterm/css/xterm.css");
      if (disposed || !mountRef.current) return;

      // Local non-null bindings so the nested callbacks below type-check
      // (control-flow narrowing doesn't carry into closures). `term`
      // mirrors `t` for the cleanup path; the fit addon is disposed with it.
      const t = new Terminal({
        cursorBlink: true,
        fontSize: 13,
        fontFamily:
          "ui-monospace, SFMono-Regular, 'JetBrains Mono', Menlo, monospace",
        theme: {
          background: "#0a1612",
          foreground: "#e7efe9",
          cursor: "#22d3a6",
          selectionBackground: "rgba(34,211,166,0.28)",
          black: "#0a1612",
          green: "#22d3a6",
          red: "#ef4444",
          yellow: "#f59e0b",
          brightBlack: "#8a9a93",
        },
      });
      const f = new FitAddon();
      term = t;
      t.loadAddon(f);
      t.open(mountRef.current);
      f.fit();

      const url = api.network.terminalWsUrl(device.id, env);
      const socket = new WebSocket(url);
      ws = socket;

      const sendResize = () => {
        try {
          f.fit();
          if (socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ t: "r", c: t.cols, r: t.rows }));
          }
        } catch {
          /* fit can throw if the node is detached mid-teardown */
        }
      };

      socket.onopen = () => {
        setStatus("connected");
        sendResize();
        t.focus();
      };
      socket.onmessage = (ev: MessageEvent) => t.write(ev.data);
      socket.onclose = () => setStatus((s) => (s === "error" ? s : "closed"));
      socket.onerror = () => setStatus("error");

      t.onData((d: string) => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ t: "i", d }));
        }
      });

      ro = new ResizeObserver(() => sendResize());
      ro.observe(mountRef.current);
    })();

    return () => {
      disposed = true;
      try {
        ro?.disconnect();
      } catch {
        /* noop */
      }
      try {
        ws?.close();
      } catch {
        /* noop */
      }
      try {
        term?.dispose();
      } catch {
        /* noop */
      }
    };
  }, [device.id, env]);

  const statusText: Record<Status, string> = {
    connecting: "connecting…",
    connected: "connected",
    closed: "session closed",
    error: "connection failed",
  };
  const statusColor: Record<Status, string> = {
    connecting: "var(--warning)",
    connected: "var(--accent)",
    closed: "var(--text-muted)",
    error: "var(--critical)",
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-[2px] p-4"
      onClick={onClose}
    >
      <div
        className="surface w-full max-w-[900px] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
          <div className="flex items-center gap-2.5">
            <span
              className="h-2 w-2 rounded-full"
              style={{
                background: statusColor[status],
                boxShadow: `0 0 7px ${statusColor[status]}`,
              }}
            />
            <span className="text-sm font-semibold text-[color:var(--text)]">
              {device.name}
            </span>
            <span className="text-xs text-muted">· ssh · {statusText[status]}</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close terminal"
            className="rounded-md p-1 text-muted hover:text-[color:var(--text)] transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div
          ref={mountRef}
          className="h-[440px] w-full px-2 py-2"
          style={{ background: "#0a1612" }}
        />
      </div>
    </div>
  );
}
