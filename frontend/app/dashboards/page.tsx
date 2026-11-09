"use client";

/**
 * /dashboards — the tenant's Grafana dashboards, rendered natively in Daalu.
 *
 * Grafana is the *catalog* (the list + each board's panel/PromQL definitions);
 * Daalu renders the panels itself by querying the tenant's Prometheus/Loki, so
 * the experience matches the AI-factory GPU panels — no Grafana detour. From
 * here an operator can open any board or generate a fresh one from a prompt
 * (the AI writes a Grafana dashboard, which then shows up in this same list).
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ChevronRight,
  Folder,
  LayoutDashboard,
  Plug,
  Sparkles,
} from "lucide-react";
import { api, type DashboardSummary } from "@/lib/api";
import { HUB } from "@/lib/hub-routes";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

export default function DashboardsPage() {
  const [showAi, setShowAi] = useState(false);

  const list = useQuery({
    queryKey: ["dashboards"],
    queryFn: () => api.dashboards.list(),
    refetchInterval: 60_000,
  });
  const data = list.data;

  const grouped = useMemo(() => {
    const map: Record<string, DashboardSummary[]> = {};
    for (const d of data?.dashboards ?? []) {
      const key = d.folder || "General";
      (map[key] ??= []).push(d);
    }
    return Object.entries(map).sort(([a], [b]) => a.localeCompare(b));
  }, [data]);

  return (
    <div className="space-y-6 max-w-[1200px]">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <LayoutDashboard className="h-5 w-5 text-accent-cyan" /> Dashboards
          </h1>
          <p className="text-muted text-sm mt-1 max-w-[760px]">
            Your Grafana dashboards, filled from your own Prometheus and Loki.
          </p>
        </div>
        <Button
          variant="primary"
          onClick={() => setShowAi(true)}
          className="shrink-0"
          icon={<Sparkles className="h-4 w-4" aria-hidden />}
        >
          Create with AI
        </Button>
      </div>

      {list.isLoading ? (
        <DashboardSkeleton />
      ) : (
      <LoadState query={list} loadingText="Loading dashboards…">
      <div className="space-y-6 motion-enter">
      {data && !data.configured && (
        <div className="rounded-2xl border border-accent-amber/40 bg-accent-amber/5 p-5">
          <div className="font-medium text-accent-amber flex items-center gap-2">
            <Plug className="h-4 w-4" /> Grafana not connected
          </div>
          <p className="text-sm text-muted mt-1 max-w-[640px]">
            {data.error ||
              "Add a Grafana integration (URL + API token) so Daalu can read your dashboards."}{" "}
            Connect it under{" "}
            <Link href={HUB.clusters} className="underline">
              Managed infra → Observability
            </Link>
            .
          </p>
        </div>
      )}

      {data && data.configured && data.dashboards.length === 0 && (
        <div className="rounded-2xl border border-line bg-bg-card p-8 text-center">
          <LayoutDashboard className="h-5 w-5 mx-auto text-muted" />
          <div className="mt-2 text-sm font-medium">No dashboards in Grafana yet</div>
          <p className="mt-1 text-sm text-muted">
            Describe what you want to watch and Daalu builds the board for you.
          </p>
          <Button
            variant="primary"
            onClick={() => setShowAi(true)}
            className="mt-3"
            icon={<Sparkles className="h-4 w-4" aria-hidden />}
          >
            Create with AI
          </Button>
        </div>
      )}

      {grouped.map(([folder, items]) => (
        <section key={folder} className="space-y-2">
          <div className="text-[11px] uppercase tracking-wider text-muted flex items-center gap-1.5">
            <Folder className="h-3.5 w-3.5" /> {folder}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {items.map((d) => (
              <Link
                key={d.uid}
                href={`/dashboards/${d.uid}`}
                className="group rounded-2xl border border-line bg-bg-card p-4 hover:border-accent-cyan/40 transition-colors"
              >
                <div className="flex items-center justify-between">
                  <LayoutDashboard className="h-4 w-4 text-accent-cyan" />
                  <ChevronRight className="h-4 w-4 text-muted group-hover:text-accent-cyan" />
                </div>
                <div className="mt-2 font-medium text-sm group-hover:text-accent-cyan">
                  {d.title}
                </div>
                {d.tags.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {d.tags.slice(0, 4).map((t) => (
                      <span
                        key={t}
                        className="text-[11px] px-1.5 py-0.5 rounded-md bg-bg-elevated/60 border border-line text-muted"
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </Link>
            ))}
          </div>
        </section>
      ))}
      </div>
      </LoadState>
      )}

      <AiCreateModal open={showAi} onClose={() => setShowAi(false)} />
    </div>
  );
}

/** Card-shaped placeholders while the list loads, so the grid doesn't jump. */
function DashboardSkeleton() {
  return (
    <div role="status" aria-label="Loading dashboards" className="space-y-2">
      <div className="h-3 w-24 rounded bg-bg-elevated/70 animate-pulse" />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="rounded-2xl border border-line bg-bg-card p-4 space-y-3">
            <div className="h-4 w-4 rounded bg-bg-elevated/70 animate-pulse" />
            <div className="h-3.5 w-2/3 rounded bg-bg-elevated/70 animate-pulse" />
            <div className="h-3 w-1/3 rounded bg-bg-elevated/70 animate-pulse" />
          </div>
        ))}
      </div>
      <span className="sr-only">Loading dashboards…</span>
    </div>
  );
}

function AiCreateModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const toast = useToast();
  const [prompt, setPrompt] = useState("");
  const [datasource, setDatasource] = useState<"prometheus" | "loki" | "mixed">(
    "prometheus"
  );
  const [title, setTitle] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.dashboards.aiCreate({
        prompt: prompt.trim(),
        datasource,
        title: title.trim() || null,
      }),
    onSuccess: (res) => {
      toast.success("Dashboard created. Opening it now.");
      router.push(`/dashboards/${res.uid}`);
    },
    onError: (err) => toast.error(errorText(err)),
  });

  const EXAMPLES = [
    "Cluster-wide CPU and memory by namespace, plus pod restart counts",
    "Ingress request rate, error rate, and p95 latency for the last 6h",
    "Node health: load, disk usage, network throughput, and unready nodes",
  ];
  const tooShort = prompt.trim().length < 4;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Create a dashboard with AI"
      description="Describe what you want to see. Daalu writes a Grafana dashboard and opens it here."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => create.mutate()}
            disabled={tooShort}
            disabledReason={tooShort ? "Describe what to show first" : undefined}
            pending={create.isPending}
            icon={<Sparkles className="h-4 w-4" aria-hidden />}
          >
            {create.isPending ? "Generating…" : "Generate dashboard"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <label className="text-xs space-y-1 block">
          <div className="text-muted uppercase tracking-wider">What to show</div>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={4}
            placeholder="e.g. CPU, memory and restart counts per namespace, plus deployments with unavailable replicas"
            className="w-full px-3 py-2 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          />
        </label>

        <div className="space-y-1.5">
          <div className="text-xs text-muted">Or start from an example</div>
          <div className="flex flex-wrap gap-1.5">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => setPrompt(ex)}
                className="motion-press text-xs px-2 py-1 rounded-md border border-line text-muted hover:text-accent-cyan hover:border-accent-cyan/40"
              >
                {ex}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">Title (optional)</div>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Named from your description if blank"
              className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
            />
          </label>
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">Data</div>
            <select
              value={datasource}
              onChange={(e) =>
                setDatasource(e.target.value as "prometheus" | "loki" | "mixed")
              }
              className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
            >
              <option value="prometheus">Metrics (Prometheus)</option>
              <option value="loki">Logs (Loki)</option>
              <option value="mixed">Mixed</option>
            </select>
          </label>
        </div>

        {create.error && (
          <div role="alert" className="text-sm text-signal-critical">
            {errorText(create.error)}
          </div>
        )}
      </div>
    </Dialog>
  );
}
