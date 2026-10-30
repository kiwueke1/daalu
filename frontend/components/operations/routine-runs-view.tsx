"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Activity, GitPullRequest } from "lucide-react";
import { api } from "@/lib/api";
import { formatRelative } from "@/lib/utils";

/**
 * Routine runs — history of reconciler + agent activity touching SoT.
 *
 * Backed by the events stream filtered to ``module=sot`` (the reconciler
 * publishes ``device.observed.snapshot`` events) plus the most recent
 * drift proposals. Together they form the operator's audit timeline.
 */
export function RoutineRunsView() {
  const { data: events, isLoading: eventsLoading } = useQuery({
    queryKey: ["events", "sot"],
    queryFn: () => api.events.list({ module: "sot", limit: 50 }),
    refetchInterval: 15_000,
  });
  const { data: driftProps, isLoading: driftLoading } = useQuery({
    queryKey: ["change-proposals", "drift-all"],
    queryFn: () => api.changeProposals.list({ kind: "drift", limit: 50 }),
    refetchInterval: 30_000,
  });

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
        Every automated action that touched the Source of Truth — reconciler
        snapshots, drift detections, executor runs. Useful when you&apos;re
        debugging &quot;why didn&apos;t the reconciler catch X?&quot;
      </p>

      {(eventsLoading || driftLoading) && (
        <div className="text-sm text-muted">Loading routine runs…</div>
      )}

      {!eventsLoading && !driftLoading && rows.length === 0 && (
        <div className="surface p-6 text-center text-sm text-muted">
          No routine runs recorded yet. The reconciler runs every 5 minutes; if
          you just connected Nautobot, give it a few cycles.
        </div>
      )}

      <div className="space-y-1">
        {rows.map((r) => (
          <RoutineRow key={`${r.kind}-${r.id}`} row={r} />
        ))}
      </div>
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
        <Activity className="h-3.5 w-3.5 mt-0.5 text-accent-cyan shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="truncate">{row.summary}</div>
          <div className="text-[10px] text-muted mt-0.5">
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
      <GitPullRequest className="h-3.5 w-3.5 mt-0.5 text-[color:var(--warning)] shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="truncate">
          Drift proposal opened — device{" "}
          <span className="font-mono">{row.device_id.slice(0, 8)}</span>
        </div>
        <div className="text-[10px] text-muted mt-0.5">
          {row.status} · {formatRelative(row.when)}
        </div>
      </div>
    </Link>
  );
}
