"use client";

/**
 * Shared integration-step catalog and form primitives.
 *
 * Both the full onboarding wizard (`/onboarding`) and the per-provider
 * "connect" cards on the managed-infra page (`/clusters`) render against
 * the exact same STEPS array and <StepView /> component, so the field
 * list for AWS / GCP / Azure / Prometheus / Loki / Thanos / OpenSearch
 * cannot drift between the two surfaces.
 *
 * The cluster (VPN) step still lives in the wizard and delegates to
 * <ClusterWorkflow /> — it's listed in STEPS so the wizard can render
 * it in sequence, but the managed-infra page has its own cluster
 * onboarding section and ignores this entry.
 */

import {
  Boxes,
  Check,
  Cloud,
  GitPullRequest,
  LayoutDashboard,
  Loader2,
  Mail,
  MessageSquare,
  PlugZap,
  Server,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react";
import type { Cluster, IntegrationConfig, OnboardingTestResult } from "@/lib/api";

// ── Types ────────────────────────────────────────────────────────────────

export type FieldType = "text" | "password" | "url" | "number" | "textarea";

export interface Field {
  key: string;
  label: string;
  type: FieldType;
  placeholder?: string;
  optional?: boolean;
  help?: string;
}

export type Category =
  | "notifications"
  | "observability"
  | "ticketing"
  | "cluster"
  | "cloud"
  | "sot";

export interface IntegrationStep {
  id: string;
  title: string;
  description: string;
  category: Category;
  icon: React.ComponentType<{ className?: string }>;
  // `null` for the cluster step — handled by <ClusterWorkflow />.
  provider: string | null;
  fields: Field[];
  optional: boolean;
  // For multi-instance providers (kubernetes): the field key whose value is
  // the integration's *name* (the cluster name) rather than part of `config`.
  // ConnectModal lifts it out of the config payload and sends it as `name`.
  nameField?: string;
  // When true, the step renders a "Reachable via federation tunnel"
  // toggle + cluster picker so the URL can resolve inside a federated
  // workload cluster rather than over the public internet. Set on the
  // observability providers; not needed for SMTP / Slack / cloud APIs.
  supportsCluster?: boolean;
}

export interface StepState {
  enabled: boolean;
  values: Record<string, string>;
  saved: boolean;
  error: string | null;
  test: OnboardingTestResult | null;
  // null = dial the URL directly (public internet / same cluster as
  // daalu-api); a UUID = dial through that ClusterTunnel's edge proxy.
  // Only meaningful when the step has supportsCluster=true.
  clusterTunnelId: string | null;
}

// ── Step catalog ─────────────────────────────────────────────────────────

export const STEPS: IntegrationStep[] = [
  {
    id: "slack",
    title: "Slack notifications",
    description:
      "Where Daalu posts daily briefings and incident updates. Slack on its own is enough to get started.",
    category: "notifications",
    icon: MessageSquare,
    provider: "slack",
    optional: true,
    fields: [
      { key: "webhook_url", label: "Incoming-webhook URL", type: "password", placeholder: "https://hooks.slack.com/services/..." },
      { key: "briefing_channel", label: "Briefing channel", type: "text", placeholder: "#operations", optional: true },
      { key: "incidents_channel", label: "Incidents channel", type: "text", placeholder: "#incidents", optional: true, help: "Optional — falls back to briefing_channel if blank." },
    ],
  },
  {
    id: "email",
    title: "Email (SMTP)",
    description: "On-call escalations and optional daily-briefing emails. Skip if you only want Slack.",
    category: "notifications",
    icon: Mail,
    provider: "smtp",
    optional: true,
    fields: [
      { key: "host", label: "SMTP host", type: "text", placeholder: "smtp.example.com" },
      { key: "port", label: "Port", type: "number", placeholder: "587" },
      { key: "username", label: "Username", type: "text" },
      { key: "password", label: "Password", type: "password" },
      { key: "from", label: "From address", type: "text", placeholder: "daalu@example.com" },
      { key: "incident_email_to", label: "On-call recipient", type: "text", placeholder: "oncall@example.com", optional: true },
    ],
  },
  {
    id: "prometheus",
    title: "Prometheus / Alertmanager",
    description: "Pull-mode ingest of firing alerts from the customer's Alertmanager.",
    category: "observability",
    icon: Sparkles,
    provider: "prometheus",
    optional: true,
    supportsCluster: true,
    fields: [
      { key: "url", label: "Alertmanager v2 base URL", type: "url", placeholder: "https://alertmanager.example.com" },
    ],
  },
  {
    id: "loki",
    title: "Loki (logs)",
    description: "On-demand log fetch from the agent. URL is required; basic-auth creds are optional.",
    category: "observability",
    icon: Cloud,
    provider: "loki",
    optional: true,
    supportsCluster: true,
    fields: [
      { key: "url", label: "Loki base URL", type: "url", placeholder: "https://loki.example.com" },
      { key: "user", label: "Username", type: "text", optional: true },
      { key: "password", label: "Password", type: "password", optional: true },
    ],
  },
  {
    id: "thanos",
    title: "Thanos (long-history metrics)",
    description: "Long-history metrics for triage context. Optional.",
    category: "observability",
    icon: Sparkles,
    provider: "thanos",
    optional: true,
    supportsCluster: true,
    fields: [
      { key: "url", label: "Thanos Query base URL", type: "url", placeholder: "https://thanos.example.com" },
    ],
  },
  {
    id: "opensearch",
    title: "OpenSearch (logs)",
    description: "Parallel log index. Use a read-only OpenSearch user scoped to your k8s log indices.",
    category: "observability",
    icon: Cloud,
    provider: "opensearch",
    optional: true,
    supportsCluster: true,
    fields: [
      { key: "url", label: "OpenSearch base URL", type: "url" },
      { key: "user", label: "Username", type: "text", optional: true },
      { key: "password", label: "Password", type: "password", optional: true },
    ],
  },
  {
    id: "grafana",
    title: "Grafana (dashboards)",
    description:
      "Daalu reads your Grafana dashboards and renders them natively (panels filled from your Prometheus/Loki). An API token with Viewer (or Editor, to let AI create dashboards) is required.",
    category: "observability",
    icon: LayoutDashboard,
    provider: "grafana",
    optional: true,
    supportsCluster: true,
    fields: [
      { key: "url", label: "Grafana base URL", type: "url", placeholder: "https://grafana.example.com" },
      { key: "api_token", label: "API token (service account)", type: "password" },
    ],
  },
  {
    id: "pagerduty",
    title: "PagerDuty",
    description: "Ingest incidents from PagerDuty into the alerts feed.",
    category: "ticketing",
    icon: ShieldAlert,
    provider: "pagerduty",
    optional: true,
    fields: [
      { key: "api_token", label: "PagerDuty API token", type: "password" },
      { key: "routing_key", label: "Events routing key", type: "text", optional: true },
    ],
  },
  {
    id: "kubernetes",
    title: "Customer kubeconfig",
    description:
      "Hand the agent a kubeconfig for the customer's cluster. Pair with the cluster tunnel below if the cluster isn't publicly routable.",
    category: "observability",
    icon: Server,
    provider: "kubernetes",
    optional: true,
    nameField: "cluster_name",
    fields: [
      { key: "cluster_name", label: "Cluster name", type: "text", placeholder: "prod", help: "A short label to tell this cluster apart from your others — alerts tagged with it route the agent here." },
      { key: "kubeconfig", label: "Kubeconfig YAML", type: "textarea", placeholder: "apiVersion: v1\nkind: Config\n...", help: "Paste the full kubeconfig the customer issued for the daalu-remediator ServiceAccount." },
      { key: "default_context", label: "Default context name", type: "text", optional: true, placeholder: "customer-slug" },
    ],
  },
  {
    id: "aws",
    title: "AWS account",
    description:
      "Read-only access key (or assume-role ARN) so the agent can describe EC2 instances, fetch CloudWatch logs and metrics, and inspect RDS / Lambda when an alert touches AWS-hosted infra. Permissions needed: ec2:Describe*, logs:FilterLogEvents, cloudwatch:GetMetricStatistics, rds:Describe*, lambda:GetFunction.",
    category: "cloud",
    icon: Cloud,
    provider: "aws",
    optional: true,
    fields: [
      { key: "access_key_id", label: "Access key ID", type: "text", placeholder: "AKIA..." },
      { key: "secret_access_key", label: "Secret access key", type: "password" },
      { key: "region", label: "Default region", type: "text", placeholder: "us-east-1" },
      { key: "session_token", label: "Session token", type: "password", optional: true, help: "Only for short-lived STS credentials." },
      { key: "role_arn", label: "Assume-role ARN", type: "text", optional: true, placeholder: "arn:aws:iam::123456789012:role/DaaluRemediation", help: "If set, the session keys above only need sts:AssumeRole on this ARN; everything else is granted via the role's policy." },
    ],
  },
  {
    id: "gcp",
    title: "Google Cloud project",
    description:
      "Service-account JSON key + project ID. The agent reads Compute Engine instances, Cloud Logging entries, Cloud Monitoring metrics, Cloud SQL state, and Cloud Functions config. Bind these roles to the SA: roles/compute.viewer, roles/logging.viewer, roles/monitoring.viewer, roles/cloudsql.viewer, roles/cloudfunctions.viewer.",
    category: "cloud",
    icon: Cloud,
    provider: "gcp",
    optional: true,
    fields: [
      { key: "project_id", label: "Project ID", type: "text", placeholder: "acme-prod" },
      {
        key: "service_account_json",
        label: "Service-account key JSON",
        type: "textarea",
        placeholder: "{\n  \"type\": \"service_account\",\n  \"project_id\": \"...\",\n  ...\n}",
        help: "Paste the full key JSON the IAM admin downloaded. The key is stored encrypted at rest.",
      },
    ],
  },
  {
    id: "azure",
    title: "Azure subscription",
    description:
      "Service principal (Azure AD app registration) + subscription ID. The agent reads VMs, Log Analytics (via Kusto), Azure Monitor metrics, SQL databases, and App Service / Functions. Required role assignments: Reader at subscription scope, Monitoring Reader, and Log Analytics Reader on the workspace.",
    category: "cloud",
    icon: Cloud,
    provider: "azure",
    optional: true,
    fields: [
      { key: "tenant_id", label: "Azure AD tenant ID", type: "text", placeholder: "00000000-0000-0000-0000-000000000000" },
      { key: "client_id", label: "Application (client) ID", type: "text", placeholder: "00000000-0000-0000-0000-000000000000" },
      { key: "client_secret", label: "Client secret", type: "password" },
      { key: "subscription_id", label: "Subscription ID", type: "text", placeholder: "00000000-0000-0000-0000-000000000000" },
    ],
  },
  {
    id: "nautobot",
    title: "Nautobot (Source of Truth)",
    description:
      "Canonical inventory + intended config for managed devices (servers, BMCs, network gear). Bring your own Nautobot URL + API token, or click 'Provision hosted' if the operator has enabled hosted Nautobot on this deploy. The API token needs at least DCIM + IPAM + ConfigContext view/add/change/delete scoped to your tenant.",
    category: "sot",
    icon: GitPullRequest,
    provider: "nautobot",
    optional: true,
    fields: [
      { key: "url", label: "Nautobot base URL", type: "url", placeholder: "https://nautobot.example.com" },
      { key: "token", label: "API token", type: "password", help: "Write-enabled token scoped to your tenant. Hosted-Nautobot users can leave both fields blank and use the Provision button (POST /api/v1/onboarding/nautobot/provision)." },
      { key: "webhook_secret", label: "Webhook secret", type: "password", optional: true, help: "If set, Nautobot's webhook signatures will be verified on incoming sot.intent.changed events. Recommended in prod." },
    ],
  },
  {
    id: "cluster",
    title: "Cluster tunnel (VPN)",
    description:
      "Optional WireGuard tunnel from a customer cluster back to the operator. This step launches the dedicated VPN workflow inline.",
    category: "cluster",
    icon: Boxes,
    provider: null,
    optional: true,
    fields: [], // delegated to <ClusterWorkflow />
  },
];

export const STEPS_BY_ID: Record<string, IntegrationStep> = Object.fromEntries(
  STEPS.map((s) => [s.id, s])
);

// ── Helpers ──────────────────────────────────────────────────────────────

export function emptyValues(step: IntegrationStep): Record<string, string> {
  return Object.fromEntries(step.fields.map((f) => [f.key, ""]));
}

export function emptyState(step: IntegrationStep): StepState {
  return {
    enabled: false,
    values: emptyValues(step),
    saved: false,
    error: null,
    test: null,
    clusterTunnelId: null,
  };
}

/**
 * Walk a step's fields against the user-entered values and return both
 * the boto3-shaped config payload and the list of required-but-missing
 * field labels. Mirrors `_REDACT_FIELDS` server-side: a value of "***"
 * means "set on the server, not echoed back" and is treated as missing
 * locally so the wizard/modal doesn't overwrite the real secret with
 * the redacted placeholder.
 */
export function payloadFromValues(
  step: IntegrationStep,
  values: Record<string, string>
): { config: Record<string, unknown>; missing: string[] } {
  const config: Record<string, unknown> = {};
  const missing: string[] = [];
  for (const f of step.fields) {
    const raw = values[f.key] ?? "";
    if (raw === "" || raw === "***") {
      if (!f.optional) missing.push(f.label);
      continue;
    }
    config[f.key] = f.type === "number" ? Number(raw) : raw;
  }
  return { config, missing };
}

/**
 * Pre-fill values from a saved IntegrationConfig. Sensitive fields are
 * echoed back as "***" by the API — keep them as-is so the redacted
 * placeholder shows up in the field, and so payloadFromValues knows to
 * treat them as "unchanged, don't resend".
 */
export function valuesFromConfig(
  step: IntegrationStep,
  config: IntegrationConfig | undefined
): Record<string, string> {
  const merged = emptyValues(step);
  if (!config) return merged;
  for (const [k, v] of Object.entries(config.config || {})) {
    if (typeof v === "string") merged[k] = v;
    else if (typeof v === "number") merged[k] = String(v);
  }
  return merged;
}

// ── Field input ──────────────────────────────────────────────────────────

export function FieldInput({
  field,
  value,
  onChange,
}: {
  field: Field;
  value: string;
  onChange: (v: string) => void;
}) {
  const isWide =
    field.type === "textarea" ||
    field.key === "webhook_url" ||
    field.key === "kubeconfig" ||
    field.key === "url";
  return (
    <label className={`text-xs space-y-1 ${isWide ? "sm:col-span-2" : ""}`}>
      <div className="text-muted uppercase tracking-wider flex items-center gap-1">
        {field.label}
        {field.optional && <span className="text-[9px]">(optional)</span>}
      </div>
      {field.type === "textarea" ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          rows={6}
          className="w-full px-3 py-2 rounded-lg bg-bg-elevated/60 border border-line text-xs font-mono"
        />
      ) : (
        <input
          type={field.type === "password" ? "password" : field.type === "number" ? "number" : "text"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
        />
      )}
      {field.help && (
        <div className="text-[10px] text-muted">{field.help}</div>
      )}
    </label>
  );
}

// ── Cluster picker (cross-cluster routing for observability) ─────────────

/**
 * "Reachable via federation tunnel" toggle + cluster dropdown.
 *
 * When a cluster is selected, the backend dials this integration's URL
 * through that cluster's daalu-edge proxy — so the URL field can be an
 * in-cluster name like `http://prometheus.monitoring.svc.cluster.local:9090`
 * without requiring a public ingress. The picker stays hidden until the
 * tenant has at least one connected cluster onboarded (the empty state
 * tells the user where to go to create one).
 */
function ClusterPicker({
  clusters,
  value,
  onChange,
  placeholderForCluster,
}: {
  clusters: Cluster[];
  value: string | null;
  onChange: (clusterTunnelId: string | null) => void;
  placeholderForCluster?: string;
}) {
  const enabled = value !== null;
  const connected = clusters.filter((c) => c.status === "connected");
  return (
    <div className="rounded-lg border border-line bg-bg-elevated/30 p-3 space-y-2">
      <label className="flex items-start gap-2 text-xs cursor-pointer">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => {
            if (!e.target.checked) onChange(null);
            else onChange(connected[0]?.id ?? null);
          }}
          disabled={connected.length === 0}
          className="h-4 w-4 mt-0.5 accent-accent-cyan"
        />
        <div className="space-y-0.5">
          <div className="text-fg">Reachable via federation tunnel</div>
          <div className="text-[10px] text-muted">
            Check this if your observability stack runs in a workload
            cluster that's not exposed publicly. Daalu will dial the URL
            through that cluster's daalu-edge proxy.
            {connected.length === 0 && (
              <>
                {" "}
                <span className="text-accent-amber">
                  No connected clusters yet — onboard one in Managed infra
                  → Kubernetes clusters first.
                </span>
              </>
            )}
          </div>
        </div>
      </label>
      {enabled && connected.length > 0 && (
        <div className="pl-6 space-y-1">
          <select
            value={value ?? ""}
            onChange={(e) => onChange(e.target.value || null)}
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          >
            {connected.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({c.slug}) — {c.tunnel_ip}
              </option>
            ))}
          </select>
          {placeholderForCluster && (
            <div className="text-[10px] text-muted">
              URL hint: <code className="font-mono">{placeholderForCluster}</code>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Test result badge ────────────────────────────────────────────────────

export function TestResult({ result }: { result: OnboardingTestResult }) {
  return (
    <div
      className={`text-[11px] flex items-center gap-1.5 ${
        result.ok ? "text-accent-emerald" : "text-red-500"
      }`}
    >
      {result.ok ? <Check className="h-3 w-3" /> : <X className="h-3 w-3" />}
      <span>{result.message}</span>
      <span className="text-muted">· {result.latency_ms}ms</span>
    </div>
  );
}

// ── Step view (generic integration form) ─────────────────────────────────

/**
 * Renders one step's header + fields + Test-connection button.
 *
 * `hideToggle` suppresses the Enabled/Skipped checkbox — used by the
 * managed-infra "Connect" modal where opening the modal IS the consent.
 * The wizard leaves it visible so users can mark a step Skipped without
 * filling anything in.
 */
export function StepView({
  step,
  state,
  existing,
  onToggle,
  onChange,
  onTest,
  testing,
  hideToggle = false,
  clusters = [],
  onClusterChange,
}: {
  step: IntegrationStep;
  state: StepState;
  existing: IntegrationConfig | undefined;
  onToggle: (enabled: boolean) => void;
  onChange: (key: string, value: string) => void;
  onTest: () => void;
  testing: boolean;
  hideToggle?: boolean;
  // Connected workload clusters available to scope this integration to.
  // Only the parent knows whether to load them — for non-observability
  // steps, leave this empty and the picker stays hidden.
  clusters?: Cluster[];
  onClusterChange?: (clusterTunnelId: string | null) => void;
}) {
  const Icon = step.icon;
  const showFields = hideToggle || state.enabled;
  return (
    <div className="rounded-2xl border border-line bg-bg-card p-5 space-y-4">
      <div className="flex items-start gap-3">
        <div
          className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0"
          style={{
            background: "color-mix(in srgb, var(--accent) 14%, transparent)",
          }}
        >
          <Icon className="h-4 w-4 text-accent-cyan" />
        </div>
        <div className="flex-1">
          <div className="text-base font-medium">{step.title}</div>
          <p className="text-sm text-muted mt-0.5">{step.description}</p>
        </div>
        {!hideToggle && (
          <label className="flex items-center gap-2 text-xs cursor-pointer shrink-0">
            <input
              type="checkbox"
              checked={state.enabled}
              onChange={(e) => onToggle(e.target.checked)}
              className="h-4 w-4 accent-accent-cyan"
            />
            <span className="text-muted">
              {state.enabled ? "Enabled" : "Skipped"}
            </span>
          </label>
        )}
      </div>

      {existing && existing.status === "connected" && showFields && (
        <div className="text-[11px] text-accent-emerald flex items-center gap-1.5">
          <Check className="h-3 w-3" /> Already configured. Edit any field to update.
        </div>
      )}

      {showFields && step.supportsCluster && onClusterChange && (
        <ClusterPicker
          clusters={clusters}
          value={state.clusterTunnelId}
          onChange={onClusterChange}
          placeholderForCluster="http://prometheus.monitoring.svc.cluster.local:9090"
        />
      )}

      {showFields && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {step.fields.map((f) => (
              <FieldInput
                key={f.key}
                field={f}
                value={state.values[f.key] ?? ""}
                onChange={(v) => onChange(f.key, v)}
              />
            ))}
          </div>
          <div className="flex items-center justify-between">
            <button
              type="button"
              onClick={onTest}
              disabled={testing}
              className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-fg disabled:opacity-50 flex items-center gap-1.5"
            >
              {testing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <PlugZap className="h-3.5 w-3.5" />
              )}
              Test connection
            </button>
            {state.test && <TestResult result={state.test} />}
          </div>
        </>
      )}

      {!showFields && step.optional && (
        <div className="text-xs text-muted">
          This step is turned off — Daalu will operate without it.
        </div>
      )}

      {state.error && (
        <div className="text-[11px] text-red-500 flex items-start gap-1.5">
          <X className="h-3 w-3 mt-0.5 shrink-0" /> {state.error}
        </div>
      )}
    </div>
  );
}
