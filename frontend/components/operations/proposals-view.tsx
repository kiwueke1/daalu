"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { api, type ChangeProposalStatus } from "@/lib/api";
import { ProposalTile } from "@/components/proposals/proposal-tile";
import { LoadState } from "@/components/ui/load-state";
import { cn } from "@/lib/utils";

type StatusFilter = ChangeProposalStatus | "all";

const FILTERS: { id: StatusFilter; label: string }[] = [
  { id: "pending", label: "Pending" },
  { id: "approved", label: "Approved" },
  { id: "executed", label: "Executed" },
  { id: "failed", label: "Failed" },
  { id: "stale", label: "Stale" },
  { id: "rejected", label: "Rejected" },
  { id: "all", label: "All" },
];

const EMPTY_TITLE: Record<StatusFilter, string> = {
  pending: "No changes are waiting for approval",
  approved: "No approved proposals",
  executed: "No executed proposals",
  failed: "No failed proposals",
  stale: "No stale proposals",
  rejected: "No rejected proposals",
  all: "No proposals yet",
};

// Enough to count every status in practice; the API caps a page at 500.
const COUNT_LIMIT = 500;

export function ProposalsView() {
  const [status, setStatus] = useState<StatusFilter>("pending");
  const list = useQuery({
    queryKey: ["change-proposals", status],
    queryFn: () => api.changeProposals.list(status === "all" ? {} : { status }),
    refetchInterval: 10_000,
  });
  // One unfiltered read so every filter can show its count.
  const all = useQuery({
    queryKey: ["change-proposals", "counts"],
    queryFn: () => api.changeProposals.list({ limit: COUNT_LIMIT }),
    refetchInterval: 30_000,
  });
  const counts = new Map<StatusFilter, number>();
  if (all.data) {
    counts.set("all", all.data.length);
    for (const p of all.data) {
      const s = p.status as StatusFilter;
      counts.set(s, (counts.get(s) ?? 0) + 1);
    }
  }
  const capped = (all.data?.length ?? 0) >= COUNT_LIMIT;

  const data = list.data ?? [];

  return (
    <div className="space-y-4">
      <p className="text-muted text-sm">
        Device-config changes suggested by an agent or by drift detection.
        Approving one queues it to run; nothing is pushed to a device without
        approval.
      </p>

      {/* Status filter: a compact segmented control, visibly different from
          the page tabs above it. Scrolls sideways on a phone. */}
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-[11px] uppercase tracking-[0.14em] text-muted shrink-0">
          Status
        </span>
        <div
          role="radiogroup"
          aria-label="Filter proposals by status"
          className="flex min-w-0 flex-nowrap overflow-x-auto rounded-md border border-line bg-bg-elevated/50 p-0.5"
        >
          {FILTERS.map((f) => {
            const selected = status === f.id;
            const n = counts.get(f.id);
            return (
              <button
                key={f.id}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => setStatus(f.id)}
                className={cn(
                  "shrink-0 whitespace-nowrap rounded px-2.5 h-7 text-xs inline-flex items-center gap-1.5 transition-colors",
                  selected
                    ? "bg-bg-card text-fg font-medium shadow-sm"
                    : "text-muted hover:text-fg"
                )}
              >
                {f.label}
                {all.data && (
                  <span
                    className={cn(
                      "tabular-nums text-[10px]",
                      f.id === "pending" && n
                        ? "text-accent font-semibold"
                        : "text-muted"
                    )}
                  >
                    {n ?? 0}
                    {capped && f.id === "all" ? "+" : ""}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      <LoadState
        query={list}
        empty={data.length === 0}
        loadingText="Loading proposals…"
        emptyTitle={EMPTY_TITLE[status]}
        emptyHint={
          status === "pending" || status === "all"
            ? "Proposals appear here when drift detection or an agent suggests a device change."
            : "Try another status, or All."
        }
        emptyAction={
          status === "pending" || status === "all" ? (
            <Link
              href="/operations?tab=drift"
              className="text-xs text-accent hover:underline inline-flex items-center gap-1"
            >
              View drift <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          ) : undefined
        }
      >
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {data.map((p) => (
            <ProposalTile key={p.id} proposal={p} />
          ))}
        </div>
      </LoadState>
    </div>
  );
}
