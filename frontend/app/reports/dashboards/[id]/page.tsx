"use client";

import { useMemo, useState } from "react";
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

  const { data: dashboard } = useQuery({
    queryKey: ["dashboard", id],
    queryFn: () => api.reports.dashboards.get(id),
    enabled: !!id,
  });
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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dashboard", id] });
      qc.invalidateQueries({ queryKey: ["dashboards"] });
    },
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
    return <div className="text-sm text-muted">Loading…</div>;
  }

  return (
    <div className="space-y-6 max-w-[1200px]">
      <div className="flex items-center justify-between gap-4">
        <Link
          href="/reports?tab=dashboards"
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-[color:var(--text)] transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Back to Dashboards
        </Link>
        {dashboard.home_pinned && (
          <span className="inline-flex items-center gap-1 text-xs text-accent-blue">
            <Pin className="h-3 w-3" /> pinned to Home
          </span>
        )}
      </div>

      <header>
        <h1 className="text-2xl font-semibold inline-flex items-center gap-2">
          <LayoutGrid className="h-5 w-5 text-accent-blue" /> {dashboard.name}
        </h1>
        <p className="text-muted text-sm mt-1">
          {dashboard.tiles.length} tile{dashboard.tiles.length === 1 ? "" : "s"}
        </p>
      </header>

      {dashboard.tiles.length === 0 ? (
        <div className="rounded-xl border border-line bg-bg-card p-6 text-sm text-muted">
          No tiles yet. Add one from your saved reports below.
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
          <button
            type="button"
            onClick={() => setAdding(true)}
            disabled={saved.length === 0}
            className="text-xs inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-accent-blue/60 text-accent-blue hover:bg-accent-blue/10 transition-colors disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" /> Add tile
          </button>
        ) : (
          <div className="space-y-3">
            <div className="text-xs uppercase tracking-wider text-muted">Add tile</div>
            <div className="flex flex-wrap gap-2 items-center">
              <select
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
              <select
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
              <button
                type="button"
                onClick={onAddTile}
                disabled={!addSavedId}
                className="text-xs px-3 py-1.5 rounded-lg border border-accent-blue/60 text-accent-blue hover:bg-accent-blue/10 disabled:opacity-50"
              >
                Add
              </button>
              <button
                type="button"
                onClick={() => setAdding(false)}
                className="text-xs px-3 py-1.5 rounded-lg border border-line text-muted hover:text-[color:var(--text)]"
              >
                Cancel
              </button>
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
    <div className="rounded-xl border border-line bg-bg-card p-4 space-y-2 group">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-medium text-sm">
            {tile.title || saved?.name || "Tile"}
          </div>
          <div className="text-[10px] uppercase tracking-wider text-muted">
            {tile.render}
            {saved?.definition?.entity ? ` · ${saved.definition.entity}` : ""}
          </div>
        </div>
        <button
          type="button"
          onClick={() => {
            if (window.confirm("Remove this tile?")) onRemove();
          }}
          className="opacity-0 group-hover:opacity-100 transition-opacity text-muted hover:text-[color:var(--critical)] p-1"
          aria-label="Remove tile"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>

      {!saved ? (
        <div className="text-sm text-muted py-6">Saved report missing.</div>
      ) : error ? (
        <div className="text-xs text-[color:var(--critical)] py-2">
          {(error as Error).message}
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
    return <div className="text-sm text-muted py-3">No rows.</div>;
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
        No time-series data to chart.
      </div>
    );
  }

  return (
    <div className="h-40">
      <ResponsiveContainer width="100%" height="100%">
        {kind === "line" ? (
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
            <XAxis dataKey="day" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 10 }} />
            <Tooltip />
            <Line type="monotone" dataKey="count" stroke="var(--accent-blue, #3b82f6)" />
          </LineChart>
        ) : (
          <BarChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
            <XAxis dataKey="day" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 10 }} />
            <Tooltip />
            <Bar dataKey="count" fill="var(--accent-blue, #3b82f6)" />
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
