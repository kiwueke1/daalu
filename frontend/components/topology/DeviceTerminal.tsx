"use client";

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import type { Terminal as XTerminal } from "@xterm/xterm";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";

type Status = "connecting" | "connected" | "closed" | "error";

// A terminal is a fixed dark surface in both themes (shell output assumes a
// dark background and its own ANSI palette), so these are concrete colours
// rather than theme tokens. Kept in one place on purpose.
const TERMINAL_THEME = {
  background: "#0a1612",
  foreground: "#e7efe9",
  cursor: "#22d3a6",
  selectionBackground: "rgba(34,211,166,0.28)",
  black: "#0a1612",
  green: "#22d3a6",
  red: "#ef4444",
  yellow: "#f59e0b",
  brightBlack: "#8a9a93",
} as const;

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
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const [status, setStatus] = useState<Status>("connecting");

  // Not the shared Dialog: its focus trap and Escape handler would swallow
  // Tab (shell completion) and Escape (vi) inside the shell. So Escape closes
  // only when focus is outside the terminal, and focus returns on close.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      if (mountRef.current?.contains(e.target as Node)) return;
      onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      opener?.focus?.();
    };
  }, [onClose]);

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
        theme: TERMINAL_THEME,
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
      className="motion-overlay fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-[2px] p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`SSH terminal: ${device.name}`}
        className="motion-dialog surface w-full max-w-[900px] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
          <div className="flex items-center gap-2.5">
            <span
              aria-hidden
              className="h-2 w-2 rounded-full"
              style={{
                background: statusColor[status],
                boxShadow: `0 0 7px ${statusColor[status]}`,
              }}
            />
            <span className="text-sm font-semibold text-[color:var(--text)]">
              {device.name}
            </span>
            <span role="status" className="text-xs text-muted">· ssh · {statusText[status]}</span>
          </div>
          <Button
            ref={closeRef}
            variant="icon"
            size="sm"
            onClick={onClose}
            aria-label="Close terminal"
            icon={<X className="h-4 w-4" aria-hidden />}
          />
        </div>
        <div
          ref={mountRef}
          className="h-[440px] w-full px-2 py-2"
          style={{ background: TERMINAL_THEME.background }}
        />
      </div>
    </div>
  );
}
