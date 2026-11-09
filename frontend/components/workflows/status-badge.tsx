/**
 * Status pill for a workflow run, shared by the Workflows list + detail pages.
 * Lives in components/ (not a page file) because Next.js app-router pages may
 * only export the default component.
 */
export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`text-[11px] font-semibold uppercase tracking-wider rounded px-1.5 py-0.5 ${
        status === "succeeded"
          ? "bg-accent/15 text-accent"
          : status === "failed"
          ? "bg-signal-critical/15 text-signal-critical"
          : "bg-bg-elevated text-muted"
      }`}
    >
      {status}
    </span>
  );
}
