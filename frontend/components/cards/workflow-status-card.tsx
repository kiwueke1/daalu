"use client";

import { useQuery } from "@tanstack/react-query";
import { Workflow as WorkflowIcon } from "lucide-react";
import { api } from "@/lib/api";

export function WorkflowStatusCard() {
  const { data } = useQuery({
    queryKey: ["workflow-runs", "summary"],
    queryFn: () => api.workflows.runs(),
    refetchInterval: 15_000,
  });
  const runs = data ?? [];
  const counts = {
    running: runs.filter((r) => r.status === "running").length,
    succeeded: runs.filter((r) => r.status === "succeeded").length,
    failed: runs.filter((r) => r.status === "failed").length,
    waiting: runs.filter((r) => r.status === "waiting_for_approval").length,
  };

  return (
    <div className="rounded-2xl border border-line bg-bg-card p-4">
      <div className="flex items-center gap-2 text-sm font-medium mb-3">
        <WorkflowIcon className="h-4 w-4 text-accent-cyan" />
        Workflow status
      </div>
      <div className="grid grid-cols-4 gap-2 text-center text-xs">
        <Stat label="Running" value={counts.running} accent="text-accent-blue" />
        <Stat label="Waiting" value={counts.waiting} accent="text-accent-amber" />
        <Stat label="Succeeded" value={counts.succeeded} accent="text-accent-emerald" />
        <Stat label="Failed" value={counts.failed} accent="text-accent-red" />
      </div>
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: number; accent: string }) {
  return (
    <div className="rounded-lg bg-bg-elevated/70 py-3">
      <div className={`text-xl font-semibold ${accent}`}>{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-muted mt-0.5">{label}</div>
    </div>
  );
}
