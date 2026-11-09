"use client";

/**
 * Per-provider observability detail page (/observability/[provider]).
 *
 * The metrics/logs sibling of the kubectl console (/clusters/[slug]). For a
 * connected Prometheus/Thanos or Loki integration it shows:
 *  - Overview: headline health/inventory read from the store's HTTP API
 *    (targets up/down, firing alerts, build version, per-job health; or for
 *    Loki, the label + namespace inventory).
 *  - A query console: tick one or more read-only PromQL/LogQL panels from the
 *    server's allowlist, pick a time window, and run them. Metrics also accept
 *    one free-form PromQL expression.
 *
 * Same "allowlisted, format-selectable runner" pattern as the kubectl console.
 *
 * `?cluster=<slug>` (set by a cluster page's Dashboards tab) scopes the page to
 * the store that belongs to that cluster; without it the tenant's store is used.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  Copy,
  FileText,
  Gauge,
  Play,
  Server,
  Tag,
} from "lucide-react";
import {
  api,
  ApiError,
  type ObsQuerySpec,
  type ObsQueryResult,
} from "@/lib/api";
import { HUB } from "@/lib/hub-routes";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/errors";
import { isUnreachable, withTimeout } from "@/lib/timeout";

const METRIC_WINDOWS = ["instant", "5m", "15m", "1h", "6h", "24h"];
const LOG_WINDOWS = ["5m", "15m", "1h", "6h", "24h"];

const PROVIDER_SUBTITLE: Record<string, string> = {
  prometheus: "Check that your metrics store is healthy and run read-only metric queries against it.",
  thanos: "Check that your long-term metrics store is healthy and run read-only metric queries against it.",
  loki: "Check that your log store is healthy and pull recent log lines with read-only queries.",
  opensearch: "Check that your search and log store is healthy and run read-only queries against it.",
};

const PROVIDER_TITLE: Record<string, string> = {
  prometheus: "Prometheus / Alertmanager",
  thanos: "Thanos",
  loki: "Loki",
  opensearch: "OpenSearch",
};

export default function ObservabilityDetailPage() {
  const params = useParams<{ provider: string }>();
  const provider = params.provider;

  const title = PROVIDER_TITLE[provider] || provider;
  const cluster = useClusterParam();

  const overviewQuery = useQuery({
    queryKey: ["obs-overview", provider, cluster],
    queryFn: () =>
      withTimeout(api.observability.overview(provider, cluster ?? undefined), title),
    // Wait until ?cluster= has been read, so a cluster page never flashes
    // the tenant's store first.
    enabled: !!provider && cluster !== undefined,
    refetchInterval: 30_000,
    retry: false,
  });
  const overview = overviewQuery.data;
  const notFound =
    overviewQuery.isError &&
    overviewQuery.error instanceof ApiError &&
    overviewQuery.error.status === 404;
  // The cluster has no store of this kind: say so instead of "unreachable".
  const noStoreOnCluster =
    !!cluster && !!overview && !overview.reachable &&
    (overview.error ?? "").includes("integration for cluster");
  const back = cluster
    ? { href: `/clusters/${cluster}`, label: cluster }
    : { href: HUB.clusters, label: "Clusters & observability" };

  const Icon =
    (overview?.family ?? (provider === "loki" ? "logs" : "metrics")) === "logs"
      ? FileText
      : Gauge;
  const baseUrl =
    overview?.metrics?.base_url || overview?.logs?.base_url || "";

  return (
    <div className="space-y-6 max-w-[1100px]">
      <div>
        <Link
          href={back.href}
          className="text-xs text-muted hover:text-fg inline-flex items-center gap-1.5"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> {back.label}
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          <h1 className="text-2xl font-semibold flex items-start gap-2">
            <Icon className="h-5 w-5 mt-1.5 shrink-0 text-accent-cyan" />
            {title}
          </h1>
          {cluster && (
            <span className="text-xs text-muted">
              cluster <span className="font-mono text-fg">{cluster}</span>
            </span>
          )}
          {overview && !noStoreOnCluster && (
            <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted">
              <span
                aria-hidden
                className={`h-2 w-2 rounded-full ${
                  overview.reachable ? "bg-accent-emerald" : "bg-signal-critical"
                }`}
              />
              {overview.reachable ? "connected" : "unreachable"}
            </span>
          )}
        </div>
        <p className="text-sm text-muted mt-1 max-w-[720px]">
          {PROVIDER_SUBTITLE[provider] ??
            "Check that this store is healthy and run read-only queries against it."}
        </p>
        {baseUrl && (
          <div className="text-xs text-muted mt-1 break-all">
            endpoint <span className="font-mono">{baseUrl}</span>
          </div>
        )}
      </div>

      {noStoreOnCluster ? (
        <LoadState
          query={{ isLoading: false, isError: false }}
          empty
          emptyTitle={`No ${title} on ${cluster} yet`}
          emptyHint={`This cluster has no ${title} connected, so there's nothing to show. Install one into the cluster, or connect an existing store as an integration linked to ${cluster}.`}
          emptyAction={
            <Link href={back.href} className="text-xs underline">
              Back to {cluster}
            </Link>
          }
        >
          {null}
        </LoadState>
      ) : notFound ? (
        <LoadState
          query={{ isLoading: false, isError: false }}
          empty
          emptyTitle={`No console for ${title}`}
          emptyHint="Daalu has a browse console for connected Prometheus, Thanos, Loki and OpenSearch stores."
          emptyAction={
            <Link href={HUB.clusters} className="text-xs underline">
              Back to clusters &amp; observability
            </Link>
          }
        >
          {null}
        </LoadState>
      ) : (
        <LoadState
          query={overviewQuery}
          loadingText={`Querying ${title}…`}
          notSetUp={{
            when: isUnreachable,
            title: `Couldn't reach ${title}`,
            hint: (
              <>
                Check the cluster&apos;s observability connection in{" "}
                <Link href={HUB.clusters} className="underline">
                  Managed infra
                </Link>
                .
              </>
            ),
            action: (
              <Button size="sm" onClick={() => overviewQuery.refetch()}>
                Retry
              </Button>
            ),
          }}
        >
          {overview && (
            <OverviewAndConsole
              provider={provider}
              overview={overview}
              cluster={cluster ?? undefined}
            />
          )}
        </LoadState>
      )}
    </div>
  );
}

function OverviewAndConsole({
  provider,
  overview,
  cluster,
}: {
  provider: string;
  overview: import("@/lib/api").ObservabilityOverview;
  cluster?: string;
}) {
  return (
    <div className="space-y-6">
      {/* ── Overview ─────────────────────────────────────────────────── */}
      {!overview.reachable ? (
        <div className="rounded-2xl border border-accent-amber/40 bg-accent-amber/5 p-4 text-sm">
          <div className="font-medium text-accent-amber">
            Endpoint not reachable
          </div>
          <p className="text-xs text-muted mt-1">
            {overview.error ||
              "Daalu couldn't query this endpoint. Check the URL on the integration (Clusters & observability → Edit) and that the store is reachable from Daalu."}
          </p>
        </div>
      ) : overview.family === "metrics" && overview.metrics ? (
        <MetricsOverviewView m={overview.metrics} />
      ) : overview.logs ? (
        <LogsOverviewView l={overview.logs} />
      ) : null}

      {/* ── Console ──────────────────────────────────────────────────── */}
      {overview.family === "metrics" ? (
        <MetricsConsole
          provider={provider}
          reachable={overview.reachable}
          cluster={cluster}
        />
      ) : (
        <LogsConsole
          provider={provider}
          reachable={overview.reachable}
          namespaces={overview.logs?.namespaces ?? []}
          cluster={cluster}
        />
      )}
    </div>
  );
}

// ── Overview views ────────────────────────────────────────────────────

function StatCard({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: typeof Server;
  label: string;
  value: string;
  tone?: "ok" | "warn" | "bad";
}) {
  const color =
    tone === "bad"
      ? "text-signal-critical"
      : tone === "warn"
      ? "text-accent-amber"
      : tone === "ok"
      ? "text-accent-emerald"
      : "text-fg";
  return (
    <div className="rounded-2xl border border-line bg-bg-card p-4">
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted">
        <Icon className="h-3.5 w-3.5" /> {label}
      </div>
      <div className={`mt-1.5 text-lg font-mono ${color}`}>{value}</div>
    </div>
  );
}

function MetricsOverviewView({
  m,
}: {
  m: import("@/lib/api").MetricsOverview;
}) {
  const down = m.targets_down ?? 0;
  const firing = m.firing_alerts ?? 0;
  return (
    <section className="space-y-3">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard
          icon={Server}
          label="Targets up"
          value={`${m.targets_up ?? "—"} / ${m.targets_total ?? "—"}`}
          tone="ok"
        />
        <StatCard
          icon={Activity}
          label="Targets down"
          value={String(down)}
          tone={down > 0 ? "bad" : "ok"}
        />
        <StatCard
          icon={AlertTriangle}
          label="Firing alerts"
          value={String(firing)}
          tone={firing > 0 ? "warn" : "ok"}
        />
        <StatCard icon={Gauge} label="Version" value={m.version || "—"} />
      </div>
      {m.jobs.length > 0 && (
        <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-bg-elevated/40 text-[11px] uppercase tracking-wider text-muted">
              <tr>
                <th className="text-left px-4 py-2 font-normal">Scrape job</th>
                <th className="text-left px-4 py-2 font-normal">Up</th>
                <th className="text-left px-4 py-2 font-normal">Targets</th>
                <th className="text-left px-4 py-2 font-normal">Health</th>
              </tr>
            </thead>
            <tbody>
              {m.jobs.map((j) => {
                const healthy = j.up >= j.total;
                return (
                  <tr key={j.job} className="border-t border-line">
                    <td className="px-4 py-2.5 font-mono text-xs">{j.job}</td>
                    <td className="px-4 py-2.5 font-mono text-xs">{j.up}</td>
                    <td className="px-4 py-2.5 font-mono text-xs">{j.total}</td>
                    <td className="px-4 py-2.5">
                      <span
                        className={
                          healthy
                            ? "text-accent-emerald text-xs"
                            : "text-signal-critical text-xs"
                        }
                      >
                        {healthy ? "all up" : `${j.total - j.up} down`}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function LogsOverviewView({ l }: { l: import("@/lib/api").LogsOverview }) {
  return (
    <section className="space-y-3">
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        <StatCard icon={Tag} label="Labels" value={String(l.label_count ?? 0)} />
        <StatCard
          icon={FileText}
          label="Namespaces"
          value={String(l.namespaces.length)}
        />
      </div>
      {l.namespaces.length > 0 && (
        <div className="rounded-2xl border border-line bg-bg-card p-4">
          <div className="text-[11px] uppercase tracking-wider text-muted mb-2">
            Namespaces seen
          </div>
          <div className="flex flex-wrap gap-1.5">
            {l.namespaces.map((ns) => (
              <span
                key={ns}
                className="text-[11px] font-mono px-2 py-0.5 rounded-md border border-line bg-bg-elevated/60"
              >
                {ns}
              </span>
            ))}
          </div>
        </div>
      )}
      {l.labels.length > 0 && (
        <div className="rounded-2xl border border-line bg-bg-card p-4">
          <div className="text-[11px] uppercase tracking-wider text-muted mb-2">
            Stream labels
          </div>
          <div className="flex flex-wrap gap-1.5">
            {l.labels.map((lbl) => (
              <span
                key={lbl}
                className="text-[11px] font-mono px-2 py-0.5 rounded-md border border-line text-muted"
              >
                {lbl}
              </span>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

// ── shared console bits ───────────────────────────────────────────────

/**
 * `?cluster=<slug>`, read after mount (window.location, not useSearchParams,
 * to avoid the Suspense-boundary requirement). `undefined` until read, then
 * the slug or `null`.
 */
function useClusterParam(): string | null | undefined {
  const [cluster, setCluster] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    setCluster(new URLSearchParams(window.location.search).get("cluster"));
  }, []);
  return cluster;
}

function useCatalog(provider: string) {
  return useQuery({
    queryKey: ["obs-catalog", provider],
    queryFn: () => api.observability.catalog(provider),
  });
}

/**
 * `?group=<Group>` deep link: once the catalog arrives, preselect every panel
 * in that group and run them — so "view network metrics/logs" links land on
 * results, not an empty picker. Reads window.location directly (rather than
 * useSearchParams) to avoid the Suspense-boundary requirement.
 */
function useGroupDeepLink(
  catalog: ObsQuerySpec[] | undefined,
  setSelected: (s: Set<string>) => void,
  runWith: (ids: string[]) => void,
) {
  const seeded = useRef(false);
  useEffect(() => {
    if (seeded.current || !catalog?.length) return;
    seeded.current = true;
    const group = new URLSearchParams(window.location.search).get("group");
    if (!group) return;
    const ids = catalog
      .filter((c) => c.group.toLowerCase() === group.toLowerCase())
      .map((c) => c.id);
    if (ids.length === 0) return;
    setSelected(new Set(ids));
    runWith(ids);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog]);
}

function CatalogPicker({
  catalog,
  selected,
  toggle,
}: {
  catalog: ObsQuerySpec[] | undefined;
  selected: Set<string>;
  toggle: (id: string) => void;
}) {
  const grouped = useMemo(() => {
    const map: Record<string, ObsQuerySpec[]> = {};
    for (const c of catalog ?? []) (map[c.group] ??= []).push(c);
    return map;
  }, [catalog]);

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-4">
      {Object.entries(grouped).map(([group, specs]) => (
        <div key={group} className="space-y-1.5">
          <div className="text-[11px] uppercase tracking-wider text-muted">
            {group}
          </div>
          {specs.map((c) => (
            <label
              key={c.id}
              title={c.description}
              className="flex items-start gap-2 text-sm cursor-pointer hover:text-fg"
            >
              <input
                type="checkbox"
                checked={selected.has(c.id)}
                onChange={() => toggle(c.id)}
                className="mt-1 accent-[color:var(--accent)]"
              />
              <span>
                <span>
                  {c.label}
                  {c.unit && (
                    <span className="text-muted"> ({c.unit})</span>
                  )}
                </span>
                <span className="block text-[11px] text-muted font-mono break-all">
                  {c.query}
                </span>
              </span>
            </label>
          ))}
        </div>
      ))}
    </div>
  );
}

function WindowToggle({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: string[];
  value: string;
  onChange: (v: string) => void;
  ariaLabel: string;
}) {
  return (
    <div role="group" aria-label={ariaLabel} className="flex flex-wrap rounded-lg border border-line overflow-hidden">
      {options.map((o) => (
        <button
          key={o}
          type="button"
          aria-pressed={value === o}
          onClick={() => onChange(o)}
          className={
            "h-9 px-3 text-xs " +
            (value === o
              ? "bg-accent-cyan/15 text-accent-cyan"
              : "text-muted hover:text-fg")
          }
        >
          {o}
        </button>
      ))}
    </div>
  );
}

function ResultBlock({ result }: { result: ObsQueryResult }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="rounded-2xl border border-line bg-bg-card overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 bg-bg-elevated/40 border-b border-line gap-3">
        <code className="text-xs font-mono break-all">
          {result.query}{" "}
          {!result.ok && <span className="text-signal-critical">— failed</span>}
        </code>
        {result.ok && result.output && (
          <button
            type="button"
            onClick={() => {
              navigator.clipboard.writeText(result.output);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
            className="text-[11px] h-7 px-2 rounded-md border border-line hover:bg-bg-elevated/60 inline-flex items-center gap-1 shrink-0"
          >
            <Copy className="h-3 w-3" aria-hidden />{" "}
            <span aria-live="polite">{copied ? "Copied" : "Copy"}</span>
          </button>
        )}
      </div>
      <pre className="text-xs p-3 overflow-x-auto whitespace-pre">
        {result.ok ? result.output || "(empty)" : result.error}
      </pre>
    </div>
  );
}

function RunButton({
  disabled,
  pending,
  count,
  onClick,
  disabledReason,
}: {
  disabled: boolean;
  pending: boolean;
  count: number;
  onClick: () => void;
  disabledReason?: string;
}) {
  return (
    <Button
      variant="primary"
      disabled={disabled}
      disabledReason={disabledReason}
      pending={pending}
      onClick={onClick}
      icon={<Play className="h-3.5 w-3.5" aria-hidden />}
    >
      {pending
        ? "Running…"
        : count
          ? `Run ${count} ${count === 1 ? "query" : "queries"}`
          : "Run queries"}
    </Button>
  );
}

// ── metrics console ───────────────────────────────────────────────────

function MetricsConsole({
  provider,
  reachable,
  cluster,
}: {
  provider: string;
  reachable: boolean;
  cluster?: string;
}) {
  const { data: catalog } = useCatalog(provider);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [timeRange, setTimeRange] = useState("instant");
  const [custom, setCustom] = useState("");

  const run = useMutation({
    mutationFn: (ids?: string[]) =>
      api.observability.runQuery(provider, {
        query_ids: [...(ids ?? selected)],
        time_range: timeRange,
        custom_query: custom.trim() || null,
      }, cluster),
  });

  // Deep link: ?group=Network preselects + runs that catalog group — the
  // "view network metrics" link from the device-onboarding panel.
  useGroupDeepLink(catalog, setSelected, (ids) => run.mutate(ids));

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const nothingPicked = selected.size === 0 && !custom.trim();

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-base font-medium flex items-center gap-2">
          <Gauge className="h-4 w-4 text-accent-cyan" /> Metric queries
        </h2>
        <p className="text-xs text-muted mt-0.5 max-w-[820px]">
          Tick one or more PromQL panels, choose a time window
          (instant or a range), and Daalu runs them against this store. Range
          windows summarise each series as last / min / max / avg. Everything
          here is read-only.
        </p>
      </div>

      <div className="rounded-2xl border border-line bg-bg-card p-4 space-y-4">
        <CatalogPicker catalog={catalog} selected={selected} toggle={toggle} />

        <div className="space-y-2 border-t border-line pt-3">
          <label className="text-xs space-y-1 block">
            <div className="text-muted uppercase tracking-wider">
              Custom PromQL (optional)
            </div>
            <input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              placeholder='sum(rate(http_requests_total[5m])) by (code)'
              className="h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm w-full font-mono"
            />
          </label>
        </div>

        <div className="flex flex-wrap items-end gap-3 border-t border-line pt-3">
          <div className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider" aria-hidden>
              Time window
            </div>
            <WindowToggle
              ariaLabel="Time window"
              options={METRIC_WINDOWS}
              value={timeRange}
              onChange={setTimeRange}
            />
          </div>
          <RunButton
            disabled={nothingPicked || !reachable || run.isPending}
            disabledReason={
              !reachable
                ? "The store isn't reachable"
                : nothingPicked
                  ? "Tick a panel or type a query first"
                  : undefined
            }
            pending={run.isPending}
            count={selected.size + (custom.trim() ? 1 : 0)}
            onClick={() => run.mutate(undefined)}
          />
        </div>
        {!reachable && (
          <p className="text-xs text-muted">
            Daalu can&apos;t reach this store right now, so queries are off.
            Check the integration, then reload.
          </p>
        )}
        {run.error && (
          <p role="alert" className="text-xs text-signal-critical">
            {errorText(run.error)}
          </p>
        )}
      </div>

      {run.data && (
        <div className="space-y-3 motion-enter" key={run.submittedAt}>
          {run.data.results.map((r) => (
            <ResultBlock key={r.id} result={r} />
          ))}
        </div>
      )}
    </section>
  );
}

// ── logs console ──────────────────────────────────────────────────────

function LogsConsole({
  provider,
  reachable,
  namespaces,
  cluster,
}: {
  provider: string;
  reachable: boolean;
  namespaces: string[];
  cluster?: string;
}) {
  const { data: catalog } = useCatalog(provider);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [namespace, setNamespace] = useState("");
  const [search, setSearch] = useState("");
  const [since, setSince] = useState("1h");
  const [limit, setLimit] = useState(200);

  const run = useMutation({
    mutationFn: (ids?: string[]) =>
      api.observability.runQuery(provider, {
        query_ids: [...(ids ?? selected)],
        namespace: namespace.trim() || null,
        search: search.trim() || null,
        since,
        limit,
      }, cluster),
  });

  // Deep link: ?group=Network preselects + runs that catalog group — the
  // "view network logs" link from the device-onboarding panel.
  useGroupDeepLink(catalog, setSelected, (ids) => run.mutate(ids));

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
          <FileText className="h-4 w-4 text-accent-cyan" /> Log queries
        </h2>
        <p className="text-xs text-muted mt-0.5 max-w-[820px]">
          Tick one or more LogQL panels, optionally narrow to a namespace and a
          search substring, and Daalu pulls the most recent matching lines.
          Everything here is read-only.
        </p>
      </div>

      <div className="rounded-2xl border border-line bg-bg-card p-4 space-y-4">
        <CatalogPicker catalog={catalog} selected={selected} toggle={toggle} />

        <div className="flex flex-wrap items-end gap-3 border-t border-line pt-3">
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">Namespace</div>
            <input
              value={namespace}
              onChange={(e) => setNamespace(e.target.value)}
              placeholder="all namespaces"
              list="obs-namespaces"
              className="h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm w-48"
            />
            <datalist id="obs-namespaces">
              {namespaces.map((ns) => (
                <option key={ns} value={ns} />
              ))}
            </datalist>
          </label>
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">
              Search (substring)
            </div>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="connection refused"
              className="h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm w-56"
            />
          </label>
          <div className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider" aria-hidden>Since</div>
            <WindowToggle
              ariaLabel="Since"
              options={LOG_WINDOWS}
              value={since}
              onChange={setSince}
            />
          </div>
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">Limit</div>
            <input
              type="number"
              min={1}
              max={1000}
              value={limit}
              onChange={(e) =>
                setLimit(
                  Math.max(1, Math.min(1000, Number(e.target.value) || 1))
                )
              }
              className="h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm w-24 font-mono"
            />
          </label>
          <RunButton
            disabled={selected.size === 0 || !reachable || run.isPending}
            disabledReason={
              !reachable
                ? "The store isn't reachable"
                : selected.size === 0
                  ? "Tick a panel first"
                  : undefined
            }
            pending={run.isPending}
            count={selected.size}
            onClick={() => run.mutate(undefined)}
          />
        </div>
        {!reachable && (
          <p className="text-xs text-muted">
            Daalu can&apos;t reach this store right now, so queries are off.
            Check the integration, then reload.
          </p>
        )}
        {run.error && (
          <p role="alert" className="text-xs text-signal-critical">
            {errorText(run.error)}
          </p>
        )}
      </div>

      {run.data && (
        <div className="space-y-3 motion-enter" key={run.submittedAt}>
          {run.data.results.map((r) => (
            <ResultBlock key={r.id} result={r} />
          ))}
        </div>
      )}
    </section>
  );
}
