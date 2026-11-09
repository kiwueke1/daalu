import { Loader2 } from "lucide-react";

/** Shown while /proposals redirects to Operations → Proposals (or a detail loads). */
export default function ProposalsLoading() {
  return (
    <div role="status" className="surface p-5 text-sm text-muted flex items-center gap-2">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Opening proposals…
    </div>
  );
}
