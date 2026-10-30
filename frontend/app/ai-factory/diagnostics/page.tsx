"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { DiagnosticsPanel } from "../components/diagnostics-panel";
import { FactoryGate, FactoryPageShell } from "../components/factory-page-shell";
import { LoadState } from "@/components/ui/load-state";

/**
 * /ai-factory/diagnostics — dcgmi diag & NCCL run history + run controls.
 * Admin-only (matches the AI Factory overview gating). Results live here, not
 * on the overview.
 */
export default function DiagnosticsPage() {
  const { user } = useAuth();
  const isAdmin = user?.is_admin ?? false;
  const overview = useQuery({
    queryKey: ["aiFactory", "overview"],
    queryFn: api.aiFactory.overview,
  });
  const role = overview.data?.role ?? "none";
  const gpuClass = overview.data?.gpu_class ?? null;

  return (
    <FactoryPageShell
      title="Diagnostics"
      subtitle="Run dcgmi diag (quick health pass) or the NCCL interconnect test against the factory floor, and review past runs."
    >
      <LoadState query={overview} loadingText="Checking your access…">
        {!isAdmin || role === "none" ? (
          <FactoryGate message="GPU diagnostics are available to tenant admins with a GPU." />
        ) : (
          <DiagnosticsPanel gpuClass={gpuClass} />
        )}
      </LoadState>
    </FactoryPageShell>
  );
}
