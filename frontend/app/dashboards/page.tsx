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
  X,
} from "lucide-react";
import { api, type DashboardSummary } from "@/lib/api";
import { HUB } from "@/lib/hub-routes";
import { LoadState } from "@/components/ui/load-state";

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
        <button
          onClick={() => setShowAi(true)}
          className="shrink-0 text-sm h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan hover:bg-accent-cyan/25 flex items-center gap-1.5"
        >
          <Sparkles className="h-4 w-4" /> Create with AI
        </button>
      </div>

      <LoadState query={list} loadingText="Loading dashboards…">
      {data && !data.configured && (
        <div className="rounded-2xl border border-accent-amber/40 bg-accent-amber/5 p-5">
          <div className="font-medium text-accent-amber flex items-center gap-2">
            <Plug className="h-4 w-4" /> Grafana not connected
          </div>
          <p className="text-xs text-muted mt-1 max-w-[640px]">
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
          <p className="mt-1 text-xs text-muted">
            Describe what you want to watch and Daalu builds the board for you.
          </p>
          <button
            type="button"
            onClick={() => setShowAi(true)}
            className="mt-3 text-sm h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan hover:bg-accent-cyan/25 inline-flex items-center gap-1.5"
          >
            <Sparkles className="h-4 w-4" /> Create with AI
          </button>
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
                        className="text-[10px] px-1.5 py-0.5 rounded-md bg-bg-elevated/60 border border-line text-muted"
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
      </LoadState>

      {showAi && <AiCreateModal onClose={() => setShowAi(false)} />}
    </div>
  );
}

function AiCreateModal({ onClose }: { onClose: () => void }) {
  const router = useRouter();
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
      router.push(`/dashboards/${res.uid}`);
    },
  });

  const EXAMPLES = [
    "Cluster-wide CPU and memory by namespace, plus pod restart counts",
    "Ingress request rate, error rate, and p95 latency for the last 6h",
    "Node health: load, disk usage, network throughput, and unready nodes",
  ];

  return (
    <div
      className="fixed inset-0 z-50 bg-black/55 backdrop-blur-[2px] flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-2xl border border-line bg-bg-card p-5 space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div className="text-sm font-medium flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-accent-cyan" /> Create a dashboard
            with AI
          </div>
          <button onClick={onClose} className="text-muted hover:text-fg">
            <X className="h-4 w-4" />
          </button>
        </div>

        <p className="text-xs text-muted">
          Describe what you want to see. Daalu writes a Grafana dashboard (PromQL
          / LogQL panels) and opens it here.
        </p>

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

        <div className="flex flex-wrap gap-1.5">
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              type="button"
              onClick={() => setPrompt(ex)}
              className="text-[11px] px-2 py-1 rounded-md border border-line text-muted hover:text-accent-cyan hover:border-accent-cyan/40"
            >
              {ex}
            </button>
          ))}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <label className="text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">Title</div>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="optional"
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
          <div className="text-[11px] text-signal-critical">
            {String((create.error as Error).message || create.error)}
          </div>
        )}

        <div className="flex items-center gap-2">
          <button
            disabled={prompt.trim().length < 4 || create.isPending}
            onClick={() => create.mutate()}
            className="text-sm h-9 px-4 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            <Sparkles className="h-4 w-4" />
            {create.isPending ? "Generating…" : "Generate"}
          </button>
          <button
            onClick={onClose}
            className="text-sm h-9 px-3 rounded-lg border border-line text-muted hover:text-fg"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
