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
import { LayoutGrid } from "lucide-react";
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

  return (
    <div className="space-y-6 max-w-[1400px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <LayoutGrid className="h-5 w-5 text-accent-cyan" /> Operations
        </h1>
        <p className="text-muted text-sm mt-1">
          Devices, drift detection, change proposals, and bulk inventory — your
          Source of Truth in one place.
        </p>
      </div>

      <TabBar
        tabs={TABS.map(({ id, label }) => ({ id, label }))}
        active={tab}
        onChange={onSelect}
        ariaLabel="Operations"
      />

      <div>
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
    <Suspense fallback={<div className="p-6 text-sm text-muted">Loading…</div>}>
      <OperationsPageInner />
    </Suspense>
  );
}
