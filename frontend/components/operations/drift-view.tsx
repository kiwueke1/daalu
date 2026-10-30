"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ArrowRight, GitPullRequest } from "lucide-react";
import { api } from "@/lib/api";
import { formatRelative } from "@/lib/utils";

/**
 * Drift tab — currently-drifted devices.
 *
 * Filters change-proposals to ``kind=drift`` and groups by device. This
 * is the page the NetOps team's daily standup tends to start on.
 */
export function DriftView() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["change-proposals", "drift"],
    queryFn: () =>
      api.changeProposals.list({ kind: "drift", status: "pending" }),
    refetchInterval: 15_000,
  });

  // Group by device — multiple proposals per device are rare (the
  // reconciler dedupes by "no drift-on-drift"), but possible historically.
  const byDevice = new Map<
    string,
    { device_id: string; latest: string; rows: typeof data }
  >();
  for (const p of data ?? []) {
    const key = p.device_id;
    const existing = byDevice.get(key);
    if (!existing) {
      byDevice.set(key, {
        device_id: key,
        latest: p.created_at,
        rows: [p],
      });
    } else {
      existing.rows!.push(p);
      if (p.created_at > existing.latest) existing.latest = p.created_at;
    }
  }
  const groups = Array.from(byDevice.values()).sort((a, b) =>
    b.latest.localeCompare(a.latest),
  );

  return (
    <div className="space-y-4">
      <p className="text-muted text-sm">
        Every device currently out of sync with its Source of Truth. The
        reconciler runs continuously; an entry here means it spotted a
        difference and opened a proposal for you to decide what to do.
      </p>

      {isLoading && (
        <div className="text-sm text-muted">Loading drift queue…</div>
      )}
      {error && (
        <div className="text-sm text-[color:var(--critical)]">
          Couldn't load drift queue.
        </div>
      )}

      {!isLoading && groups.length === 0 && (
        <div className="surface p-6 text-center text-sm text-muted">
          No devices currently drifted. ✓
        </div>
      )}

      <div className="space-y-2">
        {groups.map((g) => {
          const headline = g.rows![0];
          const diffSummary = summarizeDiff(headline.diff);
          return (
            <Link
              key={g.device_id}
              href={`/devices/${g.device_id}`}
              className="block surface p-4 hover:border-accent-blue/40 transition-colors"
            >
              <div className="flex items-center gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-[10px] uppercase tracking-wider text-[color:var(--warning)] font-semibold">
                      drift
                    </span>
                    <span className="text-[10px] uppercase tracking-wider text-muted">
                      device {g.device_id.slice(0, 8)}
                    </span>
                    {g.rows!.length > 1 && (
                      <span className="text-[10px] text-muted">
                        · {g.rows!.length} open proposals
                      </span>
                    )}
                    <span className="text-[10px] text-muted ml-auto">
                      first seen {formatRelative(g.latest)}
                    </span>
                  </div>
                  <div className="text-sm font-medium truncate">
                    {diffSummary}
                  </div>
                  <div className="text-[11px] text-muted mt-1 flex items-center gap-2">
                    <GitPullRequest className="h-3 w-3" />
                    <Link
                      href={`/proposals/${headline.id}`}
                      className="hover:text-[color:var(--text)] underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      Open proposal
                    </Link>
                    <span className="ml-auto inline-flex items-center gap-1">
                      Open device <ArrowRight className="h-3 w-3" />
                    </span>
                  </div>
                </div>
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

function summarizeDiff(diff: string): string {
  if (!diff) return "(no diff text recorded)";
  const lines = diff.split("\n");
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.startsWith("+") && !l.startsWith("+++")) added++;
    if (l.startsWith("-") && !l.startsWith("---")) removed++;
  }
  if (added === 0 && removed === 0) {
    return lines.slice(0, 1).join("") || "(empty diff)";
  }
  return `${added} lines added, ${removed} lines removed`;
}
