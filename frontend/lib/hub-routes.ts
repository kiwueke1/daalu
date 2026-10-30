/**
 * Canonical URLs for the /managed-infra tabs.
 *
 * `/clusters` and `/network` redirect here (the hub renders their views from
 * `components/managed-infra/`). `/onboarding` is still a real route — the hub
 * imports its page component directly — so linking to it bare renders the
 * section *without* the hub's tab bar, which reads to the user as "the tabs
 * disappeared."
 *
 * Link here whenever you mean "the section".
 */
export const HUB = {
  clusters: "/managed-infra?tab=clusters",
  network: "/managed-infra?tab=network",
  onboarding: "/managed-infra?tab=onboarding",
  integrations: "/managed-infra?tab=integrations",
} as const;
