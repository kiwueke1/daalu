"use client";

import { AlertTriangle, Inbox, Loader2, RefreshCw } from "lucide-react";

/**
 * Loading, error and empty states for a query, so "still loading" never
 * looks like "nothing here" or "0".
 *
 *   const q = useQuery(...);
 *   <LoadState query={q} empty={!q.data?.length} emptyTitle="No alerts"
 *              emptyHint="Alerts appear here when a connected source fires.">
 *     {q.data && <AlertList items={q.data} />}
 *   </LoadState>
 */
export function LoadState({
  query,
  empty = false,
  loadingText = "Loading…",
  emptyTitle = "Nothing here yet",
  emptyHint,
  emptyAction,
  children,
}: {
  query: {
    isLoading: boolean;
    isError: boolean;
    error?: unknown;
    refetch?: () => unknown;
  };
  /** True when the data loaded and there's nothing to show. */
  empty?: boolean;
  loadingText?: string;
  emptyTitle?: string;
  /** What the user can do about it, or where the data comes from. */
  emptyHint?: React.ReactNode;
  emptyAction?: React.ReactNode;
  children: React.ReactNode;
}) {
  if (query.isLoading) {
    return (
      <div className="surface p-5 text-sm text-muted flex items-center gap-2">
        <Loader2 className="h-4 w-4 animate-spin" /> {loadingText}
      </div>
    );
  }
  if (query.isError) {
    const msg = query.error instanceof Error ? query.error.message : "Request failed";
    return (
      <div
        className="rounded-xl p-4 text-sm flex items-start gap-3"
        style={{
          background: "color-mix(in srgb, var(--critical) 8%, var(--bg-card))",
          border: "1px solid color-mix(in srgb, var(--critical) 35%, var(--line))",
        }}
      >
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" style={{ color: "var(--critical)" }} />
        <div className="flex-1 min-w-0">
          <div className="font-medium">Couldn&apos;t load this.</div>
          <div className="text-muted text-xs mt-0.5 break-words">{msg}</div>
        </div>
        {query.refetch && (
          <button
            type="button"
            onClick={() => query.refetch?.()}
            className="h-8 px-3 rounded-lg border border-line text-xs inline-flex items-center gap-1.5 shrink-0"
          >
            <RefreshCw className="h-3.5 w-3.5" /> Retry
          </button>
        )}
      </div>
    );
  }
  if (empty) {
    return (
      <div className="surface p-6 text-center">
        <Inbox className="h-5 w-5 mx-auto text-muted" />
        <div className="mt-2 text-sm font-medium">{emptyTitle}</div>
        {emptyHint && <div className="mt-1 text-xs text-muted max-w-md mx-auto">{emptyHint}</div>}
        {emptyAction && <div className="mt-3">{emptyAction}</div>}
      </div>
    );
  }
  return <>{children}</>;
}
