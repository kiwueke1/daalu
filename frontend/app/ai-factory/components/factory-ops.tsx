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
import { cn } from "@/lib/utils";

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
  badge,
  onRun,
  running,
  runLabel = "Run",
}: {
  href: string;
  icon: React.ReactNode;
  title: string;
  desc: string;
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
      <p className="text-[11px] text-muted mt-1.5 flex-1">{desc}</p>
      <div className="mt-3 flex items-center justify-between">
        <Link
          href={href}
          className="text-[11px] text-accent-emerald inline-flex items-center gap-1 hover:underline"
        >
          Open <ArrowUpRight className="h-3 w-3" />
        </Link>
        {onRun && (
          <button
            type="button"
            onClick={onRun}
            disabled={running}
            className="text-[11px] h-7 px-2.5 rounded-lg border border-line text-muted hover:text-[color:var(--text)] hover:border-accent-emerald/40 inline-flex items-center gap-1 disabled:opacity-50"
          >
            <Play className="h-3 w-3" /> {running ? "Starting…" : runLabel}
          </button>
        )}
      </div>
    </div>
  );
}

// ── last-run pill (state only — never the output) ─────────────────────────

function StatePill({ state, when }: { state?: string; when?: string | null }) {
  if (!state) return <span className="text-[10px] text-muted">no runs yet</span>;
  const tone =
    state === "passed"
      ? "border-accent-emerald/40 text-accent-emerald"
      : state === "failed" || state === "error"
        ? "border-signal-critical/40 text-[color:var(--critical)]"
        : "border-signal-warning/40 text-[color:var(--warning)]";
  return (
    <span
      className={cn(
        "text-[10px] rounded-full px-2 py-0.5 border inline-flex items-center gap-1 whitespace-nowrap",
        tone,
      )}
      title={when ? new Date(when).toLocaleString() : undefined}
    >
      {state}
      {when && <span className="text-muted">· {timeAgo(when)}</span>}
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
  const run = useMutation({
    mutationFn: () =>
      api.aiFactory.runDiagnostic({ kind: "dcgmi_diag", level: 1 }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["aiFactory", "diagnostics"] });
      router.push("/ai-factory/diagnostics");
    },
  });
  return (
    <Tile
      href="/ai-factory/diagnostics"
      icon={<Stethoscope className="h-4 w-4" />}
      title="Diagnostics"
      desc="dcgmi diag & NCCL — on-demand GPU health and interconnect checks."
      badge={<StatePill state={last?.state} when={last?.finished_at ?? last?.started_at} />}
      onRun={() => run.mutate()}
      running={run.isPending}
      runLabel="Quick dcgmi"
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
  const run = useMutation({
    mutationFn: () => api.aiFactory.validateObservability(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["aiFactory", "diagnostics"] });
      router.push("/ai-factory/observability");
    },
  });
  return (
    <Tile
      href="/ai-factory/observability"
      icon={<ShieldCheck className="h-4 w-4" />}
      title="Observability check"
      desc="End-to-end self-check of the metrics pipeline (exporters, scrape, queryability)."
      badge={<StatePill state={last?.state} when={last?.finished_at ?? last?.started_at} />}
      onRun={() => run.mutate()}
      running={run.isPending}
      runLabel="Validate"
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
      desc="AIPerf concurrency sweep — TTFT, inter-token latency, throughput. Configure the sweep on its page."
      badge={<StatePill state={last?.state} when={last?.finished_at ?? last?.started_at} />}
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
      desc="NVSentinel auto-remediation posture & cuda-checkpoint status across the factory."
      badge={
        rel.data ? (
          <span
            className={cn(
              "text-[10px] rounded-full px-2 py-0.5 border whitespace-nowrap",
              active
                ? "border-accent-emerald/40 text-accent-emerald"
                : "border-line text-muted",
            )}
          >
            NVSentinel {active ? "active" : "not active"}
          </span>
        ) : undefined
      }
    />
  );
}

// ── helpers ─────────────────────────────────────────────────────────────

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
