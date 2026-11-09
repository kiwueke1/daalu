"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { AiperfPanel } from "../components/aiperf-panel";
import { FactoryGate, FactoryPageShell } from "../components/factory-page-shell";
import { LoadState } from "@/components/ui/load-state";

/**
 * /ai-factory/benchmark — AIPerf concurrency sweep (SLO curve) + run history.
 * Shown to the platform superuser or a GPU owner/provider; the backend pins
 * each scope to its allowed target. Results live here, not on the overview.
 */
export default function BenchmarkPage() {
  const { user } = useAuth();
  const isSuperuser = user?.is_superuser ?? false;
  const overview = useQuery({
    queryKey: ["aiFactory", "overview"],
    queryFn: api.aiFactory.overview,
  });
  const role = overview.data?.role ?? "none";
  const isHardware = role === "owner" || role === "provider";

  return (
    <FactoryPageShell
      title="Performance benchmarking"
      subtitle="Measure how fast your model answers as more users send requests at once: time to first token, time between tokens and throughput (AIPerf)."
    >
      <LoadState query={overview} loadingText="Checking your access…">
        {!(isSuperuser || isHardware) ? (
          <FactoryGate message="Benchmarking is available to site operators and GPU owners/providers." />
        ) : (
          <AiperfPanel />
        )}
      </LoadState>
    </FactoryPageShell>
  );
}
