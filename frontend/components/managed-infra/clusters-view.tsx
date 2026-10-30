"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { HUB } from "@/lib/hub-routes";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Boxes,
  CheckCircle2,
  ChevronRight,
  Copy,
  Plus,
  Server,
  Settings,
  Trash2,
  X,
} from "lucide-react";
import {
  api,
  Cluster,
  ClusterCreate,
  ClusterStatus,
  IntegrationConfig,
} from "@/lib/api";
import {
  STEPS_BY_ID,
  IntegrationStep,
} from "@/components/integrations/steps";
import { ConnectModal } from "@/components/integrations/connect-modal";

// ── Sections of providers surfaced on this page ─────────────────────────
//
// Each section is a separate table that mirrors the look of the existing
// clusters table: provider, status badge, key identifier, and an
// action button. Status is sourced from IntegrationConfig.status, which
// the backend flips to "connected" on the first successful tool call
// against the provider.

const CLOUD_PROVIDER_IDS = ["aws", "gcp", "azure"] as const;
const OBSERVABILITY_PROVIDER_IDS = [
  "prometheus",
  "loki",
  "thanos",
  "opensearch",
  "grafana",
] as const;

// Providers that have a read-only detail console at /observability/{provider}
// (the metrics/logs analogue of the kubectl console). OpenSearch isn't wired
// to a console yet, so its row stays Connect/Edit-only.
const OBSERVABILITY_CONSOLE_PROVIDERS = new Set<string>([
  "prometheus",
  "thanos",
  "loki",
  "opensearch",
]);

// Providers a tenant may connect more than once (one row per named instance).
// Mirrors the backend's _MULTI_INSTANCE_PROVIDERS. For these we never collapse
// rows by provider — each instance is edited/deleted by its own id, so the
// connect modal must not pre-fill from a single collapsed row.
const MULTI_INSTANCE_PROVIDERS = new Set<string>(["kubernetes"]);

// Which config field to surface as the "key identifier" in the table.
// Picked per-provider so each row reads at a glance.
const SUMMARY_FIELD: Record<string, string> = {
  aws: "region",
  gcp: "project_id",
  azure: "subscription_id",
  prometheus: "url",
  loki: "url",
  thanos: "url",
  opensearch: "url",
  grafana: "url",
};

const SUMMARY_LABEL: Record<string, string> = {
  aws: "Region",
  gcp: "Project",
  azure: "Subscription",
  prometheus: "Endpoint",
  loki: "Endpoint",
  thanos: "Endpoint",
  opensearch: "Endpoint",
  grafana: "Endpoint",
};

const STATUS_BADGE: Record<ClusterStatus, { dot: string; label: string }> = {
  pending: { dot: "bg-muted/60", label: "pending" },
  awaiting_handshake: { dot: "bg-accent-amber", label: "awaiting handshake" },
  connected: { dot: "bg-accent-emerald", label: "connected" },
  degraded: { dot: "bg-accent-amber", label: "degraded" },
  error: { dot: "bg-signal-critical", label: "error" },
};

function ClusterStatusBadge({ status }: { status: ClusterStatus }) {
  const s = STATUS_BADGE[status];
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted">
      <span className={`h-2 w-2 rounded-full ${s.dot}`} />
      {s.label}
    </span>
  );
}

/**
 * Connection badge for IntegrationConfig.status. The server uses
 * "connected" for "last tool call worked"; anything else (including
 * a brand-new row where no tool has fired yet) is treated as
 * "configured but not yet reporting".
 */
function ProviderStatusBadge({
  existing,
}: {
  existing: IntegrationConfig | undefined;
}) {
  if (!existing) {
    return (
      <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted">
        <span className="h-2 w-2 rounded-full bg-muted/60" />
        not connected
      </span>
    );
  }
  if (existing.status === "connected") {
    return (
      <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-accent-emerald">
        <span className="h-2 w-2 rounded-full bg-accent-emerald" />
        connected
      </span>
    );
  }
  if (existing.status === "error") {
    // Carry the probe's own message: a bare red dot gives an operator nothing
    // to act on, and the usual cause is a reachable store answering on a
    // different path rather than an outage.
    return (
      <span
        title={existing.last_error || undefined}
        className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-signal-critical"
      >
        <span className="h-2 w-2 rounded-full bg-signal-critical" />
        error
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-accent-amber">
      <span className="h-2 w-2 rounded-full bg-accent-amber" />
      {existing.status || "configured"}
    </span>
  );
}

function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

function summaryFor(
  step: IntegrationStep,
  existing: IntegrationConfig | undefined
): string | null {
  if (!existing) return null;
  const field = SUMMARY_FIELD[step.id];
  if (!field) return null;
  const raw = existing.config?.[field];
  if (raw == null || raw === "" || raw === "***") return null;
  return String(raw);
}

export default function ManagedInfraPage() {
  const qc = useQueryClient();

  // Clusters (VPN-onboarded).
  const { data: clusters, isLoading: clustersLoading } = useQuery({
    queryKey: ["clusters"],
    queryFn: () => api.clusters.list(),
    refetchInterval: 30_000,
  });

  // Provider integration rows. Indexed by `provider` for O(1) lookup
  // when rendering each table row's status.
  const { data: integrationConfigs } = useQuery({
    queryKey: ["integrations", "config"],
    queryFn: () => api.integrations.listConfig(),
    // The status field flips on tool calls — keep it fresh-ish.
    refetchInterval: 30_000,
  });
  const configByProvider = useMemo(() => {
    const map: Record<string, IntegrationConfig> = {};
    for (const c of integrationConfigs ?? []) map[c.provider] = c;
    return map;
  }, [integrationConfigs]);

  const [showClusterForm, setShowClusterForm] = useState(false);
  const [created, setCreated] = useState<ClusterCreate | null>(null);
  const [activeStep, setActiveStep] = useState<IntegrationStep | null>(null);

  const onboardCluster = useMutation({
    mutationFn: (input: { slug: string; name: string }) =>
      api.clusters.onboard(input),
    onSuccess: (res) => {
      setCreated(res);
      setShowClusterForm(false);
      qc.invalidateQueries({ queryKey: ["clusters"] });
    },
  });

  const removeCluster = useMutation({
    mutationFn: (slug: string) => api.clusters.remove(slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["clusters"] }),
  });

  // Disconnect a provider integration (prometheus / loki / kubernetes / …):
  // deletes the saved config so the row reverts to "not connected" and can be
  // re-added from scratch — the quickest way to clear a misconfigured provider
  // and re-test the connect flow. clusterId picks one of several rows for
  // multi-instance providers (kubernetes).
  const removeConfig = useMutation({
    mutationFn: (vars: { provider: string; clusterId?: string }) =>
      api.integrations.deleteConfig(vars.provider, vars.clusterId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["integrations", "config"] }),
  });

  const handleDeleteConfig = (step: IntegrationStep) => {
    if (!step.provider) return;
    if (
      confirm(
        `Disconnect ${step.title}? Daalu deletes the saved credentials and stops reading from it until you reconnect.`
      )
    ) {
      removeConfig.mutate({ provider: step.provider });
    }
  };

  const cloudSteps: IntegrationStep[] = CLOUD_PROVIDER_IDS.map(
    (id) => STEPS_BY_ID[id]
  ).filter((s): s is IntegrationStep => !!s);
  const observabilitySteps: IntegrationStep[] = OBSERVABILITY_PROVIDER_IDS.map(
    (id) => STEPS_BY_ID[id]
  ).filter((s): s is IntegrationStep => !!s);

  return (
    <div className="space-y-8 max-w-[1200px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-start gap-2">
          <Boxes className="h-5 w-5 mt-1.5 shrink-0 text-accent-cyan" /> Clusters
          &amp; observability
        </h1>
        <p className="text-muted text-sm mt-1">
          Cloud accounts, observability stacks and Kubernetes clusters Daalu
          reads from. Connect one here, or set everything up in order on the{" "}
          <Link href={HUB.onboarding} className="underline">
            Onboarding tab
          </Link>
          .
        </p>
      </div>

      {/* ── Cloud accounts ─────────────────────────────────────────────── */}
      <section className="space-y-3">
        <SectionHeader
          title="Cloud accounts"
          subtitle="Read-only credentials the agent uses to inspect instances, logs and metrics when an alert touches AWS, GCP or Azure."
        />
        <ProviderTable
          steps={cloudSteps}
          configByProvider={configByProvider}
          onConnect={(step) => setActiveStep(step)}
          onDelete={handleDeleteConfig}
        />
      </section>

      {/* ── Observability ──────────────────────────────────────────────── */}
      <section className="space-y-3">
        <SectionHeader
          title="Observability"
          subtitle="Your metrics, logs and alerting stacks. A provider shows connected once the agent has queried it; open one to browse it."
        />
        <ProviderTable
          steps={observabilitySteps}
          configByProvider={configByProvider}
          onConnect={(step) => setActiveStep(step)}
          onDelete={handleDeleteConfig}
          detailHref={(step, existing) => {
            if (!existing) return null;
            // Grafana opens the native dashboards browser, not a query
            // console — Daalu renders its dashboards under /dashboards.
            if (step.provider === "grafana") return "/dashboards";
            return OBSERVABILITY_CONSOLE_PROVIDERS.has(step.provider ?? "")
              ? `/observability/${step.provider}`
              : null;
          }}
        />
      </section>

      {/* ── Kubernetes clusters (VPN-onboarded) ────────────────────────── */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <SectionHeader
            title="Kubernetes clusters"
            subtitle="Clusters reached over a private tunnel. Onboarding gives you a one-time install snippet to run on the cluster."
          />
          <button
            onClick={() => setShowClusterForm(true)}
            className="text-xs h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan hover:bg-accent-cyan/25 flex items-center gap-1.5 shrink-0"
          >
            <Plus className="h-3.5 w-3.5" /> Onboard cluster
          </button>
        </div>

        {showClusterForm && (
          <OnboardForm
            onClose={() => setShowClusterForm(false)}
            onSubmit={(slug, name) => onboardCluster.mutate({ slug, name })}
            submitting={onboardCluster.isPending}
            error={onboardCluster.error?.message ?? null}
          />
        )}

        {created && (
          <CreatedSnippet created={created} onClose={() => setCreated(null)} />
        )}

        <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
          <table className="w-full min-w-[600px] text-sm">
            <thead className="bg-bg-elevated/40 text-[11px] uppercase tracking-wider text-muted">
              <tr>
                <th className="text-left px-4 py-2 font-normal">Cluster</th>
                <th className="text-left px-4 py-2 font-normal">Status</th>
                <th className="text-left px-4 py-2 font-normal">Tunnel IP</th>
                <th className="text-left px-4 py-2 font-normal">
                  Last handshake
                </th>
                <th className="text-right px-4 py-2 font-normal"></th>
              </tr>
            </thead>
            <tbody>
              {clustersLoading && (
                <tr>
                  <td colSpan={5} className="px-4 py-6 text-center text-muted">
                    Loading…
                  </td>
                </tr>
              )}
              {!clustersLoading && (clusters ?? []).length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-muted">
                    No clusters yet. Click <em>Onboard cluster</em> to add one.
                  </td>
                </tr>
              )}
              {(clusters ?? []).map((c: Cluster) => (
                <tr
                  key={c.id}
                  className="border-t border-line hover:bg-bg-elevated/30"
                >
                  <td className="px-4 py-3">
                    <Link
                      href={`/clusters/${c.slug}`}
                      className="group inline-flex flex-col"
                    >
                      <span className="font-medium group-hover:text-accent-cyan group-hover:underline">
                        {c.name}
                      </span>
                      <span className="text-[11px] text-muted">{c.slug}</span>
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <ClusterStatusBadge status={c.status} />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {c.tunnel_ip}
                  </td>
                  <td className="px-4 py-3 text-muted text-xs">
                    {timeAgo(c.last_handshake_at)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex items-center justify-end gap-3">
                      <button
                        onClick={() => {
                          if (confirm(`Tear down cluster ${c.slug}?`))
                            removeCluster.mutate(c.slug);
                        }}
                        className="text-muted hover:text-signal-critical"
                        title="Tear down"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                      <Link
                        href={`/clusters/${c.slug}`}
                        className="text-muted hover:text-accent-cyan inline-flex items-center gap-1 text-xs"
                        title="Open cluster"
                      >
                        Open <ChevronRight className="h-4 w-4" />
                      </Link>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {activeStep && (
        <ConnectModal
          step={activeStep}
          existing={
            // Never pre-fill from a collapsed row for multi-instance providers
            // (kubernetes), or "Connect" would edit an existing instance
            // instead of creating a new one. Single-instance providers upsert
            // their one row as before.
            activeStep.provider &&
            !MULTI_INSTANCE_PROVIDERS.has(activeStep.provider)
              ? configByProvider[activeStep.provider]
              : undefined
          }
          onClose={() => setActiveStep(null)}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["integrations", "config"] });
          }}
        />
      )}
    </div>
  );
}

// ── Section header ──────────────────────────────────────────────────────

function SectionHeader({
  title,
  subtitle,
}: {
  title: string;
  subtitle: string;
}) {
  return (
    <div>
      <h2 className="text-base font-medium">{title}</h2>
      <p className="text-xs text-muted mt-0.5 max-w-[860px]">{subtitle}</p>
    </div>
  );
}

// ── Provider table ──────────────────────────────────────────────────────
//
// Shared layout across the cloud + observability sections. Columns
// mirror the cluster table so the page reads as one consistent surface:
// Provider, Status, the key identifier (region/project/url), and an
// action button.

function ProviderTable({
  steps,
  configByProvider,
  onConnect,
  onDelete,
  detailHref,
}: {
  steps: IntegrationStep[];
  configByProvider: Record<string, IntegrationConfig>;
  onConnect: (step: IntegrationStep) => void;
  // When supplied, connected rows get a Disconnect (trash) action that
  // deletes the saved config so the provider can be re-added from scratch.
  onDelete?: (step: IntegrationStep) => void;
  // When supplied and it returns a path for a connected provider, the row
  // gets an "Open" link into a detail console (e.g. /observability/loki).
  detailHref?: (
    step: IntegrationStep,
    existing: IntegrationConfig | undefined
  ) => string | null;
}) {
  return (
    <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead className="bg-bg-elevated/40 text-[11px] uppercase tracking-wider text-muted">
          <tr>
            <th className="text-left px-4 py-2 font-normal w-[34%]">Provider</th>
            <th className="text-left px-4 py-2 font-normal">Status</th>
            <th className="text-left px-4 py-2 font-normal">Detail</th>
            <th className="text-right px-4 py-2 font-normal"></th>
          </tr>
        </thead>
        <tbody>
          {steps.map((step) => {
            const existing = step.provider
              ? configByProvider[step.provider]
              : undefined;
            const summary = summaryFor(step, existing);
            const label = SUMMARY_LABEL[step.id];
            const Icon = step.icon;
            return (
              <tr key={step.id} className="border-t border-line">
                <td className="px-4 py-3">
                  <div className="flex items-center gap-3">
                    <div
                      className="h-8 w-8 rounded-lg flex items-center justify-center shrink-0"
                      style={{
                        background:
                          "color-mix(in srgb, var(--accent) 12%, transparent)",
                      }}
                    >
                      <Icon className="h-4 w-4 text-accent-cyan" />
                    </div>
                    <div>
                      <div className="font-medium">{step.title}</div>
                      <div className="text-[11px] text-muted">
                        {step.provider}
                      </div>
                    </div>
                  </div>
                </td>
                <td className="px-4 py-3">
                  <ProviderStatusBadge existing={existing} />
                </td>
                <td className="px-4 py-3 text-xs">
                  {summary ? (
                    <>
                      <span className="text-muted">{label}: </span>
                      <span className="font-mono">{summary}</span>
                    </>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  <div className="inline-flex items-center gap-2">
                    {(() => {
                      const href = detailHref?.(step, existing);
                      return href ? (
                        <Link
                          href={href}
                          className="text-xs h-8 px-3 rounded-lg border border-line text-muted hover:text-accent-cyan hover:border-accent-cyan/40 inline-flex items-center gap-1.5"
                        >
                          Open <ChevronRight className="h-3.5 w-3.5" />
                        </Link>
                      ) : null;
                    })()}
                    <button
                      type="button"
                      onClick={() => onConnect(step)}
                      className="text-xs h-8 px-3 rounded-lg border border-line text-muted hover:text-fg inline-flex items-center gap-1.5"
                    >
                      {existing ? (
                        <>
                          <Settings className="h-3.5 w-3.5" /> Edit
                        </>
                      ) : (
                        <>
                          <Plus className="h-3.5 w-3.5" /> Connect
                        </>
                      )}
                    </button>
                    {existing && onDelete && (
                      <button
                        type="button"
                        onClick={() => onDelete(step)}
                        title="Disconnect"
                        aria-label={`Disconnect ${step.title}`}
                        className="text-xs h-8 px-2 rounded-lg border border-line text-muted hover:text-signal-critical hover:border-signal-critical/40 inline-flex items-center"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Cluster onboarding form (unchanged from the old /clusters page) ─────

function OnboardForm({
  onClose,
  onSubmit,
  submitting,
  error,
}: {
  onClose: () => void;
  onSubmit: (slug: string, name: string) => void;
  submitting: boolean;
  error: string | null;
}) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(slug.trim(), name.trim());
      }}
      className="rounded-2xl border border-line bg-bg-card p-4 space-y-3"
    >
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium flex items-center gap-2">
          <Server className="h-4 w-4 text-accent-cyan" /> Onboard new cluster
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-muted hover:text-fg"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="text-xs space-y-1">
          <div className="text-muted uppercase tracking-wider">Slug</div>
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            required
            placeholder="acme-prod"
            pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?"
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          />
        </label>
        <label className="text-xs space-y-1">
          <div className="text-muted uppercase tracking-wider">Display name</div>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder="ACME production cluster"
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          />
        </label>
      </div>
      {error && <div className="text-[11px] text-signal-critical">{error}</div>}
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={submitting || !slug || !name}
          className="text-xs h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan disabled:opacity-50"
        >
          {submitting ? "Creating…" : "Create"}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-fg"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function CreatedSnippet({
  created,
  onClose,
}: {
  created: ClusterCreate;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="rounded-2xl border border-accent-amber/40 bg-accent-amber/5 p-4 space-y-3">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-medium text-accent-amber flex items-center gap-2">
            <CheckCircle2 className="h-4 w-4" /> Cluster{" "}
            <code>{created.slug}</code> created
          </div>
          <p className="text-xs text-muted mt-1">
            Copy and run the snippet below on the customer cluster. The
            invite token is one-shot and is <strong>not</strong> shown
            again — close this panel and you'll need to re-onboard the
            cluster to get a fresh one.
          </p>
        </div>
        <button onClick={onClose} className="text-muted hover:text-fg">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="relative">
        <pre className="text-xs bg-bg-elevated/60 border border-line rounded-lg p-3 overflow-x-auto whitespace-pre">
          {created.install_snippet}
        </pre>
        <button
          onClick={() => {
            navigator.clipboard.writeText(created.install_snippet);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }}
          className="absolute top-2 right-2 text-[11px] h-7 px-2 rounded-md bg-bg-card border border-line hover:bg-bg-elevated/60 flex items-center gap-1"
        >
          <Copy className="h-3 w-3" /> {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div className="text-[11px] text-muted">
        Tunnel IP: <code className="font-mono">{created.tunnel_ip}</code>
        {" · "}
        Operator pubkey:{" "}
        <code className="font-mono">
          {created.operator_pubkey.slice(0, 8)}…
        </code>
      </div>
    </div>
  );
}
