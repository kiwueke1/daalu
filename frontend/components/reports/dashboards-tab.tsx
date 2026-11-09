"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LayoutGrid, Pin, Plus, Trash2 } from "lucide-react";
import { api, type Dashboard } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { LoadState } from "@/components/ui/load-state";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

export function ReportsDashboardsTab() {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const toast = useToast();
  const nameId = useId();

  const q = useQuery({
    queryKey: ["dashboards"],
    queryFn: () => api.reports.dashboards.list(),
  });
  const dashboards = q.data ?? [];

  const create = useMutation({
    mutationFn: () => api.reports.dashboards.create({ name }),
    onSuccess: (d) => {
      qc.invalidateQueries({ queryKey: ["dashboards"] });
      toast.success(`Dashboard "${d?.name ?? name}" created. Open it to add tiles.`);
      setName("");
      setCreating(false);
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.reports.dashboards.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dashboards"] });
      toast.success("Dashboard deleted.");
    },
    onError: (e) => toast.error(errorText(e)),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">
          Layouts of saved-report tiles. Click a dashboard to view or edit its
          tiles. Admins can pin one to the Home page.
        </p>
        {!creating && (
          <Button size="sm" onClick={() => setCreating(true)} icon={<Plus className="h-3.5 w-3.5" aria-hidden />}>
            New dashboard
          </Button>
        )}
      </div>

      {creating && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) create.mutate();
          }}
          className="motion-enter rounded-xl border border-line bg-bg-card p-4 flex flex-wrap items-end gap-2"
        >
          <div className="flex-1 min-w-[200px]">
          <label htmlFor={nameId} className="block text-xs font-medium mb-1">
            Dashboard name
          </label>
          <input
            id={nameId}
            autoFocus
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. NetOps standup"
            className="w-full bg-bg-elevated border border-line rounded-lg px-3 py-1.5 text-sm"
          />
          </div>
          <Button
            type="submit"
            size="sm"
            variant="primary"
            pending={create.isPending}
            disabled={!name.trim()}
            disabledReason="Give the dashboard a name"
          >
            Create dashboard
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setCreating(false);
              setName("");
            }}
          >
            Cancel
          </Button>
        </form>
      )}

      <LoadState
        query={q}
        loadingText="Loading dashboards…"
        empty={dashboards.length === 0}
        emptyTitle="No dashboards yet"
        emptyHint="A dashboard is a layout of saved-report tiles. Create one, then add tiles from your saved queries."
        emptyAction={
          !creating && (
            <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
              New dashboard
            </Button>
          )
        }
      >
        <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {dashboards.map((d) => (
            <DashboardCard
              key={d.id}
              dashboard={d}
              onDelete={() => remove.mutate(d.id)}
              deleting={remove.isPending && remove.variables === d.id}
            />
          ))}
        </ul>
      </LoadState>
    </div>
  );
}

function DashboardCard({
  dashboard,
  onDelete,
  deleting,
}: {
  dashboard: Dashboard;
  onDelete: () => void;
  deleting?: boolean;
}) {
  return (
    <li className="motion-enter rounded-xl border border-line bg-bg-card overflow-hidden">
      <Link
        href={`/reports/dashboards/${dashboard.id}`}
        className="block p-4 hover:bg-bg-elevated/40 transition-colors"
      >
        <div className="flex items-start gap-2">
          <LayoutGrid className="h-4 w-4 text-accent mt-0.5 flex-shrink-0" aria-hidden />
          <div className="flex-1 min-w-0">
            <div className="font-medium truncate">{dashboard.name}</div>
            <div className="text-xs text-muted mt-1">
              {dashboard.tiles.length} tile
              {dashboard.tiles.length === 1 ? "" : "s"}
              {dashboard.home_pinned && (
                <>
                  {" · "}
                  <span className="inline-flex items-center gap-1 text-accent">
                    <Pin className="h-3 w-3" aria-hidden /> pinned to Home
                  </span>
                </>
              )}
            </div>
          </div>
        </div>
      </Link>
      <div className="px-4 py-2 border-t border-line bg-bg-elevated/20 flex justify-end">
        <ConfirmButton
          label="Delete"
          confirmLabel="Delete dashboard"
          consequence="Removes the layout; saved reports stay."
          icon={<Trash2 className="h-3 w-3" aria-hidden />}
          onConfirm={onDelete}
          pending={deleting}
          tone="danger"
        />
      </div>
    </li>
  );
}
