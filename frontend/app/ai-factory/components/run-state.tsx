"use client";

import { useEffect, useRef, useState } from "react";
import { useToast } from "@/components/ui/toast";
import { Loader2 } from "lucide-react";
import { cn, formatRelative } from "@/lib/utils";

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

/** One word per state, in sentence case, used by every badge and pill. */
export const RUN_STATE_LABEL: Record<RunState, string> = {
  pending: "Queued",
  running: "Running",
  passed: "Passed",
  failed: "Failed",
  error: "Error",
};

export function RunStateBadge({
  state,
  label,
  title,
}: {
  state: RunState;
  /** Override the word, e.g. "Skipped" for an error that is an environment limit. */
  label?: string;
  title?: string;
}) {
  const busy = state === "pending" || state === "running";
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 border text-[11px] font-medium shrink-0",
        label ? "border-line text-muted" : RUN_STATE_TONE[state]
      )}
    >
      {busy && <Loader2 className="h-3 w-3 animate-spin" aria-hidden />}
      {label ?? RUN_STATE_LABEL[state]}
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
  return iso ? formatRelative(iso) : "not started";
}

/** A relative time ("3h ago") with the exact time on hover. */
export function When({ iso, fallback = "not started" }: { iso: string | null | undefined; fallback?: string }) {
  if (!iso) return <span>{fallback}</span>;
  return (
    <time dateTime={iso} title={new Date(iso).toLocaleString()}>
      {formatRelative(iso)}
    </time>
  );
}

/**
 * Watches a polled run history: returns the ids of runs that appeared after
 * the first load (render them with `motion-enter` so the new row is noticed),
 * and toasts when a run that was in progress finishes.
 */
export function useRunWatch<T extends { id: string; state: RunState }>(
  runs: T[] | undefined,
  describe: (run: T) => string
): Set<string> {
  const toast = useToast();
  const known = useRef<Set<string> | null>(null);
  const states = useRef<Map<string, RunState>>(new Map());
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!runs) return;
    if (known.current === null) {
      known.current = new Set(runs.map((r) => r.id));
      runs.forEach((r) => states.current.set(r.id, r.state));
      return;
    }
    const added = runs.filter((r) => !known.current!.has(r.id)).map((r) => r.id);
    if (added.length) {
      added.forEach((id) => known.current!.add(id));
      setFresh((cur) => new Set([...cur, ...added]));
    }
    for (const r of runs) {
      const before = states.current.get(r.id);
      const wasBusy = before === "pending" || before === "running";
      if (wasBusy && r.state !== before && r.state !== "pending" && r.state !== "running") {
        const what = describe(r);
        if (r.state === "passed") toast.success(`${what} passed.`);
        else if (r.state === "failed") toast.error(`${what} failed. Open the run for details.`);
        else toast.info(`${what} couldn't finish. Open the run for the reason.`);
      }
      states.current.set(r.id, r.state);
    }
  }, [runs, describe, toast]);

  return fresh;
}
