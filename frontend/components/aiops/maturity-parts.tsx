"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Circle, Loader2 } from "lucide-react";
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
import { ErrorNote, btnPrimary } from "./ui";

/** The 0–5 ladder: which levels are met, the current one highlighted. */
export function MaturityLadder({ maturity }: { maturity: Maturity }) {
  return (
    <ol className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
      {maturity.levels.map((l) => {
        const current = l.level === maturity.level;
        return (
          <li
            key={l.level}
            className={cn(
              "rounded-xl border p-3 text-xs",
              current ? "border-accent/60" : "border-line",
              !l.met && !current && "opacity-70"
            )}
            style={
              current
                ? { background: "color-mix(in srgb, var(--accent) 12%, var(--bg-card))" }
                : { background: "var(--bg-card)" }
            }
            title={l.description}
          >
            <div className="flex items-center gap-1.5">
              {l.met ? (
                <Check className="h-3.5 w-3.5 text-accent" />
              ) : (
                <Circle className="h-3.5 w-3.5 text-muted" />
              )}
              <span className="font-semibold tabular-nums">L{l.level}</span>
              <span className="font-medium truncate">{l.name}</span>
            </div>
            <div className="text-[11px] text-muted mt-1 line-clamp-3">{l.description}</div>
          </li>
        );
      })}
    </ol>
  );
}

export function fmtCriterionValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["aiops-maturity", days] }),
  });

  if (!maturity.questions.length) {
    return <div className="text-xs text-muted">No self-assessment questions available.</div>;
  }
  const levelName = (n: number) => maturity.levels.find((l) => l.level === n)?.name ?? "";

  return (
    <div className="space-y-4">
      {byLevel.map(([level, qs]) => (
        <div key={level}>
          <div className="text-[10px] uppercase tracking-wider text-muted mb-1.5">
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
                          "h-7 px-3 rounded-md text-xs border",
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
        <button
          type="button"
          className={btnPrimary}
          disabled={submit.isPending}
          onClick={() => submit.mutate()}
        >
          {submit.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          Save self-assessment
        </button>
        <span className="text-xs text-muted">
          Unanswered questions count as &ldquo;no&rdquo;.
        </span>
      </div>
      <ErrorNote error={submit.error} prefix="Couldn't save:" />
    </div>
  );
}
