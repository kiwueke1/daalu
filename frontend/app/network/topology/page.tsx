"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  AlertTriangle,
  ArrowLeft,
  Loader2,
  RefreshCw,
  Waypoints,
} from "lucide-react";
import { api, ApiError, type TopoLinkStatus, type TopoNode } from "@/lib/api";
import { FabricGraph, LINK_COLOR, LINK_STYLE } from "@/components/topology/FabricGraph";
import { NodeDrawer } from "@/components/topology/NodeDrawer";
import { DeviceTerminal } from "@/components/topology/DeviceTerminal";
import { cn } from "@/lib/utils";
import { HUB } from "@/lib/hub-routes";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";

// Which links each view draws. "Observed" is the wire truth — only
// adjacencies actually seen via LLDP. "Planned" is the intent — the
// cabling plan from the source of truth (Nautobot), coloured by what the
// wire says about each planned link (up / down / not yet probed).
type LinkView = "observed" | "planned";

const LEGEND: Record<LinkView, { status: TopoLinkStatus; label: string }[]> = {
  observed: [
    { status: "up", label: "Up" },
    { status: "mismatch", label: "Miscabled (not in the plan)" },
    { status: "mgmt", label: "Management network" },
  ],
  planned: [
    { status: "up", label: "Up (seen on the wire)" },
    { status: "down", label: "Down (planned, not seen)" },
    { status: "unknown", label: "Not checked yet" },
  ],
};

function LegendSwatch({ status }: { status: TopoLinkStatus }) {
  // Draw the swatch with the exact stroke treatment the graph uses — the
  // dash pattern carries as much meaning as the colour.
  const style = LINK_STYLE[status];
  return (
    <svg width="28" height="6" className="inline-block align-middle" aria-hidden>
      <line
        x1="1"
        y1="3"
        x2="27"
        y2="3"
        stroke={LINK_COLOR[status]}
        strokeWidth={Math.max(2, style.width)}
        strokeOpacity={Math.max(0.7, style.opacity)}
        strokeLinecap="round"
        strokeDasharray={style.dash}
      />
    </svg>
  );
}

// `useSearchParams` needs a Suspense boundary to keep the route statically
// renderable; the page proper lives in TopologyView below.
export default function TopologyPage() {
  return (
    <Suspense fallback={<GraphLoading />}>
      <TopologyView />
    </Suspense>
  );
}

/** Spinner plus, after a while, a hint about what the wait is on. */
function GraphLoading({
  envLabel,
  onRetry,
}: {
  envLabel?: string;
  onRetry?: () => void;
}) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => setSlow(true), 15_000);
    return () => window.clearTimeout(t);
  }, []);
  return (
    <div role="status" className="rounded-2xl border border-line bg-bg-card p-10 text-center text-sm text-muted">
      <Loader2 className="mx-auto mb-3 h-5 w-5 animate-spin text-accent-cyan" aria-hidden />
      {slow ? (
        <>
          <div>
            Still waiting on the device and LLDP data
            {envLabel ? ` from ${envLabel}` : ""}…
          </div>
          {onRetry && (
            <Button
              size="sm"
              onClick={onRetry}
              className="mt-3"
              icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden />}
            >
              Retry
            </Button>
          )}
        </>
      ) : (
        "Building the fabric graph…"
      )}
    </div>
  );
}

function TopologyView() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [terminal, setTerminal] = useState<{ id: string; name: string } | null>(null);
  const [view, setView] = useState<LinkView>("observed");

  // Which management environment's fabric this is. Every NV-CM environment
  // bundles its own Nautobot, so the slug decides which devices and cabling we
  // render — without it the page would always draw the primary environment's
  // fabric no matter which card the operator opened it from. Absent (e.g. an
  // old bookmark) still means "tenant default", which is the previous
  // behaviour.
  const searchParams = useSearchParams();
  const env = searchParams.get("env") ?? undefined;
  const envLabel = searchParams.get("env_name") ?? env;

  const { data, isLoading, error, isFetching, refetch } = useQuery({
    // Env in the key: two environments' graphs must not share a cache entry.
    queryKey: ["topology", env ?? null],
    queryFn: () => api.network.topology(env),
    refetchInterval: 10000,
  });

  const selected: TopoNode | null = useMemo(
    () => data?.nodes.find((n) => n.id === selectedId) ?? null,
    [data, selectedId]
  );

  // The API returns the reconciled union; each view draws a subset.
  const shownLinks = useMemo(() => {
    const all = data?.links ?? [];
    return view === "observed"
      ? all.filter((l) => l.source_observed || l.target_observed)
      : all.filter((l) => l.intended);
  }, [data, view]);

  const plannedCount = useMemo(
    () => (data?.links ?? []).filter((l) => l.intended).length,
    [data]
  );

  const count = (status: TopoLinkStatus) =>
    shownLinks.filter((l) => l.status === status).length;

  // 403 → the tenant doesn't have the feature flag on.
  const forbidden =
    (error instanceof ApiError && error.status === 403) ||
    (error instanceof Error && error.message.startsWith("403"));

  return (
    <div className="space-y-5 max-w-[1440px]">
      {/* Reachable both from the hub and straight off the sidebar, so this
          links to the section rather than calling router.back() — which would
          go somewhere arbitrary (or out of the app) for the latter. Points at
          the hub tab, not bare /network: the latter renders the section with
          no tab bar, which looks like the tabs vanished. */}
      <Link
        href={HUB.network}
        className="text-xs text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> Network
      </Link>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Waypoints className="h-6 w-6 text-accent-cyan" />
            Fabric topology
            {envLabel && (
              <span className="rounded-full border border-line px-2.5 py-0.5 text-xs font-normal text-muted">
                {envLabel}
              </span>
            )}
          </h1>
          <p className="mt-1 text-sm text-muted">
            {data && data.nodes.length > 0 ? (
              <>
                The live network fabric
                {envLabel ? ` in ${envLabel}` : ""} — links coloured from observed
                LLDP adjacency and device telemetry. Click a node to inspect it or
                open a shell.
              </>
            ) : (
              <>
                A live map of your network fabric
                {envLabel ? ` in ${envLabel}` : ""}: devices, the links between
                them and their health, from LLDP and device telemetry.
              </>
            )}
          </p>
        </div>
        {!forbidden && (
        <div className="flex flex-wrap items-center gap-3">
          {data && (
            <div className="text-xs text-muted">
              {data.nodes.length} nodes · {shownLinks.length} links ·{" "}
              <span className="text-accent-emerald">{count("up")} up</span>
              {count("down") > 0 && (
                <span className="text-[color:var(--critical)]">
                  {" "}
                  · {count("down")} down
                </span>
              )}
              {count("mismatch") > 0 && (
                <span className="text-[color:var(--warning)]">
                  {" "}
                  · {count("mismatch")} miscabled
                </span>
              )}
              {count("mgmt") > 0 && <> · {count("mgmt")} mgmt</>}
            </div>
          )}
          <div
            className="flex items-center rounded-lg border border-line p-0.5 text-xs"
            role="tablist"
            aria-label="Link source"
          >
            {(
              [
                ["observed", "Observed", "(LLDP)", "Links seen on the wire via LLDP"],
                ["planned", "Planned", "(Nautobot)", "The cabling plan in your source of truth"],
              ] as [LinkView, string, string, string][]
            ).map(([v, label, source, hint]) => (
              <button
                key={v}
                type="button"
                role="tab"
                title={hint}
                aria-selected={view === v}
                onClick={() => setView(v)}
                className={cn(
                  "rounded-md px-2.5 min-h-[28px] whitespace-nowrap transition-colors",
                  view === v
                    ? "bg-bg-elevated text-[color:var(--text)]"
                    : "text-muted hover:text-[color:var(--text)]"
                )}
              >
                {label}
                <span className="hidden sm:inline"> {source}</span>
              </button>
            ))}
          </div>
          <Button
            size="sm"
            onClick={() => refetch()}
            icon={<RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin" : ""}`} aria-hidden />}
          >
            Refresh
          </Button>
        </div>
        )}
      </div>

      {/* Legend: only meaningful when there's a graph to read. */}
      {!forbidden && !error && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-muted">
          {LEGEND[view].map((l) => (
            <span key={l.status} className="inline-flex items-center gap-2">
              <LegendSwatch status={l.status} />
              {l.label}
            </span>
          ))}
        </div>
      )}

      {forbidden ? (
        <div className="rounded-2xl border border-line bg-bg-card p-8 text-center">
          <Waypoints className="mx-auto mb-3 h-8 w-8 text-muted" />
          <p className="text-sm text-[color:var(--text)]">
            Live topology isn’t enabled for this workspace yet.
          </p>
          <p className="mt-1 text-sm text-muted">
            A tenant admin can turn on the fabric view for this workspace.
          </p>
          <Link
            href="/settings?tab=team"
            className="mt-3 inline-flex text-sm text-accent hover:underline"
          >
            See your workspace admins in Settings
          </Link>
        </div>
      ) : error ? (
        <LoadState
          query={{ isLoading: false, isError: true, error, refetch }}
        >
          {null}
        </LoadState>
      ) : isLoading ? (
        <GraphLoading envLabel={envLabel} onRetry={() => refetch()} />
      ) : data && data.nodes.length === 0 ? (
        <div className="rounded-2xl border border-line bg-bg-card p-10 text-center text-sm text-muted">
          No devices in the source of truth yet.{" "}
          <Link href={HUB.onboarding} className="underline">
            Onboard your fabric
          </Link>{" "}
          to see it here.
        </div>
      ) : data ? (
        <>
          {data.degraded && (
            <div className="flex items-center gap-2 rounded-xl border border-signal-warning/40 bg-signal-warning/10 px-3 py-2 text-xs text-[color:var(--warning)]">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              {data.degraded}
            </div>
          )}
          {view === "observed" && shownLinks.length === 0 && plannedCount > 0 && (
            <div className="rounded-xl border border-line bg-bg-card px-3 py-2 text-xs text-muted">
              No links observed via LLDP yet — switch to{" "}
              <button
                type="button"
                className="text-accent-cyan hover:underline"
                onClick={() => setView("planned")}
              >
                Planned (Nautobot)
              </button>{" "}
              to see the {plannedCount} intended link{plannedCount === 1 ? "" : "s"}{" "}
              from the cabling plan.
            </div>
          )}
          <div className="flex items-start gap-4">
            <div className="min-w-0 flex-1">
              <FabricGraph
                nodes={data.nodes}
                links={shownLinks}
                selectedId={selectedId}
                onSelect={(id) => setSelectedId(id)}
              />
            </div>
            {selected && (
              <NodeDrawer
                node={selected}
                env={env}
                onClose={() => setSelectedId(null)}
                onOpenTerminal={(d) => setTerminal(d)}
              />
            )}
          </div>
        </>
      ) : null}

      {terminal && (
        <DeviceTerminal
          device={terminal}
          env={env}
          onClose={() => setTerminal(null)}
        />
      )}
    </div>
  );
}
