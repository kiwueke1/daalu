"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Toasts confirm what just happened ("Policy saved as v4"), say what failed
 * and what to do, and can offer one follow-up action such as Undo or View.
 *
 *   const toast = useToast();
 *   toast.success("Runbook published", { action: { label: "View", onClick: … } });
 *   toast.error(errorText(err));
 *
 * Announced to screen readers (aria-live), dismissible, paused while
 * hovered or focused. Motion confirms the action: the toast slides up in
 * and its tick pops (`motion-toast-in`, `motion-success`); off under
 * prefers-reduced-motion.
 */
type Tone = "success" | "error" | "info";
type ToastAction = { label: string; onClick: () => void };
type ToastItem = { id: number; tone: Tone; message: string; action?: ToastAction; leaving?: boolean };
type Opts = { action?: ToastAction; durationMs?: number };

const ToastContext = createContext<{
  success: (message: string, opts?: Opts) => void;
  error: (message: string, opts?: Opts) => void;
  info: (message: string, opts?: Opts) => void;
} | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setItems((xs) => xs.map((x) => (x.id === id ? { ...x, leaving: true } : x)));
    window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 220);
  }, []);

  const push = useCallback(
    (tone: Tone, message: string, opts?: Opts) => {
      const id = nextId.current++;
      setItems((xs) => [...xs.slice(-3), { id, tone, message, action: opts?.action }]);
      return { id, durationMs: opts?.durationMs ?? (tone === "error" ? 9000 : opts?.action ? 8000 : 5000) };
    },
    []
  );

  const api = {
    success: (m: string, o?: Opts) => void push("success", m, o),
    error: (m: string, o?: Opts) => void push("error", m, o),
    info: (m: string, o?: Opts) => void push("info", m, o),
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        className="pointer-events-none fixed z-[60] bottom-4 right-4 left-4 sm:left-auto flex flex-col items-stretch sm:items-end gap-2"
        aria-live="polite"
        aria-relevant="additions"
      >
        {items.map((t) => (
          <Toast key={t.id} item={t} onDismiss={() => dismiss(t.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function Toast({ item, onDismiss }: { item: ToastItem; onDismiss: () => void }) {
  const [paused, setPaused] = useState(false);
  const duration = item.tone === "error" ? 9000 : item.action ? 8000 : 5000;

  useEffect(() => {
    if (paused || item.leaving) return;
    const t = window.setTimeout(onDismiss, duration);
    return () => window.clearTimeout(t);
  }, [paused, item.leaving, duration, onDismiss]);

  const Icon = item.tone === "success" ? CheckCircle2 : item.tone === "error" ? AlertTriangle : Info;
  const color =
    item.tone === "success" ? "var(--accent)" : item.tone === "error" ? "var(--critical)" : "var(--text-muted)";

  return (
    <div
      role={item.tone === "error" ? "alert" : "status"}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      className={cn(
        "pointer-events-auto w-full sm:w-[380px] rounded-xl border bg-bg-elevated shadow-xl px-3.5 py-3 flex items-start gap-2.5 text-sm",
        item.leaving ? "motion-toast-out" : "motion-toast-in"
      )}
      style={{ borderColor: `color-mix(in srgb, ${color} 40%, var(--line))` }}
    >
      <Icon aria-hidden className="h-4 w-4 mt-0.5 shrink-0 motion-success" style={{ color }} />
      <div className="flex-1 min-w-0 break-words">{item.message}</div>
      {item.action && (
        <button
          type="button"
          onClick={() => {
            item.action?.onClick();
            onDismiss();
          }}
          className="motion-press shrink-0 font-semibold text-accent hover:underline"
        >
          {item.action.label}
        </button>
      )}
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className="motion-press -mr-1 -mt-0.5 h-7 w-7 shrink-0 rounded-md inline-flex items-center justify-center text-muted hover:text-fg"
      >
        <X className="h-3.5 w-3.5" aria-hidden />
      </button>
    </div>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return ctx;
}
