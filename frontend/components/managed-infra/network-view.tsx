"use client";

/**
 * Network & Server Management — the tenant-facing surface for the management
 * environments the operator provisions.
 *
 * A customer runs N *named* environments (prod, lab, staging…). The list comes
 * from /onboarding/config-manager/environments; each row already carries its
 * state, namespace, chart version and upgrade posture, so the tiles need no
 * per-tile request. Pod-level detail (which is what makes an in-flight install
 * legible) comes from the per-environment progress route.
 *
 * Create returns 202 ("install scheduled") — the environment row exists
 * immediately but nothing is running yet — so the views here are driven by
 * *state*, not by the presence of an integration row:
 *   no environments            → create form (the empty state)
 *   one or more                → a tile per environment; "Add environment"
 *   a tile opened              → live install progress, or the completion state
 *
 * The name is the whole point of the plural surface: the controller derives the
 * slug from it, and the namespace and the public hostnames from that. All three
 * are baked into live URLs, so the name is immutable after creation and the
 * form says so before you commit. Client-side we only check what is objectively
 * true of any DNS label (non-empty, ≤24 chars, the label charset). The reserved
 * *prefix* rule is the controller's — it owns the tool-console namespace and we
 * would only drift out of agreement with it — so a rejected name comes back as
 * a 400 whose `detail` is written for the person who typed it and is shown
 * verbatim. Same for the 409 on a duplicate.
 *
 * Selection is local state, not a route: this page is mounted inside the
 * Managed-infra tab shell and a router push would break out of the tabs.
 *
 * Copy rule: user-visible strings are vendor-neutral (capability names only).
 */

import { useEffect, useMemo, useState } from "react";
import type { ComponentType } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import {
  Network,
  ExternalLink,
  CheckCircle2,
  Server,
  ArrowRight,
  ArrowLeft,
  ArrowUpCircle,
  AlertTriangle,
  Loader2,
  ChevronDown,
  ChevronUp,
  ChevronRight,
  Plus,
  Boxes,
  Database,
  Workflow,
  Archive,
  FileCode,
  Compass,
  Share2,
  Copy,
  Check,
  X,
} from "lucide-react";
import { api, ConfigManagerEnvironment, ConfigManagerProgress } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { OnboardDevices } from "@/components/network/onboard-devices";

type IconType = ComponentType<{ className?: string }>;

// Daalu components → label. `ui` has no upstream service of its own (it rides
// config-store), so it isn't a provisionable toggle here.
const COMPONENTS: { key: string; label: string; help: string }[] = [
  { key: "render", label: "Render service", help: "Renders intended config to vendor syntax" },
  { key: "configStore", label: "Config store", help: "Stores intended config + serves the config API" },
  { key: "temporal", label: "Workflow engine", help: "Orchestrates and retries deploys" },
  { key: "nautobot", label: "Source of truth", help: "Inventory, IPAM, intended config" },
  { key: "ztp", label: "Zero-touch provisioning", help: "Onboard new switches automatically" },
  { key: "dhcp", label: "DHCP", help: "Bundled DHCP for zero-touch provisioning" },
];

// Per-service human (browser) URLs. These are flat, single-label hosts
// served *through the hub* over the WireGuard tunnel (the backend computes
// them in compute_urls() and stores them on the environment). We read them
// explicitly rather than deriving from base_hostname, which used to resolve to
// an unreachable in-cluster gateway IP. The vendor UIs are optional/read-mostly
// — Daalu drives the stack for you.
//
// Labels are capability names, never the upstream project names.
const CONSOLES: { key: string; label: string; help: string; icon: IconType }[] = [
  {
    key: "nautobot_human",
    label: "Source of truth",
    help: "Inventory, addressing and intended state",
    icon: Database,
  },
  {
    key: "workflow_human",
    label: "Workflows",
    help: "Watch deploys run, retry and roll back",
    icon: Workflow,
  },
  {
    key: "config_store_human",
    label: "Config store",
    help: "Stored intended configuration and history",
    icon: Archive,
  },
  {
    key: "render_human",
    label: "Rendering",
    help: "Turn intended state into device syntax",
    icon: FileCode,
  },
  {
    key: "ui_human",
    label: "Config browser",
    help: "Browse the stack's own console",
    icon: Compass,
  },
];

function consoleLinks(
  config: Record<string, unknown>,
): { label: string; help: string; icon: IconType; url: string }[] {
  const get = (k: string) => {
    const v = config?.[k];
    return typeof v === "string" && v ? v : "";
  };
  return CONSOLES.map((c) => ({
    label: c.label,
    help: c.help,
    icon: c.icon,
    // The same console lives under three keys depending on the URL source:
    // `ui_human`/`ui_url` on the integration-config row, but bare `ui` in
    // compute_urls() output — which is what the environment row / progress
    // payload carry, and urlsFor() prefers those. Without the `ui` fallback
    // the Config-browser tile (the NV-CM workflows UI) silently drops off
    // the env detail card.
    url:
      c.key === "ui_human"
        ? get("ui_human") || get("ui_url") || get("ui")
        : get(c.key),
  })).filter((c) => c.url);
}

// Internal destinations — always available, they're Daalu's own pages.
//
// Topology is per-environment: each environment bundles its own Nautobot and so
// has its own fabric, so the link carries the environment's slug (plus its
// display name, purely so the page can label itself without a second fetch).
// Without that every card's Topology link landed on the primary environment's
// graph.
function internalLinks(
  env: ConfigManagerEnvironment,
): { label: string; help: string; href: string; icon: IconType }[] {
  const q = `?env=${encodeURIComponent(env.slug)}&env_name=${encodeURIComponent(env.name)}`;
  return [
    {
      label: "Topology",
      help: "See the live fabric and open a session on a device",
      href: `/network/topology${q}`,
      icon: Share2,
    },
    {
      label: "Operations",
      help: "Onboard devices, review drift, approve changes",
      href: "/operations",
      icon: Server,
    },
  ];
}

// The identifier the controller will derive from what the user typed. Kept in
// step with the server's own derivation so the preview under the Name field is
// honest about what the namespace and hostnames will be — but it is only a
// preview: the server derives it again and is the authority.
function slugify(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// A DNS-1123 label, which is what a namespace and a hostname label both are.
const SLUG_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
const SLUG_MAX = 24;

/**
 * Client-side name check. Deliberately only the rules that are objectively
 * true of any label — emptiness, length, charset. The reserved-prefix rule
 * (a name that would collide with a tool console) belongs to the controller;
 * duplicating it here could only ever drift, so we let the server 400 and show
 * its reason.
 */
function nameError(name: string): string | null {
  if (!name.trim()) return "Give this environment a name.";
  const slug = slugify(name);
  if (!slug) return "Use at least one letter or number.";
  if (slug.length > SLUG_MAX) {
    return `Too long — that becomes ${slug.length} characters and the limit is ${SLUG_MAX}.`;
  }
  if (!SLUG_RE.test(slug)) return "Use letters, numbers and hyphens only.";
  return null;
}

// A stack provisioned before named environments (or read by a member, who
// can't call the admin-only list route) is known only through its integration
// row. Name it from where it lives: first DNS label of base_hostname wins
// ("default.cmtools.example" → "Default"), then the namespace with its `cm-`
// prefix stripped.
function environmentName(baseHostname: string, namespace: string): string {
  const label = baseHostname.split(".")[0]?.trim() ?? "";
  const raw = label || namespace.replace(/^cm-/, "").trim();
  if (!raw) return "Default environment";
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function isBusy(state: string): boolean {
  return state === "pending" || state === "provisioning" || state === "unknown";
}

// Renders both the integration `status` (connected/error) and the controller
// lifecycle `state` (pending|provisioning|active|error|deleting|destroyed);
// anything unmapped falls through to amber "in flight", which is the right
// default for both vocabularies.
function StatusBadge({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    connected: ["text-accent-emerald", "bg-accent-emerald"],
    active: ["text-accent-emerald", "bg-accent-emerald"],
    error: ["text-signal-critical", "bg-signal-critical"],
    destroyed: ["text-muted", "bg-muted"],
    deleting: ["text-muted", "bg-muted"],
  };
  const [text, dot] = map[status] ?? ["text-accent-amber", "bg-accent-amber"];
  return (
    <span className={`inline-flex items-center gap-1.5 text-[11px] uppercase tracking-wider ${text}`}>
      <span className={`h-2 w-2 rounded-full ${dot}`} />
      {status || "provisioning"}
    </span>
  );
}

function extractDetail(err: Error): string | null {
  // Backend errors arrive as "<status> <statusText>: <body>". Pull the
  // body if it's a JSON object with a {detail} key, which is what
  // FastAPI HTTPException renders. Both name rejections (400 "…is reserved",
  // 409 "…already exists") and the upgrade 409 ("1.3.0 is not newer than the
  // current") are written to be read by the user, so this is shown verbatim.
  const m = err.message.match(/\d+\s[^:]+:\s(.+)$/);
  if (!m) return null;
  try {
    const body = JSON.parse(m[1]) as { detail?: string };
    return body.detail ?? null;
  } catch {
    return m[1];
  }
}

// Ordinary in-flight kubelet reasons. A pod running init containers sits in
// phase `Pending` for minutes — that's the install working, not a fault — so
// these keep it out of the warning styling.
const BENIGN_REASON = /^(Init:|ContainerCreating|PodInitializing|Completed)/;
// Reasons that mean the install is stuck on something a human must fix.
const FAILING_REASON = /(ImagePull|Err|BackOff|CrashLoop|Invalid|Failed)/;

type PodTone = "ok" | "warn" | "bad";

function podTone(pod: ConfigManagerProgress["pods"][number]): PodTone {
  const reason = pod.reason ?? "";
  if (pod.phase === "Failed" || FAILING_REASON.test(reason)) return "bad";
  // Pending with no reason (or an unrecognised one) is usually unschedulable —
  // the "Insufficient cpu" case an oversized size_profile produces.
  if (pod.phase === "Pending" && !BENIGN_REASON.test(reason)) return "warn";
  return "ok";
}

const TONE_TEXT: Record<PodTone, string> = {
  ok: "text-muted",
  warn: "text-accent-amber",
  bad: "text-signal-critical",
};

// Ready/total pods, preferring the server's own tally and falling back to
// counting the pod list (the summary is absent before the namespace exists).
function podCounts(progress: ConfigManagerProgress | undefined): [number, number] {
  const pods = progress?.pods ?? [];
  const ready =
    progress?.summary?.ready ?? pods.filter((p) => p.total > 0 && p.ready >= p.total).length;
  const total = progress?.summary?.total ?? pods.length;
  return [ready, total];
}

/**
 * Collapsible per-pod list. Shared by the install-progress panel (open while
 * the install is working) and the completion state (present but shut, so a
 * healthy stack reads as "done" rather than as a wall of pod names).
 */
function WorkloadList({
  pods,
  defaultOpen,
}: {
  pods: ConfigManagerProgress["pods"];
  defaultOpen: boolean;
}) {
  // null = "follow defaultOpen"; a boolean means the user has taken over and
  // we stop reopening on them.
  const [override, setOverride] = useState<boolean | null>(null);
  const open = override ?? defaultOpen;

  if (pods.length === 0) return null;

  return (
    <div className="rounded-lg border border-line bg-bg-base/40">
      <button
        type="button"
        onClick={() => setOverride(!open)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs"
      >
        <span className="font-semibold uppercase tracking-wider text-muted">
          Workloads ({pods.length})
        </span>
        {open ? (
          <ChevronUp className="h-3.5 w-3.5 text-muted" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 text-muted" />
        )}
      </button>

      {open && (
        <ul className="border-t border-line divide-y divide-line">
          {pods.map((p) => {
            const tone = podTone(p);
            return (
              <li
                key={p.name}
                className="flex items-center justify-between gap-3 px-3 py-1.5 text-[11px]"
              >
                <span className="font-mono truncate text-muted">{p.name}</span>
                <span className={`flex shrink-0 items-center gap-2 ${TONE_TEXT[tone]}`}>
                  {p.reason && <span>{p.reason}</span>}
                  <span>{p.phase}</span>
                  <span className="tabular-nums">
                    {p.ready}/{p.total}
                  </span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * Live install progress for an environment that's still coming up.
 *
 * Create returns 202 immediately, so this panel — not a spinner on the button
 * — is what tells the user what's happening for the several minutes the chart
 * takes to roll out. Pods answer "is it moving", events answer "why is it
 * stuck": both are needed because a pod that can't be scheduled looks
 * identical to one that's pulling a 10 GB image until you read the event.
 *
 * The query lives in the page (a single poller covers every environment), so
 * this takes the data as a prop rather than polling a second time.
 */
function ProvisioningProgress({
  progress,
  isLoading,
}: {
  progress: ConfigManagerProgress | undefined;
  isLoading: boolean;
}) {
  const busy = progress?.state === "pending" || progress?.state === "provisioning";

  const pods = progress?.pods ?? [];
  const events = progress?.events ?? [];
  const [ready, total] = podCounts(progress);

  return (
    <section className="rounded-2xl border border-line bg-bg-card p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-medium flex items-center gap-2">
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin text-accent-cyan" />
            ) : (
              <Boxes className="h-4 w-4 text-accent-cyan" />
            )}
            Building your management stack
          </h2>
          <p className="text-muted text-sm mt-1">
            {progress?.namespace ? (
              <>
                Installing into <span className="text-fg font-mono">{progress.namespace}</span>
                {progress.chart_version ? ` · chart v${progress.chart_version}` : ""}
              </>
            ) : (
              "Scheduling the install…"
            )}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <StatusBadge status={progress?.state ?? "pending"} />
          {total > 0 && (
            <span className="text-[11px] text-muted">
              {ready}/{total} ready
            </span>
          )}
        </div>
      </div>

      {busy && (
        <p className="text-[11px] text-muted">
          This runs in the background and can take a few minutes — image pulls
          dominate the first run. You can leave this page.
        </p>
      )}

      {/* A status note ("namespace not readable yet"), not a failure — the
          controller simply hasn't created things we can read yet. */}
      {progress?.message && <p className="text-xs text-muted">{progress.message}</p>}

      {progress?.state === "error" && (
        <p className="inline-flex items-start gap-1.5 text-xs text-signal-critical">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
          {progress.last_error || "The install failed."}
        </p>
      )}

      {isLoading && !progress && (
        <div className="flex items-center gap-2 text-[11px] text-muted">
          <Loader2 className="h-3 w-3 animate-spin" /> Reading cluster state…
        </div>
      )}

      <WorkloadList pods={pods} defaultOpen={busy} />

      {/* Only notable events come back (FailedScheduling, ErrImagePull…), so
          anything here is the actionable reason the install isn't finishing. */}
      {events.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-muted">
            Notable events
          </p>
          <ul className="space-y-1">
            {events.map((e, i) => (
              <li
                key={`${e.object}-${e.reason}-${i}`}
                className="rounded border border-accent-amber/40 bg-accent-amber/10 px-2 py-1.5 text-[11px] text-accent-amber"
              >
                <span className="font-semibold">{e.reason}</span>
                {e.object && <span className="font-mono opacity-80"> · {e.object}</span>}
                {e.message && <span className="block text-muted">{e.message}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/**
 * Chart version + upgrade control for one environment.
 *
 * Reads the posture straight off the environment row (the page's list poller
 * refreshes it), so there is no second version query. The upgrade endpoint is
 * fire-and-forget: a 202 means "scheduled", so we never block on the mutation
 * — we invalidate the list and let its poll, which speeds up while anything is
 * busy, report the rollout.
 *
 * Admin-only endpoint, so it's gated on is_admin — members just don't see it.
 */
function VersionRow({ env }: { env: ConfigManagerEnvironment }) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const isAdmin = user?.is_admin ?? false;

  // Set when we fire an upgrade, cleared once the controller has visibly
  // taken it (state goes busy) or finished it (no upgrade left to do).
  // Without this the button would flicker back to "Upgrade" in the gap
  // between our 202 and the controller flipping state to `pending`.
  const [scheduled, setScheduled] = useState(false);

  const busy = env.state === "pending" || env.state === "provisioning";

  useEffect(() => {
    if (busy || !env.upgrade_available) setScheduled(false);
  }, [busy, env.upgrade_available]);

  const upgrade = useMutation({
    mutationFn: () => api.onboarding.upgradeConfigManagerEnvironment(env.id, {}),
    onSuccess: () => {
      setScheduled(true);
      // Start observing immediately rather than waiting out the interval.
      qc.invalidateQueries({ queryKey: ["configManager", "environments"] });
      qc.invalidateQueries({ queryKey: ["integrations", "config"] });
    },
  });

  // Nothing version-related to say: not an admin, or the controller hasn't
  // pinned a chart to this environment yet.
  if (!isAdmin || (!env.chart_version && !env.upgrade_available)) return null;

  const inFlight = busy || scheduled || upgrade.isPending;
  const failure = upgrade.isError
    ? extractDetail(upgrade.error as Error) || "Upgrade could not be scheduled"
    : null;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {env.chart_version && (
          <span className="text-[11px] rounded-full border border-line px-2 py-0.5 text-muted">
            Chart v{env.chart_version}
          </span>
        )}

        {env.upgrade_available && (
          <span className="inline-flex items-center gap-1 text-[11px] rounded-full border border-accent-emerald/40 bg-accent-emerald/10 px-2 py-0.5 text-accent-emerald">
            <ArrowUpCircle className="h-3 w-3" />
            Upgrade available
          </span>
        )}

        {env.upgrade_available && (
          <button
            onClick={() => upgrade.mutate()}
            disabled={inFlight}
            className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1 text-xs font-medium text-black hover:opacity-90 disabled:opacity-60"
          >
            {inFlight && <Loader2 className="h-3 w-3 animate-spin" />}
            {inFlight ? "Upgrading…" : "Upgrade"}
          </button>
        )}
      </div>

      {inFlight && (
        <p className="text-[11px] text-muted">
          Upgrading the stack — this runs in the background and can take a few
          minutes. You can leave this page.
        </p>
      )}

      {env.state === "error" && (
        <p className="inline-flex items-start gap-1.5 text-xs text-signal-critical">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
          {env.last_error || "The last operation failed."}
        </p>
      )}

      {failure && (
        <p className="inline-flex items-start gap-1.5 text-xs text-signal-critical">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
          {failure}
        </p>
      )}
    </div>
  );
}

/**
 * One environment, as a card in the grid.
 *
 * Whole tile is the control — a real <button> so keyboard focus, Enter and
 * Space come for free.
 */
function EnvironmentTile({
  name,
  state,
  namespace,
  chartVersion,
  summary,
  isPrimary,
  onOpen,
}: {
  name: string;
  state: string;
  namespace: string;
  chartVersion: string;
  summary: string;
  isPrimary: boolean;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group flex w-full flex-col gap-3 rounded-2xl border border-line bg-bg-card p-5 text-left transition hover:border-accent-cyan/50 hover:bg-white/5 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-cyan"
    >
      <div className="flex items-start justify-between gap-3">
        <span className="flex min-w-0 items-center gap-2 text-base font-medium text-fg">
          <Boxes className="h-4 w-4 shrink-0 text-accent-cyan" />
          <span className="truncate">{name}</span>
          {/* Which environment the rest of Daalu talks to by default. Quiet
              on purpose — it's a fact about the environment, not a status. */}
          {isPrimary && (
            <span className="shrink-0 rounded-full border border-line px-1.5 py-px text-[10px] uppercase tracking-wider text-muted">
              Primary
            </span>
          )}
        </span>
        <StatusBadge status={state} />
      </div>

      <div className="space-y-0.5 text-xs text-muted">
        {namespace && <p className="font-mono truncate">{namespace}</p>}
        {chartVersion && <p>Version {chartVersion}</p>}
      </div>

      <div className="flex items-center justify-between gap-2 text-xs text-muted">
        <span>{summary}</span>
        <ChevronRight className="h-4 w-4 shrink-0 transition group-hover:translate-x-0.5 group-hover:text-fg" />
      </div>
    </button>
  );
}

/**
 * Create a named environment.
 *
 * Used both as the empty state and, once environments exist, as the "Add
 * environment" panel — hence `onCancel`, which is null in the empty state
 * because there is nothing to go back to.
 *
 * The Name field leads because it is the only irreversible choice on the form:
 * components and size can be changed later, the name cannot. The live slug
 * preview underneath is there so nobody discovers what "Prod EU!" became by
 * reading a hostname a week later.
 */
function CreateEnvironmentForm({
  onCreated,
  onCancel,
}: {
  onCreated: (env: ConfigManagerEnvironment) => void;
  onCancel: (() => void) | null;
}) {
  const qc = useQueryClient();

  const [name, setName] = useState("");
  // Only complain once they've tried, so the field isn't red before it's had
  // a chance to be right.
  const [touched, setTouched] = useState(false);
  const [picked, setPicked] = useState<Record<string, boolean>>({
    render: true,
    configStore: true,
    temporal: true,
    nautobot: true,
    ztp: false,
    dhcp: false,
  });
  const [size, setSize] = useState("small");
  // Break-glass Nautobot login. Collapsed by default: everyone on the team is
  // SSO'd in from their Daalu session and never needs it, so surfacing it
  // openly would imply a password is part of normal access. It matters only
  // when SSO itself is what's broken.
  const [showAdmin, setShowAdmin] = useState(false);
  const [adminUser, setAdminUser] = useState("");
  const [adminPass, setAdminPass] = useState("");

  const slug = slugify(name);
  const localError = nameError(name);
  // Mirrors local_admin.MIN_PASSWORD_LEN server-side. Blank is fine — the
  // server generates one and returns it.
  const adminPassError =
    adminPass && adminPass.length < 12
      ? "Password must be at least 12 characters"
      : null;

  // 202 = scheduled, so this resolves in milliseconds. There's no completion
  // state to show here — hand straight off to the environment's progress view.
  const create = useMutation({
    mutationFn: () =>
      api.onboarding.createConfigManagerEnvironment({
        name: name.trim(),
        components: picked,
        size_profile: size,
        // Omit rather than send blanks — the server defaults the username and
        // generates the password when these are absent.
        ...(adminUser.trim() ? { admin_username: adminUser.trim() } : {}),
        ...(adminPass ? { admin_password: adminPass } : {}),
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["integrations", "config"] });
      onCreated(res.environment);
    },
  });

  // The server's own verdict on the name (400 reserved/unusable, 409 taken)
  // is written for the person who typed it, so it goes on screen as-is.
  const serverError = create.isError
    ? extractDetail(create.error as Error) ||
      (create.error as Error)?.message ||
      "Creating the environment failed"
    : null;

  const submit = () => {
    setTouched(true);
    if (localError || adminPassError) return;
    create.mutate();
  };

  return (
    <section className="rounded-2xl border border-line bg-bg-card p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-medium">Create an environment</h2>
          <p className="text-muted text-sm mt-1">
            Name it, pick the components you need and a size. Daalu builds the
            management stack for you — the first run takes a few minutes.
          </p>
        </div>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="shrink-0 text-sm text-muted hover:text-fg"
          >
            Cancel
          </button>
        )}
      </div>

      <div className="space-y-1.5">
        <label htmlFor="env-name" className="block text-sm font-medium">
          Name
        </label>
        <input
          id="env-name"
          type="text"
          value={name}
          autoFocus
          onChange={(e) => {
            setName(e.target.value);
            create.reset();
          }}
          onBlur={() => setTouched(true)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          placeholder="Production"
          aria-invalid={touched && !!localError}
          aria-describedby="env-name-help"
          className="w-full max-w-sm rounded-lg border border-line bg-bg-card px-3 py-1.5 text-sm"
        />
        <p id="env-name-help" className="text-xs text-muted">
          {slug ? (
            <>
              Identifier: <span className="font-mono text-fg">{slug}</span> —{" "}
            </>
          ) : null}
          this becomes the namespace and the public hostnames, and{" "}
          <span className="text-fg">cannot be changed after creation</span>.
        </p>
        {touched && localError && (
          <p className="inline-flex items-start gap-1.5 text-xs text-signal-critical">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
            {localError}
          </p>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {COMPONENTS.map((c) => (
          <label
            key={c.key}
            className="flex items-start gap-2 rounded-xl border border-line p-3 cursor-pointer hover:bg-white/5"
          >
            <input
              type="checkbox"
              className="mt-1"
              checked={!!picked[c.key]}
              onChange={(e) => setPicked((prev) => ({ ...prev, [c.key]: e.target.checked }))}
            />
            <span>
              <span className="text-sm font-medium">{c.label}</span>
              <span className="block text-xs text-muted">{c.help}</span>
            </span>
          </label>
        ))}
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center gap-3">
          <label htmlFor="env-size" className="text-sm text-muted">
            Size
          </label>
          <select
            id="env-size"
            value={size}
            onChange={(e) => setSize(e.target.value)}
            className="rounded-lg border border-line bg-bg-card px-3 py-1.5 text-sm"
          >
            <option value="small">Small</option>
            <option value="medium">Medium</option>
            <option value="large">Large</option>
          </select>
        </div>
        {/* Size drives CPU *requests* and replica counts, and the pods land
            in the customer's cluster — so an oversized pick doesn't fail
            fast, it strands pods in Pending with `Insufficient cpu` after
            the install has already appeared to start. */}
        <p className="text-xs text-muted">
          {size === "small"
            ? "Single-instance Postgres and minimal CPU requests — fits a single-node cluster."
            : "Raises CPU requests and replica counts across the workflow tier. On a small cluster the pods stay Pending with “Insufficient cpu”."}
        </p>
      </div>

      {/* Break-glass admin. Everyone on the team signs in to the tools with
          their Daalu session — this account exists for the case where SSO
          itself is unavailable, which is exactly when you can't go and create
          one. Optional: left alone, the server generates a strong password and
          shows it once after creation. */}
      <div className="space-y-1.5">
        <button
          type="button"
          onClick={() => setShowAdmin((v) => !v)}
          aria-expanded={showAdmin}
          className="text-sm text-muted hover:text-fg"
        >
          {showAdmin ? "−" : "+"} Break-glass admin login (optional)
        </button>
        {showAdmin && (
          <div className="space-y-3 rounded-xl border border-line p-3">
            <p className="text-xs text-muted">
              A local Nautobot administrator, for signing in when Keycloak is
              unavailable. Everyone else is signed in automatically from their
              Daalu session and never needs this. Nautobot is the only tool
              here with its own login — the rest accept only your Daalu
              identity. Leave blank to have one generated.
            </p>
            <div className="flex flex-col gap-3 sm:flex-row">
              <div className="space-y-1.5">
                <label htmlFor="env-admin-user" className="block text-sm font-medium">
                  Username
                </label>
                <input
                  id="env-admin-user"
                  type="text"
                  autoComplete="off"
                  value={adminUser}
                  onChange={(e) => setAdminUser(e.target.value)}
                  placeholder="admin"
                  className="w-full rounded-lg border border-line bg-bg-card px-3 py-1.5 text-sm"
                />
              </div>
              <div className="space-y-1.5">
                <label htmlFor="env-admin-pass" className="block text-sm font-medium">
                  Password
                </label>
                <input
                  id="env-admin-pass"
                  type="password"
                  autoComplete="new-password"
                  value={adminPass}
                  onChange={(e) => setAdminPass(e.target.value)}
                  placeholder="Generated if blank"
                  aria-invalid={!!adminPassError}
                  className="w-full rounded-lg border border-line bg-bg-card px-3 py-1.5 text-sm"
                />
              </div>
            </div>
            {adminPassError && (
              <p className="inline-flex items-start gap-1.5 text-xs text-signal-critical">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
                {adminPassError}
              </p>
            )}
          </div>
        )}
      </div>

      <button
        onClick={submit}
        disabled={create.isPending || (touched && !!localError) || !!adminPassError}
        className="inline-flex items-center gap-2 rounded-xl bg-accent px-4 py-2 text-sm font-medium text-black hover:opacity-90 disabled:opacity-60"
      >
        {create.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
        {/* The request only *schedules* the install, so this label is on
            screen for a moment — the progress view takes it from here. */}
        {create.isPending ? "Scheduling…" : "Create environment"}
      </button>

      {serverError && (
        <p className="inline-flex items-start gap-1.5 text-sm text-signal-critical">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-px" />
          {serverError}
        </p>
      )}
    </section>
  );
}

/**
 * Where you go once an environment is up. Consoles are external (they live in
 * the customer's own cluster, reached through the hub); the Daalu links are
 * ordinary in-app navigation, so they get an arrow rather than the
 * leaving-the-app affordance.
 */
function CompletionState({
  config,
  componentCount,
  env,
}: {
  config: Record<string, unknown>;
  componentCount: number;
  env: ConfigManagerEnvironment;
}) {
  const consoles = consoleLinks(config);
  const links = internalLinks(env);

  return (
    <div className="space-y-6">
      <section className="rounded-2xl border border-accent-emerald/40 bg-accent-emerald/10 p-5">
        <h2 className="flex items-center gap-2 text-base font-medium text-fg">
          <CheckCircle2 className="h-5 w-5 text-accent-emerald" />
          All components are ready
        </h2>
        <p className="mt-1 text-sm text-muted">
          {componentCount > 0
            ? `${componentCount} component${componentCount === 1 ? "" : "s"} running in this environment.`
            : "Your management stack is running in this environment."}{" "}
          Pick up where you want to work below.
        </p>
      </section>

      {consoles.length > 0 && (
        <section className="space-y-2">
          <p className="text-xs uppercase tracking-wider text-muted">Stack consoles</p>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {consoles.map((c) => {
              const Icon = c.icon;
              return (
                <a
                  key={c.label}
                  href={c.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-start gap-3 rounded-xl border border-line bg-bg-card p-3 transition hover:border-accent-cyan/50 hover:bg-white/5"
                >
                  <Icon className="mt-0.5 h-4 w-4 shrink-0 text-accent-cyan" />
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium text-fg">{c.label}</span>
                    <span className="block text-xs text-muted">{c.help}</span>
                  </span>
                  <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />
                </a>
              );
            })}
          </div>
          <p className="text-[11px] text-muted">
            These consoles are optional and read-mostly — you sign in with your
            org SSO. Day to day, drive changes from Operations.
          </p>
        </section>
      )}

      <section className="space-y-2">
        <p className="text-xs uppercase tracking-wider text-muted">In Daalu</p>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {links.map((l) => {
            const Icon = l.icon;
            return (
              <Link
                key={l.href}
                href={l.href}
                className="flex items-start gap-3 rounded-xl border border-line bg-bg-card p-3 transition hover:border-accent-emerald/50 hover:bg-white/5"
              >
                <Icon className="mt-0.5 h-4 w-4 shrink-0 text-accent-emerald" />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium text-fg">{l.label}</span>
                  <span className="block text-xs text-muted">{l.help}</span>
                </span>
                <ArrowRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />
              </Link>
            );
          })}
        </div>
      </section>
    </div>
  );
}

/**
 * Type-the-name confirmation for a destructive, irreversible teardown.
 *
 * A native `confirm()` is one keypress away from wiping a production
 * environment — every device, credential and config history in it — so the
 * name has to be typed out. Matching is trimmed and case-insensitive: the
 * point is deliberate intent, not a spelling test.
 */
function DeleteEnvironmentDialog({
  name,
  namespace,
  stuckDeleting,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  name: string;
  namespace: string;
  stuckDeleting: boolean;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: (force: boolean) => void;
}) {
  const [typed, setTyped] = useState("");
  const [force, setForce] = useState(false);
  const matches = typed.trim().toLowerCase() === name.trim().toLowerCase();

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-bg/70 p-4 pt-16 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label={`Delete environment ${name}`}
      onClick={(e) => {
        if (e.target === e.currentTarget && !pending) onCancel();
      }}
    >
      <div className="w-full max-w-[520px] surface space-y-4 p-5">
        <div className="flex items-start justify-between gap-3">
          <h2 className="flex items-center gap-2 text-base font-semibold text-[color:var(--critical)]">
            <AlertTriangle className="h-4 w-4" />
            Delete {name}?
          </h2>
          <button
            type="button"
            onClick={onCancel}
            disabled={pending}
            aria-label="Cancel"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-line text-muted hover:text-fg disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-2 text-sm text-muted">
          <p>
            This uninstalls the whole management stack — source of truth,
            workflows, config store, rendering — from the cluster
            {namespace && (
              <>
                {" "}
                and deletes the namespace{" "}
                <span className="font-mono text-fg">{namespace}</span>
              </>
            )}
            .
          </p>
          <p>
            Every device record, credential and config history inside it goes
            with it, along with its consoles. The environment is then removed
            from Daalu entirely. This cannot be undone.
          </p>
        </div>

        <label className="block">
          <span className="text-[11px] uppercase tracking-wider text-muted">
            Type <span className="font-mono normal-case text-fg">{name}</span> to
            confirm
          </span>
          <input
            type="text"
            value={typed}
            autoFocus
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && matches && !pending) onConfirm(force);
              if (e.key === "Escape" && !pending) onCancel();
            }}
            className="mt-1 h-9 w-full rounded-lg border border-line bg-bg-elevated px-3 text-sm focus:border-signal-critical/60 focus:outline-none"
          />
        </label>

        {stuckDeleting && (
          <label className="flex items-start gap-2 rounded-lg border border-line bg-bg-elevated p-3 text-xs text-muted">
            <input
              type="checkbox"
              checked={force}
              onChange={(e) => setForce(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              <span className="text-fg">Force removal.</span> Teardown is already
              running but hasn't finished — usually because the host cluster is
              unreachable. This drops Daalu's record without waiting, which can
              leave resources behind in the cluster if it ever comes back.
            </span>
          </label>
        )}

        {error && (
          <p className="text-xs text-[color:var(--critical)]">{error}</p>
        )}

        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={pending}
            className="h-9 rounded-lg border border-line px-3 text-xs text-muted hover:text-fg disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onConfirm(force)}
            disabled={!matches || pending}
            className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-signal-critical/40 bg-signal-critical/10 px-3 text-xs text-[color:var(--critical)] disabled:opacity-40"
          >
            {pending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {force ? "Force delete" : "Delete environment"}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * One environment, opened. Header + upgrade control are constant; the body is
 * either the install-progress panel (still coming up, or failed) or the
 * completion state (up).
 *
 * Takes progress as a prop — the page owns the poller, so opening a tile never
 * adds a second one.
 */
function EnvironmentDetail({
  env,
  name,
  urls,
  componentCount,
  progress,
  isLoading,
  onBack,
}: {
  env: ConfigManagerEnvironment;
  name: string;
  urls: Record<string, unknown>;
  componentCount: number;
  progress: ConfigManagerProgress | undefined;
  isLoading: boolean;
  onBack: () => void;
}) {
  const namespace = progress?.namespace || env.namespace;
  const chartVersion = progress?.chart_version || env.chart_version;
  const active = env.state === "active";

  const qc = useQueryClient();
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  // A teardown that can't finish (host cluster gone or unreachable) parks the
  // environment in `deleting`. Offer the record-only removal from there, and
  // only from there — it can leave cluster-side leftovers.
  const stuckDeleting = env.state === "deleting";
  const destroy = useMutation({
    mutationFn: (force: boolean) =>
      api.onboarding.deleteConfigManagerEnvironment(env.id, force),
    onSuccess: () => {
      setConfirmingDelete(false);
      qc.invalidateQueries({ queryKey: ["configManager", "environments"] });
      onBack();
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-fg"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> All environments
        </button>
        <button
          type="button"
          disabled={destroy.isPending}
          onClick={() => setConfirmingDelete(true)}
          className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-signal-critical/40 px-3 text-xs text-[color:var(--critical)] hover:bg-signal-critical/10 disabled:opacity-50"
        >
          {destroy.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
          {stuckDeleting ? "Deleting… (force remove)" : "Delete environment"}
        </button>
      </div>
      {confirmingDelete && (
        <DeleteEnvironmentDialog
          name={name}
          namespace={namespace}
          stuckDeleting={stuckDeleting}
          pending={destroy.isPending}
          error={destroy.isError ? extractDetail(destroy.error as Error) ?? String(destroy.error) : null}
          onCancel={() => setConfirmingDelete(false)}
          onConfirm={(force) => destroy.mutate(force)}
        />
      )}
      {destroy.isError && !confirmingDelete && (
        <p className="text-xs text-[color:var(--critical)]">
          Delete failed: {String(destroy.error)}
        </p>
      )}

      <section className="rounded-2xl border border-line bg-bg-card p-5 space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 text-base font-medium">
              <Boxes className="h-4 w-4 text-accent-cyan" />
              {name}
              {env.is_primary && (
                <span className="rounded-full border border-line px-1.5 py-px text-[10px] uppercase tracking-wider text-muted">
                  Primary
                </span>
              )}
            </h2>
            <p className="mt-1 text-sm text-muted">
              {namespace && <span className="font-mono text-fg">{namespace}</span>}
              {namespace && chartVersion ? " · " : ""}
              {chartVersion ? `version ${chartVersion}` : ""}
            </p>
          </div>
          <StatusBadge status={env.state} />
        </div>

        <VersionRow env={env} />
      </section>

      {active ? (
        <>
          {/* Tabbed layout: the active-env page grew too many surfaces to
              stack vertically (consoles + onboarding + workloads + now
              observability). One group per tab.

              Devices is offered on every environment, not just the primary:
              each one bundles its own Nautobot, so each has an inventory of
              its own to onboard into. The panel carries the slug so it reads
              and writes that environment's source of truth. */}
          <EnvDetailTabs
            consoles={
              <CompletionState
                config={urls}
                componentCount={componentCount}
                env={env}
              />
            }
            devices={<OnboardDevices env={env} />}
            observability={<EnvObservability />}
            workloads={<WorkloadList pods={progress?.pods ?? []} defaultOpen />}
          />
        </>
      ) : (
        <ProvisioningProgress progress={progress} isLoading={isLoading} />
      )}
    </div>
  );
}

export default function NetworkServerManagementPage() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const isAdmin = user?.is_admin ?? false;

  // Which environment is open, and whether the create form is showing over
  // the list. Local state, not routes: this page renders inside the
  // Managed-infra tab shell and a router push would break out of the tabs.
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  // Break-glass credential for the environment just created. Shown once, then
  // dismissed by the user — the server won't serve the password again.
  const [justCreated, setJustCreated] = useState<{
    slug: string;
    username: string;
    password: string;
  } | null>(null);
  const [adminCopied, setAdminCopied] = useState(false);

  async function copyAdmin() {
    if (!justCreated) return;
    await navigator.clipboard.writeText(justCreated.password);
    setAdminCopied(true);
    setTimeout(() => setAdminCopied(false), 1500);
  }

  const { data: configs } = useQuery({
    queryKey: ["integrations", "config"],
    queryFn: () => api.integrations.listConfig(),
    refetchInterval: 15000,
  });

  const cm = useMemo(
    () => (configs ?? []).find((c) => c.provider === "config_manager"),
    [configs],
  );

  // The list is admin-only, so a 403 shouldn't spin on retries.
  const envQuery = useQuery({
    queryKey: ["configManager", "environments"],
    queryFn: () => api.onboarding.configManagerEnvironments(),
    enabled: isAdmin,
    retry: false,
    refetchInterval: (q) => {
      const rows = q.state.data ?? [];
      // Nothing exists: no install to watch, so don't poll at all.
      if (rows.length === 0) return false;
      // A teardown in flight polls at install speed too — the row vanishes
      // when it lands, and a tile for a deleted environment left sitting
      // there for half a minute is exactly the ghost this page must not show.
      const watching = rows.some(
        (e) => isBusy(e.state) || e.state === "deleting",
      );
      return watching ? 5000 : 30000;
    },
  });

  // A member can't call the admin list route, and a stack that predates named
  // environments may not be listed either — but the integration row is enough
  // to show one read-only tile. Synthesised, not fetched.
  const fallbackEnv = useMemo<ConfigManagerEnvironment | null>(() => {
    if (!cm) return null;
    const config = (cm.config ?? {}) as Record<string, unknown>;
    const baseHostname = String(config.base_hostname ?? "");
    return {
      id: cm.id,
      name: environmentName(baseHostname, ""),
      slug: "",
      is_primary: true,
      state: "active",
      namespace: "",
      base_hostname: baseHostname,
      chart_version: "",
      upgrade_available: false,
      last_error: null,
      urls: config,
    };
  }, [cm]);

  const envs = useMemo<ConfigManagerEnvironment[]>(() => {
    // `destroyed` is a tombstone: the stack is already uninstalled, so a tile
    // for it is a ghost. The hub filters these too — this keeps the UI honest
    // against an older backend, and drops the row the instant a delete lands.
    const rows = (envQuery.data ?? []).filter((e) => e.state !== "destroyed");
    if (rows.length > 0) return rows;
    return fallbackEnv ? [fallbackEnv] : [];
  }, [envQuery.data, fallbackEnv]);

  // A single page-level poller covers every environment that needs pod-level
  // detail: the ones still installing (their tiles show N/M pods ready) plus
  // whichever one is open. One query, not one per tile — the key is the id set
  // so selecting a tile refetches rather than starting a second poller.
  const watched = useMemo(() => {
    const ids = new Set(envs.filter((e) => isBusy(e.state)).map((e) => e.id));
    if (selected) ids.add(selected);
    // Sorted so the key is stable regardless of list order.
    return Array.from(ids).sort();
  }, [envs, selected]);

  const anyBusy = envs.some((e) => isBusy(e.state));

  const progressQuery = useQuery({
    queryKey: ["configManager", "envProgress", watched],
    queryFn: async () => {
      const entries = await Promise.all(
        watched.map(async (id) => {
          try {
            return [id, await api.onboarding.configManagerEnvironmentProgress(id)] as const;
          } catch {
            // One environment being unreadable (404 mid-teardown, a tunnel
            // blip) must not blank the others.
            return [id, null] as const;
          }
        }),
      );
      const out: Record<string, ConfigManagerProgress> = {};
      for (const [id, data] of entries) if (data) out[id] = data;
      return out;
    },
    enabled: isAdmin && watched.length > 0,
    retry: false,
    refetchInterval: anyBusy ? 5000 : 30000,
  });

  const progressById = progressQuery.data ?? {};

  // Components are recorded on the (single, primary-only) integration row, so
  // the count is only knowable for the primary environment. Elsewhere the
  // completion copy falls back to a generic line.
  const cmComponents = ((cm?.config ?? {}) as Record<string, unknown>).components as
    | Record<string, boolean>
    | undefined;
  const primaryComponentCount = COMPONENTS.filter((c) => cmComponents?.[c.key]).length;

  const componentCountFor = (env: ConfigManagerEnvironment) =>
    env.is_primary ? primaryComponentCount : 0;

  // URLs come from the environment itself; the integration row is the older
  // home for the primary's and is only read as a fallback.
  const urlsFor = (env: ConfigManagerEnvironment): Record<string, unknown> => {
    const live = progressById[env.id]?.urls;
    if (live && Object.keys(live).length > 0) return live;
    if (Object.keys(env.urls ?? {}).length > 0) return env.urls;
    return env.is_primary ? ((cm?.config ?? {}) as Record<string, unknown>) : {};
  };

  const summaryFor = (env: ConfigManagerEnvironment) => {
    if (env.state === "active") {
      const n = componentCountFor(env);
      return n > 0 ? `${n} component${n === 1 ? "" : "s"} running` : "Running";
    }
    // A teardown in flight reads as an install without this — the pod counts
    // below are the same numbers going the other way.
    if (env.state === "deleting") return "Tearing down…";
    const [ready, total] = podCounts(progressById[env.id]);
    return total > 0 ? `${ready}/${total} pods ready` : "Starting up…";
  };

  const openEnv = selected ? envs.find((e) => e.id === selected) : undefined;

  // The selection can outlive its environment (torn down elsewhere, or the
  // list reconciled it away) — fall back to the list rather than a blank page.
  useEffect(() => {
    if (selected && envs.length > 0 && !envs.some((e) => e.id === selected)) {
      setSelected(null);
    }
  }, [selected, envs]);

  // Create 202s with the row already populated, so seed it into the list and
  // open it: the user lands on live progress instead of watching the form for
  // a beat and being invited to submit twice.
  const onCreated = (env: ConfigManagerEnvironment) => {
    // The break-glass password is served exactly once. Hold it in component
    // state for the banner and keep it OUT of the query cache, which outlives
    // this view and gets re-read by anything else reading the env list.
    const { admin_username, admin_password, ...listSafe } = env;
    if (admin_password) {
      setJustCreated({
        slug: env.slug,
        username: admin_username || "admin",
        password: admin_password,
      });
    }
    qc.setQueryData<ConfigManagerEnvironment[]>(
      ["configManager", "environments"],
      (old) =>
        old
          ? old.some((e) => e.id === env.id)
            ? old
            : [...old, listSafe as ConfigManagerEnvironment]
          : [listSafe as ConfigManagerEnvironment],
    );
    qc.invalidateQueries({ queryKey: ["configManager", "environments"] });
    setCreating(false);
    setSelected(env.id);
  };

  const loading = isAdmin ? envQuery.isLoading : !configs;
  const listError = envQuery.isError && !fallbackEnv;

  return (
    <div className="space-y-8 max-w-[1100px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-start gap-2">
          <Network className="h-5 w-5 mt-1.5 shrink-0 text-accent-cyan" /> Network &amp; server management
        </h1>
        <p className="text-muted text-sm mt-1">
          Daalu manages your network switches and bare-metal servers through
          dedicated management environments running in your own clusters. Create
          one here; then work the inventory and approve changes in{" "}
          <a href="/operations" className="underline">Operations</a> — Daalu
          drives the underlying services for you.
        </p>
      </div>

      {/* Shown once, immediately after creation. The password is not stored
          anywhere the UI can re-read, so this is the only chance to copy it —
          say so plainly rather than letting it scroll past as a detail. */}
      {justCreated && (
        <section className="rounded-2xl border border-signal-warning/40 bg-signal-warning/5 p-4 space-y-2">
          <h2 className="text-sm font-medium">
            Break-glass admin for {justCreated.slug} — shown once
          </h2>
          <p className="text-xs text-muted">
            Save this now. It is not shown again. You won&apos;t need it for
            day-to-day work — you and your team are signed in to the tools
            automatically from Daalu. It exists for signing in to Nautobot if
            single sign-on is ever unavailable.
          </p>
          <dl className="flex flex-wrap gap-x-8 gap-y-1 text-sm">
            <div>
              <dt className="text-xs text-muted">Username</dt>
              <dd className="font-mono">{justCreated.username}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Password</dt>
              <dd className="font-mono break-all">{justCreated.password}</dd>
            </div>
          </dl>
          <div className="flex flex-wrap items-center gap-3 pt-1">
            {/* A shown-once secret that can only be hand-selected invites a
                truncated copy. Same affordance as the API-token reveal in
                settings. */}
            <button
              type="button"
              onClick={copyAdmin}
              className="text-xs flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-line hover:border-line-strong"
            >
              {adminCopied ? (
                <>
                  <Check className="h-3.5 w-3.5" /> Copied
                </>
              ) : (
                <>
                  <Copy className="h-3.5 w-3.5" /> Copy password
                </>
              )}
            </button>
            <button
              type="button"
              onClick={() => setJustCreated(null)}
              className="text-xs text-muted underline hover:text-fg"
            >
              I&apos;ve saved it — dismiss
            </button>
          </div>
        </section>
      )}

      {loading ? (
        <div className="text-muted text-sm inline-flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading environments…
        </div>
      ) : listError ? (
        <p className="inline-flex items-start gap-1.5 text-sm text-signal-critical">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-px" />
          {extractDetail(envQuery.error as Error) || "Could not load your environments."}
        </p>
      ) : openEnv ? (
        <EnvironmentDetail
          env={openEnv}
          name={openEnv.name || openEnv.slug || "Environment"}
          urls={urlsFor(openEnv)}
          componentCount={componentCountFor(openEnv)}
          progress={progressById[openEnv.id]}
          isLoading={progressQuery.isLoading}
          onBack={() => setSelected(null)}
        />
      ) : envs.length === 0 && !isAdmin ? (
        // Creating one is admin-only, so don't hand a member a form that can
        // only 403.
        <p className="text-muted text-sm">
          No management environment has been created yet. An administrator on
          your team can create one here.
        </p>
      ) : creating || envs.length === 0 ? (
        <CreateEnvironmentForm
          onCreated={onCreated}
          // Nothing to go back to from the empty state.
          onCancel={envs.length > 0 ? () => setCreating(false) : null}
        />
      ) : (
        <section className="space-y-3">
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs uppercase tracking-wider text-muted">Environments</p>
            {isAdmin && (
              <button
                type="button"
                onClick={() => setCreating(true)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-xs text-muted transition hover:border-accent-cyan/50 hover:text-fg"
              >
                <Plus className="h-3.5 w-3.5" /> Add environment
              </button>
            )}
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {envs.map((env) => (
              <EnvironmentTile
                key={env.id}
                name={env.name || env.slug || "Environment"}
                state={env.state}
                namespace={progressById[env.id]?.namespace || env.namespace}
                chartVersion={progressById[env.id]?.chart_version || env.chart_version}
                summary={summaryFor(env)}
                isPrimary={env.is_primary}
                onOpen={() => setSelected(env.id)}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

// ── Env detail tabs ───────────────────────────────────────────────────────

const ENV_TABS = [
  { id: "consoles", label: "Consoles" },
  { id: "devices", label: "Devices" },
  { id: "observability", label: "Observability" },
  { id: "workloads", label: "Workloads" },
] as const;

/**
 * The active environment grew too many stacked surfaces — consoles,
 * device onboarding, workloads and now the observability stack. One
 * group per tab keeps the page scannable.
 */
function EnvDetailTabs({
  consoles,
  devices,
  observability,
  workloads,
}: {
  consoles: React.ReactNode;
  devices: React.ReactNode;
  observability: React.ReactNode;
  workloads: React.ReactNode;
}) {
  const [tab, setTab] = useState<(typeof ENV_TABS)[number]["id"]>("consoles");
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-1 rounded-xl border border-line bg-bg-card p-1 w-fit">
        {ENV_TABS.filter((t) => t.id !== "devices" || devices).map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`rounded-lg px-3.5 py-1.5 text-sm transition ${
              tab === t.id
                ? "bg-bg-elevated text-fg"
                : "text-muted hover:text-fg"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "consoles" && consoles}
      {tab === "devices" && devices}
      {tab === "observability" && observability}
      {tab === "workloads" && workloads}
    </div>
  );
}

// ── Observability stack section ───────────────────────────────────────────

const OBS_TILES = [
  {
    provider: "prometheus",
    label: "Metrics",
    help: "PromQL panels over your Prometheus — nodes, workloads, GPU, network devices",
    href: "/observability/prometheus",
  },
  {
    provider: "loki",
    label: "Logs",
    help: "LogQL panels over your Loki — cluster and network-device syslog",
    href: "/observability/loki",
  },
  {
    provider: "opensearch",
    label: "Search & analytics",
    help: "Full-text search over your OpenSearch indices",
    href: "/observability/opensearch",
  },
  {
    provider: "grafana",
    label: "Dashboards",
    help: "Your Grafana dashboards, rendered natively in Daalu",
    href: "/dashboards",
  },
];

/**
 * Daalu-native observability: Daalu is the frontend, the tenant's own
 * stores (Prometheus/Loki/OpenSearch/Grafana) are the backend. A tile per
 * store — connected tiles deep-link into the in-app console; unconnected
 * ones route to the connect flow on Clusters & observability.
 */
// Stores Daalu can install into the cluster itself (vendored manifests,
// applied over the tenant's registered kubeconfig).
const OBS_INSTALLABLE = new Set(["grafana", "opensearch"]);

function EnvObservability() {
  const qc = useQueryClient();
  const { data: configs } = useQuery({
    queryKey: ["integrations", "config"],
    queryFn: () => api.integrations.listConfig(),
  });
  const connected = new Set((configs ?? []).map((c) => c.provider));

  const install = useMutation({
    mutationFn: (provider: string) => api.observability.install(provider),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["integrations", "config"] }),
  });

  return (
    <section className="space-y-3">
      <p className="text-xs text-muted max-w-[720px]">
        Everything below renders inside Daalu — your own observability stores
        are the data plane, reached over this environment&apos;s tunnel. Connect
        a store once and its console lights up; stores you don&apos;t run yet
        can be installed into the cluster with one click.
      </p>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {OBS_TILES.map((t) => {
          const isConnected = connected.has(t.provider);
          const installing =
            install.isPending && install.variables === t.provider;
          if (isConnected) {
            return (
              <Link
                key={t.provider}
                href={t.href}
                className="group rounded-2xl border border-line bg-bg-card p-4 transition hover:border-accent-cyan/50"
              >
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{t.label}</span>
                  <span className="ml-auto text-[11px] text-accent-emerald">
                    connected
                  </span>
                </div>
                <p className="mt-1 text-xs text-muted">{t.help}</p>
              </Link>
            );
          }
          return (
            <div
              key={t.provider}
              className="rounded-2xl border border-line bg-bg-card p-4"
            >
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{t.label}</span>
                <span className="ml-auto text-[11px] text-accent-amber">
                  not connected
                </span>
              </div>
              <p className="mt-1 text-xs text-muted">{t.help}</p>
              <div className="mt-3 flex items-center gap-3">
                {OBS_INSTALLABLE.has(t.provider) && (
                  <button
                    type="button"
                    disabled={install.isPending}
                    onClick={() => install.mutate(t.provider)}
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan px-3 text-xs text-bg-base disabled:opacity-50"
                  >
                    {installing && (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    )}
                    Install into cluster
                  </button>
                )}
                <Link
                  href="/managed-infra"
                  className="text-[11px] text-muted underline hover:text-fg"
                >
                  connect an existing one
                </Link>
              </div>
            </div>
          );
        })}
      </div>
      {install.isError && (
        <p className="text-xs text-[color:var(--critical)]">
          Install failed: {String(install.error)}
        </p>
      )}
      {install.data && (
        <p className="text-xs text-accent-emerald">
          Installed: {install.data.applied.length} objects applied
          {install.data.finalizing
            ? " — finishing setup in the background (the tile flips to connected once the store answers)."
            : "."}
        </p>
      )}
    </section>
  );
}
