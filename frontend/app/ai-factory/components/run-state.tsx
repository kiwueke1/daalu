"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Run-state colours shared by every factory run history (benchmark,
 * diagnostics), so the same word always has the same tone:
 * passed = accent, failed (the run finished and the result is bad) =
 * critical, error (the run itself couldn't complete) = warning.
 */
export type RunState = "pending" | "running" | "passed" | "failed" | "error";

export const RUN_STATE_TONE: Record<RunState, string> = {
  pending: "border-line text-muted",
  running: "border-accent-emerald/40 text-[color:var(--text)]",
  passed: "border-accent-emerald/40 text-accent-emerald bg-accent-emerald/10",
  failed: "border-signal-critical/40 text-[color:var(--critical)] bg-signal-critical/10",
  error: "border-signal-warning/40 text-[color:var(--warning)] bg-signal-warning/10",
};

export function RunStateBadge({ state }: { state: RunState }) {
  const busy = state === "pending" || state === "running";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 border text-[10px] uppercase tracking-wide shrink-0",
        RUN_STATE_TONE[state]
      )}
    >
      {busy && <Loader2 className="h-2.5 w-2.5 animate-spin" />}
      {state}
    </span>
  );
}

/** How many runs a history shows before "Show N older". */
export const RECENT_RUNS = 5;

/**
 * The latest few runs, plus a toggle for the rest, so a long history doesn't
 * push the page on forever.
 */
export function useRecent<T>(items: T[]): {
  shown: T[];
  hiddenCount: number;
  toggle: React.ReactNode;
} {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, RECENT_RUNS);
  const hiddenCount = items.length - RECENT_RUNS;
  const toggle =
    hiddenCount > 0 ? (
      <button
        type="button"
        onClick={() => setAll((v) => !v)}
        className="text-xs h-8 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--text)]"
      >
        {all ? "Show only the latest 5" : `Show ${hiddenCount} older`}
      </button>
    ) : null;
  return { shown, hiddenCount, toggle };
}

export function fmtWhen(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "not started";
}
