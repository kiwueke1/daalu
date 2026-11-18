"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Check, Gauge, X } from "lucide-react";
import { api } from "@/lib/api";
import { LoadState } from "@/components/ui/load-state";
import {
  PageHeader,
  Segmented,
  StatTile,
  WINDOW_OPTIONS,
  fmtDateTime,
  fmtDuration,
  fmtRate,
} from "@/components/aiops/ui";
import {
  AssessmentForm,
  MaturityLadder,
  MetricsTrend,
  fmtCriterionValue,
} from "@/components/aiops/maturity-parts";

/**
 * /maturity — where the team is on the AI-Ops Maturity Model (AOMM v0.1):
 * the automatic level computed from data, the ladder, the criteria behind
 * it, the TRACE metrics, and the self-assessment for what data can't see.
 */
export default function MaturityPage() {
  const [days, setDays] = useState(30);
  const q = useQuery({
    queryKey: ["aiops-maturity", days],
    queryFn: () => api.aiops.maturity.get(days),
  });
  const m = q.data;
  const current = m?.levels.find((l) => l.level === m.level);
  const met = m?.metrics;

  return (
    <div className="space-y-6 max-w-[1200px]">
      <PageHeader
        icon={Gauge}
        title="AI-Ops maturity"
        actions={
          <Segmented
            ariaLabel="Window"
            options={WINDOW_OPTIONS}
            value={days}
            onChange={setDays}
          />
        }
      >
        How your team works with the AI SRE, measured from what actually
        happened in the last {days} days: triage, approvals, escalations,
        verdicts and runbooks.
      </PageHeader>

      <LoadState query={q} loadingText="Computing your maturity level…">
        {m && met && (
          <>
            <section
              className="rounded-2xl border border-line p-5 lg:p-6 flex flex-wrap items-center gap-6"
              style={{
                background: "color-mix(in srgb, var(--accent) 8%, var(--bg-card))",
              }}
            >
              <div className="flex items-baseline gap-3">
                <Live value={`L${m.level}`} className="text-6xl font-bold tabular-nums text-accent" />
                <span className="text-2xl font-semibold">{m.name}</span>
              </div>
              <div className="flex-1 min-w-[16rem] text-sm text-fg/80">
                {current?.description}
                <div className="text-xs text-muted mt-1">
                  The highest level whose criteria (and every level below) are all met.
                </div>
              </div>
              {m.assessment && (
                <div className="rounded-xl border border-line bg-bg-card px-4 py-3 text-center">
                  <div className="text-[11px] uppercase tracking-wider text-muted">
                    Self-assessed
                  </div>
                  <div className="text-2xl font-semibold tabular-nums">L{m.assessment.level}</div>
                  <div className="text-xs text-muted">
                    {m.assessment.created_by || "—"} · {fmtDateTime(m.assessment.created_at)}
                  </div>
                </div>
              )}
            </section>

            <MaturityLadder maturity={m} />

            <TraceMetrics met={met} />

            <section aria-labelledby="supporting-metrics">
              <div id="supporting-metrics" className="text-[11px] uppercase tracking-wider text-muted mb-2">
                Supporting metrics · what explains the TRACE numbers
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <StatTile
                  label="AI triage rate"
                  value={<Live value={fmtRate(met.ai_triage_rate)} />}
                  hint={`${met.investigations_total} of ${met.alerts_total} alerts investigated by the AI`}
                />
                <StatTile
                  label="Approval rate"
                  value={<Live value={fmtRate(met.proposal_approval_rate)} />}
                  hint={`${met.actions.approved} approved · ${met.actions.rejected} rejected`}
                />
                <StatTile
                  label="Audit chain"
                  value={<Live value={met.audit_chain_ok ? "Intact" : "Broken"} />}
                  tone={met.audit_chain_ok ? "accent" : "critical"}
                  hint={<Link href="/audit" className="underline underline-offset-2">Open audit log</Link>}
                />
              </div>
            </section>

            <section className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5">
              <div className="text-[11px] uppercase tracking-wider text-muted mb-2">
                Daily trend · last {days} days
              </div>
              <MetricsTrend daily={met.daily} />
            </section>

            <div className="grid lg:grid-cols-[1.4fr_1fr] gap-4">
              <section className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
                <div className="px-4 pt-4 text-[11px] uppercase tracking-wider text-muted">
                  Criteria
                </div>
                <table className="w-full min-w-[520px] text-sm mt-2">
                  <thead className="text-[11px] uppercase tracking-wider text-muted bg-bg-elevated/50">
                    <tr>
                      <th className="text-left px-4 py-2">Level</th>
                      <th className="text-left px-4 py-2">Criterion</th>
                      <th className="text-left px-4 py-2">Met</th>
                      <th className="text-right px-4 py-2">Value</th>
                      <th className="text-right px-4 py-2">Target</th>
                    </tr>
                  </thead>
                  <tbody>
                    {m.criteria.map((c) => (
                      <tr key={`${c.level}-${c.id}`} className="border-t border-line/60">
                        <td className="px-4 py-2 tabular-nums text-muted">L{c.level}</td>
                        <td className="px-4 py-2">{c.label}</td>
                        <td className="px-4 py-2">
                          {c.met ? (
                            <span className="inline-flex items-center gap-1 text-accent text-xs">
                              <Check aria-hidden className="h-3.5 w-3.5" /> met
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-muted text-xs">
                              <X aria-hidden className="h-3.5 w-3.5" /> not yet
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-2 text-right tabular-nums">{fmtCriterionValue(c.value)}</td>
                        <td className="px-4 py-2 text-right tabular-nums text-muted">
                          {fmtCriterionValue(c.target)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>

              <section className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5">
                <div className="text-[11px] uppercase tracking-wider text-muted mb-2">Next steps</div>
                {m.next_steps.length ? (
                  <ul className="space-y-2">
                    {m.next_steps.map((s, i) => (
                      <li key={i} className="flex gap-2 text-sm">
                        <ArrowRight aria-hidden className="h-4 w-4 mt-0.5 shrink-0 text-accent" />
                        <span>{s}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="text-sm text-muted">
                    Every criterion is met. Keep reviewing the{" "}
                    <Link href="/autonomy" className="underline underline-offset-2">policy</Link>{" "}
                    and grading{" "}
                    <Link href="/investigations" className="underline underline-offset-2">investigations</Link>.
                  </div>
                )}
              </section>
            </div>

            <section className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5">
              <div className="flex flex-wrap items-baseline gap-2 mb-3">
                <div className="text-[15px] font-medium">Self-assessment</div>
                <div className="text-xs text-muted">
                  The organisational practices the data can&apos;t see. Reported
                  beside the automatic level; the headline stays automatic.
                </div>
              </div>
              <AssessmentForm maturity={m} days={days} />
            </section>
          </>
        )}
      </LoadState>
    </div>
  );
}

type Metrics = NonNullable<Awaited<ReturnType<typeof api.aiops.maturity.get>>>["metrics"];

/** The five TRACE metrics, one lettered card each, so each can be read out in
 *  order: Time to Triage, time to Resolve, Agreement, Change failure, Escalation.
 *  Copy follows docs/maturity-model.md ("The five TRACE metrics"). */
function TraceMetrics({ met }: { met: Metrics }) {
  const cards: {
    letter: string;
    name: string;
    group: "Speed" | "Quality" | "Autonomy";
    value: string;
    question: string;
    better: "Lower" | "Higher";
    detail: string;
    gates: string;
    bad?: boolean;
  }[] = [
    {
      letter: "T",
      name: "Time to triage",
      group: "Speed",
      value: fmtDuration(met.median_time_to_triage_s),
      question: "How long from the alert firing to the AI naming a root cause?",
      better: "Lower",
      detail: "median",
      gates: "Gates no level: compare with your own history",
    },
    {
      letter: "R",
      name: "Time to resolve",
      group: "Speed",
      value: fmtDuration(met.median_time_to_resolve_s),
      question: "How long from the alert firing to a verified fix?",
      better: "Lower",
      detail: "median (MTTR)",
      gates: "Gates no level: compare with your own history",
    },
    {
      letter: "A",
      name: "Agreement rate",
      group: "Quality",
      value: fmtRate(met.agreement_rate),
      question: "How often does the team agree with the AI's root cause?",
      better: "Higher",
      detail:
        met.verdict_coverage === null || met.verdict_coverage === 0
          ? "no verdicts yet: grade investigations to measure it"
          : `verdicts on ${fmtRate(met.verdict_coverage)} of investigations`,
      gates: "Gates Level 5 (≥ 80 %)",
    },
    {
      letter: "C",
      name: "Change-failure rate",
      group: "Quality",
      value: fmtRate(met.change_failure_rate),
      question: "Of the AI fixes that ran, how many didn't fix the problem?",
      better: "Lower",
      detail: `${met.actions.failed_verification} of ${met.actions.executed} executed failed verification`,
      gates: "Gates Level 3",
      bad: met.change_failure_rate !== null && met.change_failure_rate > 0.15,
    },
    {
      letter: "E",
      name: "Escalation rate",
      group: "Autonomy",
      value: fmtRate(met.escalation_rate),
      question: "Of resolved incidents, how many still needed a human?",
      better: "Lower",
      detail: `${met.resolutions.approved + met.resolutions.manual} needed a human · ${met.resolutions.auto} auto-resolved`,
      gates: "Gates Level 5 (≤ 50 %)",
    },
  ];
  return (
    <section aria-labelledby="trace-title" className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 mb-3">
        <h2 id="trace-title" className="text-[15px] font-medium">
          <span className="tracking-[0.25em] font-bold text-accent">TRACE</span> · how well the AI is doing
        </h2>
        <div className="text-xs text-muted">
          Two speed numbers, two quality numbers, one for autonomy. &ldquo;—&rdquo; means nothing to
          measure yet in this window.
        </div>
      </div>
      <ol className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
        {cards.map((c) => (
          <li key={c.letter} className="rounded-xl border border-line bg-bg-elevated/40 p-4 flex flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <span
                aria-hidden
                className="inline-flex h-10 w-10 items-center justify-center rounded-lg text-xl font-bold text-accent"
                style={{ background: "color-mix(in srgb, var(--accent) 14%, transparent)" }}
              >
                {c.letter}
              </span>
              <span className="text-[11px] uppercase tracking-wider text-muted">{c.group}</span>
            </div>
            <div className="text-sm font-medium">
              <span className="sr-only">{c.letter}: </span>
              {c.name}
            </div>
            <div
              className="text-2xl font-semibold tabular-nums"
              style={c.bad ? { color: "var(--critical)" } : undefined}
            >
              <Live value={c.value} />
            </div>
            <div className="text-xs text-muted">{c.detail}</div>
            <p className="text-xs text-fg/80 leading-relaxed">{c.question}</p>
            <div className="mt-auto pt-2 border-t border-line/60 text-xs text-muted space-y-0.5">
              <div>{c.better} is better</div>
              <div>{c.gates}</div>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

/** A value that tints once (`motion-update`) when a refetch changes it, so a
 *  moved metric draws the eye; nothing animates on first render. */
function Live({ value, className }: { value: string; className?: string }) {
  const prev = useRef(value);
  const [changes, setChanges] = useState(0);
  useEffect(() => {
    if (prev.current !== value) {
      prev.current = value;
      setChanges((n) => n + 1);
    }
  }, [value]);
  return (
    <span key={changes} className={[changes ? "motion-update" : "", className ?? ""].join(" ").trim() || undefined}>
      {value}
    </span>
  );
}
