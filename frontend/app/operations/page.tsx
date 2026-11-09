"use client";

/**
 * /operations — the Source of Truth hub.
 *
 * Tabbed layout matches docs/book-customer/03-core-concepts/12-source-of-truth.md
 * and the chapter-17 walkthrough. Sub-pages (/devices, /proposals) used
 * to be separate sidebar entries; they now redirect here and the tabs
 * own the surface area.
 */

import { Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import { LayoutGrid } from "lucide-react";
import { api } from "@/lib/api";
import { LoadState } from "@/components/ui/load-state";
import { OverviewView } from "@/components/operations/overview-view";
import { DevicesView } from "@/components/operations/devices-view";
import { DriftView } from "@/components/operations/drift-view";
import { ProposalsView } from "@/components/operations/proposals-view";
import { ImportView } from "@/components/operations/import-view";
import { RoutineRunsView } from "@/components/operations/routine-runs-view";
import { TabBar, useUrlTab } from "@/components/ui/tabs";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "devices", label: "Devices" },
  { id: "drift", label: "Drift" },
  { id: "proposals", label: "Proposals" },
  { id: "bulk-import", label: "Bulk import" },
  { id: "routine-runs", label: "Routine runs" },
] as const;

type TabId = (typeof TABS)[number]["id"];

const TAB_IDS = TABS.map((t) => t.id);

function OperationsPageInner() {
  const [tab, onSelect] = useUrlTab<TabId>(TAB_IDS, "overview");
  // Open drift needs an operator decision: badge the Drift tab from any tab
  // (same query key as the Overview/Drift views, so it's one shared fetch).
  const driftQ = useQuery({
    queryKey: ["change-proposals", "drift"],
    queryFn: () =>
      api.changeProposals.list({ kind: "drift", status: "pending" }),
    refetchInterval: 15_000,
  });
  // Without Nautobot the SoT tabs show a setup step, so a drift count would
  // contradict them (it can be stale or seeded data): no badge until it's set
  // up. Same query key as the Overview/Devices views, so no extra fetch.
  const devicesQ = useQuery({
    queryKey: ["sot-devices"],
    queryFn: () => api.sot.devices.list(),
  });
  const sotReady = !!devicesQ.data;
  const openDrift = sotReady ? driftQ.data?.length ?? 0 : 0;

  return (
    <div className="space-y-6 max-w-[1400px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <LayoutGrid className="h-5 w-5 text-accent-cyan" aria-hidden /> Operations
        </h1>
        <p className="text-muted text-sm mt-1">
          Devices, drift, change proposals and bulk inventory: your Source of
          Truth in one place.
        </p>
      </div>

      <TabBar
        tabs={TABS.map(({ id, label }) =>
          id === "drift" && openDrift > 0
            ? { id, label, badge: openDrift, badgeTone: "accent" as const }
            : { id, label },
        )}
        active={tab}
        onChange={onSelect}
        ariaLabel="Operations"
      />

      <div key={tab} className="motion-enter">
        {tab === "overview" && (
          <OverviewView onJumpTo={(id) => onSelect(id as TabId)} />
        )}
        {tab === "devices" && <DevicesView />}
        {tab === "drift" && <DriftView />}
        {tab === "proposals" && <ProposalsView />}
        {tab === "bulk-import" && <ImportView urlSync />}
        {tab === "routine-runs" && <RoutineRunsView />}
      </div>
    </div>
  );
}

export default function OperationsPage() {
  // useSearchParams must be wrapped in Suspense per Next 14's app-router rules.
  return (
    <Suspense
      fallback={
        <LoadState query={{ isLoading: true, isError: false }}>{null}</LoadState>
      }
    >
      <OperationsPageInner />
    </Suspense>
  );
}
