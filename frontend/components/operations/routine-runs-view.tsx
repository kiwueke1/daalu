"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Activity, GitPullRequest } from "lucide-react";
import { api } from "@/lib/api";
import { formatRelative } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";

/**
 * Routine runs — history of reconciler + agent activity touching SoT.
 *
 * Backed by the events stream filtered to ``module=sot`` (the reconciler
 * publishes ``device.observed.snapshot`` events) plus the most recent
 * drift proposals. Together they form the operator's audit timeline.
 */
export function RoutineRunsView() {
  const eventsQ = useQuery({
    queryKey: ["events", "sot"],
    queryFn: () => api.events.list({ module: "sot", limit: 50 }),
    refetchInterval: 15_000,
  });
  const driftQ = useQuery({
    queryKey: ["change-proposals", "drift-all"],
    queryFn: () => api.changeProposals.list({ kind: "drift", limit: 50 }),
    refetchInterval: 30_000,
  });

  const events = eventsQ.data;
  const driftProps = driftQ.data;
  // One state for both reads: loading until both answer, the first error wins.
  const combined = {
    isLoading: eventsQ.isLoading || driftQ.isLoading,
    isError: eventsQ.isError || driftQ.isError,
    error: eventsQ.error ?? driftQ.error,
    refetch: () => {
      eventsQ.refetch();
      driftQ.refetch();
    },
  };

  // Merge both streams into one chronological list.
  type Row =
    | {
        kind: "event";
        id: string;
        when: string;
        summary: string;
        severity: string;
      }
    | {
        kind: "proposal";
        id: string;
        when: string;
        device_id: string;
        status: string;
      };

  const rows: Row[] = [
    ...(events ?? []).map<Row>((e) => ({
      kind: "event",
      id: e.id,
      when: e.occurred_at,
      summary: e.summary,
      severity: e.severity,
    })),
    ...(driftProps ?? []).map<Row>((p) => ({
      kind: "proposal",
      id: p.id,
      when: p.created_at,
      device_id: p.device_id,
      status: p.status,
    })),
  ].sort((a, b) => b.when.localeCompare(a.when));

  return (
    <div className="space-y-4">
      <p className="text-muted text-sm">
        Every automated action that touched the Source of Truth: reconciler
        snapshots, drift detections and executor runs. Use it to check what
        the reconciler saw, and when.
      </p>

      <LoadState
        query={combined}
        loadingText="Loading routine runs…"
        empty={rows.length === 0}
        emptyTitle="No routine runs recorded yet"
        emptyHint="The reconciler runs every 5 minutes. If you just connected Nautobot, give it a few cycles."
      >
      <div className="space-y-1">
        {rows.map((r) => (
          <RoutineRow key={`${r.kind}-${r.id}`} row={r} />
        ))}
      </div>
      </LoadState>
    </div>
  );
}

function RoutineRow({
  row,
}: {
  row:
    | {
        kind: "event";
        id: string;
        when: string;
        summary: string;
        severity: string;
      }
    | {
        kind: "proposal";
        id: string;
        when: string;
        device_id: string;
        status: string;
      };
}) {
  if (row.kind === "event") {
    return (
      <div className="flex items-start gap-3 px-3 py-2 rounded-lg border border-line/40 hover:bg-bg-elevated/40 text-xs">
        <Activity className="h-3.5 w-3.5 mt-0.5 text-accent-cyan shrink-0" aria-hidden />
        <div className="flex-1 min-w-0">
          <div className="truncate">{row.summary}</div>
          <div className="text-xs text-muted mt-0.5">
            {row.severity} · {formatRelative(row.when)}
          </div>
        </div>
      </div>
    );
  }
  return (
    <Link
      href={`/proposals/${row.id}`}
      className="flex items-start gap-3 px-3 py-2 rounded-lg border border-line/40 hover:bg-bg-elevated/40 text-xs"
    >
      <GitPullRequest className="h-3.5 w-3.5 mt-0.5 text-[color:var(--warning)] shrink-0" aria-hidden />
      <div className="flex-1 min-w-0">
        <div className="truncate">
          Drift proposal opened — device{" "}
          <span className="font-mono">{row.device_id.slice(0, 8)}</span>
        </div>
        <div className="text-xs text-muted mt-0.5">
          {row.status} · {formatRelative(row.when)}
        </div>
      </div>
    </Link>
  );
}
