"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useId, useState } from "react";
import {
  Play,
  AlertTriangle,
  ShieldAlert,
  Download,
} from "lucide-react";
import {
  api,
  type AiFactoryAiperfRun,
  type AiFactoryAiperfArtifact,
  type AiFactoryAiperfLevel,
} from "@/lib/api";
import { Accordion, AccordionItem } from "@/components/ui/accordion";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";
import { cn } from "@/lib/utils";
import { RunStateBadge, When, useRecent, useRunWatch } from "./run-state";

/**
 * AIPerf — load-test / SLO benchmarking. Access is gated server-side:
 *  · site superuser — benchmarks the shared serving stack; free target choice;
 *    sees every run.
 *  · GPU owner / provider — benchmarks ONLY their own endpoint; sees own runs.
 * The page renders this panel for either; the backend enforces both the gate
 * and the per-scope target restriction. A sweep IS load, so it carries a hazard
 * banner. See docs/plans/nvidia-ai-factory/04-aiperf.md.
 */
type Target = "vllm" | "gateway";

export function AiperfPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const [target, setTarget] = useState<Target>("vllm");
  const [concurrency, setConcurrency] = useState("1,2,4,8,16,32");
  const [requestCount, setRequestCount] = useState(200);
  const [err, setErr] = useState<string | null>(null);

  const runs = useQuery({
    queryKey: ["aiFactory", "aiperf"],
    queryFn: api.aiFactory.aiperfRuns,
    refetchInterval: (q) => {
      const data = q.state.data as
        | { runs?: AiFactoryAiperfRun[] }
        | undefined;
      const busy = (data?.runs ?? []).some(
        (r) => r.state === "pending" || r.state === "running"
      );
      return busy ? 5_000 : 30_000;
    },
  });

  const execEnabled = runs.data?.exec_enabled ?? false;
  const scope = runs.data?.scope ?? "site";
  // Only a site operator picks the target; an owner/provider always benchmarks
  // their own endpoint, so the selector is hidden for them.
  const isSite = scope === "site";

  const run = useMutation({
    mutationFn: () =>
      api.aiFactory.runAiperf({
        concurrency,
        request_count: requestCount,
        via_gateway: isSite && target === "gateway",
      }),
    onMutate: () => setErr(null),
    onSuccess: () => {
      toast.success("Benchmark sweep queued. It appears in Past sweeps below.");
      qc.invalidateQueries({ queryKey: ["aiFactory", "aiperf"] });
    },
    onError: (e: unknown) => {
      setErr(errorText(e));
      toast.error(errorText(e));
    },
  });

  const allRuns = runs.data?.runs ?? [];
  const recent = useRecent(allRuns);
  const describe = useCallback(() => "Benchmark sweep", []);
  const fresh = useRunWatch(runs.data?.runs, describe);
  const runsOff = !!runs.data && !execEnabled;

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
        {/* Load-test hazard banner: the icon carries the colour, the text
            stays readable on both themes. */}
        <div className="flex items-start gap-2 rounded-lg border border-signal-warning/30 bg-signal-warning/5 px-3 py-2 text-xs text-[color:var(--text)]">
          <ShieldAlert className="h-4 w-4 mt-px shrink-0 text-[color:var(--warning)]" aria-hidden />
          <span>
            A sweep is real load on the endpoint under test and will spike its
            latency while it runs.{" "}
            {isSite
              ? "This targets the shared serving stack: run it off-peak, or against a candidate node before traffic moves to it."
              : "This benchmarks your own GPU endpoint: run it when you can spare the capacity."}
          </span>
        </div>

        <div className="mt-4 flex flex-wrap items-end gap-3">
          {isSite && (
            <Field label="Target">
              {(id) => (
              <select
                id={id}
                value={target}
                onChange={(e) => setTarget(e.target.value as Target)}
                className="h-8 rounded-md border border-line bg-bg-base px-2 text-xs"
              >
                <option value="vllm">vLLM (llm-classifier)</option>
                <option value="gateway">Gateway (front door)</option>
              </select>
              )}
            </Field>
          )}
          <Field label="Concurrent users per step">
            {(id) => (
            <input
              id={id}
              value={concurrency}
              onChange={(e) => setConcurrency(e.target.value)}
              className="h-8 rounded-md border border-line bg-bg-base px-2 text-xs w-40 font-mono"
              placeholder="1,2,4,8,16,32"
            />
            )}
          </Field>
          <Field label="Requests per step">
            {(id) => (
            <input
              id={id}
              type="number"
              min={1}
              max={2000}
              value={requestCount}
              onChange={(e) => setRequestCount(Number(e.target.value))}
              className="h-8 rounded-md border border-line bg-bg-base px-2 text-xs w-24"
            />
            )}
          </Field>
          <Button
            variant="primary"
            size="sm"
            disabled={runsOff || runs.isLoading}
            disabledReason={runsOff ? "Benchmark runs are switched off on this deployment" : undefined}
            pending={run.isPending}
            onClick={() => run.mutate()}
            icon={<Play className="h-3.5 w-3.5" aria-hidden />}
          >
            Run sweep
          </Button>
        </div>
        {!isSite && (
          <div className="mt-2 text-xs text-muted">
            Target: your own GPU endpoint.
          </div>
        )}

        {runsOff && (
          <div className="mt-3 text-xs text-muted" role="note">
            Benchmark runs are switched off on this deployment, so you can&apos;t
            start a sweep yet. Ask your Daalu operator to turn them on.
          </div>
        )}

        {err && (
          <div role="alert" className="mt-3 text-xs text-[color:var(--critical)] flex items-center gap-1.5">
            <AlertTriangle className="h-3.5 w-3.5" aria-hidden /> {err}
          </div>
        )}
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-medium">Past sweeps</h2>
        <LoadState
          query={runs}
          empty={allRuns.length === 0}
          loadingText="Loading benchmark runs…"
          emptyTitle="No benchmark runs yet"
          emptyHint="Run a sweep above. Each run records time to first token (TTFT), time between tokens (ITL) and throughput at every step."
        >
          <Accordion>
            {recent.shown.map((r) =>
              fresh.has(r.id) ? (
                <div key={r.id} className="motion-enter">
                  <AiperfRow run={r} fresh />
                </div>
              ) : (
                <AiperfRow key={r.id} run={r} />
              )
            )}
          </Accordion>
          {recent.toggle && <div className="pt-1">{recent.toggle}</div>}
        </LoadState>
      </section>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: (id: string) => React.ReactNode;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs text-muted">
        {label}
      </label>
      {children(id)}
    </div>
  );
}

function metricsOf(
  summary: Record<string, unknown> | null
): Record<string, number> | null {
  const m = summary?.["metrics"];
  if (m && typeof m === "object" && !Array.isArray(m)) {
    return m as Record<string, number>;
  }
  return null;
}

function levelsOf(
  summary: Record<string, unknown> | null
): AiFactoryAiperfLevel[] {
  const l = summary?.["concurrency_levels"];
  return Array.isArray(l) ? (l as AiFactoryAiperfLevel[]) : [];
}

function errorOf(summary: Record<string, unknown> | null): string | null {
  // The server's message for this case names an internal setting; say it in
  // the user's terms instead.
  if (summary?.["code"] === "exec_disabled") {
    return "Benchmark runs are switched off on this deployment, so this run didn't start.";
  }
  const e = summary?.["error"];
  return typeof e === "string" ? e : null;
}

const METRIC_LABEL: Record<string, [string, string]> = {
  ttft_ms: ["TTFT", "ms"],
  itl_ms: ["ITL", "ms"],
  tpot_ms: ["TPOT", "ms"],
  request_latency_ms: ["Req latency", "ms"],
  output_token_throughput: ["Output tok/s", ""],
  request_throughput: ["Req/s", ""],
};

// The columns of the per-concurrency SLO curve table, in display order.
const CURVE_COLS: [string, string][] = [
  ["ttft_ms", "TTFT (ms)"],
  ["itl_ms", "ITL (ms)"],
  ["output_token_throughput", "Output tok/s"],
  ["request_throughput", "Req/s"],
];

function fmt(v: number | undefined): string {
  return typeof v === "number" ? v.toFixed(1) : "—";
}

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function ConcurrencyCurve({ levels }: { levels: AiFactoryAiperfLevel[] }) {
  if (levels.length === 0) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px] border-collapse">
        <thead>
          <tr className="text-muted text-left">
            <th className="font-normal py-1 pr-3">Concurrency</th>
            {CURVE_COLS.map(([, label]) => (
              <th key={label} className="font-normal py-1 pr-3 text-right">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="font-mono">
          {levels.map((lv, i) => (
            <tr key={lv.path ?? i} className="border-t border-line/60">
              <td className="py-1 pr-3">{lv.concurrency ?? "—"}</td>
              {CURVE_COLS.map(([key]) => (
                <td key={key} className="py-1 pr-3 text-right">
                  {fmt(lv.metrics?.[key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ArtifactList({ runId }: { runId: string }) {
  const q = useQuery({
    queryKey: ["aiFactory", "aiperfArtifacts", runId],
    queryFn: () => api.aiFactory.aiperfArtifacts(runId),
  });
  const artifacts: AiFactoryAiperfArtifact[] = q.data?.artifacts ?? [];
  if (q.isLoading) {
    return <div className="text-[11px] text-muted">Loading artifacts…</div>;
  }
  if (q.data?.artifacts_error) {
    return (
      <div className="text-[11px] text-[color:var(--warning)]">
        Artifacts unavailable: {q.data.artifacts_error}
      </div>
    );
  }
  if (artifacts.length === 0) {
    return (
      <div className="text-[11px] text-muted">
        No downloadable artifacts for this run.
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <div className="text-[11px] uppercase tracking-wide text-muted">
        Artifacts ({artifacts.length})
      </div>
      <ul className="space-y-0.5">
        {artifacts.map((a) => (
          <li key={a.path} className="flex items-center gap-2 text-[11px]">
            <a
              href={api.aiFactory.aiperfArtifactUrl(runId, a.path)}
              download
              className="inline-flex items-center gap-1 text-accent-cyan hover:underline font-mono break-all"
            >
              <Download className="h-3 w-3 shrink-0" aria-hidden />
              {a.path}
            </a>
            <span className="text-muted whitespace-nowrap">{bytes(a.size)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** "Sweep 1→32 concurrency · 200 req/level" from the run's config. */
function sweepLabel(run: AiFactoryAiperfRun): string {
  const levels = run.concurrency
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
  const range =
    levels.length > 1
      ? `${levels[0]}→${levels[levels.length - 1]}`
      : levels[0] ?? "?";
  const via = run.via_gateway ? "Gateway · " : "";
  return `${via}Sweep ${range} concurrency · ${run.request_count} req/level`;
}

/** The headline number: inter-token latency at the highest level reached. */
function headline(
  levels: AiFactoryAiperfLevel[],
  metrics: Record<string, number> | null
): string | null {
  const last = levels[levels.length - 1];
  const itl = last?.metrics?.["itl_ms"] ?? metrics?.["itl_ms"];
  if (typeof itl !== "number") return null;
  const at = last?.concurrency != null ? ` at ${last.concurrency} concurrent` : "";
  return `${itl.toFixed(1)} ms between tokens${at}`;
}

function AiperfRow({ run, fresh = false }: { run: AiFactoryAiperfRun; fresh?: boolean }) {
  const metrics = metricsOf(run.summary);
  const levels = levelsOf(run.summary);
  const error = errorOf(run.summary);
  const head = headline(levels, metrics);
  const bad = run.state === "error" || run.state === "failed";

  return (
    <AccordionItem
      id={run.id}
      tone={
        run.state === "failed"
          ? "var(--critical)"
          : run.state === "error"
            ? "var(--warning)"
            : undefined
      }
      title={
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn("font-medium px-1 -mx-1", fresh && "motion-update")}>{sweepLabel(run)}</span>
            <RunStateBadge state={run.state} />
            {head && (
              <span
                className="text-xs text-muted"
                title="Inter-token latency (ITL) at the highest concurrency level reached"
              >
                {head}
              </span>
            )}
          </div>
          {bad && error && (
            <div className="flex items-start gap-1.5 text-[11px] text-muted">
              <AlertTriangle className="h-3 w-3 mt-0.5 shrink-0 text-[color:var(--warning)]" />
              <span className="line-clamp-2 break-words">{error}</span>
            </div>
          )}
        </div>
      }
      meta={<When iso={run.started_at} />}
    >
      <AiperfDetail run={run} metrics={metrics} levels={levels} error={error} />
    </AccordionItem>
  );
}

function AiperfDetail({
  run,
  metrics,
  levels,
  error,
}: {
  run: AiFactoryAiperfRun;
  metrics: Record<string, number> | null;
  levels: AiFactoryAiperfLevel[];
  error: string | null;
}) {
  const busy = run.state === "pending" || run.state === "running";
  const done = run.state === "passed" || run.state === "failed";

  // Only fetched while the row is open (the accordion mounts this then).
  const detail = useQuery({
    queryKey: ["aiFactory", "aiperfRun", run.id],
    queryFn: () => api.aiFactory.aiperfRun(run.id),
    refetchInterval: busy ? 5_000 : false,
  });

  return (
    <div className="space-y-3">
      <div className="text-[11px] text-muted font-mono break-all">
        {run.model} → {run.target_url} · {run.endpoint_type} · in{" "}
        {run.input_tokens} / out {run.output_tokens} tokens
      </div>

      {metrics && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(metrics).map(([k, v]) => {
            const meta = METRIC_LABEL[k] ?? [k, ""];
            return (
              <span
                key={k}
                className="text-[11px] rounded border border-line px-1.5 py-0.5 font-mono"
              >
                <span className="text-muted">{meta[0]}</span>{" "}
                {typeof v === "number" ? v.toFixed(1) : String(v)}
                {meta[1]}
              </span>
            );
          })}
        </div>
      )}

      {/* Per-concurrency SLO curve, parsed from the artifact JSONs. */}
      {levels.length > 0 && <ConcurrencyCurve levels={levels} />}

      {/* Downloadable structured artifacts (CSV/JSON/logs). */}
      {done && <ArtifactList runId={run.id} />}

      <details>
        <summary className="text-[11px] text-muted cursor-pointer hover:text-[color:var(--text)]">
          Show raw output
        </summary>
        {detail.isLoading ? (
          <div className="mt-2 text-[11px] text-muted">Loading output…</div>
        ) : (
          <pre className="mt-2 text-[11px] font-mono bg-bg-base rounded-md border border-line p-3 overflow-x-auto whitespace-pre-wrap max-h-72 overflow-y-auto">
            {detail.data?.output ||
              error ||
              (run.summary
                ? JSON.stringify(run.summary, null, 2)
                : "(no output)")}
          </pre>
        )}
      </details>
    </div>
  );
}
