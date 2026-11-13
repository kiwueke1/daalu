/**
 * Typed fetch helpers for the Daalu Automation API.
 * The Next dev server proxies /api/* to the backend (see next.config.js),
 * so the browser sees a single origin.
 */

const BASE = "/api/v1";

/**
 * `?env=<slug>` for the calls that read or write one NV-CM management
 * environment's bundled Nautobot (topology, devices). Each environment has its
 * own source of truth, so leaving the slug off means "the tenant's default
 * (primary) one" — which is what every pre-multi-environment caller wants.
 * Returns "" for an absent slug so it can be appended unconditionally.
 */
function envQuery(env?: string, sep: "?" | "&" = "?"): string {
  return env ? `${sep}env=${encodeURIComponent(env)}` : "";
}

export class UnauthorizedError extends Error {
  constructor(message = "unauthorized") {
    super(message);
    this.name = "UnauthorizedError";
  }
}

/**
 * A non-2xx response. `message` keeps the historical
 * "<status> <statusText>: <body>" format; `status` and `detail` (FastAPI's
 * `detail` string when the body is JSON) let a caller show a 403 / 422 nicely.
 */
export class ApiError extends Error {
  status: number;
  body: string;
  detail: string;
  constructor(status: number, body: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    let detail = body;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (typeof parsed?.detail === "string") detail = parsed.detail;
      else if (parsed?.detail !== undefined) detail = JSON.stringify(parsed.detail);
    } catch {
      /* not JSON */
    }
    this.detail = detail;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  // Multipart uploads: let the browser set the Content-Type (with boundary)
  // by *not* injecting our JSON default.
  const isMultipart = init.body instanceof FormData;
  const headers = isMultipart
    ? (init.headers ?? {})
    : { "Content-Type": "application/json", ...(init.headers || {}) };
  const res = await fetch(`${BASE}${path}`, {
    headers,
    cache: "no-store",
    credentials: "include",
    ...init,
  });
  if (res.status === 401) {
    // /login, /accept-invite, /signup and /verify-email handle their own auth
    // lifecycle. Bouncing them to /login on a 401 would break those flows
    // (invite redemption + self-service signup are used by logged-out users).
    if (
      typeof window !== "undefined"
      && !window.location.pathname.startsWith("/login")
      && !window.location.pathname.startsWith("/accept-invite")
      && !window.location.pathname.startsWith("/signup")
      && !window.location.pathname.startsWith("/verify-email")
    ) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.replace(`/login?next=${next}`);
    }
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new ApiError(res.status, body, `${res.status} ${res.statusText}: ${body || path}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export type RoutingPolicy =
  | "local_first"
  | "hybrid"
  | "external_only"
  | "sovereign";

export type InferenceTier =
  | "local"
  | "external_classifier"
  | "external_quality"
  | "sovereign";

export interface Sku {
  id: string;
  slug: string;
  name: string;
  tagline: string;
  description: string;
  routing_policy: RoutingPolicy;
  monthly_base_usd: number;
  included_events_per_month: number;
  price_local_in_per_mtok: number;
  price_local_out_per_mtok: number;
  price_external_classifier_in_per_mtok: number;
  price_external_classifier_out_per_mtok: number;
  price_external_quality_in_per_mtok: number;
  price_external_quality_out_per_mtok: number;
  monthly_soft_cap_usd: number;
  is_active: boolean;
  display_order: number;
}

export interface PeriodTotals {
  period_start: string;
  period_end: string;
  events: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  base_usd: number;
  included_events: number;
  included_events_used: number;
}

export interface BreakdownRow {
  key: string;
  events: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
}

export interface DailyPoint {
  day: string;
  events: number;
  cost_usd: number;
}

export interface LocalGpuStatus {
  configured: boolean;
  healthy: boolean;
  base_url: string;
  model_classifier: string;
  model_quality: string;
  source?: "sovereign" | "local" | "none";
  state?: string | null;
}

export interface GpuCluster {
  cluster_tunnel_id: string;
  slug: string;
  name: string;
  tunnel_ip: string;
  suggested_base_url: string;
}

export interface GpuStatus {
  configured: boolean;
  state?: string | null;
  healthy?: boolean;
  base_url?: string | null;
  model_classifier?: string | null;
  source?: string;
  cluster_tunnel_id?: string | null;
  gpu_node?: string | null;
  last_error?: string | null;
}

// Live deploy progress. A first vLLM boot is three sequential multi-minute
// waits (image pull → weights → CUDA-graph compile) that all look the same
// from outside, so the panel shows kubelet events until a container starts
// and vLLM's own logs once it has.
export interface GpuProgressContainer {
  name: string;
  ready: boolean;
  restarts: number;
  reason?: string | null;
  message?: string | null;
}

export interface GpuProgressPod {
  name: string;
  phase?: string | null;
  node?: string | null;
  containers: GpuProgressContainer[];
  logs?: string | null;
}

export interface GpuProgressEvent {
  type?: string | null;
  reason?: string | null;
  object?: string | null;
  count: number;
  message?: string | null;
  at?: string | null;
}

export interface GpuProgress {
  phase?: string | null;
  pods: GpuProgressPod[];
  events: GpuProgressEvent[];
}

export interface GpuOnboardBody {
  target_cluster_tunnel_id?: string | null;
  gpu_node?: string | null;
  gpu_class?: string;
  hf_token?: string | null;
  model_classifier?: string;
  model_quality?: string | null;
  base_url?: string | null;
  endpoint_token?: string | null;
}

/** What Daalu deployed onto the tenant's GPU (GET /onboarding/gpu/serving). */
export interface GpuServing {
  model: string;
  hf_model: string;
  open_weights: boolean;
  quantization: string;
  engine: string;
  image: string;
  gpu_memory_utilization: number;
  context_len: number;
  tool_calling: boolean;
  cluster_name?: string | null;
  cluster_slug?: string | null;
  node?: string | null;
  namespace: string;
  gpu_class: string;
  state: string;
  healthy: boolean;
  endpoint?: string | null;
  online_since?: string | null;
}

/** The NVIDIA GPU Operator on a connected cluster (GET /onboarding/gpu/operator). */
export interface GpuOperatorStatus {
  /** absent | installing | blocked | error | ready */
  phase: "absent" | "installing" | "blocked" | "error" | "ready";
  installed: boolean;
  gpu_nodes: number;
  distribution?: string | null;
  pods_total: number;
  pods_ready: number;
  /** What's in the way, in a sentence. */
  hint?: string | null;
  /** k3s: the commands that put the NVIDIA runtime on the node. */
  host_commands?: string | null;
}

export interface GpuNode {
  name: string;
  gpu_class?: string | null;
  allocatable_gpus?: string | null;
  ready?: boolean;
}

export const api = {
  events: {
    list: (params: { module?: string; since_hours?: number; limit?: number } = {}) => {
      const qs = new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => [k, String(v)])
      );
      return request<Event[]>(`/events?${qs}`);
    },
  },
  briefings: {
    latest: (channel: string) =>
      request<Briefing>(`/briefings/latest?channel=${channel}`),
    list: (channel?: string) =>
      request<Briefing[]>(`/briefings${channel ? `?channel=${channel}` : ""}`),
    get: (id: string) => request<Briefing>(`/briefings/${id}`),
    generate: (channel: string) =>
      request<Briefing>(`/briefings/${channel}/generate`, { method: "POST" }),
  },
  alerts: {
    list: (params: { module?: string; status?: string; severity?: string } = {}) => {
      const qs = new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => [k, String(v)])
      );
      return request<Alert[]>(`/alerts?${qs}`);
    },
    get: (id: string) => request<Alert>(`/alerts/${id}`),
    occurrences: (id: string) =>
      request<AlertOccurrence[]>(`/alerts/${id}/occurrences`),
    acknowledge: (id: string) => request<Alert>(`/alerts/${id}/acknowledge`, { method: "POST" }),
    resolve: (id: string) => request<Alert>(`/alerts/${id}/resolve`, { method: "POST" }),
    chat: {
      list: (alertId: string) =>
        request<AlertChatMessage[]>(`/alerts/${alertId}/chat`),
      send: (alertId: string, content: string) =>
        request<AlertChatMessage[]>(`/alerts/${alertId}/chat`, {
          method: "POST",
          body: JSON.stringify({ content }),
        }),
      triage: (alertId: string, opts: { force?: boolean } = {}) =>
        request<AlertChatMessage[]>(
          `/alerts/${alertId}/triage${opts.force ? "?force=true" : ""}`,
          { method: "POST" }
        ),
      approve: (alertId: string, actionId: string) =>
        request<ApproveActionResult>(
          `/alerts/${alertId}/actions/${actionId}/approve`,
          { method: "POST" }
        ),
      reject: (alertId: string, actionId: string) =>
        request<AlertChatMessage[]>(
          `/alerts/${alertId}/actions/${actionId}/reject`,
          { method: "POST" }
        ),
    },
  },
  recommendations: {
    list: (params: { module?: string; status?: string } = {}) => {
      const qs = new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => [k, String(v)])
      );
      return request<Recommendation[]>(`/recommendations?${qs}`);
    },
    approve: (id: string) =>
      request<Recommendation>(`/recommendations/${id}/approve`, { method: "POST" }),
    dismiss: (id: string) =>
      request<Recommendation>(`/recommendations/${id}/dismiss`, { method: "POST" }),
  },
  agents: {
    list: () => request<AgentDescriptor[]>("/agents"),
    runs: (agent_name?: string) =>
      request<AgentRun[]>(`/agents/runs${agent_name ? `?agent_name=${agent_name}` : ""}`),
  },
  workflows: {
    list: () => request<WorkflowDescriptor[]>("/workflows"),
    runs: (opts: { module?: string; source?: "alert" } = {}) => {
      const qs = new URLSearchParams();
      if (opts.module) qs.set("module", opts.module);
      if (opts.source) qs.set("source", opts.source);
      const q = qs.toString();
      return request<WorkflowRun[]>(`/workflows/runs${q ? `?${q}` : ""}`);
    },
    runDetail: (runId: string) =>
      request<WorkflowRun>(`/workflows/runs/${runId}`),
    run: (name: string, input: Record<string, unknown> = {}) =>
      request<{ run_id: string }>("/workflows/run", {
        method: "POST",
        body: JSON.stringify({ name, input }),
      }),
  },
  integrations: {
    list: () => request<IntegrationDescriptor[]>("/integrations"),
    ingest: (provider: string) =>
      request<{ events_emitted: number }>(`/integrations/${provider}/ingest`, {
        method: "POST",
      }),
    listConfig: () =>
      request<IntegrationConfig[]>("/integrations/config"),
    putConfig: (
      provider: string,
      payload: {
        config: Record<string, unknown>;
        name?: string;
        // Multi-instance providers (kubernetes): pass an existing row's id to
        // edit it; omit to create a new named instance. Single-instance
        // providers ignore it and upsert their one row.
        cluster_id?: string;
        // Omit to leave whatever's stored; pass `null` to detach; pass a
        // UUID to attach/move. Matches the backend's
        // ``model_fields_set`` semantics so the wizard can edit
        // credentials without forgetting which cluster they belong to.
        cluster_tunnel_id?: string | null;
      }
    ) =>
      request<IntegrationConfig>(`/integrations/config/${provider}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      }),
    // clusterId selects which instance to delete for multi-instance
    // providers (kubernetes); omit for single-instance providers.
    deleteConfig: (provider: string, clusterId?: string) =>
      request<void>(
        `/integrations/config/${provider}${clusterId ? `?cluster_id=${clusterId}` : ""}`,
        { method: "DELETE" }
      ),
  },
  infra: {
    incidents: (params: { status?: string; severity?: string } = {}) => {
      const qs = new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => [k, String(v)])
      );
      return request<Incident[]>(`/infra/incidents?${qs}`);
    },
    promoteAlertToIncident: (
      alertId: string,
      payload: { title: string; severity?: string; summary?: string }
    ) =>
      request<Incident>(`/infra/incidents/from-alert/${alertId}`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  },
  copilot: {
    // Sends the cluster whose page is open, so "what pods are running?"
    // asked on a cluster page is answered about that cluster.
    ask: (query: string) =>
      request<{ answer: string; references: { id: string; summary: string }[] }>(
        "/copilot/ask",
        {
          method: "POST",
          body: JSON.stringify({ query, cluster: currentClusterSlug() }),
        }
      ),
  },
  reports: {
    schema: () => request<ReportsSchema>("/reports/query/schema"),
    runQuery: (payload: ReportsQueryRequest) =>
      request<ReportsQueryResponse>("/reports/query", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    translate: (payload: { question: string; entity_hint?: string | null }) =>
      request<ReportsTranslateResponse>("/reports/query/translate", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    saved: {
      list: () => request<SavedReport[]>("/reports/saved"),
      create: (payload: { name: string; definition: ReportsQueryRequest; pinned?: boolean }) =>
        request<SavedReport>("/reports/saved", {
          method: "POST",
          body: JSON.stringify(payload),
        }),
      get: (id: string) => request<SavedReport>(`/reports/saved/${id}`),
      update: (
        id: string,
        payload: { name?: string; definition?: ReportsQueryRequest; pinned?: boolean }
      ) =>
        request<SavedReport>(`/reports/saved/${id}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        }),
      remove: (id: string) =>
        request<void>(`/reports/saved/${id}`, { method: "DELETE" }),
    },
    export: (payload: ReportsQueryRequest, format: "csv" | "json") => {
      // Returns a Blob the caller can save / link to.
      return fetch(`/api/v1/reports/query/export?format=${format}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(async (res) => {
        if (!res.ok) {
          throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
        }
        return res.blob();
      });
    },
    schedules: {
      list: () => request<ReportSchedule[]>("/reports/schedules"),
      create: (payload: {
        name: string;
        saved_report_id: string;
        cron: string;
        destination: "slack" | "email";
        recipient?: string;
        format?: "markdown" | "csv";
      }) =>
        request<ReportSchedule>("/reports/schedules", {
          method: "POST",
          body: JSON.stringify(payload),
        }),
      update: (
        id: string,
        payload: {
          name?: string;
          cron?: string;
          destination?: "slack" | "email";
          recipient?: string;
          format?: "markdown" | "csv";
          enabled?: boolean;
        }
      ) =>
        request<ReportSchedule>(`/reports/schedules/${id}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        }),
      remove: (id: string) =>
        request<void>(`/reports/schedules/${id}`, { method: "DELETE" }),
    },
    dashboards: {
      list: () => request<Dashboard[]>("/reports/dashboards"),
      get: (id: string) => request<Dashboard>(`/reports/dashboards/${id}`),
      create: (payload: { name: string; tiles?: DashboardTile[]; home_pinned?: boolean }) =>
        request<Dashboard>("/reports/dashboards", {
          method: "POST",
          body: JSON.stringify(payload),
        }),
      update: (
        id: string,
        payload: { name?: string; tiles?: DashboardTile[]; home_pinned?: boolean }
      ) =>
        request<Dashboard>(`/reports/dashboards/${id}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        }),
      remove: (id: string) =>
        request<void>(`/reports/dashboards/${id}`, { method: "DELETE" }),
    },
  },
  ide: {
    eligibility: () =>
      request<{ eligible: boolean; reason: string }>("/ide/eligibility"),
    models: () => request<CodingModelView[]>("/ide/models"),
    me: () =>
      request<WorkspaceView | null>("/ide/sessions/me").catch(() => null),
    create: (spec: {
      profile: "small" | "medium" | "large";
      model?: string | null;
      git_repo_url?: string | null;
      git_branch?: string;
      // Personal-access-token for a private repo. Write-only — never
      // returned. Omit/null for a public repo or an empty workspace.
      git_token?: string | null;
    }) =>
      request<WorkspaceView>("/ide/sessions", {
        method: "POST",
        body: JSON.stringify({ git_branch: "main", ...spec }),
      }),
    destroy: () =>
      request<{ status: string }>("/ide/sessions/me", { method: "DELETE" }),
  },
  cli: {
    // Mint a one-time `daalu login --code …` command for the express flow.
    loginCommand: () =>
      request<{ command: string; device_code: string; expires_in: number }>(
        "/cli/login-command",
        { method: "POST" },
      ),
    // The /cli/activate device-approval page.
    devicePending: (userCode: string) =>
      request<{
        user_code: string;
        client_name: string | null;
        status: string;
        expires_at: string;
      }>(`/cli/device/pending?user_code=${encodeURIComponent(userCode)}`),
    deviceApprove: (userCode: string) =>
      request<{ status: string }>("/cli/device/approve", {
        method: "POST",
        body: JSON.stringify({ user_code: userCode }),
      }),
    deviceDeny: (userCode: string) =>
      request<void>("/cli/device/deny", {
        method: "POST",
        body: JSON.stringify({ user_code: userCode }),
      }),
  },
  onboarding: {
    status: () => request<OnboardingStatus>("/onboarding/status"),
    test: (
      provider: string,
      config: Record<string, unknown>,
      cluster_tunnel_id?: string | null,
    ) =>
      request<OnboardingTestResult>(`/onboarding/test/${provider}`, {
        method: "POST",
        body: JSON.stringify({ config, cluster_tunnel_id }),
      }),
    validate: (provider: string, config: Record<string, unknown>) =>
      request<{ ok: boolean; missing: string[] }>(
        `/onboarding/validate/${provider}`,
        {
          method: "POST",
          body: JSON.stringify({ config }),
        }
      ),
    // Provision the per-tenant NV-CM (network/server config-management) stack.
    // Returns 202 as soon as the install is *scheduled* — it does NOT wait for
    // the chart to roll out (that takes minutes). Poll configManagerProgress()
    // for pending → provisioning → active; that endpoint is also what writes
    // Integration(provider=config_manager) once the stack is up.
    provisionConfigManager: (body: {
      components?: Record<string, boolean>;
      size_profile?: string;
      base_hostname?: string | null;
    }) =>
      request<ProvisionConfigManagerResult>(
        "/onboarding/config-manager/provision",
        { method: "POST", body: JSON.stringify(body) },
      ),
    // Running vs available NV-CM chart versions (admin only). Cheap enough to
    // poll — the page speeds this up while an upgrade is in flight.
    configManagerVersion: () =>
      request<ConfigManagerVersion>("/onboarding/config-manager/version"),
    // Live install progress for the NV-CM stack (admin only): row state plus
    // per-pod readiness and the notable kubelet events. Reaches through the
    // tunnel to the tenant's apiserver, so the page polls it fast (5s) only
    // while the install is actually moving and backs off once it settles.
    configManagerProgress: () =>
      request<ConfigManagerProgress>("/onboarding/config-manager/progress"),
    // Schedule a chart upgrade (admin only). Returns 202 as soon as the
    // controller accepts it — it does NOT wait for the release to roll out.
    // Poll configManagerVersion() for provisioning → active. Omit
    // `chart_version` to take the latest published chart. Rejections (e.g.
    // "not newer than the current") come back as a 409 with a {detail} body.
    upgradeConfigManager: (body: { chart_version?: string | null } = {}) =>
      request<UpgradeConfigManagerResult>(
        "/onboarding/config-manager/upgrade",
        { method: "POST", body: JSON.stringify(body) },
      ),
    // ── Named environments ─────────────────────────────────────────────
    // A tenant may run several management environments. These address one by
    // id; the four singular routes above stay valid and act on the PRIMARY
    // environment, so nothing that predates this has to change.
    //
    // Every environment this tenant runs, primary first. Empty (not an error)
    // when provisioning isn't configured on the deploy. Admin only.
    configManagerEnvironments: () =>
      request<ConfigManagerEnvironment[]>(
        "/onboarding/config-manager/environments",
      ),
    // Create a named environment. Returns 202 as soon as the install is
    // *scheduled* — poll configManagerEnvironmentProgress() from there.
    //
    // The controller derives the slug, namespace and public hostnames from
    // `name` and all three are immutable afterwards, so it rejects a name it
    // can't use: 400 when the name isn't a usable label or collides with a
    // tool-console prefix, 409 when the tenant already has that slug. Both
    // carry a {detail} written for the person who typed the name — show it
    // verbatim rather than substituting a generic failure.
    // admin_username/admin_password set the break-glass Nautobot login. Both
    // optional — omit them and the server defaults the username and generates
    // the password, returning it once on the response. 422 when the credential
    // is one Nautobot would reject.
    createConfigManagerEnvironment: (body: {
      name: string;
      components?: Record<string, boolean>;
      size_profile?: string;
      admin_username?: string;
      admin_password?: string;
    }) =>
      request<CreateConfigManagerEnvironmentResult>(
        "/onboarding/config-manager/environments",
        { method: "POST", body: JSON.stringify(body) },
      ),
    // Per-environment twin of configManagerProgress(), same payload. Reaches
    // through the tunnel to the tenant's apiserver, so poll it fast (5s) only
    // while the install is moving. 404s once the environment is gone.
    configManagerEnvironmentProgress: (envId: string) =>
      request<ConfigManagerProgress>(
        `/onboarding/config-manager/environments/${envId}/progress`,
      ),
    // Per-environment twin of upgradeConfigManager(): 202 as soon as the
    // controller accepts it, then poll progress. Omit `chart_version` to take
    // the latest published chart. Rejections (404 no such environment, 409
    // "not newer than the current") come back with a {detail} body.
    upgradeConfigManagerEnvironment: (
      envId: string,
      body: { chart_version?: string | null } = {},
    ) =>
      request<UpgradeConfigManagerResult>(
        `/onboarding/config-manager/environments/${envId}/upgrade`,
        { method: "POST", body: JSON.stringify(body) },
      ),
    // Schedule teardown (202). The hub also removes the config_manager
    // integration row when this was the primary environment, so no ghost
    // tile survives. Poll configManagerEnvironments() — the row reports
    // `deleting` until the controller finishes, then disappears entirely.
    // `force` removes the record without waiting for a clean uninstall: the
    // way out when the host cluster is gone and teardown can never finish.
    deleteConfigManagerEnvironment: (envId: string, force = false) =>
      request<{ detail: string }>(
        `/onboarding/config-manager/environments/${envId}` +
          (force ? "?force=true" : ""),
        { method: "DELETE" },
      ),
    // Local-GPU onboarding: deploy the vLLM stack onto a joined cluster
    // over the tunnel + register it as the tenant's SOVEREIGN tier.
    gpu: {
      status: () => request<GpuStatus>("/onboarding/gpu"),
      clusters: () => request<GpuCluster[]>("/onboarding/gpu/clusters"),
      nodes: (clusterTunnelId?: string | null) =>
        request<GpuNode[]>(
          `/onboarding/gpu/nodes${clusterTunnelId ? `?cluster_tunnel_id=${clusterTunnelId}` : ""}`,
        ),
      // The NVIDIA GPU Operator on a cluster that advertises no GPU yet.
      operator: (clusterTunnelId: string) =>
        request<GpuOperatorStatus>(
          `/onboarding/gpu/operator?cluster_tunnel_id=${clusterTunnelId}`,
        ),
      // 202: the install runs in the background; poll operator().
      installOperator: (clusterTunnelId: string) =>
        request<{ state: string }>("/onboarding/gpu/operator", {
          method: "POST",
          body: JSON.stringify({ cluster_tunnel_id: clusterTunnelId }),
        }),
      // Pod phase + kubelet events + vLLM logs for an in-flight deploy.
      progress: () => request<GpuProgress>("/onboarding/gpu/progress"),
      // What is deployed on the card, as the controller renders it.
      serving: () => request<GpuServing | null>("/onboarding/gpu/serving"),
      // Returns 202 as soon as the controller accepts the spec — it does NOT
      // wait for vLLM. Poll `status()` for provisioning → active.
      provision: (body: GpuOnboardBody) =>
        request<GpuStatus>("/onboarding/gpu/provision", {
          method: "POST",
          body: JSON.stringify(body),
        }),
      remove: () =>
        request<GpuStatus>("/onboarding/gpu", { method: "DELETE" }),
    },
  },
  clusters: {
    list: () => request<Cluster[]>("/clusters"),
    get: (slug: string) => request<Cluster>(`/clusters/${slug}`),
    onboard: (payload: { slug: string; name: string }) =>
      request<ClusterCreate>("/clusters", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    remove: (slug: string) =>
      request<void>(`/clusters/${slug}`, { method: "DELETE" }),
    overview: (slug: string) =>
      request<ClusterOverview>(`/clusters/${slug}/overview`),
    observability: (slug: string) =>
      request<ClusterStore[]>(`/clusters/${slug}/observability`),
    kubectlCatalog: () =>
      request<KubectlCommandSpec[]>("/clusters/kubectl/catalog"),
    runKubectl: (slug: string, body: KubectlRunRequest) =>
      request<{ results: KubectlResult[] }>(`/clusters/${slug}/kubectl`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },
  dashboards: {
    list: () => request<DashboardList>("/dashboards"),
    get: (uid: string) => request<Dashboard>(`/dashboards/${uid}`),
    query: (body: DashboardQueryRequest) =>
      request<DashboardQueryResult>("/dashboards/query", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    aiCreate: (body: DashboardAiCreateRequest) =>
      request<DashboardAiCreateResult>("/dashboards/ai", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },
  observability: {
    install: (provider: string) =>
      request<{ applied: string[]; finalizing: boolean }>(
        `/observability/${provider}/install`,
        { method: "POST" },
      ),
    overview: (provider: string, cluster?: string) =>
      request<ObservabilityOverview>(
        `/observability/${provider}/overview${clusterQs(cluster)}`,
      ),
    catalog: (provider: string) =>
      request<ObsQuerySpec[]>(`/observability/${provider}/catalog`),
    runQuery: (provider: string, body: ObsQueryRequest, cluster?: string) =>
      request<{ results: ObsQueryResult[] }>(`/observability/${provider}/query${clusterQs(cluster)}`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },
  changeProposals: {
    list: (
      params: { status?: string; device_id?: string; kind?: string; limit?: number } = {},
    ) => {
      const qs = new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => [k, String(v)]),
      );
      return request<ChangeProposal[]>(`/change-proposals?${qs}`);
    },
    get: (id: string) => request<ChangeProposal>(`/change-proposals/${id}`),
    approve: (id: string) =>
      request<ChangeProposal>(`/change-proposals/${id}/approve`, { method: "POST" }),
    reject: (id: string) =>
      request<ChangeProposal>(`/change-proposals/${id}/reject`, { method: "POST" }),
  },
  sot: {
    hostedStatus: () =>
      request<{ hosted_available: boolean; detail: string }>(
        "/onboarding/nautobot/hosted-status",
      ),
    provisionNautobot: () =>
      request<{
        ok: boolean;
        message: string;
        url: string;
        nautobot_tenant_slug: string;
        nautobot_tenant_id: string;
      }>("/onboarding/nautobot/provision", { method: "POST" }),
    // Every call takes an optional NV-CM environment slug — each management
    // environment bundles its own Nautobot, so the slug picks which inventory
    // is read or written. Omit it for the tenant's default (primary) one.
    devices: {
      // Wiring probe: is a Nautobot resolved for this tenant (NV-CM-bundled
      // or BYO), and does it answer through the resolved transport?
      status: (env?: string) =>
        request<{
          wired: boolean;
          source: "nvcm" | "byo" | null;
          reachable: boolean;
          detail: string;
        }>(`/sot/devices/_status${envQuery(env)}`),
      list: (params: { platform?: string } = {}, env?: string) => {
        const qs = new URLSearchParams(
          Object.entries(params)
            .filter(([, v]) => v !== undefined && v !== null && v !== "")
            .map(([k, v]) => [k, String(v)]),
        );
        if (env) qs.set("env", env);
        return request<SotDevice[]>(`/sot/devices?${qs}`);
      },
      get: (id: string, env?: string) =>
        request<SotDevice>(`/sot/devices/${id}${envQuery(env)}`),
      intent: (id: string, env?: string) =>
        request<SotIntent>(`/sot/devices/${id}/intent${envQuery(env)}`),
      catalog: (env?: string) =>
        request<SotCatalog>(`/sot/devices/_catalog/list${envQuery(env)}`),
      create: (payload: SotDeviceCreate, env?: string) =>
        request<SotDevice>(`/sot/devices${envQuery(env)}`, {
          method: "POST",
          body: JSON.stringify(payload),
        }),
      updateIntent: (
        id: string,
        facts: Record<string, unknown>,
        env?: string,
      ) =>
        request<SotIntent>(`/sot/devices/${id}/intent${envQuery(env)}`, {
          method: "PUT",
          body: JSON.stringify({ facts }),
        }),
      remove: (id: string, env?: string) =>
        request<void>(`/sot/devices/${id}${envQuery(env)}`, { method: "DELETE" }),
      monitoringStatus: (env?: string) =>
        request<SotMonitoringStatus>(
          `/sot/devices/monitoring/status${envQuery(env)}`,
        ),
      monitoringConnect: (
        collector_host: string,
        collector_port = 5514,
        env?: string,
      ) =>
        request<SotMonitoringConnectOut>(
          `/sot/devices/monitoring/connect${envQuery(env)}`,
          {
            method: "POST",
            body: JSON.stringify({ collector_host, collector_port }),
          },
        ),
      // `createMissing` lets the run create the sites / device types / roles /
      // platforms / parent prefixes the file names but the environment's
      // Nautobot doesn't have — a freshly provisioned one has no locations at
      // all, so without it the first import into any new environment fails
      // every row. The dry-run only reports them; nothing is written until
      // the operator applies.
      bulkImport: (
        file: File,
        dryRun: boolean,
        env?: string,
        createMissing = true,
      ) => {
        const fd = new FormData();
        fd.append("file", file);
        return request<SotBulkImportResult>(
          `/sot/devices/bulk-import?dry_run=${dryRun ? "true" : "false"}` +
            `&create_missing=${createMissing ? "true" : "false"}` +
            envQuery(env, "&"),
          { method: "POST", body: fd },
        );
      },
      reconcile: (id: string, env?: string) =>
        request<SotReconcileResult>(
          `/sot/devices/${id}/reconcile${envQuery(env)}`,
          { method: "POST" },
        ),
    },
    // The rest of the model, uploaded the same way the inventory is. Three
    // artifacts rather than one file because they have a dependency order —
    // an address cannot land on an interface that does not exist yet — and
    // the backend enforces it with a real error instead of a partial import.
    fabric: {
      // `artifact` is the tab the file was dropped on. The file declares its
      // own `kind:` too; sending both lets the API reject a mismatch by name
      // rather than importing the wrong thing and reporting zero rows.
      import: (
        file: File,
        artifact: SotFabricArtifact,
        dryRun: boolean,
        env?: string,
      ) => {
        const fd = new FormData();
        fd.append("file", file);
        return request<SotFabricImportResult>(
          `/sot/fabric/import?dry_run=${dryRun ? "true" : "false"}` +
            `&artifact=${artifact}` +
            envQuery(env, "&"),
          { method: "POST", body: fd },
        );
      },
    },
  },
  auth: {
    me: () => request<CurrentUser>("/auth/me"),
    // Public: which login mode the hub is in ("password" | "oidc") and the
    // path to start SSO. Used by the login page to render the form or
    // bounce straight to Keycloak.
    config: () => request<AuthConfig>("/auth/config"),
    login: (email: string, password: string) =>
      request<LoginResult>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      }),
    logout: () => request<void>("/auth/logout", { method: "POST" }),
    // Public: create a local account. Always resolves 202 with a neutral body
    // (no account enumeration) — the user is told to check their email.
    signup: (payload: {
      email: string;
      password: string;
      full_name?: string | null;
      organization?: string | null;
    }) =>
      request<{ status: string }>("/auth/signup", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    // Public: redeem the email-verification token. On success the account is
    // activated and the session cookie is set, so the caller is logged in.
    verifyEmail: (token: string) =>
      request<LoginResult>("/auth/verify-email", {
        method: "POST",
        body: JSON.stringify({ token }),
      }),
    // Public: re-send a verification link. Neutral 204/202 regardless.
    resendVerification: (email: string) =>
      request<void>("/auth/verify-email/resend", {
        method: "POST",
        body: JSON.stringify({ email }),
      }),
    updateMe: (payload: {
      full_name?: string | null;
      preferences?: Record<string, unknown>;
    }) =>
      request<CurrentUser>("/auth/me", {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    changePassword: (current_password: string, new_password: string) =>
      request<void>("/auth/password", {
        method: "POST",
        body: JSON.stringify({ current_password, new_password }),
      }),
    tokens: {
      list: () => request<PersonalAccessToken[]>("/auth/tokens"),
      create: (name: string, expires_in_days?: number | null) =>
        request<PersonalAccessTokenCreated>("/auth/tokens", {
          method: "POST",
          body: JSON.stringify({ name, expires_in_days: expires_in_days ?? null }),
        }),
      revoke: (id: string) =>
        request<void>(`/auth/tokens/${id}`, { method: "DELETE" }),
    },
    // Invite-side of auth — unauthenticated calls used by the
    // accept-invite page. Note these are under /api/v1/auth/* on the
    // backend so they bypass the AuthGate middleware naturally.
    previewInvite: (token: string) =>
      request<InvitePreview>(`/auth/invite/${encodeURIComponent(token)}`),
    redeemInvite: (payload: {
      token: string;
      full_name?: string | null;
      password: string;
    }) =>
      request<LoginResult>("/auth/redeem-invite", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  },
  invites: {
    list: (include_terminal = false) =>
      request<Invite[]>(
        `/invites${include_terminal ? "?include_terminal=true" : ""}`,
      ),
    create: (payload: { email: string; role: "admin" | "user"; message?: string | null }) =>
      request<Invite>("/invites", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    resend: (id: string) =>
      request<Invite>(`/invites/${id}/resend`, { method: "POST" }),
    revoke: (id: string) =>
      request<void>(`/invites/${id}`, { method: "DELETE" }),
  },
  feedback: {
    send: (payload: { message: string; category?: string; page_url?: string }) =>
      request<{ id: string; created_at: string }>("/feedback", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  },
  meta: {
    version: () =>
      // Lives at /version (no API prefix). Use absolute path so the
      // shared request() wrapper's `${BASE}${path}` doesn't apply.
      fetch("/version", { credentials: "include", cache: "no-store" }).then(
        (r) => r.json() as Promise<BuildInfo>,
      ),
  },
  aiFactory: {
    // Role + capability probe — drives which panels the page renders.
    overview: () => request<AiFactoryOverview>("/ai-factory/overview"),
    // owner/provider get a per-GPU hardware summary; consumer gets a
    // usage-centric summary instead. The page picks the shape by role.
    gpuSummary: () => request<AiFactoryGpuSummary>("/ai-factory/gpu/summary"),
    // `card` (the GPU's stable id = its DCGM UUID) scopes the series to a
    // single physical card for the detail view; omit it for the tenant-wide
    // aggregate. Scoping by UUID — not the gpu index — keeps two single-GPU
    // cards (both gpu="0") distinct.
    timeseries: (metric: AiFactoryMetric, range: AiFactoryRange, card?: string) =>
      request<AiFactoryTimeseries>(
        `/ai-factory/gpu/timeseries?metric=${metric}&range=${range}` +
          (card != null ? `&card=${encodeURIComponent(card)}` : ""),
      ),
    events: (card?: string) =>
      request<{ events: AiFactoryGpuEvent[] }>(
        "/ai-factory/gpu/events" +
          (card != null ? `?card=${encodeURIComponent(card)}` : ""),
      ),
    alerts: (card?: string) =>
      request<{ alerts: AiFactoryAlert[] }>(
        "/ai-factory/alerts" +
          (card != null ? `?card=${encodeURIComponent(card)}` : ""),
      ),
    // ── Admin-only ──
    validateObservability: () =>
      request<AiFactoryValidateResult>("/ai-factory/observability/validate", {
        method: "POST",
      }),
    // Returns a typed union: a stressful run (dcgmi -r2/-r3, NCCL) without
    // `acknowledged` comes back as { requiresAck, warning } (HTTP 412) so the
    // UI can show the warning and re-submit with acknowledged=true.
    runDiagnostic: async (body: {
      kind: AiFactoryDiagKind;
      level?: 1 | 2 | 3;
      acknowledged?: boolean;
    }): Promise<AiFactoryDiagStart> => {
      const res = await fetch(`${BASE}/ai-factory/gpu/diagnostics`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        cache: "no-store",
        body: JSON.stringify(body),
      });
      if (res.status === 412) {
        const data = (await res.json().catch(() => ({}))) as {
          detail?: { warning?: string };
        };
        return {
          requiresAck: true,
          warning: data.detail?.warning ?? "This diagnostic needs confirmation.",
        };
      }
      if (!res.ok) {
        throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
      }
      const data = (await res.json()) as { id: string; state: "pending" };
      return { requiresAck: false, id: data.id, state: data.state };
    },
    diagnostics: () =>
      request<{ runs: AiFactoryDiagRun[] }>("/ai-factory/gpu/diagnostics"),
    diagnostic: (id: string) =>
      request<AiFactoryDiagRunDetail>(`/ai-factory/gpu/diagnostics/${id}`),
    // ── Reliability (NVSentinel + cuda-checkpoint), read-only, owner/provider ──
    reliability: () =>
      request<AiFactoryReliability>("/ai-factory/reliability"),
    // ── AIPerf (load-test / SLO benchmarking) ──
    // Access is gated server-side to a site superuser (free target choice, sees
    // every run) OR a GPU owner/provider (own endpoint only, own runs only).
    // Everyone else gets 403. `scope` echoes which the caller is.
    aiperfRuns: () =>
      request<{
        runs: AiFactoryAiperfRun[];
        exec_enabled: boolean;
        scope: string;
      }>("/ai-factory/aiperf/runs"),
    aiperfRun: (id: string) =>
      request<AiFactoryAiperfRunDetail>(`/ai-factory/aiperf/runs/${id}`),
    runAiperf: (body: AiFactoryAiperfRunRequest) =>
      request<{ id: string; state: "pending" }>("/ai-factory/aiperf/runs", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    aiperfArtifacts: (id: string) =>
      request<{
        run_id: string;
        artifacts: AiFactoryAiperfArtifact[];
        artifacts_error: string | null;
      }>(`/ai-factory/aiperf/runs/${id}/artifacts`),
    // A same-origin URL the browser can download directly (cookie-authed).
    aiperfArtifactUrl: (id: string, path: string) =>
      `${BASE}/ai-factory/aiperf/runs/${id}/artifacts/` +
      path.split("/").map(encodeURIComponent).join("/"),
  },
  network: {
    // Every call here takes an optional NV-CM environment slug. Each management
    // environment bundles its own Nautobot and so has its own fabric — omitting
    // it falls back to the tenant's default (primary) source of truth.
    //
    // The live fabric graph — nodes + links + health. Safe to poll (~10s);
    // the backend caches the underlying LLDP sweep.
    topology: (env?: string) =>
      request<TopologyGraph>(`/topology${envQuery(env)}`),
    // Detail for one node (SoT facts + the neighbours it currently sees).
    node: (id: string, env?: string) =>
      request<TopoNodeDetail>(
        `/topology/nodes/${encodeURIComponent(id)}${envQuery(env)}`
      ),
    // Same-origin WebSocket URL for the in-browser SSH terminal. Proxied to
    // daalu-api via next.config.js (/ws/*) in dev and Traefik in prod.
    terminalWsUrl: (deviceId: string, env?: string) => {
      const proto =
        typeof window !== "undefined" && window.location.protocol === "https:"
          ? "wss:"
          : "ws:";
      const host = typeof window !== "undefined" ? window.location.host : "";
      return (
        `${proto}//${host}/ws/device-terminal/` +
        `${encodeURIComponent(deviceId)}${envQuery(env)}`
      );
    },
  },
  billing: {
    skus: () => request<Sku[]>("/billing/skus"),
    current: () =>
      request<{ sku: Sku | null; totals: PeriodTotals }>("/billing/current"),
    breakdown: () =>
      request<{ by_tier: BreakdownRow[]; by_source: BreakdownRow[] }>(
        "/billing/breakdown"
      ),
    daily: (days = 30) =>
      request<DailyPoint[]>(`/billing/daily?days=${days}`),
    localGpu: () => request<LocalGpuStatus>("/billing/local-gpu"),
    changeSku: (sku_slug: string) =>
      request<{ id: string }>("/billing/sku", {
        method: "PUT",
        body: JSON.stringify({ sku_slug }),
      }),
  },
  // AI-Ops Maturity Model — docs/design/ai-ops-maturity.md §11 / §13.
  aiops: {
    policy: {
      get: (opts: { schema?: boolean } = {}) =>
        request<AutonomyPolicy>(`/aiops/policy${opts.schema ? "?schema=true" : ""}`),
      save: (yaml: string, note = "") =>
        request<AutonomyPolicy>("/aiops/policy", {
          method: "PUT",
          body: JSON.stringify({ yaml, note }),
        }),
      history: () => request<PolicyVersion[]>("/aiops/policy/history"),
      simulate: (body: PolicySimulateIn) =>
        request<PolicyDecision>("/aiops/policy/simulate", {
          method: "POST",
          body: JSON.stringify(body),
        }),
      killSwitch: (enabled: boolean) =>
        request<AutonomyPolicy>("/aiops/policy/kill-switch", {
          method: "POST",
          body: JSON.stringify({ enabled }),
        }),
      resetBreaker: (ruleId = "") =>
        request<AutonomyPolicy>(
          `/aiops/policy/breaker/reset${aiopsQuery({ rule_id: ruleId })}`,
          { method: "POST" }
        ),
    },
    trust: () => request<TrustRow[]>("/aiops/trust"),
    investigations: {
      list: (params: { days?: number; resolution?: string; needs_verdict?: boolean; limit?: number } = {}) =>
        request<Investigation[]>(`/aiops/investigations${aiopsQuery(params)}`),
      get: (id: string) => request<InvestigationDetail>(`/aiops/investigations/${id}`),
      /** 404 (ApiError.status) = no investigation for this alert yet. */
      byAlert: (alertId: string) =>
        request<InvestigationDetail>(`/aiops/investigations/by-alert/${alertId}`),
      verdict: (id: string, verdict: Verdict, note = "") =>
        request<Investigation>(`/aiops/investigations/${id}/verdict`, {
          method: "POST",
          body: JSON.stringify({ verdict, note }),
        }),
    },
    audit: {
      list: (params: { kind?: string; actor?: string; subject?: string; before_seq?: number; limit?: number } = {}) =>
        request<AuditEvent[]>(`/aiops/audit${aiopsQuery(params)}`),
      verify: () => request<AuditVerifyResult>("/aiops/audit/verify"),
      kinds: () => request<string[]>("/aiops/audit/kinds"),
    },
    runbooks: {
      list: (status?: RunbookStatus) =>
        request<Runbook[]>(`/aiops/runbooks${aiopsQuery({ status })}`),
      get: (id: string) => request<Runbook>(`/aiops/runbooks/${id}`),
      create: (body: RunbookIn) =>
        request<Runbook>("/aiops/runbooks", { method: "POST", body: JSON.stringify(body) }),
      update: (id: string, body: RunbookIn) =>
        request<Runbook>(`/aiops/runbooks/${id}`, { method: "PUT", body: JSON.stringify(body) }),
      publish: (id: string) =>
        request<Runbook>(`/aiops/runbooks/${id}/publish`, { method: "POST" }),
      remove: (id: string) =>
        request<void>(`/aiops/runbooks/${id}`, { method: "DELETE" }),
      matchForAlert: (alertId: string) =>
        request<Runbook[]>(`/aiops/runbooks/match${aiopsQuery({ alert_id: alertId })}`),
    },
    metrics: (days = 30) => request<AiopsMetrics>(`/aiops/metrics?days=${days}`),
    maturity: {
      get: (days = 30) => request<Maturity>(`/aiops/maturity?days=${days}`),
      assess: (answers: Record<string, boolean>) =>
        request<MaturityAssessment>("/aiops/maturity/assessment", {
          method: "POST",
          body: JSON.stringify({ answers }),
        }),
    },
    evals: {
      scenarios: () => request<EvalScenario[]>("/aiops/evals/scenarios"),
      runs: (params: { scenario_id?: string; label?: string } = {}) =>
        request<EvalRun[]>(`/aiops/evals/runs${aiopsQuery(params)}`),
      leaderboard: () => request<EvalLeaderboardRow[]>("/aiops/evals/leaderboard"),
    },
  },
};

/** `?a=1&b=2` from the defined, non-empty, non-false params ("" when none). */
function aiopsQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const qs = new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false)
      .map(([k, v]) => [k, String(v)])
  ).toString();
  return qs ? `?${qs}` : "";
}

// ── AI-Ops Maturity Model types ─────────────────────────────────────────

/** Rates are 0..1 floats, or null when the denominator is 0. */
export type Rate = number | null;
export type PolicyMode = "deny" | "approve" | "auto" | "auto_after_trust";
export type Verdict = "correct" | "partial" | "wrong";
export type Resolution = "open" | "auto" | "approved" | "manual" | "unresolved";
export type RunbookStatus = "draft" | "published";

export interface AutonomyPolicy {
  yaml: string;
  /** 0 = the built-in default (never saved). */
  version: number;
  updated_at: string | null;
  note: string;
  parsed: Record<string, unknown>;
  kill_switch: boolean;
  kill_switch_by: string;
  kill_switch_at: string | null;
  /** rule_id ("__defaults__" for the defaults) → tripped-until ISO time. */
  breaker: Record<string, string>;
  json_schema?: Record<string, unknown> | null;
}

export interface PolicyVersion {
  version: number;
  yaml: string;
  note: string;
  updated_by: string;
  updated_at: string;
}

export interface PolicySimulateIn {
  tool: string;
  input?: Record<string, unknown>;
  cluster?: string;
  severity?: string;
  external?: boolean;
  /** Simulate against this draft instead of the saved policy. */
  yaml?: string | null;
}

export interface PolicyDecision {
  mode: "deny" | "approve" | "auto";
  rule_id: string;
  reason: string;
  approvers: number;
  configured_mode: PolicyMode;
  context?: {
    kill_switch: boolean;
    auto_actions_last_hour: number;
    trust: Record<string, { successes: number; failures: number; failure_rate: number }>;
  };
}

export interface TrustRow {
  rule_id: string;
  mode: PolicyMode;
  tools: string[];
  successes: number;
  failures: number;
  failure_rate: number;
  min_successes: number | null;
  max_failure_rate: number | null;
  earned: boolean;
  breaker_until: string | null;
}

export interface Investigation {
  id: string;
  alert_id: string;
  alert_title: string;
  alert_severity: string;
  alert_status: string;
  alert_created_at: string;
  started_at: string;
  triaged_at: string | null;
  time_to_triage_s: number | null;
  root_cause: string;
  actions_proposed: number;
  actions_approved: number;
  actions_rejected: number;
  actions_auto: number;
  resolution: Resolution | string;
  resolved_at: string | null;
  time_to_resolve_s: number | null;
  verified: boolean | null;
  runbook_ids: string[];
  verdict: Verdict | null;
  verdict_by: string;
  verdict_at: string | null;
  verdict_note: string;
  model: string;
}

export interface InvestigationAction {
  id: string;
  tool: string;
  input: Record<string, unknown>;
  status: string;
  policy_mode: string;
  policy_rule: string;
  auto_executed: boolean;
  verified_resolved: boolean | null;
  approvals: ActionApproval[];
  actor: string;
}

export interface InvestigationDetail extends Investigation {
  actions: InvestigationAction[];
}

export interface AuditEvent {
  id: string;
  seq: number;
  kind: string;
  actor: string;
  subject: string;
  data: Record<string, unknown>;
  prev_hash: string;
  hash: string;
  created_at: string;
}

export interface AuditVerifyResult {
  ok: boolean;
  checked: number;
  broken_at: number | null;
  reason: string;
}

export interface RunbookMatch {
  alertname?: string[];
  labels?: Record<string, string>;
}

export interface Runbook {
  id: string;
  slug: string;
  title: string;
  body: string;
  match: RunbookMatch;
  allowed_actions: string[];
  status: RunbookStatus | string;
  source: "manual" | "generated" | "imported" | string;
  version: number;
  from_investigation_id: string | null;
  published_by: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
}

/** Either the structured fields or a whole runbook file (`markdown`). */
export type RunbookIn =
  | {
      title: string;
      body: string;
      match: RunbookMatch;
      allowed_actions: string[];
      slug?: string;
      status?: RunbookStatus;
    }
  | { markdown: string };

export interface AiopsMetrics {
  days: number;
  alerts_total: number;
  investigations_total: number;
  ai_triage_rate: Rate;
  median_time_to_triage_s: number | null;
  agreement_rate: Rate;
  verdict_coverage: Rate;
  verdicts: { correct: number; partial: number; wrong: number };
  proposal_approval_rate: Rate;
  actions: {
    proposed: number;
    approved: number;
    rejected: number;
    auto: number;
    executed: number;
    failed_verification: number;
  };
  change_failure_rate: Rate;
  escalation_rate: Rate;
  resolutions: { auto: number; approved: number; manual: number; unresolved: number; open: number };
  median_time_to_resolve_s: number | null;
  runbooks_published: number;
  runbooks_generated: number;
  eval_runs: number;
  eval_mean_score: number | null;
  audit_chain_ok: boolean;
  daily: { date: string; alerts: number; investigations: number; auto_resolved: number; approved_resolved: number }[];
}

export interface MaturityAssessment {
  level: number;
  answers: Record<string, boolean>;
  created_at: string;
  created_by: string;
}

export interface Maturity {
  level: number;
  name: string;
  levels: { level: number; name: string; description: string; met: boolean }[];
  criteria: {
    level: number;
    id: string;
    label: string;
    met: boolean;
    value: unknown;
    target: unknown;
  }[];
  next_steps: string[];
  metrics: AiopsMetrics;
  assessment: MaturityAssessment | null;
  questions: { id: string; level: number; text: string }[];
}

export interface EvalScenario {
  id: string;
  title: string;
  description: string;
  namespace: string;
  alert: string;
  expect: Record<string, unknown>;
}

export interface EvalRun {
  id: string;
  scenario_id: string;
  label: string;
  alert_id: string | null;
  started_at: string;
  finished_at: string | null;
  score: number;
  passed: boolean;
  details: Record<string, unknown>;
}

export interface EvalLeaderboardRow {
  label: string;
  runs: number;
  mean_score: number;
  pass_rate: number;
  scenarios: number;
}

// ── AI Factory (GPU observability + diagnostics) ─────────────────────
//
// Mirrors the /ai-factory/* router. Tenant-scoped server-side; the role
// the backend computes (owner / provider / consumer / none) decides which
// panels the page shows and which summary shape /gpu/summary returns.

export type AiFactoryRole = "owner" | "provider" | "consumer" | "none";
export type AiFactoryMetric = "util" | "temp" | "mem" | "power";
export type AiFactoryRange = "1h" | "6h" | "24h" | "7d";
export type AiFactoryHealth = "ok" | "warn" | "crit";
export type AiFactoryDiagKind = "dcgmi_diag" | "nccl_test";

export interface AiFactoryOverview {
  role: AiFactoryRole;
  has_gpu: boolean;
  gpu_class: string | null;
  metrics_available: boolean;
  // Which UI sections apply for this tenant — the page treats this as
  // advisory and still gates by role, but it lets the backend hide a
  // panel (e.g. diagnostics) without a frontend change.
  panels: string[];
}

export interface AiFactoryGpu {
  // Stable, globally-unique selection key (the card's DCGM UUID, or
  // host:index fallback). Use this — not `gpu` — to deep-link a card.
  id: string;
  gpu: string;
  uuid: string;
  model: string;
  hostname: string;
  gpu_class: string;
  temp_c: number;
  util_pct: number;
  mem_used_gb: number;
  mem_total_gb: number;
  mem_pct: number;
  power_w: number;
  sm_active_pct: number;
  health: AiFactoryHealth;
  xid_errors: number;
  ecc_dbe: number;
}

export interface AiFactoryConsumerSummary {
  tokens_prompt: number;
  tokens_completion: number;
  requests: number;
  quota_used: number;
  quota_limit: number;
  avg_latency_ms: number;
  pool_util_pct: number | null;
}

// owner/provider → { gpus, updated_at }; consumer → { consumer }.
// Both keys are optional so a single typed response covers every role.
export interface AiFactoryGpuSummary {
  gpus?: AiFactoryGpu[];
  updated_at?: string;
  consumer?: AiFactoryConsumerSummary;
}

export interface AiFactoryTimeseries {
  metric: string;
  series: { ts: number; value: number }[];
}

export interface AiFactoryGpuEvent {
  ts: string;
  gpu: string;
  kind: "xid" | "ecc_dbe" | "ecc_sbe";
  detail: string;
}

export interface AiFactoryAlert {
  name: string;
  gpu?: string | null;
  severity: "info" | "warning" | "critical";
  state: "firing" | "pending";
  summary: string;
  since: string;
}

export interface AiFactoryValidateCheck {
  name: string;
  status: "pass" | "fail" | "skip";
  detail: string;
}

export interface AiFactoryValidateResult {
  run_id: string;
  checks: AiFactoryValidateCheck[];
  passed: boolean;
}

export type AiFactoryDiagStart =
  | { requiresAck: true; warning: string }
  | { requiresAck: false; id: string; state: "pending" };

export type AiFactoryDiagState =
  | "pending"
  | "running"
  | "passed"
  | "failed"
  | "error";

export interface AiFactoryDiagRun {
  id: string;
  kind: AiFactoryDiagKind;
  level: number | null;
  state: AiFactoryDiagState;
  summary: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface AiFactoryDiagRunDetail extends AiFactoryDiagRun {
  output: string;
}

// ── Reliability (NVSentinel auto-remediation + cuda-checkpoint) ──────
export type AiFactorySignalLevel = "ok" | "warn" | "crit";

export interface AiFactoryReliabilitySignal {
  name: string;
  value: number;
  unit: string;
  level: AiFactorySignalLevel;
}

export interface AiFactoryReliability {
  // "ok" | "warn" | "crit" for an owned card; "n/a" (no card) | "unknown"
  // (no metrics source) otherwise.
  status: "ok" | "warn" | "crit" | "n/a" | "unknown";
  has_gpu: boolean;
  metrics_available?: boolean;
  signals: AiFactoryReliabilitySignal[];
  nvsentinel: { active: boolean; remediations?: number; mode?: string };
  cuda_checkpoint: { enabled: boolean; status: "gated" | "enabled"; note: string };
  updated_at?: string;
}

// ── AIPerf (load-test / SLO benchmarking) — superuser only ──────────
export type AiFactoryAiperfState =
  | "pending"
  | "running"
  | "passed"
  | "failed"
  | "error";

export interface AiFactoryAiperfRunRequest {
  model?: string;
  concurrency?: string;
  request_count?: number;
  input_tokens?: number;
  output_tokens?: number;
  endpoint_type?: string;
  streaming?: boolean;
  via_gateway?: boolean;
  target_url?: string;
}

export interface AiFactoryAiperfRun {
  id: string;
  state: AiFactoryAiperfState;
  model: string;
  target_url: string;
  via_gateway: boolean;
  endpoint_type: string;
  concurrency: string;
  request_count: number;
  input_tokens: number;
  output_tokens: number;
  summary: Record<string, unknown> | null;
  requested_by: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface AiFactoryAiperfRunDetail extends AiFactoryAiperfRun {
  output: string | null;
}

// One downloadable file the uploader pushed to object storage, e.g.
// "ISL200_OSL200/CON10/profile_export_aiperf.json".
export interface AiFactoryAiperfArtifact {
  path: string;
  size: number;
}

// One point on the SLO curve, parsed from a per-concurrency
// profile_export_aiperf.json. Lives inside run.summary.concurrency_levels.
export interface AiFactoryAiperfLevel {
  concurrency: number | null;
  isl: number | null;
  osl: number | null;
  metrics: Record<string, number>;
  path: string;
}

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string | null;
  is_admin: boolean;
  // Platform operator — gates site-wide tools (AI-factory AIPerf benchmarking).
  // Distinct from is_admin (tenant admin).
  is_superuser: boolean;
  tenant_id: string;
  // Free-form JSON blob set by /settings — theme, density, accent,
  // per-channel notification toggles. Server merges PATCH payloads so
  // a partial update from one tab doesn't blow away others.
  preferences: Record<string, unknown>;
}

export interface LoginResult {
  token: string;
  expires_at: string;
  user: CurrentUser;
}

export interface SSOProvider {
  // Keycloak IdP alias, sent as `idp` to the SSO start path. e.g. "google".
  id: string;
  label: string;
}

export interface AuthConfig {
  // "password" → local email+password form; "oidc" → Daalu-branded SSO buttons.
  mode: "password" | "oidc";
  sso_login_path: string;
  // In "oidc" mode, the providers Daalu brokers on the user's behalf. The
  // login page renders a button per provider that deep-links to
  // `${sso_login_path}?idp=${id}` so the user goes straight to Google.
  sso_providers?: SSOProvider[];
  // Visitors may create a local account → show the "Create an account" link.
  self_signup_enabled?: boolean;
  // Show the local email+password form (always in password mode; also in oidc
  // mode once self-signup is on, so local accounts log in beside the SSO buttons).
  password_login_enabled?: boolean;
}

export interface PersonalAccessToken {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface PersonalAccessTokenCreated extends PersonalAccessToken {
  // Cleartext token, returned exactly once by POST /auth/tokens. Client
  // must show it and discard — there is no way to recover the value
  // server-side.
  token: string;
}

export interface BuildInfo {
  version: string;
  commit_sha: string;
  built_at: string;
}

// ── Invites ─────────────────────────────────────────────────────────
//
// `token` + `invite_url` only appear on create/resend responses,
// where the cleartext is returned exactly once.

export type InviteStatus = "pending" | "expired" | "revoked" | "accepted";

export interface Invite {
  id: string;
  email: string;
  role: "admin" | "user";
  status: InviteStatus;
  invited_by_user_id: string | null;
  inviter_name: string | null;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  created_at: string;
  // Cleartext, present only on create/resend responses.
  token: string | null;
  invite_url: string | null;
  // True iff the backend actually delivered the invite via SMTP. False
  // means SMTP isn't configured for the tenant — the UI then shows the
  // "copy the link manually" fallback. Present only on create/resend.
  delivered?: boolean | null;
}

export interface InvitePreview {
  email: string;
  role: "admin" | "user";
  tenant_name: string;
  inviter_name: string | null;
  message: string | null;
  expires_at: string;
}

// ── Types — kept aligned with `src/daalu_automation/api/schemas` ─────────
export interface Event {
  id: string;
  type: string;
  module: string;
  source: string;
  severity: "info" | "warning" | "critical";
  summary: string;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface Briefing {
  id: string;
  channel: string;
  status: string;
  coverage_date: string;
  title: string;
  summary: string;
  body_markdown: string;
  metrics: Record<string, number | string>;
  source_event_ids: string[];
  created_at: string;
}

export interface Alert {
  id: string;
  module: string;
  severity: "info" | "warning" | "critical";
  status: "open" | "acknowledged" | "resolved" | "suppressed";
  title: string;
  body: string;
  ai_confidence: number;
  metadata_json: Record<string, unknown>;
  fingerprint: string | null;
  occurrence_count: number;
  last_seen_at: string | null;
  created_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
}

export interface AlertOccurrence {
  id: string;
  alert_id: string;
  occurred_at: string;
  source_event_id: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
}

export interface AlertAction {
  id: string;
  message_id: string;
  tool_call_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  requires_approval: boolean;
  status: "pending" | "approved" | "rejected" | "executed" | "failed";
  result_output: string;
  result_error: string;
  approved_at: string | null;
  executed_at: string | null;
  created_at: string;
  // AI-Ops maturity (docs/design/ai-ops-maturity.md §2/§3). Optional so an
  // older backend without these fields still type-checks at runtime.
  policy_mode?: string; // "approve" | "auto" | "deny" | ""
  policy_rule?: string;
  policy_reason?: string;
  approvals_required?: number;
  approvals?: ActionApproval[];
  preview?: ActionPreview;
  auto_executed?: boolean;
  verified_resolved?: boolean | null;
  actor?: string; // "agent" | "mcp:<email>"
}

export interface ActionApproval {
  user_id: string;
  email: string;
  at: string;
}

export interface ActionPreview {
  summary?: string;
  blast_radius?: { pods?: number; deployments?: number };
  details?: unknown;
}

export interface AlertChatMessage {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_calls_json: Array<{ id: string; name: string; input: Record<string, unknown> }>;
  tool_call_id: string | null;
  created_at: string;
  actions: AlertAction[];
}

export interface Recommendation {
  id: string;
  module: string;
  status: "pending" | "approved" | "dismissed" | "executed";
  title: string;
  rationale: string;
  suggested_action: string;
  confidence: number;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface AgentDescriptor {
  name: string;
  module: string;
  description: string;
  subscribed_event_types: string[];
}

export interface AgentRun {
  id: string;
  agent_name: string;
  module: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  activity: string;
  metrics: Record<string, unknown>;
  error_message: string | null;
}

export interface WorkflowDescriptor {
  name: string;
  module: string;
}

export interface WorkflowStep {
  order: number;
  kind: string; // "action" | "verify"
  title: string;
  tool: string;
  input: Record<string, unknown>;
  output: string;
  status: string; // "ok" | "error"
}

export interface WorkflowRun {
  id: string;
  workflow_name: string;
  module: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  input_payload: Record<string, unknown>;
  output_payload: Record<string, unknown>;
  error_message: string | null;
  alert_id: string | null;
  alert_title: string | null;
  steps: WorkflowStep[];
}

export interface ApproveActionResult {
  messages: AlertChatMessage[];
  /** "" when the policy needs more approvers and this one only recorded a vote. */
  workflow_run_id: string;
  approvals_remaining?: number;
}

export type ClusterStatus =
  | "pending"
  | "awaiting_handshake"
  | "connected"
  | "degraded"
  | "error";

export interface Cluster {
  id: string;
  slug: string;
  name: string;
  status: ClusterStatus;
  tunnel_ip: string;
  operator_pubkey: string;
  customer_pubkey: string | null;
  customer_endpoint: string | null;
  last_handshake_at: string | null;
  last_error: string | null;
}

export interface ClusterNode {
  name: string;
  status: string;
  roles: string[];
  version: string;
  internal_ip: string | null;
  os_image: string | null;
  cpu: string | null;
  memory: string | null;
  created_at: string | null;
}

export interface SymptomaticPod {
  namespace: string;
  name: string;
  phase: string;
  reason: string;
  ready: string;
  restarts: number;
  node: string | null;
}

export interface ClusterHealth {
  ready_nodes: number;
  not_ready_nodes: number;
  pods_total: number;
  pods_running: number;
  pods_pending: number;
  pods_failed: number;
  pods_succeeded: number;
  pods_symptomatic: number;
  pod_restarts_total: number;
  containers_total: number;
  deployments_total: number;
  deployments_unavailable: number;
  statefulsets_total: number;
  daemonsets_total: number;
  daemonsets_unavailable: number;
  replicasets_total: number;
  services_total: number;
  pvc_total: number;
  pvc_unbound: number;
  symptomatic_pods: SymptomaticPod[];
  partial: boolean;
}

export interface ClusterOverview {
  reachable: boolean;
  server_version: string | null;
  node_count: number;
  namespace_count: number;
  nodes: ClusterNode[];
  health: ClusterHealth | null;
  error: string | null;
}

export interface KubectlCommandSpec {
  id: string;
  label: string;
  kubectl: string;
  group: string;
  namespaced: boolean;
  supports_selector: boolean;
}

export type KubectlOutput = "json" | "yaml" | "cli";

export interface KubectlRunRequest {
  command_ids: string[];
  namespace?: string | null;
  label_selector?: string | null;
  output: KubectlOutput;
}

export interface KubectlResult {
  id: string;
  command: string;
  ok: boolean;
  output: string;
  error: string | null;
}

export interface ClusterCreate extends Cluster {
  // Shown exactly once on POST /clusters; subsequent GETs do not echo
  // these fields. The UI must present a "copy and store somewhere safe"
  // affordance — if lost, the operator has to re-onboard the cluster.
  invite_token: string;
  install_snippet: string;
}

// ── Dashboards (Grafana-backed, rendered natively) ────────────────────────

export interface DashboardSummary {
  uid: string;
  title: string;
  folder: string | null;
  tags: string[];
  source: string;
}

export interface DashboardList {
  configured: boolean;
  dashboards: DashboardSummary[];
  error: string | null;
}

export type DashboardPanelType = "timeseries" | "stat" | "table" | "logs";

export interface DashboardPanel {
  id: string;
  title: string;
  type: DashboardPanelType;
  datasource: string; // "prometheus" | "loki"
  expr: string;
  unit: string | null;
  w: number;
  h: number;
  x: number;
  y: number;
}

export interface Dashboard {
  uid: string;
  title: string;
  description: string;
  tags: string[];
  source: string;
  panels: DashboardPanel[];
}

export type DashboardRange = "15m" | "1h" | "6h" | "24h" | "7d";

export interface DashboardQueryRequest {
  datasource: "prometheus" | "loki";
  expr: string;
  kind: DashboardPanelType;
  range: DashboardRange;
}

export interface DashboardSeriesPoint {
  ts: number;
  value: number;
}

export interface DashboardSeries {
  name: string;
  points: DashboardSeriesPoint[];
}

export interface DashboardLogLine {
  ts: number;
  line: string;
  labels: Record<string, string>;
}

export interface DashboardQueryResult {
  kind: string;
  series: DashboardSeries[];
  instant: { name: string; value: number; labels: Record<string, string> }[];
  logs: DashboardLogLine[];
  unit: string | null;
  error: string | null;
}

export interface DashboardAiCreateRequest {
  prompt: string;
  datasource: "prometheus" | "loki" | "mixed";
  title?: string | null;
}

export interface DashboardAiCreateResult {
  uid: string;
  title: string;
  url: string | null;
  panel_count: number;
}

// ── Observability console (Prometheus/Thanos metrics, Loki logs) ──────────

export interface ObsQuerySpec {
  id: string;
  label: string;
  group: string;
  query: string;
  description: string;
  unit: string;
}

export interface JobHealth {
  job: string;
  up: number;
  total: number;
}

export interface MetricsOverview {
  base_url: string;
  version: string | null;
  targets_total: number | null;
  targets_up: number | null;
  targets_down: number | null;
  firing_alerts: number | null;
  jobs: JobHealth[];
}

export interface LogsOverview {
  base_url: string;
  label_count: number | null;
  labels: string[];
  namespaces: string[];
}

/** A metrics/logs/dashboard store that belongs to one cluster. */
export interface ClusterStore {
  provider: string;
  name: string;
  status: string;
}

/** The cluster the user is looking at: `/clusters/<slug>` or a console's
 *  `?cluster=<slug>`. `null` elsewhere (and during SSR). */
function currentClusterSlug(): string | null {
  if (typeof window === "undefined") return null;
  const m = window.location.pathname.match(/^\/clusters\/([a-z0-9-]+)\/?$/);
  if (m) return m[1];
  return new URLSearchParams(window.location.search).get("cluster");
}

/** `?cluster=<slug>` scopes an observability console to one cluster's store. */
function clusterQs(cluster?: string): string {
  return cluster ? `?cluster=${encodeURIComponent(cluster)}` : "";
}

export interface ObservabilityOverview {
  provider: string;
  family: "metrics" | "logs";
  reachable: boolean;
  error: string | null;
  metrics: MetricsOverview | null;
  logs: LogsOverview | null;
}

export interface ObsQueryRequest {
  query_ids: string[];
  // metrics
  custom_query?: string | null;
  time_range?: string;
  // logs
  namespace?: string | null;
  search?: string | null;
  since?: string;
  limit?: number;
}

export interface ObsQueryResult {
  id: string;
  query: string;
  ok: boolean;
  output: string;
  error: string | null;
}

export interface OnboardingStepStatus {
  id: string;
  provider: string | null;
  configured: boolean;
  status: string;
  count: number;
  missing: string[];
}

export interface OnboardingStatus {
  steps: OnboardingStepStatus[];
  completed: number;
  total: number;
}

export interface OnboardingTestResult {
  ok: boolean;
  message: string;
  latency_ms: number;
}

// 202 = the install was *scheduled*, not finished — there are no service URLs
// yet, because no pods exist. Poll configManagerProgress() from here.
export interface ProvisionConfigManagerResult {
  ok: boolean;
  message: string;
  state: string;
  namespace: string;
  base_hostname: string;
}

// `reason` is why the pod isn't up yet — `Init:PodInitializing` /
// `ContainerCreating` are ordinary progress; `ImagePullBackOff` and friends
// are not. ready/total counts containers within the pod.
export interface ConfigManagerProgressPod {
  name: string;
  phase: string;
  ready: number;
  total: number;
  reason: string | null;
}

// Only *notable* events are returned (FailedScheduling, ErrImagePull,
// BackOff…), so anything in this list is worth surfacing as-is.
export interface ConfigManagerProgressEvent {
  reason: string;
  message: string;
  object: string;
}

// Live install progress. `requested` is false when no stack has been asked
// for at all — that's the only case where the provision form should show.
// `message` is a status note (e.g. "namespace not readable yet"), NOT a
// failure; real failures land in `last_error` with state === "error".
export interface ConfigManagerProgress {
  requested: boolean;
  state: string;
  namespace: string;
  chart_version: string;
  last_error: string | null;
  message: string | null;
  pods: ConfigManagerProgressPod[];
  events: ConfigManagerProgressEvent[];
  summary: {
    total?: number;
    running?: number;
    pending?: number;
    succeeded?: number;
    failed?: number;
    ready?: number;
  };
  urls: Record<string, string>;
}

// Chart-version posture for the per-tenant NV-CM stack. `state` mirrors the
// controller's lifecycle field (pending|provisioning|active|error|deleting|
// destroyed) — during an upgrade it goes pending → provisioning → active.
// `latest_chart_version` is null when the controller image ships no vendored
// charts at all; available_chart_versions is that same on-disk set.
export interface ConfigManagerVersion {
  provisioned: boolean;
  state: string;
  chart_version: string;
  latest_chart_version: string | null;
  available_chart_versions: string[];
  upgrade_available: boolean;
  last_error: string | null;
}

// 202 = the upgrade was *scheduled*, not finished. Poll version() until
// `state` leaves pending/provisioning.
export interface UpgradeConfigManagerResult {
  ok: boolean;
  message: string;
  chart_version: string;
  state: string;
}

// One named management environment. `slug` is derived from `name` by the
// controller and is what the `namespace` and the public hostnames are built
// from — which is why none of the three can be changed after creation.
// Exactly one environment per tenant has `is_primary`, and it is the one the
// singular /config-manager/* routes address.
export interface ConfigManagerEnvironment {
  id: string;
  name: string;
  slug: string;
  is_primary: boolean;
  // Controller lifecycle: pending|provisioning|active|error|deleting|
  // destroyed, or "unknown" before the controller has looked.
  state: string;
  namespace: string;
  base_hostname: string;
  chart_version: string;
  upgrade_available: boolean;
  last_error: string | null;
  // Per-service URLs, same shape as ConfigManagerProgress["urls"] — empty
  // until the stack is up.
  urls: Record<string, unknown>;
  // Break-glass Nautobot login, present ONLY on the create response. The
  // password is never re-served, so a caller that doesn't show it here has
  // lost its chance — an operator has to recover it from the row.
  admin_username?: string | null;
  admin_password?: string | null;
}

// 202 = the install was *scheduled*. `environment` is the freshly created row
// (it already has its id, slug and namespace), so a caller can select it and
// start polling progress without waiting for the list to catch up.
export interface CreateConfigManagerEnvironmentResult {
  ok: boolean;
  message: string;
  environment: ConfigManagerEnvironment;
}

export interface IntegrationConfig {
  id: string;
  provider: string;
  module: string;
  name: string;
  status: string;
  // Why the last health probe failed, and when it ran. Set by the periodic
  // integrations.health_check sweep; `last_error` is null while connected.
  last_error?: string | null;
  last_probed_at?: string | null;
  // Sensitive fields (webhook_url, password, api_token, kubeconfig)
  // are echoed back as "***" — see _REDACT_FIELDS in
  // src/daalu_automation/api/routers/integrations.py. Treat any
  // string that equals "***" as "set on the server, unknown to me".
  config: Record<string, unknown>;
  // When set, the backend routes this integration's HTTP calls through
  // the named cluster's daalu-edge proxy — required for in-cluster URLs
  // like `http://prometheus.monitoring.svc.cluster.local:9090`. Null
  // means: dial the URL directly from the hub (legacy / public URL).
  cluster_tunnel_id: string | null;
}

export interface IntegrationDescriptor {
  provider: string;
  module: string;
  display_name: string;
  description: string;
  required_settings: string[];
  configured: boolean;
}

export interface Incident {
  id: string;
  title: string;
  summary: string;
  severity: string;
  status: string;
  started_at: string;
  resolved_at: string | null;
  ai_root_cause: string;
  ai_remediation: string;
  evidence: Record<string, unknown>[];
}

export type ChangeProposalStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "executed"
  | "failed"
  | "stale";

export type ChangeProposalKind = "drift" | "manual" | "intended_change";

export interface ChangeProposal {
  id: string;
  device_id: string;
  // NV-CM environment the device lives in; null for the tenant's default
  // source of truth. Device ids repeat across environments, so this is what
  // says which fabric a proposal is about.
  environment_slug?: string | null;
  kind: ChangeProposalKind;
  status: ChangeProposalStatus;
  intended_config: string;
  observed_config: string;
  diff: string;
  renderer_version: string;
  // Evidence is open-ended; the engine writes
  // { triggered_by, llm_reasoning, llm_model, evidence_events, evidence_alerts, evidence_metrics, confidence }
  // and the reconciler writes { triggered_by: "reconciler", facts_changed, confidence }.
  // The detail page renders well-known keys with labels and dumps the rest as JSON.
  evidence: Record<string, unknown>;
  created_by: string | null;
  approved_by: string | null;
  approved_at: string | null;
  executed_at: string | null;
  executor_result: Record<string, unknown>;
  created_at: string;
}

// ── SoT device-management ─────────────────────────────────────────────

export type SotTransport =
  | "linux_ssh"
  | "redfish"
  | "junos"
  | "iosxr"
  | "eos"
  | "unknown";

export interface SotDevice {
  id: string;
  name: string;
  primary_ip: string | null;
  platform: string;
  transport: SotTransport;
  tags: string[];
  // Nautobot custom_field_data — operator-defined fields the editor
  // surfaces verbatim (daalu_transport, ssh_user, *_credentials_ref, …)
  extra: Record<string, unknown>;
}

export interface SotIntent {
  device_id: string;
  revision: string;
  transport: SotTransport;
  // Shape depends on transport — LinuxFacts / RedfishFacts / NetworkFacts.
  // The detail page picks the right form based on transport.
  facts: Record<string, unknown>;
  fetched_at: string;
}

export interface SotCatalogItem {
  id: string;
  name: string;
  slug: string | null;
}

export interface SotCatalog {
  sites: SotCatalogItem[];
  device_types: SotCatalogItem[];
  device_roles: SotCatalogItem[];
  platforms: SotCatalogItem[];
}

export interface SotDeviceCreate {
  name: string;
  primary_ip: string;
  site_id: string;
  device_type_id: string;
  device_role_id: string;
  platform_id?: string | null;
  transport: SotTransport;
}

export interface SotMonitoringStatus {
  devices_total: number;
  metrics_devices: string[];
  logs_devices: string[];
  prometheus_ok: boolean;
  loki_ok: boolean;
  detail: string;
}

export interface SotMonitoringDeviceResult {
  device: string;
  ok: boolean;
  detail: string;
}

export interface SotMonitoringConnectOut {
  results: SotMonitoringDeviceResult[];
}

export type SotFabricArtifact = "cabling" | "addressing" | "intent";

/** `exists` is the idempotency signal: the object is already correct, so the
 *  apply will skip it. Re-uploading a plan is expected, not exceptional. */
export type SotFabricRowStatus = "valid" | "exists" | "created" | "error";

/** One object in a fabric plan. Unlike a device row the columns can't be
 *  fixed, because a plan holds six kinds of object — so `op` says what it is
 *  and `target`/`detail` are its two identifying halves (for a cable, the two
 *  ends; for an address, the interface and the address). */
export interface SotFabricRow {
  row: number;
  op: "interface" | "cable" | "prefix" | "address" | "role" | "config_context";
  target: string;
  detail: string;
  status: SotFabricRowStatus;
  error: string | null;
  object_id: string | null;
  warnings: string[];
}

export interface SotFabricImportResult {
  dry_run: boolean;
  artifact: SotFabricArtifact;
  summary: {
    total: number;
    valid: number;
    created: number;
    skipped: number;
    errors: number;
  };
  // Per-object-type counts — what formed. This is the headline the upload is
  // for: "71 interfaces, 32 cables".
  counts: Partial<Record<SotFabricRow["op"], number>>;
  rows: SotFabricRow[];
  errors?: string[];
}

export type SotBulkRowStatus = "valid" | "error" | "created";

export interface SotBulkRow {
  row: number;
  name: string;
  primary_ip: string;
  transport: string;
  site: string;
  device_type: string;
  role: string;
  platform: string | null;
  status: SotBulkRowStatus;
  error: string | null;
  device_id: string | null;
  // Created, but something after the device itself didn't land — almost
  // always the primary IP. A success worth qualifying, not hiding.
  warnings?: string[];
}

export interface SotBulkImportResult {
  dry_run: boolean;
  summary: {
    total: number;
    valid: number;
    errors: number;
    created: number;
    warnings?: number;
  };
  rows: SotBulkRow[];
  // Run-level problems: an unreadable catalog, a custom field that couldn't
  // be created. Without these an unreachable Nautobot looks exactly like a
  // file full of bad names.
  errors?: string[];
  // Catalog objects the file names that Nautobot doesn't have yet. On preview
  // this is what would be created; null when create-missing was off.
  planned_catalog?: SotCatalogPlan | null;
  // What actually landed on apply.
  created_catalog?: string[];
}

export interface SotCatalogPlan {
  sites: string[];
  device_types: string[];
  roles: string[];
  platforms: string[];
  // Parent prefixes for the rows' IPs — Nautobot refuses an IP with no
  // covering prefix, which is what silently costs a device its primary IP.
  prefixes: string[];
}

export type SotReconcileStatus = "in_sync" | "drift" | "skipped" | "error";

export interface SotReconcileResult {
  device_id: string;
  status: SotReconcileStatus;
  detail: string | null;
  proposal_id: string | null;
}

// ── Live network topology ─────────────────────────────────────────────
//
// Mirrors the /topology router. Tenant-scoped server-side; gated behind the
// tenant's network_topology_enabled flag. Links are coloured from observed
// LLDP adjacency reconciled with the Nautobot cabling plan + device telemetry.

export type TopoTier = "spine" | "leaf" | "server" | "gateway" | "unknown";
export type TopoLinkStatus = "up" | "down" | "mismatch" | "mgmt" | "unknown";

export interface TopoNode {
  id: string;
  name: string;
  tier: TopoTier;
  platform: string;
  transport: string;
  primary_ip: string | null;
  tags: string[];
  // Live signals — null = not probed (draw neutral, never as a failure).
  reachable: boolean | null;
  lldp_ok: boolean | null;
  can_terminal: boolean;
  note: string;
}

export interface TopoLink {
  id: string;
  source: string;
  source_if: string | null;
  target: string;
  target_if: string | null;
  status: TopoLinkStatus;
  source_observed: boolean;
  target_observed: boolean;
  intended: boolean;
  detail: string;
}

export interface TopologyGraph {
  nodes: TopoNode[];
  links: TopoLink[];
  generated_at: string;
  // True when a live source (LLDP/telemetry) contributed; else `degraded` says why.
  observed: boolean;
  degraded: string | null;
  stats: Record<string, number>;
}

export interface TopoNeighbor {
  local_if: string;
  remote_name: string;
  remote_if: string;
}

export interface TopoNodeDetail {
  id: string;
  name: string;
  platform: string;
  transport: string;
  primary_ip: string | null;
  tags: string[];
  reachable: boolean | null;
  probe_error: string | null;
  neighbors: TopoNeighbor[];
  can_terminal: boolean;
}

// ── Reports query API ────────────────────────────────────────────────

export interface ReportsQueryColumn {
  key: string;
  label: string;
}

export interface ReportsEntityDescriptor {
  name: string;
  columns: ReportsQueryColumn[];
  filter_fields: string[];
  time_field: string | null;
}

export interface ReportsSchema {
  entities: ReportsEntityDescriptor[];
}

export interface ReportsQueryRequest {
  entity: string;
  filters?: Record<string, string | number | null>;
  since_hours?: number | null;
  limit?: number;
  display?: "table" | "count";
}

export interface ReportsQueryResponse {
  entity: string;
  display: "table" | "count";
  columns: ReportsQueryColumn[];
  rows: Array<Record<string, unknown> & { id: string }>;
  total: number;
}

export interface ReportsTranslateResponse {
  query: ReportsQueryRequest & {
    entity: string;
    filters: Record<string, string>;
    since_hours: number | null;
    limit: number;
    display: "table" | "count";
  };
  rationale: string;
}

export interface SavedReport {
  id: string;
  name: string;
  definition: ReportsQueryRequest;
  owner_user_id: string | null;
  pinned: boolean;
  created_at: string;
  updated_at: string;
}

export interface DashboardTile {
  saved_report_id: string;
  render: "table" | "number" | "line" | "bar" | "pie";
  title?: string | null;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
}

export interface Dashboard {
  id: string;
  name: string;
  tiles: DashboardTile[];
  owner_user_id: string | null;
  home_pinned: boolean;
  created_at: string;
  updated_at: string;
}

export interface ReportSchedule {
  id: string;
  name: string;
  saved_report_id: string;
  cron: string;
  destination: "slack" | "email";
  recipient: string;
  format: "markdown" | "csv";
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceView {
  id: string;
  state: "provisioning" | "active" | "paused" | "destroyed" | "error";
  profile: "small" | "medium" | "large";
  model: string;
  git_repo_url: string | null;
  git_branch: string;
  // True when a PAT is stored for private-repo auth. The token itself is
  // never returned by the API.
  git_authenticated: boolean;
  ide_url: string;
}

export interface CodingModelView {
  id: string;
  label: string;
  description: string;
  gpu_class: string;
  vram_gb: number;
  available: boolean;
  default: boolean;
}
