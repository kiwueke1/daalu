"use client";

import { Suspense, useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FileText, RotateCcw } from "lucide-react";
import { api } from "@/lib/api";
import { ReportsQueryTab } from "@/components/reports/query-tab";
import { ReportsDashboardsTab } from "@/components/reports/dashboards-tab";
import { TabBar, TabPanel, useUrlTab } from "@/components/ui/tabs";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

const TABS = [
  { id: "briefings", label: "Briefings" },
  { id: "query", label: "Query" },
  { id: "dashboards", label: "Dashboards" },
  { id: "schedules", label: "Schedules" },
] as const;
type Tab = (typeof TABS)[number]["id"];
const TAB_IDS = TABS.map((t) => t.id);

function ReportsPageInner() {
  const router = useRouter();
  const [tab, setTab] = useUrlTab<Tab>(TAB_IDS, "briefings");

  return (
    <div className="space-y-6 max-w-[1200px]">
      <header>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <FileText className="h-5 w-5 text-accent" aria-hidden /> Reports
        </h1>
        <p className="text-muted text-sm mt-1">
          Daily AI briefings, ad-hoc queries over your events, and reports
          delivered on a schedule.
        </p>
      </header>

      <TabBar
        tabs={TABS.map(({ id, label }) => ({ id, label }))}
        active={tab}
        // Schedules has its own page; the tab takes you there.
        onChange={(id) => (id === "schedules" ? router.push("/reports/schedules") : setTab(id))}
        ariaLabel="Reports sections"
      />

      <TabPanel id={tab}>
        {tab === "query" ? (
          <ReportsQueryTab />
        ) : tab === "dashboards" ? (
          <ReportsDashboardsTab />
        ) : (
          <BriefingsTab />
        )}
      </TabPanel>
    </div>
  );
}

export default function ReportsPage() {
  return (
    <Suspense fallback={null}>
      <ReportsPageInner />
    </Suspense>
  );
}

function BriefingsTab() {
  const qc = useQueryClient();
  const toast = useToast();
  const router = useRouter();
  const q = useQuery({
    queryKey: ["briefings", "infra"],
    queryFn: () => api.briefings.list("infra"),
  });

  const generate = useMutation({
    mutationFn: () => api.briefings.generate("infra"),
    onSuccess: (b) => {
      qc.invalidateQueries({ queryKey: ["briefings"] });
      qc.invalidateQueries({ queryKey: ["briefing"] });
      toast.success("New briefing ready.", {
        action: b?.id ? { label: "Open", onClick: () => router.push(`/reports/briefings/${b.id}`) } : undefined,
      });
    },
    onError: (e) => toast.error(errorText(e)),
  });

  useEffect(() => {
    // Refresh stale list whenever the user re-enters this tab.
    qc.invalidateQueries({ queryKey: ["briefings", "infra"] });
  }, [qc]);

  const data = q.data;
  const latest = data?.[0];
  const earlier = (data ?? []).slice(1);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">
          Daalu writes a briefing every day from your events. Generate one now
          to cover what changed since the last one.
        </p>
        {!q.isLoading && (
          <Button
            size="sm"
            variant={latest ? "secondary" : "primary"}
            onClick={() => generate.mutate()}
            pending={generate.isPending}
            icon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}
          >
            {generate.isPending
              ? "Generating… (about a minute)"
              : latest
              ? "Generate a new briefing"
              : "Generate today's briefing"}
          </Button>
        )}
      </div>

      <LoadState
        query={q}
        loadingText="Loading briefings…"
        empty={!latest}
        emptyTitle="No briefings yet"
        emptyHint={
          <>
            Click <span className="text-fg">Generate today&apos;s briefing</span>{" "}
            above, or run <code className="text-fg">daalu briefing infra</code>{" "}
            from the CLI.
          </>
        }
      >
        {latest && (
          <Link
            key={latest.id}
            href={`/reports/briefings/${latest.id}`}
            className="motion-enter block gradient-border p-6 lg:p-8 bg-hero-grad hover:opacity-95 transition-opacity"
          >
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-accent mb-2">
              <FileText className="h-3.5 w-3.5" aria-hidden /> Latest · {latest.channel} ·{" "}
              {latest.coverage_date}
            </div>
            <h2 className="text-2xl font-semibold mb-3">{latest.title}</h2>
            <p className="text-muted line-clamp-3">{latest.summary}</p>
            <span className="mt-3 inline-block text-xs text-muted">Open the briefing →</span>
          </Link>
        )}
      </LoadState>

      {earlier.length > 0 && (
        <section>
          <h3 className="text-sm uppercase tracking-wider text-muted mb-3">
            Earlier
          </h3>
          <ul className="space-y-2">
            {earlier.slice(0, 20).map((b) => (
              <li key={b.id}>
                <Link
                  href={`/reports/briefings/${b.id}`}
                  className="block w-full text-left rounded-xl border border-line bg-bg-card p-3 text-sm hover:border-accent/60 hover:bg-bg-elevated/60 transition-colors"
                >
                  <div className="text-muted text-xs">{b.coverage_date}</div>
                  <div className="font-medium">{b.title}</div>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
