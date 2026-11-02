"use client";

/**
 * Cluster tunnel (VPN) onboarding workflow — reusable component.
 *
 * Used in two contexts:
 *
 *   • Standalone: `/onboarding/cluster` page renders this full-bleed.
 *   • Embedded:   the general `/onboarding` wizard renders this as
 *                 its cluster step, so a user running the full wizard
 *                 doesn't get bounced to a separate route.
 *
 * Either way the same multi-step flow runs:
 *
 *   1. Intro      — explain what this does, lay out prerequisites.
 *   2. Details    — collect slug + name, POST /clusters, surface the
 *                   one-shot install snippet.
 *   3. Wait       — poll GET /clusters/{slug} until the customer-side
 *                   edge has bootstrapped (status flips from
 *                   `pending` → `awaiting_handshake` → `connected`).
 *   4. Done       — show the connected state, optional "create
 *                   another" button or fire the parent's onComplete.
 *
 * The workflow owns its own step state. The parent passes an
 * `onComplete` callback so the embedded variant can advance the outer
 * wizard once this sub-flow finishes.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Boxes,
  Check,
  ChevronRight,
  Circle,
  Copy,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import { api, Cluster, ClusterCreate } from "@/lib/api";

type WorkflowStep = "intro" | "details" | "wait" | "done";

const STATUS_LABEL: Record<string, string> = {
  pending: "Waiting for customer install",
  awaiting_handshake: "Peer registered, awaiting handshake",
  connected: "Connected",
  degraded: "Connected (degraded)",
  error: "Connection lost",
};

const STATUS_DOT: Record<string, string> = {
  pending: "bg-muted/60",
  awaiting_handshake: "bg-accent-amber",
  connected: "bg-accent-emerald",
  degraded: "bg-accent-amber",
  error: "bg-red-500",
};

export interface ClusterWorkflowProps {
  /**
   * Fired after the user clicks "Finish" on the done screen. The
   * standalone page hooks this to navigate back to /clusters; the
   * embedded variant uses it to advance the parent wizard.
   */
  onComplete?: () => void;
  /**
   * When true (the standalone page), surface a small "create another"
   * affordance on the done screen. The embedded variant hides this —
   * the parent wizard owns flow control.
   */
  allowAnother?: boolean;
}

export function ClusterWorkflow({
  onComplete,
  allowAnother = true,
}: ClusterWorkflowProps) {
  const [step, setStep] = useState<WorkflowStep>("intro");
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [created, setCreated] = useState<ClusterCreate | null>(null);

  const onboard = useMutation({
    mutationFn: (input: { slug: string; name: string }) =>
      api.clusters.onboard(input),
    onSuccess: (res) => {
      setCreated(res);
      setStep("wait");
    },
  });

  function reset() {
    setSlug("");
    setName("");
    setCreated(null);
    setStep("intro");
  }

  return (
    <div className="space-y-4">
      <Header step={step} />

      {step === "intro" && (
        <IntroPanel onStart={() => setStep("details")} />
      )}

      {step === "details" && (
        <DetailsPanel
          slug={slug}
          name={name}
          onSlug={setSlug}
          onName={setName}
          submitting={onboard.isPending}
          error={onboard.error ? (onboard.error as Error).message : null}
          onBack={() => setStep("intro")}
          onSubmit={() =>
            onboard.mutate({ slug: slug.trim(), name: name.trim() })
          }
        />
      )}

      {step === "wait" && created && (
        <WaitPanel
          created={created}
          onConnected={() => setStep("done")}
          onSkip={() => setStep("done")}
        />
      )}

      {step === "done" && created && (
        <DonePanel
          created={created}
          allowAnother={allowAnother}
          onAnother={reset}
          onFinish={onComplete}
        />
      )}
    </div>
  );
}

// ── Header — small progress trail ───────────────────────────────────────

function Header({ step }: { step: WorkflowStep }) {
  // Same thin bar + "Step x of y" label as the other onboarding pages.
  const order: WorkflowStep[] = ["intro", "details", "wait", "done"];
  const idx = order.indexOf(step);
  const pct = Math.round(((idx + 1) / order.length) * 100);
  return (
    <div>
      <div
        className="h-1.5 rounded-full bg-bg-elevated overflow-hidden"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <div
          className="h-full rounded-full bg-accent transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
        Step {idx + 1} of {order.length}: {labelFor(step)}
      </div>
    </div>
  );
}

function labelFor(s: WorkflowStep): string {
  switch (s) {
    case "intro":
      return "Overview";
    case "details":
      return "Cluster details";
    case "wait":
      return "Customer install";
    case "done":
      return "Done";
  }
}

// ── Panels ──────────────────────────────────────────────────────────────

function IntroPanel({ onStart }: { onStart: () => void }) {
  return (
    <div className="rounded-2xl border border-line bg-bg-card p-5 space-y-4">
      <div className="flex items-start gap-3">
        <div
          className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0"
          style={{
            background: "color-mix(in srgb, var(--accent) 14%, transparent)",
          }}
        >
          <Boxes className="h-4 w-4 text-accent-cyan" />
        </div>
        <div>
          <div className="text-base font-medium">Cluster tunnel (VPN)</div>
          <p className="text-sm text-muted mt-0.5">
            Establish a WireGuard tunnel from a customer Kubernetes cluster back
            to the operator. The customer's cluster doesn't need to be
            publicly routable — it dials out to the operator's hub.
          </p>
        </div>
      </div>

      <div className="rounded-lg border border-line bg-bg-elevated/30 p-3 space-y-2">
        <div className="text-[11px] uppercase tracking-wider text-muted">
          Before you start
        </div>
        <ul className="text-sm space-y-1.5">
          <li className="flex items-start gap-2">
            <Circle className="h-2 w-2 mt-1.5 text-muted fill-current shrink-0" />
            The customer has Helm installed and a kubeconfig for their cluster.
          </li>
          <li className="flex items-start gap-2">
            <Circle className="h-2 w-2 mt-1.5 text-muted fill-current shrink-0" />
            Outbound UDP/51820 from the customer cluster to the operator's
            WireGuard hub is allowed.
          </li>
          <li className="flex items-start gap-2">
            <Circle className="h-2 w-2 mt-1.5 text-muted fill-current shrink-0" />
            You can share a one-line install snippet with them over a secure
            channel (Slack DM, password manager, etc.).
          </li>
        </ul>
      </div>

      <div className="flex justify-end">
        <button
          onClick={onStart}
          className="text-xs h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan flex items-center gap-1.5"
        >
          Enter cluster details <ChevronRight className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

function DetailsPanel({
  slug,
  name,
  onSlug,
  onName,
  submitting,
  error,
  onBack,
  onSubmit,
}: {
  slug: string;
  name: string;
  onSlug: (v: string) => void;
  onName: (v: string) => void;
  submitting: boolean;
  error: string | null;
  onBack: () => void;
  onSubmit: () => void;
}) {
  const valid = slug.trim().length > 0 && name.trim().length > 0;
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) onSubmit();
      }}
      className="rounded-2xl border border-line bg-bg-card p-5 space-y-4"
    >
      <div className="text-base font-medium">Cluster details</div>
      <p className="text-sm text-muted">
        Pick a stable identifier for this cluster. The slug shows up in the
        install snippet and in <code>/clusters</code> — keep it short and
        DNS-friendly.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="text-xs space-y-1">
          <div className="text-muted uppercase tracking-wider">Slug</div>
          <input
            value={slug}
            onChange={(e) => onSlug(e.target.value)}
            required
            placeholder="acme-prod"
            pattern="[a-z0-9]([a-z0-9\-]*[a-z0-9])?"
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          />
          <div className="text-[10px] text-muted">
            Lowercase letters, digits, hyphens. No leading/trailing dash.
          </div>
        </label>
        <label className="text-xs space-y-1">
          <div className="text-muted uppercase tracking-wider">Display name</div>
          <input
            value={name}
            onChange={(e) => onName(e.target.value)}
            required
            placeholder="ACME production cluster"
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          />
        </label>
      </div>

      {error && (
        <div className="text-[11px] text-red-500 flex items-start gap-1.5">
          <X className="h-3 w-3 mt-0.5 shrink-0" /> {error}
        </div>
      )}

      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onBack}
          className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-fg"
        >
          Back
        </button>
        <button
          type="submit"
          disabled={!valid || submitting}
          className="text-xs h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan disabled:opacity-50 flex items-center gap-1.5"
        >
          {submitting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          Create cluster <ChevronRight className="h-3.5 w-3.5" />
        </button>
      </div>
    </form>
  );
}

function WaitPanel({
  created,
  onConnected,
  onSkip,
}: {
  created: ClusterCreate;
  onConnected: () => void;
  onSkip: () => void;
}) {
  // Poll the cluster row every 5s while we wait for the customer-side
  // edge to call back. Auto-advance as soon as we observe a real
  // handshake (status === "connected"). Operators in a hurry can also
  // click "Skip waiting" — the standalone /clusters page will still
  // reflect the live state, so this isn't lossy.
  const { data } = useQuery({
    queryKey: ["clusters", created.slug],
    queryFn: () => api.clusters.get(created.slug),
    refetchInterval: 5_000,
    // Avoid the React-Query-shaped retry log spam when the row is
    // briefly unavailable mid-deploy.
    retry: 1,
  });
  const status = data?.status ?? created.status;
  const handshake = data?.last_handshake_at ?? null;
  const seenConnected = useRef(false);
  useEffect(() => {
    if (!seenConnected.current && status === "connected") {
      seenConnected.current = true;
      // Tiny delay so the user sees the green badge flicker on.
      setTimeout(onConnected, 900);
    }
  }, [status, onConnected]);

  return (
    <div className="rounded-2xl border border-line bg-bg-card p-5 space-y-4">
      <div className="text-base font-medium">Send the install snippet</div>
      <p className="text-sm text-muted">
        Copy the snippet below and give it to the customer. The bootstrap token
        is one-shot — once their edge container calls back, the token is
        invalidated and won't be shown again.
      </p>

      <Snippet snippet={created.install_snippet} />

      <div className="rounded-lg border border-line bg-bg-elevated/30 p-3 flex items-center gap-3">
        <span className={`h-2 w-2 rounded-full ${STATUS_DOT[status] ?? "bg-muted"}`} />
        <div className="flex-1">
          <div className="text-sm">{STATUS_LABEL[status] ?? status}</div>
          <div className="text-[11px] text-muted">
            Tunnel IP <code className="font-mono">{created.tunnel_ip}</code>
            {handshake && ` · last handshake ${timeAgo(handshake)}`}
          </div>
        </div>
        {status !== "connected" && (
          <Loader2 className="h-4 w-4 animate-spin text-muted" />
        )}
      </div>

      <div className="flex items-center justify-between">
        <div className="text-[11px] text-muted">
          Polling every 5s. We'll advance as soon as the tunnel is connected.
        </div>
        <button
          type="button"
          onClick={onSkip}
          className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-fg flex items-center gap-1.5"
        >
          Skip waiting
        </button>
      </div>
    </div>
  );
}

function DonePanel({
  created,
  allowAnother,
  onAnother,
  onFinish,
}: {
  created: ClusterCreate;
  allowAnother: boolean;
  onAnother: () => void;
  onFinish?: () => void;
}) {
  const { data } = useQuery({
    queryKey: ["clusters", created.slug, "done"],
    queryFn: () => api.clusters.get(created.slug),
    // After hitting the done screen the row can still drift; do a
    // single fetch to capture the latest snapshot but don't poll.
    refetchInterval: false,
  });
  const live: Cluster | undefined = data;
  const status = live?.status ?? created.status;
  return (
    <div className="rounded-2xl border border-accent-emerald/40 bg-accent-emerald/5 p-5 space-y-4">
      <div className="text-base font-medium flex items-center gap-2 text-accent-emerald">
        <Check className="h-4 w-4" /> Cluster <code>{created.slug}</code> set up
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
        <div className="rounded-lg border border-line bg-bg-card p-3">
          <div className="text-[11px] uppercase tracking-wider text-muted">Status</div>
          <div className="mt-1 flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${STATUS_DOT[status] ?? "bg-muted"}`} />
            {STATUS_LABEL[status] ?? status}
          </div>
        </div>
        <div className="rounded-lg border border-line bg-bg-card p-3">
          <div className="text-[11px] uppercase tracking-wider text-muted">Tunnel IP</div>
          <div className="mt-1 font-mono text-xs">{created.tunnel_ip}</div>
        </div>
      </div>
      <div className="flex items-center justify-between">
        {allowAnother ? (
          <button
            onClick={onAnother}
            className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-fg flex items-center gap-1.5"
          >
            <RefreshCw className="h-3.5 w-3.5" /> Set up another
          </button>
        ) : (
          <div />
        )}
        {onFinish && (
          <button
            onClick={onFinish}
            className="text-xs h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan flex items-center gap-1.5"
          >
            Finish <ChevronRight className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}

function Snippet({ snippet }: { snippet: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      <pre className="text-xs bg-bg-elevated/60 border border-line rounded-lg p-3 overflow-x-auto whitespace-pre">
        {snippet}
      </pre>
      <button
        onClick={() => {
          navigator.clipboard.writeText(snippet);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
        className="absolute top-2 right-2 text-[11px] h-7 px-2 rounded-md bg-bg-card border border-line hover:bg-bg-elevated/60 flex items-center gap-1"
      >
        <Copy className="h-3 w-3" /> {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function timeAgo(iso: string): string {
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return `${Math.floor(sec)}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
