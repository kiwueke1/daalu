"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Factory,
  Thermometer,
  Gauge,
  MemoryStick,
  Zap,
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Cpu,
  Plus,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  api,
  type AiFactoryGpu,
  type AiFactoryHealth,
  type AiFactoryMetric,
  type AiFactoryRange,
  type AiFactoryRole,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatRelative } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { FactoryOps } from "./components/factory-ops";
import { LoadState } from "@/components/ui/load-state";
import { GpuOnboardingCard, GpuServingPanel } from "@/components/gpu/connect-gpu";

/**
 * /ai-factory — the customer-facing window into the GPU "factory" that
 * manufactures their tokens. This page surfaces the same observability +
 * diagnostics that ops sees in Grafana/dcgmi, but natively and
 * role-scoped, so customers never have to leave the hub.
 *
 * Role-aware (the backend computes the role server-side):
 *  · owner / provider — the GPU list + a factory-operations launcher. Clicking
 *    a card opens its detail view (metric cards, timeseries, XID/ECC events,
 *    firing alerts, per-card reliability) — all per-GPU data lives THERE, not
 *    on the overview.
 *  · provider — also a "Consumers of my GPU" placeholder.
 *  · consumer — a usage-centric panel (their tokens/requests/quota), NOT
 *    someone else's raw hardware health.
 *
 * Whole-floor tests (dcgmi/NCCL diagnostics, AIPerf benchmark, observability
 * self-check, reliability posture) are launched from FactoryOps tiles and open
 * on their own routes (/ai-factory/{diagnostics,benchmark,observability,
 * reliability}) — results/history never clutter the overview.
 */
export default function AiFactoryPage() {
  const { user } = useAuth();
  const isAdmin = user?.is_admin ?? false;
  // Site operator — gates the AIPerf benchmarking panel (a site-wide load test,
  // not a tenant feature). Distinct from isAdmin (tenant admin).
  const isSuperuser = user?.is_superuser ?? false;

  // Which GPU (DCGM gpu index) the user has drilled into; null = the list.
  const [selectedGpu, setSelectedGpu] = useState<string | null>(null);
  // "Add GPU": onboard a GPU node from a cluster already connected under
  // Managed infra, right here rather than on Usage & Pricing.
  const [addingGpu, setAddingGpu] = useState(false);

  const overview = useQuery({
    queryKey: ["aiFactory", "overview"],
    queryFn: api.aiFactory.overview,
    refetchInterval: 30_000,
  });

  const role = overview.data?.role ?? "none";
  const gpuClass = overview.data?.gpu_class ?? null;
  const metricsAvailable = overview.data?.metrics_available ?? false;
  const panels = overview.data?.panels ?? [];
  const showPanel = (id: string) => panels.length === 0 || panels.includes(id);

  const isHardware = role === "owner" || role === "provider";

  // Leave a card's detail view once the tenant no longer has a GPU (e.g.
  // right after Disconnect), so the page lands on the empty state.
  useEffect(() => {
    if (!isHardware && selectedGpu != null) setSelectedGpu(null);
  }, [isHardware, selectedGpu]);

  // Summary drives the health pill + the metric cards / consumer panel.
  const summary = useQuery({
    queryKey: ["aiFactory", "summary"],
    queryFn: api.aiFactory.gpuSummary,
    enabled: role !== "none" && metricsAvailable,
    refetchInterval: 15_000,
  });

  const gpus = summary.data?.gpus ?? [];
  const consumer = summary.data?.consumer ?? null;
  const worstHealth = worstOf(gpus);

  return (
    <div className="space-y-8 max-w-[1200px]">
      {/* ── Header ──────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <Factory className="h-5 w-5 text-accent-emerald" /> AI Factory
          </h1>
          <p className="text-muted text-sm mt-1 max-w-[640px]">
            Live observability for the GPUs that manufacture your tokens —
            utilisation, thermals, memory and health, plus diagnostics. No
            Grafana detour required.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {isAdmin && (
            <Button
              variant="primary"
              size="sm"
              onClick={() => setAddingGpu((v) => !v)}
              aria-expanded={addingGpu}
              icon={<Plus className="h-3.5 w-3.5" aria-hidden />}
            >
              Add GPU
            </Button>
          )}
          <RoleBadge role={role} />
          {gpuClass && (
            <span
              className="text-xs rounded-full px-3 py-1.5 border border-line text-muted inline-flex items-center gap-1.5"
              title="The GPU class this tenant runs on"
            >
              <Cpu className="h-3.5 w-3.5" aria-hidden /> GPU class{" "}
              <span className="font-mono text-fg">{gpuClass}</span>
            </span>
          )}
          {/* Only meaningful with at least one GPU: worstOf([]) is vacuously
              "ok", which rendered a green "Healthy" pill directly above "No
              GPUs are reporting metrics yet." */}
          {isHardware && metricsAvailable && gpus.length > 0 && (
            <HealthPill health={worstHealth} />
          )}
        </div>
      </div>

      {/* Stays open after Deploy & connect: the card then shows the
          Deploying… status and the Deploy progress panel for the boot. */}
      {addingGpu && isAdmin && <GpuOnboardingCard defaultOpen />}

      {/* The overview decides everything below; don't let "loading" or
          "failed" read as "no activity". */}
      {(overview.isLoading || overview.isError) && (
        <LoadState query={overview} loadingText="Loading your AI Factory…">
          {null}
        </LoadState>
      )}

      {/* ── role === "none" explainer ──────────────────────────────── */}
      {role === "none" && overview.isSuccess && (
        <section className="rounded-xl border border-line bg-bg-elevated/40 p-8 text-center">
          <Factory className="h-8 w-8 mx-auto text-muted" />
          <div className="text-sm font-medium mt-3">
            No AI Factory activity yet
          </div>
          <p className="text-xs text-muted mt-1.5 max-w-[480px] mx-auto">
            This tenant isn&apos;t running on a GPU and isn&apos;t consuming
            a shared pool yet. Add a GPU from a cluster you&apos;ve connected
            under Managed infra, or pick a plan that routes to a GPU pool,
            and the factory floor will light up here.
          </p>
          {isAdmin && !addingGpu && (
            <Button
              variant="primary"
              size="sm"
              className="mt-4"
              onClick={() => setAddingGpu(true)}
              icon={<Plus className="h-3.5 w-3.5" aria-hidden />}
            >
              Add GPU
            </Button>
          )}
        </section>
      )}

      {/* ── metrics not available ──────────────────────────────────── */}
      {role !== "none" && !metricsAvailable && overview.isSuccess && (
        <section className="rounded-xl border border-line bg-bg-elevated/40 p-8 text-center">
          <Activity className="h-8 w-8 mx-auto text-muted" />
          <div className="text-sm font-medium mt-3">
            Connect a GPU to see metrics
          </div>
          <p className="text-xs text-muted mt-1.5 max-w-[480px] mx-auto">
            We can&apos;t reach a metrics source for this tenant yet. Connect
            a GPU and we&apos;ll start streaming utilisation, thermals and
            health here.
          </p>
          {isAdmin && !addingGpu && (
            <Button
              variant="primary"
              size="sm"
              className="mt-4"
              onClick={() => setAddingGpu(true)}
              icon={<Plus className="h-3.5 w-3.5" aria-hidden />}
            >
              Add GPU
            </Button>
          )}
        </section>
      )}

      {/* ── Owner / Provider — hardware health ───────────────────────
          Overview = the GPU list + the factory-operations launcher only.
          Per-GPU observability (timeseries / events / alerts / per-card
          reliability) lives inside each card's detail view; whole-floor
          tests (diagnostics / benchmark / observability / reliability) open
          on their own pages from the tiles — results never clutter the
          overview, and one card's noise (e.g. GpuUnderUtilised) stays under
          that card. */}
      {isHardware && metricsAvailable && selectedGpu == null && (
        <>
          {showPanel("metrics") && (
            <GpuList
              gpus={gpus}
              query={summary}
              onSelect={setSelectedGpu}
            />
          )}
          <FactoryOps
            isAdmin={isAdmin}
            isSuperuser={isSuperuser}
            isHardware={isHardware}
          />
          {role === "provider" && showPanel("consumers") && <ConsumersNote />}
        </>
      )}
      {isHardware && metricsAvailable && selectedGpu != null && (
        <GpuDetail
          gpu={gpus.find((g) => g.id === selectedGpu) ?? null}
          gpuId={selectedGpu}
          onBack={() => setSelectedGpu(null)}
          canManage={isAdmin}
          showEvents={showPanel("events")}
          showAlerts={showPanel("alerts")}
        />
      )}

      {/* ── Consumer — usage-centric ───────────────────────────────── */}
      {role === "consumer" && metricsAvailable && (
        <ConsumerPanel consumer={consumer} loading={summary.isLoading} />
      )}

      {/* ── Superuser without an owned card — still gets the factory-ops
          launcher (e.g. benchmarking the operator's shared serving stack). */}
      {isSuperuser && !isHardware && role !== "none" && selectedGpu == null && (
        <FactoryOps isAdmin={isAdmin} isSuperuser={isSuperuser} isHardware={false} />
      )}
    </div>
  );
}

// ── Role badge ──────────────────────────────────────────────────────────

const ROLE_LABEL: Record<AiFactoryRole, string> = {
  owner: "Owner",
  provider: "Provider",
  consumer: "Consumer",
  none: "—",
};

function RoleBadge({ role }: { role: AiFactoryRole }) {
  if (role === "none") return null;
  return (
    <span
      className="text-xs text-muted inline-flex items-center gap-1.5"
      title="Your role for this AI Factory"
    >
      <Factory className="h-3.5 w-3.5" aria-hidden /> Your role:{" "}
      <span className="font-medium text-fg">{ROLE_LABEL[role]}</span>
    </span>
  );
}

// ── Live health pill ────────────────────────────────────────────────────

function HealthPill({ health }: { health: AiFactoryHealth }) {
  const map: Record<AiFactoryHealth, { label: string; cls: string }> = {
    ok: {
      label: "Healthy",
      cls: "border-accent-emerald/40 bg-accent-emerald/10 text-[color:var(--text)]",
    },
    warn: {
      label: "Degraded",
      cls: "border-signal-warning/40 bg-signal-warning/10 text-[color:var(--warning)]",
    },
    crit: {
      label: "Critical",
      cls: "border-signal-critical/40 bg-signal-critical/10 text-[color:var(--critical)]",
    },
  };
  const m = map[health];
  return (
    <span
      className={cn(
        "text-xs rounded-full px-3 py-1.5 border inline-flex items-center gap-1.5",
        m.cls
      )}
    >
      <span
        className="h-2 w-2 rounded-full"
        style={{
          background:
            health === "ok"
              ? "var(--accent)"
              : health === "warn"
                ? "var(--warning)"
                : "var(--critical)",
          boxShadow: "0 0 8px currentColor",
        }}
      />
      {m.label}
    </span>
  );
}

// Callers must not render this for an empty list — "worst of nothing" is
// vacuously "ok", which reads as a green health claim about GPUs that aren't
// reporting. Guard on `gpus.length > 0` at the call site.
function worstOf(gpus: AiFactoryGpu[]): AiFactoryHealth {
  if (gpus.some((g) => g.health === "crit")) return "crit";
  if (gpus.some((g) => g.health === "warn")) return "warn";
  return "ok";
}

// ── Metric cards ────────────────────────────────────────────────────────

function MetricCards({ gpus }: { gpus: AiFactoryGpu[] }) {
  if (gpus.length === 0) {
    return (
      <section className="rounded-xl border border-line p-6 text-muted text-sm">
        Loading GPU summary…
      </section>
    );
  }
  // Aggregate across all of the tenant's GPUs — average for rates,
  // sum for absolute capacity.
  const n = gpus.length;
  const avg = (sel: (g: AiFactoryGpu) => number) =>
    gpus.reduce((a, g) => a + sel(g), 0) / n;
  const sum = (sel: (g: AiFactoryGpu) => number) =>
    gpus.reduce((a, g) => a + sel(g), 0);

  const memUsed = sum((g) => g.mem_used_gb);
  const memTotal = sum((g) => g.mem_total_gb);

  return (
    <section className="space-y-3">
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
        <MetricCard
          icon={<Thermometer className="h-4 w-4" />}
          label="Temperature"
          value={`${avg((g) => g.temp_c).toFixed(0)}°C`}
          sub={n > 1 ? `avg of ${n}` : undefined}
        />
        <MetricCard
          icon={<Gauge className="h-4 w-4" />}
          label="Utilisation"
          value={`${avg((g) => g.util_pct).toFixed(0)}%`}
          sub={n > 1 ? `avg of ${n}` : undefined}
        />
        <MetricCard
          icon={<MemoryStick className="h-4 w-4" />}
          label="VRAM"
          value={`${memUsed.toFixed(0)} / ${memTotal.toFixed(0)} GB`}
          sub={`${memTotal ? ((memUsed / memTotal) * 100).toFixed(0) : 0}%`}
        />
        <MetricCard
          icon={<Zap className="h-4 w-4" />}
          label="Power"
          value={`${sum((g) => g.power_w).toFixed(0)} W`}
          sub={n > 1 ? `${n} GPUs` : undefined}
        />
        <MetricCard
          icon={<Activity className="h-4 w-4" />}
          label="SM-active"
          value={`${avg((g) => g.sm_active_pct).toFixed(0)}%`}
          sub={n > 1 ? `avg of ${n}` : undefined}
        />
      </div>

      {/* Per-GPU breakdown when more than one card is present. */}
      {n > 1 && (
        <div className="rounded-xl border border-line bg-bg-elevated/40 overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-muted">
              <tr className="border-b border-line">
                <th className="text-left font-medium px-4 py-2.5">GPU</th>
                <th className="text-left font-medium px-4 py-2.5">Host</th>
                <th className="text-right font-medium px-4 py-2.5">Temp</th>
                <th className="text-right font-medium px-4 py-2.5">Util</th>
                <th className="text-right font-medium px-4 py-2.5">VRAM</th>
                <th className="text-right font-medium px-4 py-2.5">Power</th>
                <th className="text-center font-medium px-4 py-2.5">Health</th>
              </tr>
            </thead>
            <tbody>
              {gpus.map((g) => (
                <tr key={`${g.hostname}-${g.gpu}`} className="border-b border-line/50 last:border-0">
                  <td className="px-4 py-2.5 font-mono">{g.gpu}</td>
                  <td className="px-4 py-2.5 font-mono text-muted">{g.hostname}</td>
                  <td className="px-4 py-2.5 text-right">{g.temp_c.toFixed(0)}°C</td>
                  <td className="px-4 py-2.5 text-right">{g.util_pct.toFixed(0)}%</td>
                  <td className="px-4 py-2.5 text-right">
                    {g.mem_used_gb.toFixed(0)}/{g.mem_total_gb.toFixed(0)}
                  </td>
                  <td className="px-4 py-2.5 text-right">{g.power_w.toFixed(0)}W</td>
                  <td className="px-4 py-2.5 text-center">
                    <HealthDot health={g.health} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ── GPU list (one selectable card per GPU) ──────────────────────────────

/** True once `active` has been true for `ms` without turning false. */
function useSlow(active: boolean, ms = 10_000): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) {
      setSlow(false);
      return;
    }
    const t = window.setTimeout(() => setSlow(true), ms);
    return () => window.clearTimeout(t);
  }, [active, ms]);
  return slow;
}

function GpuList({
  gpus,
  query,
  onSelect,
}: {
  gpus: AiFactoryGpu[];
  query: {
    isLoading: boolean;
    isError: boolean;
    error?: unknown;
    refetch: () => unknown;
  };
  onSelect: (gpu: string) => void;
}) {
  const loading = query.isLoading && gpus.length === 0;
  const slow = useSlow(loading);

  if (query.isError && gpus.length === 0) {
    return <LoadState query={query}>{null}</LoadState>;
  }
  if (loading) {
    return (
      <section className="space-y-3" aria-busy="true">
        <div className="text-sm font-medium flex items-center gap-2">
          Your GPUs
          <span className="text-xs text-muted font-normal">loading metrics…</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="rounded-xl border border-line bg-bg-elevated/40 p-4 space-y-3 animate-pulse"
            >
              <div className="h-4 w-1/3 rounded bg-bg-elevated" />
              <div className="h-3 w-2/3 rounded bg-bg-elevated" />
              <div className="h-8 w-full rounded bg-bg-elevated" />
            </div>
          ))}
        </div>
        {slow && (
          <div className="rounded-xl border border-line p-4 text-sm flex flex-wrap items-center gap-3">
            <span className="flex-1 min-w-[200px] text-muted">
              GPU metrics are slow to respond. The metrics source may be busy
              or unreachable.
            </span>
            <button
              type="button"
              onClick={() => query.refetch()}
              className="h-8 px-3 rounded-lg border border-line text-xs"
            >
              Retry
            </button>
            <Link
              href="/ai-factory/observability"
              className="h-8 px-3 rounded-lg border border-line text-xs inline-flex items-center"
            >
              Check observability
            </Link>
          </div>
        )}
      </section>
    );
  }
  if (gpus.length === 0) {
    return (
      <section className="rounded-xl border border-line p-6 text-sm space-y-2">
        <div className="text-muted">No GPUs are reporting metrics yet.</div>
        {/* A deployed, serving GPU still shows nothing here if its DCGM
            metrics aren't in the Prometheus this hub queries — the two are
            independent, and "no metrics" was being read as "no GPU". */}
        <details className="text-[11px] text-muted">
          <summary className="cursor-pointer hover:text-fg">
            My GPU is connected. Why isn&apos;t it here?
          </summary>
          <p className="mt-1.5">
            This page reads GPU metrics (DCGM) from your Prometheus, which is
            separate from whether a GPU is deployed and serving. If you have
            connected one on <span className="font-mono">Usage &amp; Pricing</span>,
            check that your Prometheus scrapes its{" "}
            <span className="font-mono">dcgm-exporter</span>.
          </p>
        </details>
      </section>
    );
  }
  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">
          Your GPUs{" "}
          <span className="text-muted font-normal">({gpus.length})</span>
        </h3>
        <span className="text-[11px] text-muted">Select a GPU for details</span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {gpus.map((g) => (
          <button
            key={g.id}
            type="button"
            onClick={() => onSelect(g.id)}
            className="text-left rounded-xl border border-line bg-bg-elevated/40 p-4 hover:border-accent-emerald/40 transition-colors group"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="text-sm font-medium truncate flex items-center gap-1.5">
                  <Cpu className="h-4 w-4 text-accent-emerald shrink-0" />
                  {g.model || `GPU ${g.gpu}`}
                </div>
                <div className="text-[11px] text-muted font-mono mt-0.5 truncate">
                  {g.hostname || "—"} · gpu {g.gpu}
                </div>
              </div>
              <HealthDot health={g.health} />
            </div>
            <div className="grid grid-cols-3 gap-2 mt-3 text-xs">
              <MiniStat label="Util" value={`${g.util_pct.toFixed(0)}%`} />
              <MiniStat label="Temp" value={`${g.temp_c.toFixed(0)}°C`} />
              <MiniStat
                label="VRAM"
                value={`${g.mem_pct.toFixed(0)}%`}
              />
            </div>
            <div className="mt-3 text-[11px] text-accent-emerald opacity-0 group-hover:opacity-100 transition-opacity inline-flex items-center gap-1">
              View details <ArrowUpRight className="h-3 w-3" />
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-bg-base/40 px-2 py-1.5">
      <div className="text-[11px] uppercase tracking-wide text-muted">
        {label}
      </div>
      <div className="text-sm font-medium mt-0.5">{value}</div>
    </div>
  );
}

// ── GPU detail (observability for a single card) ────────────────────────

function GpuDetail({
  gpu,
  gpuId,
  onBack,
  canManage,
  showEvents,
  showAlerts,
}: {
  gpu: AiFactoryGpu | null;
  gpuId: string;
  onBack: () => void;
  /** Tenant admin: may disconnect the model served from this card. */
  canManage: boolean;
  showEvents: boolean;
  showAlerts: boolean;
}) {
  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <button
          type="button"
          onClick={onBack}
          className="text-xs h-8 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
        >
          ← All GPUs
        </button>
        <div className="flex items-center gap-3">
          <div className="text-sm font-medium flex items-center gap-2">
            <Cpu className="h-4 w-4 text-accent-emerald" />
            {gpu?.model || `GPU ${gpuId}`}
            {gpu && <HealthDot health={gpu.health} />}
          </div>
        </div>
      </div>
      {gpu?.uuid && (
        <div className="text-[11px] text-muted font-mono">
          {gpu.hostname} · gpu {gpu.gpu} · {gpu.uuid}
        </div>
      )}
      {gpu && <GpuServingPanel hostname={gpu.hostname} canManage={canManage} />}
      {gpu && <MetricCards gpus={[gpu]} />}
      {gpu && gpu.mem_pct >= 40 && gpu.util_pct < 5 && (
        <div className="flex items-start gap-2 rounded-lg border border-line bg-bg-elevated/30 px-3.5 py-2.5 text-[11px] text-muted">
          <Cpu className="h-3.5 w-3.5 mt-0.5 shrink-0 text-accent-emerald" />
          <span>
            High VRAM with ~0% utilisation is expected here — not a fault. The
            vLLM server reserves its share of VRAM up front (model weights +
            KV-cache pool; the share is under Serving on this GPU) and holds
            it whether or not requests are flowing.{" "}
            <strong className="text-[color:var(--text)] font-medium">
              Utilisation
            </strong>{" "}
            tracks live inference work, so it sits at 0% while the model is
            loaded but idle and rises as traffic hits the card.
          </span>
        </div>
      )}
      {gpu && <CardReliabilityStrip gpu={gpu} />}
      <TimeseriesPanel card={gpuId} />
      {showEvents && <EventsTable card={gpuId} />}
      {showAlerts && <AlertsList card={gpuId} />}
    </section>
  );
}

// ── Per-card reliability (DCGM health signals for ONE card) ─────────────────
// The factory-wide NVSentinel / cuda-checkpoint posture lives on
// /ai-factory/reliability; this strip is the single card's own XID/ECC/thermal
// health, shown inside its detail view.
function CardReliabilityStrip({ gpu }: { gpu: AiFactoryGpu }) {
  const items: { label: string; value: string; crit: boolean }[] = [
    { label: "Driver errors (XID)", value: String(gpu.xid_errors), crit: gpu.xid_errors > 0 },
    {
      label: "Memory errors (ECC)",
      value: String(gpu.ecc_dbe),
      crit: gpu.ecc_dbe > 0,
    },
    {
      label: "Temperature",
      value: `${gpu.temp_c.toFixed(0)}°C`,
      crit: gpu.temp_c >= 90,
    },
  ];
  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <h3 className="text-sm font-medium mb-3 flex items-center gap-1.5">
        <Activity className="h-4 w-4 text-accent-emerald" aria-hidden /> Reliability
        <span className="text-muted font-normal text-xs">
          · this GPU&apos;s driver errors, memory errors and overheating
        </span>
      </h3>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {items.map((it) => (
          <div
            key={it.label}
            className="rounded-lg bg-bg-base/40 px-3 py-2.5 flex items-center justify-between"
          >
            <span className="text-[11px] text-muted">{it.label}</span>
            <span
              className={cn(
                "text-sm font-medium",
                it.crit ? "text-[color:var(--critical)]" : undefined,
              )}
            >
              {it.value}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

function MetricCard({
  icon,
  label,
  value,
  sub,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="rounded-xl border border-line bg-bg-elevated/40 p-4">
      <div className="flex items-center gap-1.5 text-muted text-xs uppercase tracking-[0.12em]">
        {icon}
        {label}
      </div>
      <div className="text-xl font-semibold mt-2">{value}</div>
      {sub && <div className="text-[11px] text-muted mt-0.5">{sub}</div>}
    </div>
  );
}

function HealthDot({ health }: { health: AiFactoryHealth }) {
  const color =
    health === "ok"
      ? "var(--accent)"
      : health === "warn"
        ? "var(--warning)"
        : "var(--critical)";
  const word = health === "ok" ? "Healthy" : health === "warn" ? "Degraded" : "Critical";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted whitespace-nowrap">
      <span
        aria-hidden
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: color, boxShadow: `0 0 8px ${color}` }}
      />
      {word}
    </span>
  );
}

// ── Timeseries panel ────────────────────────────────────────────────────

const METRICS: { key: AiFactoryMetric; label: string }[] = [
  { key: "util", label: "Utilisation" },
  { key: "temp", label: "Temp" },
  { key: "mem", label: "Memory" },
  { key: "power", label: "Power" },
];
const RANGES: AiFactoryRange[] = ["1h", "6h", "24h", "7d"];
const METRIC_UNIT: Record<AiFactoryMetric, string> = {
  util: "%",
  temp: "°C",
  mem: "%",
  power: "W",
};

function TimeseriesPanel({ card }: { card?: string }) {
  const [metric, setMetric] = useState<AiFactoryMetric>("util");
  const [range, setRange] = useState<AiFactoryRange>("6h");

  const ts = useQuery({
    queryKey: ["aiFactory", "timeseries", metric, range, card ?? "all"],
    queryFn: () => api.aiFactory.timeseries(metric, range, card),
    refetchInterval: 30_000,
  });

  const data = (ts.data?.series ?? []).map((p) => ({
    t: new Date(p.ts * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    }),
    value: p.value,
  }));
  const unit = METRIC_UNIT[metric];
  // Percentage metrics get a fixed 0–100 axis so an idle (all-zero) GPU
  // reads as a flat line at the bottom of a full scale, not a misleading
  // auto-zoomed 0–4. Temp/power keep an auto domain.
  const yDomain: [number | string, number | string] =
    unit === "%" ? [0, 100] : ["auto", "auto"];
  const allZero = data.length > 0 && data.every((d) => d.value === 0);

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h3 className="text-sm font-medium">
          Trend
          <span className="text-muted font-normal ml-1.5 text-xs">
            {card != null ? "this GPU" : "all GPUs"} ·{" "}
            {METRICS.find((m) => m.key === metric)?.label}
          </span>
        </h3>
        <div className="flex flex-wrap items-center gap-2">
          <Switcher
            options={METRICS.map((m) => ({ key: m.key, label: m.label }))}
            value={metric}
            onChange={setMetric}
          />
          <Switcher
            options={RANGES.map((r) => ({ key: r, label: r }))}
            value={range}
            onChange={setRange}
          />
        </div>
      </div>
      <div className="h-56 mt-3">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data}>
            <defs>
              <linearGradient id="aiFactoryFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.4} />
                <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="var(--line)" vertical={false} />
            <XAxis dataKey="t" stroke="var(--text-muted)" fontSize={11} />
            <YAxis
              stroke="var(--text-muted)"
              fontSize={11}
              domain={yDomain}
              width={38}
              tickFormatter={(v: number) => `${v}${unit}`}
            />
            <Tooltip
              contentStyle={{
                background: "var(--bg-card)",
                border: "1px solid var(--line)",
                color: "var(--text)",
                fontSize: 12,
              }}
              formatter={(v: number) => `${v.toFixed(1)}${unit}`}
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke="var(--accent)"
              strokeWidth={1.5}
              fill="url(#aiFactoryFill)"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      {allZero && (metric === "util" || metric === "mem") && (
        <div className="text-[11px] text-muted mt-1">
          Flat at 0 — the GPU has been idle over this window (no inference
          traffic). It&apos;ll rise here as requests hit the card.
        </div>
      )}
    </section>
  );
}

function Switcher<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { key: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex rounded-lg border border-line overflow-hidden">
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          onClick={() => onChange(o.key)}
          className={cn(
            "text-[11px] px-2.5 py-1 transition-colors",
            value === o.key
              ? "bg-accent-emerald/15 text-[color:var(--text)]"
              : "text-muted hover:text-[color:var(--text)]"
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ── XID / ECC events table ──────────────────────────────────────────────

const EVENT_LABEL: Record<string, string> = {
  xid: "XID",
  ecc_dbe: "ECC double-bit",
  ecc_sbe: "ECC single-bit",
};

function EventsTable({ card }: { card?: string }) {
  const ev = useQuery({
    queryKey: ["aiFactory", "events", card ?? "all"],
    queryFn: () => api.aiFactory.events(card),
    refetchInterval: 30_000,
  });
  const events = ev.data?.events ?? [];

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <h3 className="text-sm font-medium mb-3 flex items-center gap-1.5">
        <AlertTriangle className="h-4 w-4 text-[color:var(--warning)]" aria-hidden /> Hardware
        error events
        <span className="text-muted font-normal text-xs">
          · driver (XID) and memory (ECC) errors
        </span>
      </h3>
      {events.length === 0 ? (
        <div className="text-xs text-muted">
          No hardware error events recorded. That&apos;s a good sign.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-xs">
            <thead className="text-muted">
              <tr className="border-b border-line">
                <th className="text-left font-medium px-3 py-2">When</th>
                <th className="text-left font-medium px-3 py-2">GPU</th>
                <th className="text-left font-medium px-3 py-2">Kind</th>
                <th className="text-left font-medium px-3 py-2">Detail</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e, i) => (
                <tr key={i} className="border-b border-line/50 last:border-0">
                  <td
                    className="px-3 py-2 text-muted whitespace-nowrap"
                    title={new Date(e.ts).toLocaleString()}
                  >
                    {formatRelative(e.ts)}
                  </td>
                  <td className="px-3 py-2 font-mono">{e.gpu}</td>
                  <td className="px-3 py-2">
                    <span
                      className={cn(
                        "inline-flex rounded px-1.5 py-0.5 border text-[11px]",
                        e.kind === "ecc_sbe"
                          ? "border-signal-warning/40 text-[color:var(--warning)]"
                          : "border-signal-critical/40 text-[color:var(--critical)]"
                      )}
                    >
                      {EVENT_LABEL[e.kind] ?? e.kind}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-muted">{e.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ── Firing alerts list ──────────────────────────────────────────────────

function AlertsList({ card }: { card?: string }) {
  const al = useQuery({
    queryKey: ["aiFactory", "alerts", card ?? "all"],
    queryFn: () => api.aiFactory.alerts(card),
    refetchInterval: 15_000,
  });
  const alerts = al.data?.alerts ?? [];

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <h3 className="text-sm font-medium mb-3">
        Alerts{" "}
        <span className="text-muted font-normal">
          {card != null ? "· this GPU" : "· all GPUs"}
        </span>
      </h3>
      {alerts.length === 0 ? (
        <div className="text-xs text-muted">No firing or pending alerts.</div>
      ) : (
        <div className="space-y-2">
          {alerts.map((a, i) => {
            const tone =
              a.severity === "critical"
                ? "border-signal-critical/40 text-[color:var(--critical)]"
                : a.severity === "warning"
                  ? "border-signal-warning/40 text-[color:var(--warning)]"
                  : "border-line text-muted";
            return (
              <div
                key={i}
                className="flex items-start gap-3 rounded-lg border border-line bg-bg-base/40 px-3 py-2.5"
              >
                <span
                  className={cn(
                    "mt-0.5 inline-flex rounded px-1.5 py-0.5 border text-[11px] uppercase tracking-wide shrink-0",
                    tone
                  )}
                >
                  {a.state === "pending" ? "Pending" : a.severity}
                </span>
                <div className="min-w-0">
                  <div className="text-xs font-medium">{a.name}</div>
                  <div className="text-[11px] text-muted">{a.summary}</div>
                </div>
                <div
                  className="ml-auto text-xs text-muted whitespace-nowrap"
                  title={new Date(a.since).toLocaleString()}
                >
                  {timeAgo(a.since)}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ── Provider — "Consumers of my GPU" placeholder ────────────────────────

function ConsumersNote() {
  // TODO: wire to a real per-consumer breakdown endpoint once the
  // backend exposes it. For now this is an intentional placeholder
  // (the contract only provides alerts/summary today).
  return (
    <section className="rounded-xl border border-dashed border-line bg-bg-elevated/30 p-5">
      <h3 className="text-sm font-medium flex items-center gap-1.5">
        <Cpu className="h-4 w-4 text-accent-emerald" /> Consumers of my GPU
      </h3>
      <p className="text-xs text-muted mt-1.5 max-w-[560px]">
        You&apos;re providing GPU capacity to a shared pool. A per-consumer
        breakdown (tokens served, requests, who&apos;s on your card) is
        coming soon — for now, watch the alerts above for pool-level
        pressure.
      </p>
    </section>
  );
}

// ── Consumer usage panel ────────────────────────────────────────────────

function ConsumerPanel({
  consumer,
  loading,
}: {
  consumer: {
    tokens_prompt: number;
    tokens_completion: number;
    requests: number;
    quota_used: number;
    quota_limit: number;
    avg_latency_ms: number;
    pool_util_pct: number | null;
  } | null;
  loading: boolean;
}) {
  if (loading || !consumer) {
    return (
      <section className="rounded-xl border border-line p-6 text-muted text-sm">
        Loading your usage…
      </section>
    );
  }
  const quotaPct = consumer.quota_limit
    ? Math.min(100, (consumer.quota_used / consumer.quota_limit) * 100)
    : 0;
  return (
    <section className="space-y-3">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard
          icon={<ArrowUpRight className="h-4 w-4" />}
          label="Tokens in"
          value={`${(consumer.tokens_prompt / 1000).toFixed(1)}k`}
        />
        <MetricCard
          icon={<ArrowUpRight className="h-4 w-4 rotate-90" />}
          label="Tokens out"
          value={`${(consumer.tokens_completion / 1000).toFixed(1)}k`}
        />
        <MetricCard
          icon={<Activity className="h-4 w-4" />}
          label="Requests"
          value={consumer.requests.toLocaleString()}
        />
        <MetricCard
          icon={<Gauge className="h-4 w-4" />}
          label="Avg latency"
          value={`${consumer.avg_latency_ms.toFixed(0)} ms`}
        />
      </div>

      <div className="rounded-xl border border-line bg-bg-elevated/40 p-5">
        <div className="flex justify-between text-xs text-muted mb-1.5">
          <span>
            Quota: {consumer.quota_used.toLocaleString()} /{" "}
            {consumer.quota_limit.toLocaleString()}
          </span>
          <span>{quotaPct.toFixed(0)}%</span>
        </div>
        <div className="h-1.5 rounded-full bg-bg-elevated overflow-hidden">
          <div
            className="h-full rounded-full"
            style={{
              width: `${quotaPct}%`,
              background:
                quotaPct >= 90 ? "var(--critical)" : "var(--accent)",
              boxShadow: "0 0 10px var(--accent-glow)",
            }}
          />
        </div>
        {consumer.pool_util_pct != null && (
          <div className="mt-4 text-xs text-muted">
            Shared pool utilisation:{" "}
            <span className="text-[color:var(--text)]">
              {consumer.pool_util_pct.toFixed(0)}%
            </span>
          </div>
        )}
      </div>
    </section>
  );
}

// ── helpers ─────────────────────────────────────────────────────────────

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
