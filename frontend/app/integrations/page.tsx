import { redirect } from "next/navigation";

/**
 * Integrations used to be its own sidebar entry. The view now lives
 * inside /managed-infra as the Integrations tab — wiring external
 * systems (channels, ingest sources, BYO SoT) is managed-infra work.
 */
export default function IntegrationsRedirect() {
  redirect("/managed-infra?tab=integrations");
}
