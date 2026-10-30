"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LayoutGrid, Pin, Plus, Trash2 } from "lucide-react";
import { api, type Dashboard } from "@/lib/api";

export function ReportsDashboardsTab() {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  const { data: dashboards = [], isLoading } = useQuery({
    queryKey: ["dashboards"],
    queryFn: () => api.reports.dashboards.list(),
  });

  const create = useMutation({
    mutationFn: () => api.reports.dashboards.create({ name }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dashboards"] });
      setName("");
      setCreating(false);
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.reports.dashboards.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dashboards"] });
    },
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted">
          Layouts of saved-report tiles. Click a dashboard to view or edit its
          tiles. Admins can pin one to the Home page.
        </p>
        {!creating && (
          <button
            type="button"
            onClick={() => setCreating(true)}
            className="text-xs inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-accent-blue/60 text-accent-blue hover:bg-accent-blue/10 transition-colors"
          >
            <Plus className="h-3.5 w-3.5" /> New dashboard
          </button>
        )}
      </div>

      {creating && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) create.mutate();
          }}
          className="rounded-xl border border-line bg-bg-card p-4 flex items-center gap-2"
        >
          <input
            autoFocus
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. NetOps standup"
            className="flex-1 bg-bg-elevated border border-line rounded-lg px-3 py-1.5 text-sm"
          />
          <button
            type="submit"
            disabled={!name.trim() || create.isPending}
            className="text-xs px-3 py-1.5 rounded-lg border border-accent-blue/60 text-accent-blue hover:bg-accent-blue/10 disabled:opacity-50"
          >
            Create
          </button>
          <button
            type="button"
            onClick={() => {
              setCreating(false);
              setName("");
            }}
            className="text-xs px-3 py-1.5 rounded-lg border border-line text-muted hover:text-[color:var(--text)]"
          >
            Cancel
          </button>
        </form>
      )}

      {isLoading ? (
        <div className="text-sm text-muted">Loading…</div>
      ) : dashboards.length === 0 ? (
        <div className="rounded-xl border border-line bg-bg-card p-6 text-sm text-muted">
          No dashboards yet. Create one above, then add tiles from the Query
          tab's saved list.
        </div>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {dashboards.map((d) => (
            <DashboardCard key={d.id} dashboard={d} onDelete={() => remove.mutate(d.id)} />
          ))}
        </ul>
      )}
    </div>
  );
}

function DashboardCard({
  dashboard,
  onDelete,
}: {
  dashboard: Dashboard;
  onDelete: () => void;
}) {
  return (
    <li className="rounded-xl border border-line bg-bg-card overflow-hidden group">
      <Link
        href={`/reports/dashboards/${dashboard.id}`}
        className="block p-4 hover:bg-bg-elevated/40 transition-colors"
      >
        <div className="flex items-start gap-2">
          <LayoutGrid className="h-4 w-4 text-accent-blue mt-0.5 flex-shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="font-medium truncate">{dashboard.name}</div>
            <div className="text-xs text-muted mt-1">
              {dashboard.tiles.length} tile
              {dashboard.tiles.length === 1 ? "" : "s"}
              {dashboard.home_pinned && (
                <>
                  {" · "}
                  <span className="inline-flex items-center gap-1 text-accent-blue">
                    <Pin className="h-3 w-3" /> pinned to Home
                  </span>
                </>
              )}
            </div>
          </div>
        </div>
      </Link>
      <div className="px-4 py-2 border-t border-line bg-bg-elevated/20 flex justify-end opacity-0 group-hover:opacity-100 transition-opacity">
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            if (window.confirm(`Delete dashboard "${dashboard.name}"?`)) onDelete();
          }}
          className="text-xs inline-flex items-center gap-1 text-muted hover:text-[color:var(--critical)]"
        >
          <Trash2 className="h-3 w-3" /> Delete
        </button>
      </div>
    </li>
  );
}
