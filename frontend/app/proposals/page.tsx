import { redirect } from "next/navigation";

/**
 * Proposals used to be its own top-level page. The list view now lives
 * inside /operations as the Proposals tab. Detail pages
 * (/proposals/[id]) still resolve directly.
 */
export default function ProposalsIndexRedirect() {
  redirect("/operations?tab=proposals");
}
