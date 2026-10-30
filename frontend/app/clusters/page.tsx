import { redirect } from "next/navigation";

/**
 * Clusters used to be its own top-level page. It now lives inside
 * /managed-infra as the "Clusters & observability" tab, which has the tab bar
 * and the sidebar highlight. Detail pages (/clusters/[slug]) still resolve
 * directly.
 */
export default function ClustersIndexRedirect() {
  redirect("/managed-infra?tab=clusters");
}
