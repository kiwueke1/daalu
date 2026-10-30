/**
 * Status pill for a workflow run, shared by the Workflows list + detail pages.
 * Lives in components/ (not a page file) because Next.js app-router pages may
 * only export the default component.
 */
export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`text-[10px] uppercase tracking-wider rounded px-1.5 py-0.5 ${
        status === "succeeded"
          ? "bg-accent-emerald/15 text-accent-emerald"
          : status === "failed"
          ? "bg-accent-red/15 text-accent-red"
          : "bg-accent-blue/15 text-accent-blue"
      }`}
    >
      {status}
    </span>
  );
}
