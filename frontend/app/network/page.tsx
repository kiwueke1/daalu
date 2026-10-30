import { redirect } from "next/navigation";

/**
 * Network & servers used to be its own top-level page. It now lives inside
 * /managed-infra as the "Network & servers" tab, which has the tab bar and the
 * sidebar highlight. /network/topology still resolves directly.
 */
export default function NetworkIndexRedirect() {
  redirect("/managed-infra?tab=network");
}
