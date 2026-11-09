"use client";

import { useId, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  ArrowLeft,
  LayoutGrid,
  Plus,
  Trash2,
  Pin,
} from "lucide-react";
import {
  api,
  type Dashboard,
  type DashboardTile,
  type ReportsQueryResponse,
  type SavedReport,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { LoadState } from "@/components/ui/load-state";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

const RENDER_OPTIONS: Array<DashboardTile["render"]> = [
  "table",
  "number",
  "line",
  "bar",
  "pie",
];

export default function DashboardDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const qc = useQueryClient();
  const toast = useToast();
  const savedFieldId = useId();
  const renderFieldId = useId();

  const dashboardQ = useQuery({
    queryKey: ["dashboard", id],
    queryFn: () => api.reports.dashboards.get(id),
    enabled: !!id,
  });
  const dashboard = dashboardQ.data;
  const { data: saved = [] } = useQuery({
    queryKey: ["reports-saved"],
    queryFn: () => api.reports.saved.list(),
  });

  const [adding, setAdding] = useState(false);
  const [addSavedId, setAddSavedId] = useState("");
  const [addRender, setAddRender] = useState<DashboardTile["render"]>("table");

  const updateMutation = useMutation({
    mutationFn: (next: Partial<Pick<Dashboard, "name" | "tiles" | "home_pinned">>) =>
      api.reports.dashboards.update(id, next),
    onSuccess: (_d, next) => {
      qc.invalidateQueries({ queryKey: ["dashboard", id] });
      qc.invalidateQueries({ queryKey: ["dashboards"] });
      if (next.tiles && dashboard) {
        toast.success(next.tiles.length > dashboard.tiles.length ? "Tile added." : "Tile removed.");
      }
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const onAddTile = () => {
    if (!dashboard || !addSavedId) return;
    const nextTile: DashboardTile = {
      saved_report_id: addSavedId,
      render: addRender,
    };
    updateMutation.mutate({ tiles: [...dashboard.tiles, nextTile] });
    setAdding(false);
    setAddSavedId("");
    setAddRender("table");
  };

  const onRemoveTile = (index: number) => {
    if (!dashboard) return;
    updateMutation.mutate({
      tiles: dashboard.tiles.filter((_, i) => i !== index),
    });
  };

  if (!dashboard) {
    return (
      <div className="space-y-4 max-w-[1200px]">
        <Link
          href="/reports?tab=dashboards"
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-[color:var(--text)] transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> Back to Dashboards
        </Link>
        <LoadState
          query={dashboardQ}
          loadingText="Loading dashboard…"
          empty
          emptyTitle="Dashboard not found"
          emptyHint="It may have been deleted. The Dashboards tab lists the ones that exist."
        >
          {null}
        </LoadState>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-[1200px] motion-enter">
      <div className="flex items-center justify-between gap-4">
        <Link
          href="/reports?tab=dashboards"
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-[color:var(--text)] transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> Back to Dashboards
        </Link>
        {dashboard.home_pinned && (
          <span className="inline-flex items-center gap-1 text-xs text-accent">
            <Pin className="h-3 w-3" aria-hidden /> pinned to Home
          </span>
        )}
      </div>

      <header>
        <h1 className="text-2xl font-semibold inline-flex items-center gap-2">
          <LayoutGrid className="h-5 w-5 text-accent" aria-hidden /> {dashboard.name}
        </h1>
        <p className="text-muted text-sm mt-1">
          {dashboard.tiles.length} tile{dashboard.tiles.length === 1 ? "" : "s"}
        </p>
      </header>

      {dashboard.tiles.length === 0 ? (
        <div className="rounded-xl border border-line bg-bg-card p-6 text-sm text-muted">
          No tiles yet. Add one from your saved reports below
          {saved.length === 0 && (
            <>
              {" "}(save a query on the{" "}
              <Link href="/reports?tab=query" className="text-accent hover:underline">
                Query tab
              </Link>{" "}
              first)
            </>
          )}
          .
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {dashboard.tiles.map((tile, i) => (
            <TileBody
              key={`${tile.saved_report_id}-${i}`}
              tile={tile}
              saved={saved.find((s) => s.id === tile.saved_report_id)}
              onRemove={() => onRemoveTile(i)}
            />
          ))}
        </div>
      )}

      <section className="rounded-xl border border-line bg-bg-card p-4">
        {!adding ? (
          <Button
            size="sm"
            onClick={() => setAdding(true)}
            disabled={saved.length === 0}
            disabledReason="Save a query on the Query tab first; tiles show saved reports"
            icon={<Plus className="h-3.5 w-3.5" aria-hidden />}
          >
            Add tile
          </Button>
        ) : (
          <div className="space-y-3">
            <div className="text-xs uppercase tracking-wider text-muted">Add tile</div>
            <div className="flex flex-wrap gap-2 items-end">
              <div>
              <label htmlFor={savedFieldId} className="block text-xs font-medium mb-1">
                Saved report
              </label>
              <select
                id={savedFieldId}
                value={addSavedId}
                onChange={(e) => setAddSavedId(e.target.value)}
                className="bg-bg-elevated border border-line rounded-lg px-2 py-1.5 text-sm"
              >
                <option value="">Pick a saved report…</option>
                {saved.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
              </div>
              <div>
              <label htmlFor={renderFieldId} className="block text-xs font-medium mb-1">
                Show as
              </label>
              <select
                id={renderFieldId}
                value={addRender}
                onChange={(e) => setAddRender(e.target.value as DashboardTile["render"])}
                className="bg-bg-elevated border border-line rounded-lg px-2 py-1.5 text-sm"
              >
                {RENDER_OPTIONS.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              </div>
              <Button
                size="sm"
                variant="primary"
                onClick={onAddTile}
                disabled={!addSavedId}
                disabledReason="Pick a saved report"
                pending={updateMutation.isPending}
              >
                Add tile
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>
                Cancel
              </Button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Individual tile
// ─────────────────────────────────────────────────────────────────────

function TileBody({
  tile,
  saved,
  onRemove,
}: {
  tile: DashboardTile;
  saved?: SavedReport;
  onRemove: () => void;
}) {
  const { data: result, error } = useQuery<ReportsQueryResponse>({
    queryKey: ["reports-query", saved?.definition],
    queryFn: () => api.reports.runQuery(saved!.definition),
    enabled: !!saved,
    refetchInterval: 60_000,
  });

  return (
    <div className="motion-enter rounded-xl border border-line bg-bg-card p-4 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-medium text-sm">
            {tile.title || saved?.name || "Tile"}
          </div>
          <div className="text-[11px] uppercase tracking-wider text-muted">
            {tile.render}
            {saved?.definition?.entity ? ` · ${saved.definition.entity}` : ""}
          </div>
        </div>
        <ConfirmButton
          label={<span className="sr-only">Remove tile</span>}
          confirmLabel="Remove tile"
          consequence="The saved report stays."
          icon={<Trash2 className="h-3.5 w-3.5" aria-hidden />}
          onConfirm={onRemove}
          tone="danger"
          className="border-0 px-2 text-muted hover:text-[color:var(--critical)]"
        />
      </div>

      {!saved ? (
        <div className="text-sm text-muted py-6">The saved report behind this tile was deleted. Remove the tile, or add a new one.</div>
      ) : error ? (
        <div role="alert" className="text-sm text-[color:var(--critical)] py-2">
          {errorText(error)}
        </div>
      ) : !result ? (
        <div className="text-sm text-muted py-6">Loading…</div>
      ) : (
        <TileRender tile={tile} result={result} />
      )}
    </div>
  );
}

function TileRender({
  tile,
  result,
}: {
  tile: DashboardTile;
  result: ReportsQueryResponse;
}) {
  if (tile.render === "number" || result.display === "count") {
    return (
      <div className="py-6 text-center">
        <div className="text-4xl font-semibold">{result.total}</div>
        <div className="text-xs text-muted mt-1">{result.entity}</div>
      </div>
    );
  }

  if (tile.render === "line" || tile.render === "bar") {
    return <TimeSeriesTile result={result} kind={tile.render} />;
  }

  // Default + pie fallback → table.
  if (result.rows.length === 0) {
    return <div className="text-sm text-muted py-3">No matching rows. Widen the time range or loosen the filters in the saved query.</div>;
  }
  const visible = result.rows.slice(0, 6);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted">
          <tr>
            {result.columns.slice(0, 4).map((c) => (
              <th key={c.key} className="text-left px-2 py-1 whitespace-nowrap">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {visible.map((row) => (
            <tr key={row.id} className="border-t border-line">
              {result.columns.slice(0, 4).map((c) => (
                <td key={c.key} className="px-2 py-1 align-top">
                  {formatCell(row[c.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {result.rows.length > visible.length && (
        <div className="text-xs text-muted mt-1">
          +{result.rows.length - visible.length} more rows
        </div>
      )}
    </div>
  );
}

function TimeSeriesTile({
  result,
  kind,
}: {
  result: ReportsQueryResponse;
  kind: "line" | "bar";
}) {
  // Bucket rows by day using whichever column looks like a timestamp.
  const tsCol = useMemo(() => {
    const candidates = ["occurred_at", "created_at", "started_at"];
    return result.columns.find((c) => candidates.includes(c.key))?.key;
  }, [result.columns]);

  const data = useMemo(() => {
    if (!tsCol) return [];
    const buckets = new Map<string, number>();
    for (const row of result.rows) {
      const v = row[tsCol];
      if (typeof v !== "string") continue;
      const day = v.slice(0, 10);
      buckets.set(day, (buckets.get(day) ?? 0) + 1);
    }
    return Array.from(buckets.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([day, count]) => ({ day, count }));
  }, [result.rows, tsCol]);

  if (!tsCol || data.length === 0) {
    return (
      <div className="text-sm text-muted py-3">
        No time-series data to chart. Show it as a table, or widen the saved query&apos;s time range.
      </div>
    );
  }

  return (
    <div className="h-40">
      <ResponsiveContainer width="100%" height="100%">
        {kind === "line" ? (
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--line-strong)" />
            <XAxis dataKey="day" tick={{ fontSize: 11, fill: "var(--text-muted)" }} />
            <YAxis tick={{ fontSize: 11, fill: "var(--text-muted)" }} />
            <Tooltip />
            <Line type="monotone" dataKey="count" stroke="var(--accent)" />
          </LineChart>
        ) : (
          <BarChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--line-strong)" />
            <XAxis dataKey="day" tick={{ fontSize: 11, fill: "var(--text-muted)" }} />
            <YAxis tick={{ fontSize: 11, fill: "var(--text-muted)" }} />
            <Tooltip />
            <Bar dataKey="count" fill="var(--accent)" />
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") {
    if (/^\d{4}-\d{2}-\d{2}T/.test(v)) {
      try {
        return new Date(v).toLocaleString();
      } catch {
        return v;
      }
    }
    return v;
  }
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
