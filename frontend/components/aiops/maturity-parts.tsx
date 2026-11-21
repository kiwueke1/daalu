"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Circle, Lock, RotateCw, X } from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, type AiopsMetrics, type Maturity } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { ErrorNote } from "./ui";

/**
 * Hover (mouse), keyboard focus or a tap (touch) opens a card; leaving, blurring
 * or tapping again closes it. Shared by the level flip cards and the TRACE cards
 * so both read the same way: the headline first, the detail on demand.
 */
export function useReveal() {
  const [open, setOpen] = useState(false);
  const pointer = useRef("mouse");
  const bind = {
    tabIndex: 0,
    onPointerDown: (e: React.PointerEvent) => {
      pointer.current = e.pointerType;
    },
    onPointerEnter: (e: React.PointerEvent) => {
      if (e.pointerType === "mouse") setOpen(true);
    },
    onPointerLeave: (e: React.PointerEvent) => {
      if (e.pointerType === "mouse") setOpen(false);
    },
    // Only keyboard focus opens: a tap focuses too, and the click toggles.
    onFocus: (e: React.FocusEvent<HTMLElement>) => {
      if (e.currentTarget.matches(":focus-visible")) setOpen(true);
    },
    onBlur: () => setOpen(false),
    onClick: () => {
      if (pointer.current !== "mouse") setOpen((o) => !o);
    },
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    },
  };
  return { open, bind };
}

type Level = Maturity["levels"][number];
type Criterion = Maturity["criteria"][number];

/** The four-level ladder (Level 0 is the starting point, not a card). Each card shows the level and whether it's met; hover
 *  (or focus, or tap) flips it to the description and that level's criteria. */
export function MaturityLadder({ maturity }: { maturity: Maturity }) {
  return (
    <ol className="grid grid-cols-2 lg:grid-cols-4 gap-2">
      {maturity.levels.filter((l) => l.level > 0).map((l) => (
        <LevelCard
          key={l.level}
          level={l}
          current={l.level === maturity.level}
          // A level counts only when every level below it is met too, so a met
          // level above the current one is "met but blocked", not achieved.
          blockedBy={
            l.met ? maturity.levels.find((b) => b.level > 0 && b.level < l.level && !b.met)?.level : undefined
          }
          criteria={maturity.criteria.filter((c) => c.level === l.level)}
        />
      ))}
    </ol>
  );
}

function LevelCard({
  level: l,
  current,
  blockedBy,
  criteria,
}: {
  level: Level;
  current: boolean;
  blockedBy: number | undefined;
  criteria: Criterion[];
}) {
  const { open, bind } = useReveal();
  const achieved = l.met && blockedBy === undefined;
  const metCount = criteria.filter((c) => c.met).length;
  const status = current
    ? "Your level"
    : achieved
      ? "Met"
      : blockedBy !== undefined
        ? `Met · needs L${blockedBy} first`
        : "Not met";
  const StatusIcon = achieved ? Check : blockedBy !== undefined ? Lock : Circle;
  const face = cn(
    "[backface-visibility:hidden] rounded-xl border p-3 flex flex-col",
    current ? "border-accent/60" : "border-line"
  );
  const faceBg = current
    ? "color-mix(in srgb, var(--accent) 12%, var(--bg-card))"
    : "var(--bg-card)";
  return (
    <li
      {...bind}
      aria-label={`Level ${l.level}, ${l.name}: ${status}`}
      className={cn(
        "relative [perspective:900px] rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-accent/60 cursor-default",
        open && "z-20"
      )}
    >
      {/* The front sets the card's size; the back overlays it and may grow
          past the bottom (L3 has five criteria), so the row never reflows. */}
      <div
        className="relative h-full [transform-style:preserve-3d] transition-transform duration-[var(--dur-slow)] ease-[var(--ease-out)]"
        style={{ transform: open ? "rotateY(180deg)" : undefined }}
      >
        {/* Front: the level and whether it's met. */}
        <div className={cn(face, "h-full items-start gap-1 min-h-[7.5rem]")} style={{ background: faceBg }}>
          <div className="flex w-full items-start justify-between gap-2">
            <span
              className={cn(
                "text-3xl font-bold tabular-nums leading-none",
                achieved ? "text-accent" : "text-fg/70"
              )}
            >
              L{l.level}
            </span>
            <RotateCw aria-hidden className="h-3.5 w-3.5 text-muted/70" />
          </div>
          <div className="text-sm font-medium">{l.name}</div>
          <div
            className={cn(
              "mt-auto inline-flex items-center gap-1 text-xs",
              achieved ? "text-accent" : "text-muted"
            )}
          >
            <StatusIcon aria-hidden className="h-3.5 w-3.5 shrink-0" />
            {status}
          </div>
        </div>

        {/* Back: what the level means and the criteria behind the verdict. */}
        <div
          aria-hidden={!open}
          className={cn(face, "absolute inset-x-0 top-0 min-h-full gap-2 text-xs shadow-lg [transform:rotateY(180deg)]")}
          style={{ background: faceBg }}
        >
          <div className="flex items-baseline justify-between gap-2">
            <span className="font-semibold">
              L{l.level} {l.name}
            </span>
            {criteria.length > 0 && (
              <span className="text-muted tabular-nums shrink-0">
                {metCount}/{criteria.length}
              </span>
            )}
          </div>
          {criteria.length > 0 ? (
            <ul className="space-y-2.5">
              {criteria.map((c) => (
                <CriterionProgress key={c.id} c={c} />
              ))}
            </ul>
          ) : (
            <p className="text-muted leading-snug">{l.description} The starting point: nothing to meet.</p>
          )}
          {blockedBy !== undefined && (
            <p className="text-muted">Levels count in order, so this waits for L{blockedBy}.</p>
          )}
        </div>
      </div>
    </li>
  );
}

/** One requirement on a level card's back: its name, a bar for "reach at
 *  least" targets, and where the user stands in words. */
function CriterionProgress({ c }: { c: Criterion }) {
  const p = c.progress;
  const Icon = c.met ? Check : X;
  return (
    <li className="leading-snug">
      <div className="flex gap-1.5">
        <Icon aria-hidden className={cn("h-3.5 w-3.5 mt-px shrink-0", c.met ? "text-accent" : "text-signal-critical")} />
        <span className="min-w-0 font-medium">{c.label}</span>
      </div>
      {p && (
        <div className="pl-5 mt-1 space-y-1">
          {typeof p.ratio === "number" && (
            <div
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(p.ratio * 100)}
              aria-label={c.label}
              className="h-1.5 rounded-full bg-line/60 overflow-hidden"
            >
              <div
                className={cn("h-full rounded-full", c.met ? "bg-accent" : "bg-signal-warning")}
                style={{ width: `${Math.max(p.ratio > 0 ? 4 : 0, p.ratio * 100)}%` }}
              />
            </div>
          )}
          <div className="text-muted tabular-nums">{p.text}</div>
        </div>
      )}
    </li>
  );
}

/** Criterion values arrive raw (0.43, 1, true). A rate is a fraction of 1; we
 *  know it's a rate when its target is written as a percentage. */
export function fmtCriterionValue(v: unknown, target?: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    if (typeof target === "string" && target.includes("%")) return `${Math.round(v * 100)}%`;
    return Number.isInteger(v) ? String(v) : v <= 1 && v >= 0 ? `${Math.round(v * 100)}%` : v.toFixed(2);
  }
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

/** Daily trend: alerts, investigations, auto- and approval-resolved. One axis
 *  (all counts); the legend plus a dashed line keep series apart without
 *  relying on colour alone. */
export function MetricsTrend({ daily }: { daily: AiopsMetrics["daily"] }) {
  const data = useMemo(
    () => daily.map((d) => ({ ...d, day: d.date.slice(5) })),
    [daily]
  );
  if (!daily.length) {
    return <div className="text-xs text-muted py-8 text-center">No daily data in this window.</div>;
  }
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <CartesianGrid stroke="var(--line)" vertical={false} />
          <XAxis dataKey="day" stroke="var(--text-muted)" fontSize={10} tickLine={false} />
          <YAxis stroke="var(--text-muted)" fontSize={10} allowDecimals={false} tickLine={false} />
          <Tooltip
            contentStyle={{
              background: "var(--bg-elevated)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              fontSize: 12,
              color: "var(--text)",
            }}
            labelStyle={{ color: "var(--text-muted)" }}
          />
          <Legend wrapperStyle={{ fontSize: 11, color: "var(--text-muted)" }} iconType="plainline" />
          <Line type="monotone" dataKey="alerts" name="Alerts" stroke="var(--text-muted)" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="investigations" name="Investigated" stroke="var(--info)" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="auto_resolved" name="Auto-resolved" stroke="var(--accent)" strokeWidth={2} dot={false} />
          <Line
            type="monotone"
            dataKey="approved_resolved"
            name="Resolved by approved fix"
            stroke="var(--accent)"
            strokeWidth={2}
            strokeDasharray="4 3"
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Yes/no per question, grouped by level; submit → self-assessed level. */
export function AssessmentForm({ maturity, days }: { maturity: Maturity; days: number }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [answers, setAnswers] = useState<Record<string, boolean>>({});
  useEffect(() => {
    setAnswers({ ...(maturity.assessment?.answers ?? {}) });
  }, [maturity.assessment]);

  const byLevel = useMemo(() => {
    const m = new Map<number, Maturity["questions"]>();
    for (const q of maturity.questions) {
      m.set(q.level, [...(m.get(q.level) ?? []), q]);
    }
    return [...m.entries()].sort((a, b) => a[0] - b[0]);
  }, [maturity.questions]);

  const submit = useMutation({
    mutationFn: () =>
      api.aiops.maturity.assess(
        Object.fromEntries(maturity.questions.map((q) => [q.id, answers[q.id] === true]))
      ),
    onSuccess: (a) => {
      qc.invalidateQueries({ queryKey: ["aiops-maturity", days] });
      toast.success(`Self-assessment saved: L${a.level}`);
    },
  });

  if (!maturity.questions.length) {
    return <div className="text-xs text-muted">No self-assessment questions available.</div>;
  }
  const levelName = (n: number) => maturity.levels.find((l) => l.level === n)?.name ?? "";

  return (
    <div className="space-y-4">
      {byLevel.map(([level, qs]) => (
        <div key={level}>
          <div className="text-[11px] uppercase tracking-wider text-muted mb-1.5">
            Level {level} · {levelName(level)}
          </div>
          <ul className="space-y-1.5">
            {qs.map((q) => {
              const v = answers[q.id];
              return (
                <li
                  key={q.id}
                  className="flex flex-wrap items-center gap-3 rounded-lg border border-line bg-bg-elevated/30 px-3 py-2 text-[13px]"
                >
                  <span className="flex-1 min-w-[12rem]">{q.text}</span>
                  <span className="inline-flex gap-1" role="radiogroup" aria-label={q.text}>
                    {([true, false] as const).map((opt) => (
                      <button
                        key={String(opt)}
                        type="button"
                        role="radio"
                        aria-checked={v === opt}
                        onClick={() => setAnswers((a) => ({ ...a, [q.id]: opt }))}
                        className={cn(
                          "motion-press h-7 px-3 rounded-md text-xs border",
                          v === opt
                            ? opt
                              ? "border-accent/60 bg-accent/15 text-accent font-semibold"
                              : "border-line bg-bg-card text-fg font-semibold"
                            : "border-line text-muted hover:text-fg"
                        )}
                      >
                        {opt ? "Yes" : "No"}
                      </button>
                    ))}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" size="sm" pending={submit.isPending} onClick={() => submit.mutate()}>
          Save self-assessment
        </Button>
        <span className="text-xs text-muted">
          Unanswered questions count as &ldquo;no&rdquo;.
        </span>
      </div>
      <ErrorNote error={submit.error} prefix="Couldn't save:" />
    </div>
  );
}
