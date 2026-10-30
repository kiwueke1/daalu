"use client";

import { Suspense } from "react";
import { Boxes, Cable, Network, Sparkles } from "lucide-react";
import { TabBar, useUrlTab } from "@/components/ui/tabs";
import ClustersPage from "@/components/managed-infra/clusters-view";
import NetworkPage from "@/components/managed-infra/network-view";
import OnboardingPage from "@/app/onboarding/page";
import { IntegrationsView } from "@/components/integrations/integrations-view";

/**
 * /managed-infra — a single home for the closely-related infra surfaces
 * that used to be separate sidebar entries: cloud accounts +
 * observability + Kubernetes clusters (Managed infra), the per-tenant
 * NV-CM stack (Network & servers), the guided integration wizard
 * (Onboarding), and external integrations (channels, ingest sources,
 * BYO SoT). They're all "wire up / inspect the things Daalu reads
 * from", so they live here as tabs.
 *
 * Only the active tab mounts, so a tab's data fetching doesn't run until
 * it's opened. /clusters and /network redirect to their tab here;
 * /onboarding still resolves directly. The active tab syncs to
 * ?tab= (same pattern as /operations) so tabs are deep-linkable —
 * /integrations redirects to ?tab=integrations.
 */

type Tab = "clusters" | "network" | "onboarding" | "integrations";

const TABS: { key: Tab; label: string; icon: typeof Boxes }[] = [
  { key: "clusters", label: "Clusters & observability", icon: Boxes },
  { key: "network", label: "Network & servers", icon: Network },
  { key: "onboarding", label: "Onboarding", icon: Sparkles },
  { key: "integrations", label: "Integrations", icon: Cable },
];

const TAB_IDS = TABS.map((t) => t.key);

function ManagedInfraHubInner() {
  const [tab, setTab] = useUrlTab<Tab>(TAB_IDS, "clusters");

  return (
    <div className="space-y-6">
      <TabBar
        tabs={TABS.map(({ key, label, icon }) => ({ id: key, label, icon }))}
        active={tab}
        onChange={setTab}
        ariaLabel="Managed infrastructure"
      />

      <div>
        {tab === "clusters" && <ClustersPage />}
        {tab === "network" && <NetworkPage />}
        {tab === "onboarding" && <OnboardingPage />}
        {tab === "integrations" && <IntegrationsView />}
      </div>
    </div>
  );
}

export default function ManagedInfraHub() {
  // useSearchParams requires a Suspense boundary at the page level.
  return (
    <Suspense fallback={null}>
      <ManagedInfraHubInner />
    </Suspense>
  );
}
