"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { ValidatePanel } from "../components/validate-panel";
import { FactoryGate, FactoryPageShell } from "../components/factory-page-shell";
import { LoadState } from "@/components/ui/load-state";

/**
 * /ai-factory/observability — the metrics-pipeline self-check (exporters,
 * scrape targets, queryability). Admin-only; results live here.
 */
export default function ObservabilityPage() {
  const { user } = useAuth();
  const isAdmin = user?.is_admin ?? false;
  const overview = useQuery({
    queryKey: ["aiFactory", "overview"],
    queryFn: api.aiFactory.overview,
  });
  const role = overview.data?.role ?? "none";

  return (
    <FactoryPageShell
      title="Observability check"
      subtitle="End-to-end self-check of the metrics pipeline so you can confirm the factory is wired up before trusting the dashboards."
    >
      <LoadState query={overview} loadingText="Checking your access…">
        {!isAdmin || role === "none" ? (
          <FactoryGate message="The observability self-check is available to tenant admins with a GPU." />
        ) : (
          <ValidatePanel />
        )}
      </LoadState>
    </FactoryPageShell>
  );
}
