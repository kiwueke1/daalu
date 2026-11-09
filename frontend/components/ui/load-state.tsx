"use client";

import { AlertTriangle, Inbox, Loader2, RefreshCw } from "lucide-react";
import { ApiError } from "@/lib/api";
import { errorText } from "@/lib/errors";

/**
 * Loading, error and empty states for a query, so "still loading" never
 * looks like "nothing here" or "0".
 *
 *   const q = useQuery(...);
 *   <LoadState query={q} empty={!q.data?.length} emptyTitle="No alerts"
 *              emptyHint="Alerts appear here when a connected source fires.">
 *     {q.data && <AlertList items={q.data} />}
 *   </LoadState>
 *
 * Errors are shown as a plain sentence (`errorText`), never a raw body.
 * `notSetUp` turns a "not configured yet" answer (a 409/404 the page knows
 * means setup is missing) into a neutral next step instead of a red error.
 * Wrap the loaded content in `motion-enter` where the layout allows, so the
 * swap from "Loading…" to data reads as one change.
 */
export function LoadState({
  query,
  empty = false,
  loadingText = "Loading…",
  emptyTitle = "Nothing here yet",
  emptyHint,
  emptyAction,
  notSetUp,
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
  /** When the error means "this isn't set up yet": what to show instead. */
  notSetUp?: {
    when: (error: unknown) => boolean;
    title: string;
    hint?: React.ReactNode;
    action?: React.ReactNode;
  };
  children: React.ReactNode;
}) {
  if (query.isLoading) {
    return (
      <div role="status" className="surface p-5 text-sm text-muted flex items-center gap-2">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> {loadingText}
      </div>
    );
  }
  if (query.isError && notSetUp?.when(query.error)) {
    return (
      <div className="surface p-6 text-center motion-enter">
        <Inbox className="h-5 w-5 mx-auto text-muted" aria-hidden />
        <div className="mt-2 text-sm font-medium">{notSetUp.title}</div>
        {notSetUp.hint && <div className="mt-1 text-sm text-muted max-w-md mx-auto">{notSetUp.hint}</div>}
        {notSetUp.action && <div className="mt-3">{notSetUp.action}</div>}
      </div>
    );
  }
  if (query.isError) {
    const msg = errorText(query.error);
    // Retrying a 4xx (other than a timeout or rate limit) gives the same answer.
    const retryable =
      !(query.error instanceof ApiError) ||
      query.error.status >= 500 ||
      query.error.status === 408 ||
      query.error.status === 429;
    return (
      <div
        role="alert"
        className="rounded-xl p-4 text-sm flex items-start gap-3 motion-enter"
        style={{
          background: "color-mix(in srgb, var(--critical) 8%, var(--bg-card))",
          border: "1px solid color-mix(in srgb, var(--critical) 35%, var(--line))",
        }}
      >
        <AlertTriangle aria-hidden className="h-4 w-4 mt-0.5 shrink-0" style={{ color: "var(--critical)" }} />
        <div className="flex-1 min-w-0">
          <div className="font-medium">Couldn&apos;t load this.</div>
          <div className="text-muted text-sm mt-0.5 break-words">{msg}</div>
        </div>
        {query.refetch && retryable && (
          <button
            type="button"
            onClick={() => query.refetch?.()}
            className="motion-press h-8 px-3 rounded-lg border border-line text-xs inline-flex items-center gap-1.5 shrink-0 hover:bg-bg-elevated/60"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Retry
          </button>
        )}
      </div>
    );
  }
  if (empty) {
    return (
      <div className="surface p-6 text-center motion-enter">
        <Inbox className="h-5 w-5 mx-auto text-muted" aria-hidden />
        <div className="mt-2 text-sm font-medium">{emptyTitle}</div>
        {emptyHint && <div className="mt-1 text-sm text-muted max-w-md mx-auto">{emptyHint}</div>}
        {emptyAction && <div className="mt-3">{emptyAction}</div>}
      </div>
    );
  }
  return <>{children}</>;
}
