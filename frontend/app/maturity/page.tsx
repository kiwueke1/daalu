"use client";

import { useState } from "react";
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
 * it, the key metrics, and the self-assessment for what data can't see.
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
        happened in the last {days} days: triage, approvals, auto-resolution,
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
                <span className="text-6xl font-bold tabular-nums text-accent">L{m.level}</span>
                <span className="text-2xl font-semibold">{m.name}</span>
              </div>
              <div className="flex-1 min-w-[16rem] text-sm text-fg/80">
                {current?.description}
                <div className="text-[11px] text-muted mt-1">
                  The highest level whose criteria (and every level below) are all met.
                </div>
              </div>
              {m.assessment && (
                <div className="rounded-xl border border-line bg-bg-card px-4 py-3 text-center">
                  <div className="text-[10px] uppercase tracking-wider text-muted">
                    Self-assessed
                  </div>
                  <div className="text-2xl font-semibold tabular-nums">L{m.assessment.level}</div>
                  <div className="text-[10px] text-muted">
                    {m.assessment.created_by || "—"} · {fmtDateTime(m.assessment.created_at)}
                  </div>
                </div>
              )}
            </section>

            <MaturityLadder maturity={m} />

            <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <StatTile
                label="AI triage rate"
                value={fmtRate(met.ai_triage_rate)}
                hint={`${met.investigations_total} of ${met.alerts_total} alerts`}
              />
              <StatTile label="Time to diagnosis" value={fmtDuration(met.median_time_to_diagnosis_s)} hint="median" />
              <StatTile
                label="RCA agreement"
                value={fmtRate(met.rca_agreement_rate)}
                hint={`verdicts on ${fmtRate(met.verdict_coverage)} of investigations`}
              />
              <StatTile
                label="Approval rate"
                value={fmtRate(met.proposal_approval_rate)}
                hint={`${met.actions.approved} approved · ${met.actions.rejected} rejected`}
              />
              <StatTile
                label="AI change-failure rate"
                value={fmtRate(met.ai_change_failure_rate)}
                hint={`${met.actions.failed_verification} of ${met.actions.executed} executed failed verification`}
                tone={
                  met.ai_change_failure_rate !== null && met.ai_change_failure_rate > 0.15
                    ? "critical"
                    : undefined
                }
              />
              <StatTile
                label="Auto-resolution rate"
                value={fmtRate(met.auto_resolution_rate)}
                hint={`${met.resolutions.auto} auto-resolved`}
              />
              <StatTile label="MTTR" value={fmtDuration(met.median_time_to_resolve_s)} hint="median time to resolve" />
              <StatTile
                label="Audit chain"
                value={met.audit_chain_ok ? "Intact" : "Broken"}
                tone={met.audit_chain_ok ? "accent" : "critical"}
                hint={<Link href="/audit" className="underline underline-offset-2">Open audit log</Link>}
              />
            </section>

            <section className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5">
              <div className="text-[10px] uppercase tracking-wider text-muted mb-2">
                Daily trend · last {days} days
              </div>
              <MetricsTrend daily={met.daily} />
            </section>

            <div className="grid lg:grid-cols-[1.4fr_1fr] gap-4">
              <section className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
                <div className="px-4 pt-4 text-[10px] uppercase tracking-wider text-muted">
                  Criteria
                </div>
                <table className="w-full min-w-[520px] text-sm mt-2">
                  <thead className="text-[10px] uppercase tracking-wider text-muted bg-bg-elevated/50">
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
                              <Check className="h-3.5 w-3.5" /> met
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-muted text-xs">
                              <X className="h-3.5 w-3.5" /> not yet
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
                <div className="text-[10px] uppercase tracking-wider text-muted mb-2">Next steps</div>
                {m.next_steps.length ? (
                  <ul className="space-y-2">
                    {m.next_steps.map((s, i) => (
                      <li key={i} className="flex gap-2 text-sm">
                        <ArrowRight className="h-4 w-4 mt-0.5 shrink-0 text-accent" />
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
