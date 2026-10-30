"use client";

import { useQuery } from "@tanstack/react-query";
import { Workflow as WorkflowIcon } from "lucide-react";
import { api, type WorkflowRun } from "@/lib/api";
import { formatRelative } from "@/lib/utils";

/**
 * Active workflows tile — illuminated glass surface, emerald progress
 * bars with a subtle inner glow so they read as lit conduits.
 */
export function ActiveWorkflowsCard() {
  const { data } = useQuery({
    queryKey: ["workflow-runs", "active"],
    queryFn: () => api.workflows.runs(),
    refetchInterval: 12_000,
  });

  const runs = (data ?? [])
    .filter(
      (r) =>
        r.status === "running" ||
        r.status === "waiting_for_approval" ||
        r.status === "succeeded"
    )
    .slice(0, 5);

  return (
    <div className="surface p-4">
      <div className="flex items-center gap-2 text-sm font-medium mb-4">
        <WorkflowIcon className="h-4 w-4" style={{ color: "var(--accent)" }} />
        Active workflows
      </div>
      <div className="space-y-3.5">
        {runs.length === 0 && (
          <div className="text-xs text-muted">Nothing running right now.</div>
        )}
        {runs.map((w) => (
          <WorkflowRow key={w.id} run={w} />
        ))}
      </div>
    </div>
  );
}

function WorkflowRow({ run }: { run: WorkflowRun }) {
  const pct = progressFor(run);
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <div className="min-w-0">
          <div className="text-[13px] font-medium truncate">
            {prettyName(run.workflow_name)}
          </div>
          <div className="text-[10px] text-muted">
            {labelFor(run)} · {formatRelative(run.started_at || run.finished_at)}
          </div>
        </div>
        <div className="text-[11px] text-muted">{pct}%</div>
      </div>
      <div
        className="relative h-1.5 rounded-full overflow-hidden"
        style={{
          background: "rgba(255,255,255,0.04)",
          boxShadow: "inset 0 1px 1px rgba(0,0,0,0.4)",
        }}
      >
        <div
          className="absolute inset-y-0 left-0 rounded-full transition-all duration-500"
          style={{
            width: `${pct}%`,
            background:
              "linear-gradient(90deg, var(--accent) 0%, color-mix(in srgb, var(--accent) 60%, transparent) 100%)",
            boxShadow: "0 0 12px var(--accent-glow), inset 0 0 6px rgba(255,255,255,0.18)",
          }}
        />
      </div>
    </div>
  );
}

function prettyName(name: string) {
  return name
    .replace(/\./g, " · ")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function progressFor(r: WorkflowRun): number {
  if (r.status === "succeeded") return 100;
  if (r.status === "failed") return 100;
  if (r.status === "waiting_for_approval") return 50;
  if (r.status === "running") return 70;
  return 25;
}

function labelFor(r: WorkflowRun): string {
  if (r.status === "succeeded") return "Completed";
  if (r.status === "failed") return "Failed";
  if (r.status === "waiting_for_approval") return "Waiting for approval";
  if (r.status === "running") return "Running";
  return r.status;
}
