"use client";

import { useQuery } from "@tanstack/react-query";
import { Clock } from "lucide-react";
import { api } from "@/lib/api";
import { formatRelative } from "@/lib/utils";

interface AlertOccurrencesProps {
  alertId: string;
  occurrenceCount: number;
}

/**
 * Fire-history timeline for a single alert.
 *
 * The alerts list shows one tile per *logical* alert (grouped by
 * fingerprint), but the underlying signal usually fires many times.
 * This component lists each individual fire — newest first — so the
 * operator can see "yes, this has been firing every 15m for 3 days."
 *
 * Hidden when there is only one occurrence (the parent alert's
 * ``created_at`` already says everything there is to say).
 */
export function AlertOccurrences({
  alertId,
  occurrenceCount,
}: AlertOccurrencesProps) {
  const { data, isLoading } = useQuery({
    queryKey: ["alert-occurrences", alertId],
    queryFn: () => api.alerts.occurrences(alertId),
    refetchInterval: 30_000,
  });

  if (occurrenceCount <= 1) return null;

  return (
    <details className="surface p-4" open>
      <summary className="cursor-pointer text-[11px] uppercase tracking-wider text-muted flex items-center gap-2">
        <Clock className="h-3 w-3" />
        Fire history
        <span className="text-[11px] font-medium px-1.5 py-0.5 rounded-md border border-line text-fg/80 normal-case tracking-normal">
          ×{occurrenceCount}
        </span>
      </summary>
      <div className="mt-3">
        {isLoading && (
          <div className="text-xs text-muted">Loading fire history…</div>
        )}
        {data && data.length === 0 && (
          <div className="text-xs text-muted">
            No fire-time records — this alert pre-dates the occurrence log.
          </div>
        )}
        {data && data.length > 0 && (
          <ol className="space-y-1.5 text-[12px]">
            {data.map((occ, idx) => {
              const ts = new Date(occ.occurred_at);
              return (
                <li
                  key={occ.id}
                  className="flex items-baseline gap-3 font-mono"
                >
                  <span className="text-muted w-6 text-right tabular-nums">
                    {idx === 0 ? "now" : `#${data.length - idx}`}
                  </span>
                  <span className="text-fg/85">
                    {ts.toLocaleString([], {
                      year: "numeric",
                      month: "short",
                      day: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                  <span className="text-muted">
                    {formatRelative(occ.occurred_at)}
                  </span>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </details>
  );
}
