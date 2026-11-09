"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  Activity,
  ArrowUpRight,
  Gauge,
  Play,
  ShieldCheck,
  Stethoscope,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn, formatRelative } from "@/lib/utils";
import { errorText } from "@/lib/errors";
import { useToast } from "@/components/ui/toast";
import { RUN_STATE_LABEL, type RunState } from "./run-state";

/**
 * Factory operations — the main-page launcher for tests that act on the WHOLE
 * factory floor (every card), not a single GPU: dcgmi/NCCL diagnostics, the
 * AIPerf load sweep, the observability self-check, and the reliability posture.
 *
 * Deliberately results-free: a tile shows only a tiny last-run status pill and
 * a Run shortcut for the parameter-less checks. The actual output, history and
 * full run controls live on each test's own page — per-GPU detail stays under
 * the GPU, factory-wide detail stays one click away. This keeps the overview
 * from turning into the wall of diagnostic dumps it used to be.
 */
export function FactoryOps({
  isAdmin,
  isSuperuser,
  isHardware,
}: {
  isAdmin: boolean;
  isSuperuser: boolean;
  isHardware: boolean;
}) {
  const showDiagnostics = isAdmin;
  const showObservability = isAdmin;
  const showBenchmark = isSuperuser || isHardware;
  const showReliability = isHardware;

  if (!showDiagnostics && !showObservability && !showBenchmark && !showReliability) {
    return null;
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">Factory operations</h3>
        <span className="text-[11px] text-muted">
          Whole-floor tests — open one for history &amp; results
        </span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
        {showDiagnostics && <DiagnosticsTile />}
        {showObservability && <ObservabilityTile />}
        {showBenchmark && <BenchmarkTile />}
        {showReliability && <ReliabilityTile />}
      </div>
    </section>
  );
}

// ── shared tile shell ─────────────────────────────────────────────────────

function Tile({
  href,
  icon,
  title,
  desc,
  tool,
  badge,
  onRun,
  running,
  runLabel = "Run",
}: {
  href: string;
  icon: React.ReactNode;
  title: string;
  desc: string;
  /** The tool behind it, shown small under the plain-English line. */
  tool?: string;
  badge?: React.ReactNode;
  onRun?: () => void;
  running?: boolean;
  runLabel?: string;
}) {
  return (
    <div className="flex flex-col rounded-xl border border-line bg-bg-elevated/40 p-4 hover:border-accent-emerald/40 transition-colors">
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          <span className="text-accent-emerald">{icon}</span>
          {title}
        </div>
        {badge}
      </div>
      <p className="text-xs text-muted mt-1.5 flex-1">
        {desc}
        {tool && <span className="block mt-1 text-[11px] font-mono opacity-90">{tool}</span>}
      </p>
      <div className="mt-3 flex items-center justify-between gap-2">
        <Link
          href={href}
          className="text-xs text-accent-emerald inline-flex items-center gap-1 hover:underline"
          aria-label={`Open ${title}`}
        >
          Open <ArrowUpRight className="h-3 w-3" aria-hidden />
        </Link>
        {onRun && (
          <button
            type="button"
            onClick={onRun}
            disabled={running}
            aria-busy={running || undefined}
            className="motion-press text-xs h-8 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--text)] hover:border-accent-emerald/40 inline-flex items-center gap-1 disabled:opacity-50"
          >
            <Play className="h-3 w-3" aria-hidden /> {running ? "Starting…" : runLabel}
          </button>
        )}
      </div>
    </div>
  );
}

// ── last-run pill (state only — never the output) ─────────────────────────

function StatePill({
  state,
  when,
  loading,
}: {
  state?: string;
  when?: string | null;
  loading?: boolean;
}) {
  if (loading) return <span className="text-[11px] text-muted">Loading…</span>;
  if (!state) return <span className="text-[11px] text-muted">No runs yet</span>;
  const tone =
    state === "passed"
      ? "border-accent-emerald/40 text-accent-emerald"
      : state === "failed" || state === "error"
        ? "border-signal-critical/40 text-[color:var(--critical)]"
        : "border-signal-warning/40 text-[color:var(--warning)]";
  return (
    <span
      className={cn(
        "text-[11px] rounded-full px-2 py-0.5 border inline-flex items-center gap-1 whitespace-nowrap",
        tone,
      )}
      title={when ? new Date(when).toLocaleString() : undefined}
    >
      {RUN_STATE_LABEL[state as RunState] ?? state}
      {when && <span className="text-muted">· {formatRelative(when)}</span>}
    </span>
  );
}

// ── tiles ─────────────────────────────────────────────────────────────────

const DIAG_KINDS = new Set(["dcgmi_diag", "nccl_test"]);

function DiagnosticsTile() {
  const router = useRouter();
  const qc = useQueryClient();
  const runs = useQuery({
    queryKey: ["aiFactory", "diagnostics"],
    queryFn: api.aiFactory.diagnostics,
    refetchInterval: 30_000,
  });
  const last = (runs.data?.runs ?? []).find((r) => DIAG_KINDS.has(r.kind));
  const toast = useToast();
  const run = useMutation({
    mutationFn: () =>
      api.aiFactory.runDiagnostic({ kind: "dcgmi_diag", level: 1 }),
    onSuccess: () => {
      toast.success("Quick health check started. Results appear on the Diagnostics page.");
      qc.invalidateQueries({ queryKey: ["aiFactory", "diagnostics"] });
      router.push("/ai-factory/diagnostics");
    },
    onError: (e) => toast.error(errorText(e)),
  });
  return (
    <Tile
      href="/ai-factory/diagnostics"
      icon={<Stethoscope className="h-4 w-4" />}
      title="Diagnostics"
      desc="Check each GPU's health and the links between GPUs."
      tool="dcgmi diag · NCCL tests"
      badge={<StatePill loading={runs.isLoading} state={last?.state} when={last?.finished_at ?? last?.started_at} />}
      onRun={() => run.mutate()}
      running={run.isPending}
      runLabel="Run health check"
    />
  );
}

function ObservabilityTile() {
  const router = useRouter();
  const qc = useQueryClient();
  const runs = useQuery({
    queryKey: ["aiFactory", "diagnostics"],
    queryFn: api.aiFactory.diagnostics,
    refetchInterval: 30_000,
  });
  const last = (runs.data?.runs ?? []).find(
    (r) => (r.kind as string) === "observability_validate",
  );
  const toast = useToast();
  const run = useMutation({
    mutationFn: () => api.aiFactory.validateObservability(),
    onSuccess: () => {
      toast.success("Observability check started. Results appear on its page.");
      qc.invalidateQueries({ queryKey: ["aiFactory", "diagnostics"] });
      router.push("/ai-factory/observability");
    },
    onError: (e) => toast.error(errorText(e)),
  });
  return (
    <Tile
      href="/ai-factory/observability"
      icon={<ShieldCheck className="h-4 w-4" />}
      title="Observability check"
      desc="Make sure GPU metrics are being collected and can be charted."
      tool="exporters · scrape · query self-check"
      badge={<StatePill loading={runs.isLoading} state={last?.state} when={last?.finished_at ?? last?.started_at} />}
      onRun={() => run.mutate()}
      running={run.isPending}
      runLabel="Run self-check"
    />
  );
}

function BenchmarkTile() {
  const runs = useQuery({
    queryKey: ["aiFactory", "aiperfRuns"],
    queryFn: api.aiFactory.aiperfRuns,
    refetchInterval: 30_000,
  });
  const last = runs.data?.runs?.[0];
  return (
    <Tile
      href="/ai-factory/benchmark"
      icon={<Gauge className="h-4 w-4" />}
      title="Benchmark"
      desc="Measure how fast your model answers as more users send requests at once."
      tool="AIPerf · time to first token, throughput"
      badge={<StatePill loading={runs.isLoading} state={last?.state} when={last?.finished_at ?? last?.started_at} />}
    />
  );
}

function ReliabilityTile() {
  const rel = useQuery({
    queryKey: ["aiFactory", "reliability"],
    queryFn: api.aiFactory.reliability,
    refetchInterval: 30_000,
  });
  const active = rel.data?.nvsentinel?.active;
  return (
    <Tile
      href="/ai-factory/reliability"
      icon={<Activity className="h-4 w-4" />}
      title="Reliability"
      desc="See whether GPU faults are fixed automatically and jobs can resume after one."
      tool="NVSentinel · cuda-checkpoint"
      badge={
        rel.data ? (
          <span
            className={cn(
              "text-[11px] rounded-full px-2 py-0.5 border whitespace-nowrap",
              active
                ? "border-accent-emerald/40 text-accent-emerald"
                : "border-line text-muted",
            )}
          >
            {active ? "Auto-fix on" : "Auto-fix off"}
          </span>
        ) : undefined
      }
    />
  );
}
