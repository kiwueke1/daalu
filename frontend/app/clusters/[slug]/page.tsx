"use client";

/**
 * Per-cluster detail page (/clusters/[slug]).
 *
 * Two surfaces against a customer cluster reached over the WireGuard mesh:
 *  - Overview: server version, node inventory, namespace count.
 *  - kubectl console: tick one or more read-only commands from the
 *    server's allowlist, choose an output format (json / yaml / cli
 *    table), and run them through the tunnel.
 *
 * This is the first instance of a pattern we intend to reuse for other
 * managed infra (Prometheus, logs, cloud): a detail page + an
 * allowlisted, format-selectable command runner. See the engineer book
 * chapter "The managed-infra console pattern".
 */

import { Suspense, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Boxes,
  ChevronRight,
  Copy,
  Cpu,
  Database,
  Gauge,
  HardDrive,
  Layers,
  LayoutDashboard,
  LineChart,
  Play,
  RefreshCw,
  ScrollText,
  Server,
  Terminal,
} from "lucide-react";
import {
  api,
  type ClusterHealth,
  type ClusterStatus,
  type KubectlCommandSpec,
  type KubectlOutput,
  type KubectlResult,
  type SymptomaticPod,
} from "@/lib/api";
import { HUB } from "@/lib/hub-routes";
import { TabBar, useUrlTab } from "@/components/ui/tabs";
import { LoadState } from "@/components/ui/load-state";

const STATUS_BADGE: Record<ClusterStatus, { dot: string; label: string }> = {
  pending: { dot: "bg-muted/60", label: "pending" },
  awaiting_handshake: { dot: "bg-accent-amber", label: "awaiting handshake" },
  connected: { dot: "bg-accent-emerald", label: "connected" },
  degraded: { dot: "bg-accent-amber", label: "degraded" },
  error: { dot: "bg-signal-critical", label: "error" },
};

const OUTPUT_OPTIONS: { id: KubectlOutput; label: string }[] = [
  { id: "cli", label: "CLI table" },
  { id: "json", label: "JSON" },
  { id: "yaml", label: "YAML" },
];

function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

function nodeAge(iso: string | null): string {
  if (!iso) return "—";
  const days = Math.floor(
    (Date.now() - new Date(iso).getTime()) / 86_400_000
  );
  if (days >= 1) return `${days}d`;
  const hours = Math.floor((Date.now() - new Date(iso).getTime()) / 3_600_000);
  return `${hours}h`;
}

type DetailTab = "overview" | "kubectl" | "dashboards";
const DETAIL_TABS = [
  { id: "overview" as const, label: "Overview", icon: Gauge },
  { id: "kubectl" as const, label: "Run kubectl", icon: Terminal },
  { id: "dashboards" as const, label: "Dashboards", icon: LayoutDashboard },
];
const DETAIL_TAB_IDS = DETAIL_TABS.map((t) => t.id);

export default function ClusterDetailPage() {
  // useSearchParams (the ?tab= state) needs a Suspense boundary.
  return (
    <Suspense
      fallback={<div className="p-6 text-sm text-muted">Loading cluster…</div>}
    >
      <ClusterDetail />
    </Suspense>
  );
}

function ClusterDetail() {
  const params = useParams<{ slug: string }>();
  const slug = params.slug;
  const [tab, setTab] = useUrlTab<DetailTab>(DETAIL_TAB_IDS, "overview");

  const { data: cluster, isLoading: clusterLoading } = useQuery({
    queryKey: ["cluster", slug],
    queryFn: () => api.clusters.get(slug),
    enabled: !!slug,
    refetchInterval: 30_000,
  });

  const overviewQuery = useQuery({
    queryKey: ["cluster-overview", slug],
    queryFn: () => api.clusters.overview(slug),
    enabled: !!slug,
    refetchInterval: 60_000,
  });
  const overview = overviewQuery.data;

  if (clusterLoading) {
    return <div className="p-6 text-sm text-muted">Loading cluster…</div>;
  }
  if (!cluster) {
    return (
      <div className="p-6 text-sm text-muted">
        Cluster not found.{" "}
        <Link href={HUB.clusters} className="underline">
          Back to clusters
        </Link>
      </div>
    );
  }

  const badge = STATUS_BADGE[cluster.status];

  return (
    <div className="space-y-6 max-w-[1100px]">
      <div>
        <Link
          href={HUB.clusters}
          className="text-xs text-muted hover:text-fg inline-flex items-center gap-1.5"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Clusters
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          <h1 className="text-2xl font-semibold flex items-start gap-2 break-all">
            <Boxes className="h-5 w-5 mt-1.5 shrink-0 text-accent-cyan" />
            {cluster.name}
          </h1>
          <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted">
            <span className={`h-2 w-2 rounded-full ${badge.dot}`} />
            {badge.label}
          </span>
        </div>
        <div className="text-xs text-muted mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
          <span>
            slug <span className="font-mono">{cluster.slug}</span>
          </span>
          <span>
            tunnel IP <span className="font-mono">{cluster.tunnel_ip}</span>
          </span>
          <span>last handshake {timeAgo(cluster.last_handshake_at)}</span>
        </div>
      </div>

      <TabBar
        tabs={DETAIL_TABS}
        active={tab}
        onChange={setTab}
        ariaLabel="Cluster sections"
      />

      {tab === "overview" && (
        <section className="space-y-3">
          <LoadState
            query={overviewQuery}
            loadingText="Reading the cluster over the tunnel…"
          >
            {overview && !overview.reachable && (
              <div className="rounded-2xl border border-signal-warning/40 bg-signal-warning/5 p-4 text-sm">
                <div className="font-medium text-accent-amber">
                  Cluster not reachable
                </div>
                <p className="text-xs text-muted mt-1">
                  {overview.error ||
                    "No Kubernetes credentials are linked to this cluster yet. Add a Kubernetes integration whose kubeconfig points at the tunnel IP, then reload."}
                </p>
              </div>
            )}
            {overview && overview.reachable && (
              <>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <StatCard
                    icon={Server}
                    label="Server version"
                    value={overview.server_version || "—"}
                  />
                  <StatCard
                    icon={Cpu}
                    label="Nodes"
                    value={String(overview.node_count)}
                  />
                  <StatCard
                    icon={Layers}
                    label="Namespaces"
                    value={String(overview.namespace_count)}
                  />
                </div>
                {overview.health && (
                  <ClusterHealthSection health={overview.health} />
                )}
                <NodesTable nodes={overview.nodes} />
              </>
            )}
          </LoadState>
        </section>
      )}

      {tab === "kubectl" && (
        <KubectlConsole
          slug={slug}
          // undefined while the overview is still loading: don't claim
          // "not reachable" before we know.
          reachable={overview ? overview.reachable : undefined}
        />
      )}

      {tab === "dashboards" && <RelatedDashboards slug={slug} />}
    </div>
  );
}

// ── Cluster health (Datadog-style overview) ───────────────────────────
//
// Mirrors the "Troubleshooting Patterns" + resource counts Datadog surfaces
// on its Kubernetes overview: pod phase breakdown, container restarts, node
// readiness, unbound volumes, and workloads with unavailable replicas — all
// read from the live cluster over the tunnel.

function ClusterHealthSection({ health }: { health: ClusterHealth }) {
  const trouble = [
    {
      icon: AlertTriangle,
      label: "Symptomatic pods",
      value: health.pods_symptomatic,
      hint: "not Running / Succeeded",
    },
    {
      icon: Layers,
      label: "Deployments w/ unavailable replicas",
      value: health.deployments_unavailable,
      hint: `of ${health.deployments_total}`,
    },
    {
      icon: RefreshCw,
      label: "Container restarts",
      value: health.pod_restarts_total,
      hint: `${health.containers_total} containers`,
    },
    {
      icon: Server,
      label: "Unready nodes",
      value: health.not_ready_nodes,
      hint: `${health.ready_nodes} ready`,
    },
    {
      icon: HardDrive,
      label: "Unbound volumes",
      value: health.pvc_unbound,
      hint: `of ${health.pvc_total} PVCs`,
    },
    {
      icon: Activity,
      label: "DaemonSets w/ unavailable pods",
      value: health.daemonsets_unavailable,
      hint: `of ${health.daemonsets_total}`,
    },
  ];

  const workloads = [
    { label: "Pods", value: health.pods_total },
    { label: "Running", value: health.pods_running },
    { label: "Pending", value: health.pods_pending },
    { label: "Failed", value: health.pods_failed },
    { label: "Deployments", value: health.deployments_total },
    { label: "StatefulSets", value: health.statefulsets_total },
    { label: "DaemonSets", value: health.daemonsets_total },
    { label: "ReplicaSets", value: health.replicasets_total },
    { label: "Services", value: health.services_total },
  ];

  return (
    <div className="space-y-3 pt-2">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium flex items-center gap-2">
          <Gauge className="h-4 w-4 text-accent-cyan" /> Cluster health
        </h2>
        {health.partial && (
          <span className="text-[11px] text-accent-amber inline-flex items-center gap-1.5">
            <AlertTriangle className="h-3.5 w-3.5" /> partial — some resources
            couldn&apos;t be read
          </span>
        )}
      </div>

      {/* Troubleshooting tiles — coloured when non-zero. */}
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        {trouble.map((t) => (
          <HealthTile key={t.label} {...t} />
        ))}
      </div>

      {/* Resource counts. */}
      <div className="rounded-2xl border border-line bg-bg-card p-4">
        <div className="text-[11px] uppercase tracking-wider text-muted mb-3">
          Resources
        </div>
        <div className="grid grid-cols-3 sm:grid-cols-5 gap-y-3 gap-x-4">
          {workloads.map((w) => (
            <div key={w.label}>
              <div className="text-lg font-mono">{w.value}</div>
              <div className="text-[11px] text-muted">{w.label}</div>
            </div>
          ))}
        </div>
      </div>

      {health.symptomatic_pods.length > 0 && (
        <SymptomaticPodsTable pods={health.symptomatic_pods} />
      )}
    </div>
  );
}

function HealthTile({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: typeof Server;
  label: string;
  value: number;
  hint: string;
}) {
  const bad = value > 0;
  return (
    <div
      className={
        "rounded-2xl border bg-bg-card p-4 " +
        (bad ? "border-accent-amber/40" : "border-line")
      }
    >
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted">
        <Icon
          className={
            "h-3.5 w-3.5 " + (bad ? "text-accent-amber" : "text-muted")
          }
        />
        {label}
      </div>
      <div
        className={
          "mt-1.5 text-2xl font-mono " +
          (bad ? "text-accent-amber" : "text-fg")
        }
      >
        {value}
      </div>
      <div className="text-[11px] text-muted">{hint}</div>
    </div>
  );
}

function SymptomaticPodsTable({ pods }: { pods: SymptomaticPod[] }) {
  return (
    <div className="rounded-2xl border border-line bg-bg-card overflow-hidden">
      <div className="px-4 py-2 bg-bg-elevated/40 border-b border-line text-[11px] uppercase tracking-wider text-muted">
        Pods in symptomatic phases ({pods.length})
      </div>
      <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead className="bg-bg-elevated/20 text-[11px] uppercase tracking-wider text-muted">
          <tr>
            <th className="text-left px-4 py-2 font-normal">Pod</th>
            <th className="text-left px-4 py-2 font-normal">Namespace</th>
            <th className="text-left px-4 py-2 font-normal">Status</th>
            <th className="text-left px-4 py-2 font-normal">Ready</th>
            <th className="text-left px-4 py-2 font-normal">Restarts</th>
            <th className="text-left px-4 py-2 font-normal">Node</th>
          </tr>
        </thead>
        <tbody>
          {pods.map((p) => (
            <tr key={`${p.namespace}/${p.name}`} className="border-t border-line">
              <td className="px-4 py-2.5 font-medium">{p.name}</td>
              <td className="px-4 py-2.5 text-xs font-mono text-muted">
                {p.namespace}
              </td>
              <td className="px-4 py-2.5">
                <span className="text-accent-amber text-xs">{p.reason}</span>
              </td>
              <td className="px-4 py-2.5 font-mono text-xs">{p.ready}</td>
              <td className="px-4 py-2.5 font-mono text-xs">
                {p.restarts > 0 ? (
                  <span className="text-accent-amber">{p.restarts}</span>
                ) : (
                  p.restarts
                )}
              </td>
              <td className="px-4 py-2.5 font-mono text-xs text-muted">
                {p.node || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  );
}

// ── Related dashboards ────────────────────────────────────────────────
//
// Only what belongs to *this* cluster: its own metrics/logs stores (linked
// with ?cluster= so the console queries that store, not the tenant's) and
// its Grafana dashboards. A cluster with nothing connected says so rather
// than showing another cluster's data. The GPU view is tenant-wide.

const METRICS_PROVIDERS = ["prometheus", "thanos"];
const LOGS_PROVIDERS = ["loki", "opensearch"];

function RelatedDashboards({ slug }: { slug: string }) {
  const storesQuery = useQuery({
    queryKey: ["cluster-observability", slug],
    queryFn: () => api.clusters.observability(slug),
    staleTime: 60_000,
  });
  const stores = storesQuery.data ?? [];
  const metrics = stores.find((s) => METRICS_PROVIDERS.includes(s.provider));
  const logs = stores.find((s) => LOGS_PROVIDERS.includes(s.provider));
  const hasGrafana = stores.some((s) => s.provider === "grafana");

  const { data } = useQuery({
    queryKey: ["dashboards"],
    queryFn: () => api.dashboards.list(),
    staleTime: 60_000,
    enabled: hasGrafana,
  });
  const dashboards = hasGrafana ? (data?.dashboards ?? []).slice(0, 4) : [];

  const consoles = [
    {
      icon: LineChart,
      title: "Metrics console",
      desc: metrics
        ? `PromQL against this cluster's ${metrics.name}.`
        : "No metrics store connected to this cluster yet.",
      href: metrics
        ? `/observability/${metrics.provider}?cluster=${encodeURIComponent(slug)}`
        : null,
    },
    {
      icon: ScrollText,
      title: "Logs",
      desc: logs
        ? `Search this cluster's logs in ${logs.name}.`
        : "No log store connected to this cluster yet.",
      href: logs
        ? `/observability/${logs.provider}?cluster=${encodeURIComponent(slug)}`
        : null,
    },
    {
      icon: Cpu,
      title: "GPU & AI factory",
      desc: "Per-GPU utilisation, memory, temperature, and diagnostics.",
      href: "/ai-factory",
    },
  ];

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium flex items-center gap-2">
          <Database className="h-4 w-4 text-accent-cyan" /> Related dashboards
        </h2>
        {hasGrafana && (
          <Link
            href="/dashboards"
            className="text-xs text-muted hover:text-accent-cyan inline-flex items-center gap-1"
          >
            All dashboards <ChevronRight className="h-3.5 w-3.5" />
          </Link>
        )}
      </div>

      {dashboards.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {dashboards.map((d) => (
            <Link
              key={d.uid}
              href={`/dashboards/${d.uid}`}
              className="group rounded-2xl border border-line bg-bg-card p-4 hover:border-accent-cyan/40 transition-colors"
            >
              <div className="flex items-center justify-between">
                <LayoutDashboard className="h-4 w-4 text-accent-cyan" />
                <ChevronRight className="h-4 w-4 text-muted group-hover:text-accent-cyan" />
              </div>
              <div className="mt-2 font-medium text-sm group-hover:text-accent-cyan truncate">
                {d.title}
              </div>
              <div className="text-[11px] text-muted mt-1">
                {d.folder || "Grafana"}
              </div>
            </Link>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {consoles.map((c) =>
          c.href ? (
            <Link
              key={c.title}
              href={c.href}
              className="group rounded-2xl border border-line bg-bg-card p-4 hover:border-accent-cyan/40 transition-colors"
            >
              <div className="flex items-center justify-between">
                <c.icon className="h-4 w-4 text-accent-cyan" />
                <ChevronRight className="h-4 w-4 text-muted group-hover:text-accent-cyan" />
              </div>
              <div className="mt-2 font-medium text-sm group-hover:text-accent-cyan">
                {c.title}
              </div>
              <div className="text-[11px] text-muted mt-1">{c.desc}</div>
            </Link>
          ) : (
            <div
              key={c.title}
              className="rounded-2xl border border-dashed border-line bg-bg-card/50 p-4"
              aria-disabled="true"
            >
              <c.icon className="h-4 w-4 text-muted" />
              <div className="mt-2 font-medium text-sm text-muted">{c.title}</div>
              <div className="text-[11px] text-muted mt-1">
                {storesQuery.isLoading ? "Checking this cluster…" : c.desc}
              </div>
            </div>
          ),
        )}
      </div>
    </section>
  );
}

function StatCard({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Server;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-2xl border border-line bg-bg-card p-4">
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted">
        <Icon className="h-3.5 w-3.5" /> {label}
      </div>
      <div className="mt-1.5 text-lg font-mono">{value}</div>
    </div>
  );
}

function NodesTable({
  nodes,
}: {
  nodes: import("@/lib/api").ClusterNode[];
}) {
  return (
    <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
      <table className="w-full min-w-[760px] text-sm">
        <thead className="bg-bg-elevated/40 text-[11px] uppercase tracking-wider text-muted">
          <tr>
            <th className="text-left px-4 py-2 font-normal">Node</th>
            <th className="text-left px-4 py-2 font-normal">Status</th>
            <th className="text-left px-4 py-2 font-normal">Roles</th>
            <th className="text-left px-4 py-2 font-normal">Version</th>
            <th className="text-left px-4 py-2 font-normal">Internal IP</th>
            <th className="text-left px-4 py-2 font-normal">CPU</th>
            <th className="text-left px-4 py-2 font-normal">Memory</th>
            <th className="text-left px-4 py-2 font-normal">Age</th>
          </tr>
        </thead>
        <tbody>
          {nodes.length === 0 && (
            <tr>
              <td colSpan={8} className="px-4 py-6 text-center text-muted">
                No nodes returned.
              </td>
            </tr>
          )}
          {nodes.map((n) => (
            <tr key={n.name} className="border-t border-line">
              <td className="px-4 py-3">
                <div className="font-medium">{n.name}</div>
                {n.os_image && (
                  <div className="text-[11px] text-muted">{n.os_image}</div>
                )}
              </td>
              <td className="px-4 py-3">
                <span
                  className={
                    n.status.startsWith("Ready")
                      ? "text-accent-emerald text-xs"
                      : "text-accent-amber text-xs"
                  }
                >
                  {n.status}
                </span>
              </td>
              <td className="px-4 py-3 text-xs">{n.roles.join(", ")}</td>
              <td className="px-4 py-3 font-mono text-xs">{n.version}</td>
              <td className="px-4 py-3 font-mono text-xs">
                {n.internal_ip || "—"}
              </td>
              <td className="px-4 py-3 font-mono text-xs">{n.cpu || "—"}</td>
              <td className="px-4 py-3 font-mono text-xs">
                {n.memory || "—"}
              </td>
              <td className="px-4 py-3 text-xs text-muted">
                {nodeAge(n.created_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── kubectl console ───────────────────────────────────────────────────

function KubectlConsole({
  slug,
  reachable,
}: {
  slug: string;
  /** undefined = not known yet (overview still loading). */
  reachable: boolean | undefined;
}) {
  const { data: catalog } = useQuery({
    queryKey: ["kubectl-catalog"],
    queryFn: () => api.clusters.kubectlCatalog(),
  });

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [namespace, setNamespace] = useState("");
  const [selector, setSelector] = useState("");
  const [output, setOutput] = useState<KubectlOutput>("cli");

  const run = useMutation({
    mutationFn: () =>
      api.clusters.runKubectl(slug, {
        command_ids: [...selected],
        namespace: namespace.trim() || null,
        label_selector: selector.trim() || null,
        output,
      }),
  });

  const grouped = useMemo(() => {
    const map: Record<string, KubectlCommandSpec[]> = {};
    for (const c of catalog ?? []) {
      (map[c.group] ??= []).push(c);
    }
    return map;
  }, [catalog]);

  const anyNamespaced = useMemo(
    () =>
      (catalog ?? []).some(
        (c) => selected.has(c.id) && c.namespaced
      ),
    [catalog, selected]
  );

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-base font-medium flex items-center gap-2">
          <Terminal className="h-4 w-4 text-accent-cyan" /> kubectl commands
        </h2>
        <p className="text-xs text-muted mt-0.5 max-w-[820px]">
          Pick one or more read-only commands, choose an output format, and
          Daalu runs them on the cluster over the tunnel. Everything here is
          read-only — there is no way to mutate the cluster from this panel.
        </p>
      </div>

      <div className="rounded-2xl border border-line bg-bg-card p-4 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-4">
          {Object.entries(grouped).map(([group, cmds]) => (
            <div key={group} className="space-y-1.5">
              <div className="text-[11px] uppercase tracking-wider text-muted">
                {group}
              </div>
              {cmds.map((c) => (
                <label
                  key={c.id}
                  title={c.kubectl}
                  className="flex items-start gap-2 text-sm cursor-pointer hover:text-fg"
                >
                  <input
                    type="checkbox"
                    checked={selected.has(c.id)}
                    onChange={() => toggle(c.id)}
                    className="mt-1 accent-[color:var(--accent)]"
                  />
                  <span>{c.label}</span>
                </label>
              ))}
            </div>
          ))}
        </div>

        <div className="flex flex-wrap items-end gap-3 border-t border-line pt-3">
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">
              Namespace {anyNamespaced ? "" : "(n/a)"}
            </div>
            <input
              value={namespace}
              onChange={(e) => setNamespace(e.target.value)}
              placeholder="all namespaces"
              className="h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm w-48"
            />
          </label>
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">
              Label selector
            </div>
            <input
              value={selector}
              onChange={(e) => setSelector(e.target.value)}
              placeholder="app=checkout"
              className="h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm w-56"
            />
          </label>
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">Output</div>
            <div className="flex rounded-lg border border-line overflow-hidden">
              {OUTPUT_OPTIONS.map((o) => (
                <button
                  key={o.id}
                  type="button"
                  onClick={() => setOutput(o.id)}
                  className={
                    "h-9 px-3 text-xs " +
                    (output === o.id
                      ? "bg-accent-cyan/15 text-accent-cyan"
                      : "text-muted hover:text-fg")
                  }
                >
                  {o.label}
                </button>
              ))}
            </div>
          </label>
          <button
            type="button"
            disabled={selected.size === 0 || reachable !== true || run.isPending}
            onClick={() => run.mutate()}
            className="h-9 px-4 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan text-sm disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            <Play className="h-3.5 w-3.5" />
            {run.isPending
              ? "Running…"
              : selected.size === 0
                ? "Pick a command to run"
                : `Run ${selected.size} read-only command${selected.size === 1 ? "" : "s"}`}
          </button>
        </div>
        {reachable === false && (
          <p className="text-[11px] text-accent-amber">
            Cluster is not reachable — commands are disabled until the tunnel
            is connected and Kubernetes credentials are linked.
          </p>
        )}
        {run.error && (
          <p className="text-[11px] text-signal-critical">
            {String(run.error.message || run.error)}
          </p>
        )}
      </div>

      {run.data && (
        <div className="space-y-3">
          {run.data.results.map((r) => (
            <ResultBlock key={r.id} result={r} />
          ))}
        </div>
      )}
    </section>
  );
}

function ResultBlock({ result }: { result: KubectlResult }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="rounded-2xl border border-line bg-bg-card overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-4 py-2 bg-bg-elevated/40 border-b border-line">
        <code className="text-xs font-mono break-all">
          {result.command}{" "}
          {!result.ok && (
            <span className="text-signal-critical">— failed</span>
          )}
        </code>
        {result.ok && result.output && (
          <button
            onClick={() => {
              navigator.clipboard.writeText(result.output);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
            className="text-[11px] h-7 px-2 rounded-md border border-line hover:bg-bg-elevated/60 inline-flex items-center gap-1"
          >
            <Copy className="h-3 w-3" /> {copied ? "Copied" : "Copy"}
          </button>
        )}
      </div>
      <pre className="text-xs p-3 overflow-x-auto whitespace-pre">
        {result.ok ? result.output || "(empty)" : result.error}
      </pre>
    </div>
  );
}
