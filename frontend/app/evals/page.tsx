"use client";

import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, FlaskConical, Terminal } from "lucide-react";
import { api, type EvalRun } from "@/lib/api";
import { cn, formatRelative } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";
import { Chip, PageHeader, fmtRate, fmtScore, inputCls } from "@/components/aiops/ui";

/**
 * /evals — benchmark the AI SRE: scripted failure scenarios run against a
 * live cluster, graded on root cause, fix tool, forbidden tools and speed.
 */
export default function EvalsPage() {
  const [scenario, setScenario] = useState("");
  const [label, setLabel] = useState("");
  const board = useQuery({ queryKey: ["aiops-evals-board"], queryFn: () => api.aiops.evals.leaderboard() });
  const scenarios = useQuery({ queryKey: ["aiops-evals-scenarios"], queryFn: () => api.aiops.evals.scenarios() });
  const runs = useQuery({
    queryKey: ["aiops-evals-runs", scenario, label],
    queryFn: () => api.aiops.evals.runs({ scenario_id: scenario || undefined, label: label || undefined }),
  });

  return (
    <div className="space-y-6 max-w-[1200px]">
      <PageHeader icon={FlaskConical} title="Evals">
        Break something on purpose, let the AI SRE investigate, and grade what it
        did. Compare models (labels) on the same scenarios before you trust one
        with more autonomy.
      </PageHeader>

      <section className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5 space-y-2">
        <div className="flex items-center gap-2 text-[15px] font-medium">
          <Terminal className="h-4 w-4 text-accent" /> How to run
        </div>
        <p className="text-xs text-muted">
          With <span className="font-mono">DAALU_URL</span> and{" "}
          <span className="font-mono">DAALU_TOKEN</span> set and a kubeconfig for the
          target cluster, list the scenarios and run one. The run is graded
          (root cause 40%, correct fix tool 40%, no forbidden tool 10%, triaged
          in time 10%) and recorded here.
        </p>
        <pre className="text-[12px] font-mono bg-bg-elevated/60 border border-line rounded-lg p-3 overflow-x-auto">
{`daalu eval list
daalu eval run <id> --label <model> --kubeconfig ~/.kube/config`}
        </pre>
      </section>

      <section className="space-y-2">
        <div className="text-[15px] font-medium">Leaderboard</div>
        <LoadState
          query={board}
          empty={(board.data ?? []).length === 0}
          emptyTitle="No eval runs recorded yet"
          emptyHint="Run a scenario with the command above; results land here grouped by label."
        >
          <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead className="text-[10px] uppercase tracking-wider text-muted bg-bg-elevated/50">
                <tr>
                  <th className="text-left px-4 py-2">#</th>
                  <th className="text-left px-4 py-2">Label</th>
                  <th className="text-right px-4 py-2">Mean score</th>
                  <th className="text-right px-4 py-2">Pass rate</th>
                  <th className="text-right px-4 py-2">Runs</th>
                  <th className="text-right px-4 py-2">Scenarios</th>
                </tr>
              </thead>
              <tbody>
                {[...(board.data ?? [])]
                  .sort((a, b) => b.mean_score - a.mean_score)
                  .map((r, i) => (
                    <tr
                      key={r.label}
                      className="border-t border-line/60 hover:bg-bg-elevated/30 cursor-pointer"
                      onClick={() => setLabel(r.label)}
                    >
                      <td className="px-4 py-2 text-muted tabular-nums">{i + 1}</td>
                      <td className="px-4 py-2 font-mono">{r.label}</td>
                      <td className="px-4 py-2 text-right tabular-nums font-semibold">{fmtScore(r.mean_score)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{fmtRate(r.pass_rate)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{r.runs}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{r.scenarios}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </LoadState>
      </section>

      <section className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-[15px] font-medium flex-1">Runs</div>
          <select aria-label="Scenario" value={scenario} onChange={(e) => setScenario(e.target.value)} className={inputCls}>
            <option value="">All scenarios</option>
            {(scenarios.data ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.id}
              </option>
            ))}
          </select>
          <input
            aria-label="Label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Label (model)"
            className={inputCls}
          />
        </div>
        <LoadState
          query={runs}
          empty={(runs.data ?? []).length === 0}
          emptyTitle="No runs match"
          emptyHint="Clear the filters, or run a scenario."
        >
          <RunsTable runs={runs.data ?? []} />
        </LoadState>
      </section>

      <section className="space-y-2">
        <div className="text-[15px] font-medium">Scenarios</div>
        <LoadState
          query={scenarios}
          empty={(scenarios.data ?? []).length === 0}
          emptyTitle="No scenarios found"
          emptyHint="Scenarios are YAML files in evals/scenarios/."
        >
          <div className="grid md:grid-cols-2 gap-3">
            {(scenarios.data ?? []).map((s) => (
              <div key={s.id} className="rounded-2xl border border-line bg-bg-card p-4">
                <div className="text-[10px] uppercase tracking-wider text-muted font-mono">{s.id}</div>
                <div className="text-[15px] font-medium mt-0.5">{s.title}</div>
                {s.description && <div className="text-xs text-fg/80 mt-1">{s.description}</div>}
                <div className="text-[11px] text-muted mt-2 flex flex-wrap gap-x-3">
                  {s.namespace && <span>namespace <span className="font-mono">{s.namespace}</span></span>}
                  {s.alert && <span>alert <span className="font-mono">{s.alert}</span></span>}
                </div>
                {Object.keys(s.expect ?? {}).length > 0 && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-[11px] text-muted">Expectations</summary>
                    <pre className="mt-1 text-[11px] font-mono whitespace-pre-wrap bg-bg-elevated/60 border border-line rounded p-2">
                      {JSON.stringify(s.expect, null, 2)}
                    </pre>
                  </details>
                )}
              </div>
            ))}
          </div>
        </LoadState>
      </section>
    </div>
  );
}

function RunsTable({ runs }: { runs: EvalRun[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead className="text-[10px] uppercase tracking-wider text-muted bg-bg-elevated/50">
          <tr>
            <th className="px-2" />
            <th className="text-left px-4 py-2">Scenario</th>
            <th className="text-left px-4 py-2">Label</th>
            <th className="text-right px-4 py-2">Score</th>
            <th className="text-left px-4 py-2">Result</th>
            <th className="text-left px-4 py-2">Started</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <Fragment key={r.id}>
              <tr
                className="border-t border-line/60 hover:bg-bg-elevated/30 cursor-pointer"
                onClick={() => setOpen(open === r.id ? null : r.id)}
              >
                <td className="px-2 text-muted">
                  <ChevronRight className={cn("h-4 w-4 transition-transform", open === r.id && "rotate-90")} />
                </td>
                <td className="px-4 py-2 font-mono text-[13px]">{r.scenario_id}</td>
                <td className="px-4 py-2 font-mono text-[13px]">{r.label}</td>
                <td className="px-4 py-2 text-right tabular-nums font-semibold">{fmtScore(r.score)}</td>
                <td className="px-4 py-2">
                  {r.passed ? <Chip tone="accent">pass</Chip> : <Chip tone="critical">fail</Chip>}
                </td>
                <td className="px-4 py-2 text-muted">{formatRelative(r.started_at)}</td>
              </tr>
              {open === r.id && (
                <tr className="bg-bg-elevated/20">
                  <td />
                  <td colSpan={5} className="px-4 py-3">
                    <RunDetails details={r.details} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Per-check breakdown. `details` is free-form JSON from the grader; we show
 * `checks` (a list or a {name: {...}} map) as rows when present and the rest
 * as JSON.
 */
function RunDetails({ details }: { details: Record<string, unknown> }) {
  const raw = details?.checks;
  const checks: { name: string; passed?: boolean; score?: number; weight?: number; note?: string }[] = [];
  if (Array.isArray(raw)) {
    for (const c of raw as Record<string, unknown>[]) {
      checks.push({
        name: String(c.name ?? c.id ?? "check"),
        passed: typeof c.passed === "boolean" ? c.passed : undefined,
        score: typeof c.score === "number" ? c.score : undefined,
        weight: typeof c.weight === "number" ? c.weight : undefined,
        note: typeof c.detail === "string" ? c.detail : typeof c.note === "string" ? c.note : undefined,
      });
    }
  } else if (raw && typeof raw === "object") {
    for (const [name, v] of Object.entries(raw as Record<string, unknown>)) {
      if (v && typeof v === "object") {
        const c = v as Record<string, unknown>;
        checks.push({
          name,
          passed: typeof c.passed === "boolean" ? c.passed : undefined,
          score: typeof c.score === "number" ? c.score : undefined,
          weight: typeof c.weight === "number" ? c.weight : undefined,
          note: typeof c.detail === "string" ? c.detail : typeof c.note === "string" ? c.note : undefined,
        });
      } else {
        checks.push({ name, passed: typeof v === "boolean" ? v : undefined, score: typeof v === "number" ? v : undefined });
      }
    }
  }
  const { checks: _c, ...rest } = details ?? {};
  return (
    <div className="space-y-2 text-xs">
      {checks.length > 0 && (
        <ul className="space-y-1">
          {checks.map((c) => (
            <li key={c.name} className="flex flex-wrap items-center gap-2">
              {c.passed === undefined ? null : c.passed ? <Chip tone="accent">pass</Chip> : <Chip tone="critical">fail</Chip>}
              <span className="font-mono">{c.name}</span>
              {c.score !== undefined && (
                <span className="tabular-nums text-muted">
                  {c.score}
                  {c.weight !== undefined ? ` / ${c.weight}` : ""}
                </span>
              )}
              {c.note && <span className="text-fg/80">{c.note}</span>}
            </li>
          ))}
        </ul>
      )}
      {Object.keys(rest).length > 0 && (
        <pre className="text-[11px] font-mono whitespace-pre-wrap bg-bg-elevated/60 border border-line rounded p-2 max-h-[260px] overflow-auto">
          {JSON.stringify(rest, null, 2)}
        </pre>
      )}
      {checks.length === 0 && Object.keys(rest).length === 0 && <span className="text-muted">No details recorded.</span>}
    </div>
  );
}
