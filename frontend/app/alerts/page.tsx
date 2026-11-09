"use client";

import { Suspense } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { api, type Alert } from "@/lib/api";
import { AlertTile } from "@/components/alerts/alert-tile";
import { TabBar, TabPanel, useUrlTab } from "@/components/ui/tabs";
import { LoadState } from "@/components/ui/load-state";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

const TABS = [
  { id: "open", label: "Open" },
  { id: "acknowledged", label: "Acknowledged" },
  { id: "resolved", label: "Resolved" },
] as const;
type Status = (typeof TABS)[number]["id"];
const TAB_IDS = TABS.map((t) => t.id);

const SEVERITY_RANK: Record<Alert["severity"], number> = {
  critical: 0,
  warning: 1,
  info: 2,
};

/** Most severe first, then most recently seen, so the order is stable. */
function sortAlerts(items: Alert[]): Alert[] {
  const seen = (a: Alert) => new Date(a.last_seen_at ?? a.created_at).getTime();
  return [...items].sort(
    (a, b) =>
      (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) ||
      seen(b) - seen(a) ||
      a.id.localeCompare(b.id)
  );
}

const EMPTY_HINT: Record<Status, string> = {
  open: "Nothing is firing. Alerts appear here when a connected source reports a problem.",
  acknowledged: "Acknowledged alerts are ones someone is looking at; none right now.",
  resolved: "Alerts you or Daalu close show up here.",
};

function AlertsPageInner() {
  const [tab, setTab] = useUrlTab<Status>(TAB_IDS, "open");
  const qc = useQueryClient();
  const toast = useToast();

  // Open + acknowledged always load, so their tabs can show counts.
  const openQ = useQuery({
    queryKey: ["alerts", "open"],
    queryFn: () => api.alerts.list({ status: "open" }),
    refetchInterval: 10_000,
  });
  const ackQ = useQuery({
    queryKey: ["alerts", "acknowledged"],
    queryFn: () => api.alerts.list({ status: "acknowledged" }),
    refetchInterval: 10_000,
  });
  const resolvedQ = useQuery({
    queryKey: ["alerts", "resolved"],
    queryFn: () => api.alerts.list({ status: "resolved" }),
    refetchInterval: 10_000,
    enabled: tab === "resolved",
  });
  const q = tab === "open" ? openQ : tab === "acknowledged" ? ackQ : resolvedQ;

  const ack = useMutation({
    mutationFn: (id: string) => api.alerts.acknowledge(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alerts"] });
      toast.success("Alert acknowledged. It stays open under Acknowledged.");
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const resolve = useMutation({
    mutationFn: (id: string) => api.alerts.resolve(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alerts"] });
      toast.success("Alert resolved. Its investigation has stopped.");
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const openCount = openQ.data?.length;
  const openCritical = (openQ.data ?? []).filter((a) => a.severity === "critical").length;
  const items = sortAlerts(q.data ?? []);

  return (
    <div className="space-y-6 max-w-[1200px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <AlertTriangle aria-hidden className="h-5 w-5 text-accent" /> Alerts
        </h1>
        <p className="text-muted text-sm mt-1">
          Problems Daalu picked out of your event stream, most severe first.
          Open one to see the investigation and the proposed fix.
        </p>
      </div>

      <TabBar
        tabs={TABS.map(({ id, label }) => ({
          id,
          label,
          badge:
            id === "open"
              ? openCount || undefined
              : id === "acknowledged"
              ? ackQ.data?.length || undefined
              : undefined,
          badgeTone: id === "open" && openCritical > 0 ? "accent" : "muted",
        }))}
        active={tab}
        onChange={setTab}
        ariaLabel="Alert status"
      />

      <LoadState
        query={q}
        loadingText="Loading alerts…"
        empty={items.length === 0}
        emptyTitle={`No ${tab} alerts`}
        emptyHint={EMPTY_HINT[tab]}
      >
        <TabPanel id={tab} className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {items.map((a) => (
            <AlertTile
              key={a.id}
              alert={a}
              onAcknowledge={() => ack.mutate(a.id)}
              onResolve={() => resolve.mutate(a.id)}
              resolving={resolve.isPending && resolve.variables === a.id}
            />
          ))}
        </TabPanel>
      </LoadState>
    </div>
  );
}

export default function AlertsPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted">Loading…</div>}>
      <AlertsPageInner />
    </Suspense>
  );
}
