"use client";

import { AlertTriangle } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { ApiError } from "@/lib/api";
import { errorText } from "@/lib/errors";
import { cn } from "@/lib/utils";
import { buttonClass } from "@/components/ui/button";

/** Shared bits for the AI Ops pages (/maturity, /autonomy, /investigations,
 *  /runbooks, /evals, /audit) and the alert-page additions. */

export function PageHeader({
  icon: Icon,
  title,
  children,
  actions,
}: {
  icon: LucideIcon;
  title: string;
  children?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start gap-4">
      <div className="flex-1 min-w-[16rem]">
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <Icon className="h-5 w-5 text-accent" /> {title}
        </h1>
        {children && <p className="text-muted text-sm mt-1 max-w-3xl">{children}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export type Tone = "accent" | "warning" | "critical" | "info" | "muted";

export const TONE_VAR: Record<Tone, string> = {
  accent: "var(--accent)",
  warning: "var(--warning)",
  critical: "var(--critical)",
  info: "var(--info)",
  muted: "var(--text-muted)",
};

/** Small uppercase pill. */
export function Chip({
  tone = "muted",
  solid = false,
  children,
  title,
  className,
}: {
  tone?: Tone;
  solid?: boolean;
  children: React.ReactNode;
  title?: string;
  className?: string;
}) {
  const c = TONE_VAR[tone];
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider whitespace-nowrap",
        className
      )}
      style={
        solid
          ? { background: c, color: "var(--bg-base)" }
          : {
              background: `color-mix(in srgb, ${c} 14%, transparent)`,
              color: c,
              border: `1px solid color-mix(in srgb, ${c} 35%, transparent)`,
            }
      }
    >
      {children}
    </span>
  );
}

/** A single metric. `value` is pre-formatted; pass "—" for unknown. */
export function StatTile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: Tone;
}) {
  return (
    <div className="rounded-2xl border border-line bg-bg-card p-4">
      <div className="text-[11px] uppercase tracking-wider text-muted">{label}</div>
      <div
        className="text-2xl font-semibold mt-1 tabular-nums"
        style={tone ? { color: TONE_VAR[tone] } : undefined}
      >
        {value}
      </div>
      {hint && <div className="text-xs text-muted mt-1">{hint}</div>}
    </div>
  );
}

/** Segmented control (window selector, filters). */
export function Segmented<T extends string | number>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  ariaLabel: string;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className="inline-flex rounded-lg border border-line p-0.5 bg-bg-elevated/40"
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={String(o.value)}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(o.value)}
            className={cn(
              "h-7 px-2.5 rounded-md text-xs transition-colors",
              active ? "bg-bg-card text-fg font-medium shadow-sm" : "text-muted hover:text-fg"
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export const WINDOW_OPTIONS = [
  { value: 7, label: "7 days" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
];

/** Inline error box for a failed mutation. */
export function ErrorNote({ error, prefix }: { error: unknown; prefix?: string }) {
  if (!error) return null;
  return (
    <div
      role="alert"
      className="rounded-md px-3 py-2 text-[12px] flex items-start gap-2"
      style={{
        background: "color-mix(in srgb, var(--critical) 10%, var(--bg-base))",
        border: "1px solid color-mix(in srgb, var(--critical) 40%, var(--line))",
      }}
    >
      <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" style={{ color: "var(--critical)" }} />
      <span className="break-words min-w-0 whitespace-pre-wrap">
        {prefix ? `${prefix} ` : ""}
        {errorText(error)}
      </span>
    </div>
  );
}

/** Human-readable error; see `lib/errors.ts`. */
export { errorText };

export function isStatus(error: unknown, status: number): boolean {
  return error instanceof ApiError && error.status === status;
}

// ── Formatting ─────────────────────────────────────────────────────────

export function fmtRate(r: number | null | undefined): string {
  if (r === null || r === undefined || Number.isNaN(r)) return "—";
  return `${Math.round(r * 100)}%`;
}

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = s / 60;
  if (m < 60) return `${Math.round(m)}m`;
  const h = m / 60;
  if (h < 48) return `${h < 10 ? h.toFixed(1) : Math.round(h)}h`;
  return `${Math.round(h / 24)}d`;
}

/** Eval scores may come back as 0..1 or 0..100; show a 0..100 number. */
export function fmtScore(s: number | null | undefined): string {
  if (s === null || s === undefined || Number.isNaN(s)) return "—";
  const v = s <= 1 ? s * 100 : s;
  return `${Math.round(v)}`;
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// The global :focus-visible ring is the focus style, so no outline-none here.
export const inputCls =
  "h-8 rounded-lg bg-bg-elevated/60 border border-line px-2.5 text-xs focus:border-accent/60";

/** Prefer `<Button>`; these keep older call sites on the same look. */
export const btnSecondary = buttonClass("secondary", "sm");

export const btnPrimary = buttonClass("primary", "sm");
