"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  AlertOctagon,
  ArrowLeft,
  Bot,
  Check,
  CheckCircle2,
  CircleX,
  ExternalLink,
  Gauge,
  GitPullRequest,
  Sparkles,
  UserCheck,
} from "lucide-react";
import { api, type ChangeProposal, type ChangeProposalStatus } from "@/lib/api";
import { formatRelative } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { LoadState } from "@/components/ui/load-state";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";
import { ProposalDeviceLabel, proposalReason } from "@/components/proposals/proposal-tile";

const STATUS_STRIPE: Record<ChangeProposalStatus, string> = {
  pending: "var(--warning)",
  approved: "var(--info)",
  executed: "var(--accent)",
  rejected: "var(--text-muted)",
  failed: "var(--critical)",
  stale: "var(--text-muted)",
};

export default function ProposalDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const toast = useToast();
  const id = params.id;

  const q = useQuery({
    queryKey: ["change-proposal", id],
    queryFn: () => api.changeProposals.get(id),
    enabled: !!id,
    refetchInterval: 15_000,
  });
  const proposal = q.data;

  const approve = useMutation({
    mutationFn: () => api.changeProposals.approve(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["change-proposal", id] });
      qc.invalidateQueries({ queryKey: ["change-proposals"] });
      toast.success("Approved. The executor pushes it within 30 seconds.");
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const reject = useMutation({
    mutationFn: () => api.changeProposals.reject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["change-proposal", id] });
      qc.invalidateQueries({ queryKey: ["change-proposals"] });
      toast.success("Proposal rejected. Nothing will be pushed.");
    },
    onError: (e) => toast.error(errorText(e)),
  });

  if (!proposal) {
    return (
      <div className="max-w-2xl space-y-4">
        <Link
          href="/operations?tab=proposals"
          className="text-sm text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> Back to proposals
        </Link>
        <LoadState query={q} loadingText="Loading proposal…" empty emptyTitle="Proposal not found">
          {null}
        </LoadState>
      </div>
    );
  }

  const stripe = STATUS_STRIPE[proposal.status];
  const canDecide = proposal.status === "pending";
  const mutating = approve.isPending || reject.isPending;

  return (
    <div className="max-w-[1200px] space-y-4">
      <div>
        <button
          type="button"
          onClick={() => router.push("/operations?tab=proposals")}
          className="text-xs text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Back to proposals
        </button>
      </div>

      <Header proposal={proposal} stripe={stripe} />

      {canDecide && (
        <section className="surface p-4 flex items-center gap-3 flex-wrap">
          <span className="text-sm text-muted flex-1 min-w-[240px]">
            Approving queues this change for the executor service — it
            runs on the next tick (≤ 30s) and pushes the rendered config
            to the device. Re-render is freshly recomputed at execution
            time; if the SoT intent has drifted, the proposal flips to
            <span className="font-mono"> stale</span> instead of pushing.
          </span>
          <div className="ml-auto flex gap-2">
            <ConfirmButton
              label="Reject"
              confirmLabel="Reject proposal"
              consequence="Nothing is pushed; the proposal closes."
              icon={<CircleX className="h-3.5 w-3.5" aria-hidden />}
              onConfirm={() => reject.mutate()}
              pending={reject.isPending}
              disabled={mutating}
              tone="danger"
            />
            <Button
              variant="primary"
              onClick={() => approve.mutate()}
              pending={approve.isPending}
              disabled={mutating}
              icon={<CheckCircle2 className="h-4 w-4" aria-hidden />}
            >
              Approve &amp; push
            </Button>
          </div>
        </section>
      )}

      <EvidencePanel proposal={proposal} />

      <DiffPanel proposal={proposal} />

      <ExecutorResultPanel proposal={proposal} />

      <details className="surface p-4">
        <summary className="cursor-pointer text-[11px] uppercase tracking-wider text-muted">
          Raw rendered configs
        </summary>
        <div className="mt-3 grid grid-cols-1 lg:grid-cols-2 gap-3 text-xs font-mono">
          <div>
            <div className="text-[11px] uppercase tracking-wider text-muted mb-1">
              Observed (or previous intent)
            </div>
            <pre className="bg-bg-elevated/60 border border-line rounded-lg p-3 max-h-[280px] overflow-auto whitespace-pre-wrap break-words">
              {proposal.observed_config || "(none)"}
            </pre>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-wider text-muted mb-1">
              Intended (snapshot — what executor will compare against)
            </div>
            <pre className="bg-bg-elevated/60 border border-line rounded-lg p-3 max-h-[280px] overflow-auto whitespace-pre-wrap break-words">
              {proposal.intended_config || "(none)"}
            </pre>
          </div>
        </div>
      </details>

      <details className="surface p-4">
        <summary className="cursor-pointer text-[11px] uppercase tracking-wider text-muted">
          Raw evidence JSON
        </summary>
        <pre className="text-xs font-mono whitespace-pre-wrap break-words bg-bg-elevated/60 border border-line rounded-lg p-3 mt-3 max-h-[320px] overflow-auto">
          {JSON.stringify(proposal.evidence, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function Header({
  proposal,
  stripe,
}: {
  proposal: ChangeProposal;
  stripe: string;
}) {
  return (
    <section
      className="surface relative overflow-hidden p-5"
      style={{ boxShadow: `inset 6px 0 0 ${stripe}` }}
    >
      <div className="flex items-start gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1.5 flex-wrap">
            <span
              className="text-[11px] uppercase tracking-wider font-semibold"
              style={{ color: stripe }}
            >
              {proposal.status}
            </span>
            <span className="text-[11px] uppercase tracking-wider text-muted">
              {proposal.kind}
            </span>
            <span className="text-xs text-muted">
              · proposed {formatRelative(proposal.created_at)}
            </span>
            {proposal.approved_at && (
              <span className="text-xs text-muted">
                · decided {formatRelative(proposal.approved_at)}
              </span>
            )}
            {proposal.executed_at && (
              <span className="text-xs text-muted">
                · executed {formatRelative(proposal.executed_at)}
              </span>
            )}
            <span className="text-[11px] uppercase tracking-wider text-muted">
              · renderer {proposal.renderer_version}
            </span>
          </div>
          <h1 className="text-lg font-semibold leading-tight inline-flex items-center gap-2">
            <GitPullRequest className="h-4 w-4 text-muted" aria-hidden /> Change for{" "}
            <ProposalDeviceLabel proposal={proposal} />
          </h1>
          <p className="mt-1 text-sm text-muted">{proposalReason(proposal)}</p>
          <p className="mt-1 text-xs text-muted font-mono break-all">Device id {proposal.device_id}</p>
        </div>
      </div>
    </section>
  );
}

function EvidencePanel({ proposal }: { proposal: ChangeProposal }) {
  const ev = proposal.evidence || {};
  const triggeredBy = pickString(ev.triggered_by);
  const reasoning = pickString(ev.llm_reasoning);
  const model = pickString(ev.llm_model);
  const confidence =
    typeof ev.confidence === "number" ? (ev.confidence as number) : null;
  const eventIds = pickStringArray(ev.evidence_events);
  const alertIds = pickStringArray(ev.evidence_alerts);
  const metrics = Array.isArray(ev.evidence_metrics)
    ? (ev.evidence_metrics as Record<string, unknown>[])
    : [];
  const factsChanged = pickStringArray(ev.facts_changed);

  // If the row is entirely empty (no reasoning, no event refs, no
  // metrics), don't render an empty card — the raw-JSON expander below
  // is enough.
  const hasContent =
    !!reasoning ||
    !!triggeredBy ||
    eventIds.length > 0 ||
    alertIds.length > 0 ||
    metrics.length > 0 ||
    factsChanged.length > 0 ||
    confidence !== null;
  if (!hasContent) return null;

  return (
    <section className="surface p-4 space-y-3">
      <div className="text-[11px] uppercase tracking-wider text-muted inline-flex items-center gap-1.5">
        <Sparkles className="h-3.5 w-3.5 text-accent" /> Why this change
      </div>
      {triggeredBy && (
        <div className="flex items-center gap-2 text-xs">
          <TriggerBadge triggeredBy={triggeredBy} />
          {model && (
            <span className="text-xs text-muted inline-flex items-center gap-1">
              <Bot className="h-3 w-3" /> {model}
            </span>
          )}
          {confidence !== null && (
            <span className="text-xs text-muted inline-flex items-center gap-1">
              <Gauge className="h-3 w-3" /> confidence{" "}
              {Math.round(confidence * 100)}%
            </span>
          )}
        </div>
      )}
      {reasoning && (
        <p className="text-[13.5px] text-fg/85 whitespace-pre-line leading-relaxed">
          {reasoning}
        </p>
      )}
      {factsChanged.length > 0 && (
        <div className="text-xs">
          <span className="text-muted">Facts changed: </span>
          {factsChanged.map((f, i) => (
            <span
              key={i}
              className="inline-block font-mono text-xs mr-1 px-1.5 py-0.5 rounded border border-line text-fg/80"
            >
              {f}
            </span>
          ))}
        </div>
      )}
      {(eventIds.length > 0 || alertIds.length > 0) && (
        <div className="text-xs space-y-1.5">
          {eventIds.length > 0 && (
            <div>
              <span className="text-muted">Triggering events: </span>
              {eventIds.map((eid) => (
                <Link
                  key={eid}
                  href={`/operations?event=${eid}`}
                  className="inline-flex items-center gap-1 font-mono text-xs mr-2 text-[color:var(--accent)] hover:underline"
                  title={eid}
                >
                  {eid.slice(0, 8)} <ExternalLink className="h-3 w-3" />
                </Link>
              ))}
            </div>
          )}
          {alertIds.length > 0 && (
            <div>
              <span className="text-muted">Related alerts: </span>
              {alertIds.map((aid) => (
                <Link
                  key={aid}
                  href={`/alerts/${aid}`}
                  className="inline-flex items-center gap-1 font-mono text-xs mr-2 text-[color:var(--accent)] hover:underline"
                  title={aid}
                >
                  {aid.slice(0, 8)} <ExternalLink className="h-3 w-3" />
                </Link>
              ))}
            </div>
          )}
        </div>
      )}
      {metrics.length > 0 && (
        <div className="text-xs">
          <div className="text-muted mb-1">Supporting metrics:</div>
          <ul className="space-y-0.5">
            {metrics.map((m, i) => (
              <li key={i} className="font-mono text-xs text-fg/80">
                {formatMetric(m)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function TriggerBadge({ triggeredBy }: { triggeredBy: string }) {
  const lc = triggeredBy.toLowerCase();
  if (lc === "engine") {
    return (
      <span className="text-xs px-2 py-0.5 rounded-md border border-accent/60 bg-accent/10 text-[color:var(--text)] inline-flex items-center gap-1">
        <Bot className="h-3 w-3" /> AI engine
      </span>
    );
  }
  if (lc === "reconciler") {
    return (
      <span className="text-xs px-2 py-0.5 rounded-md border border-warning/60 bg-warning/10 text-[color:var(--text)] inline-flex items-center gap-1">
        <AlertOctagon className="h-3 w-3" /> Drift reconciler
      </span>
    );
  }
  if (lc === "user") {
    return (
      <span className="text-xs px-2 py-0.5 rounded-md border border-line text-muted inline-flex items-center gap-1">
        <UserCheck className="h-3 w-3" /> Human
      </span>
    );
  }
  return (
    <span className="text-xs px-2 py-0.5 rounded-md border border-line text-muted">
      {triggeredBy}
    </span>
  );
}

function DiffPanel({ proposal }: { proposal: ChangeProposal }) {
  return (
    <section className="surface p-4">
      <div className="text-[11px] uppercase tracking-wider text-muted mb-2">
        Unified diff
      </div>
      <pre className="text-xs font-mono whitespace-pre-wrap break-words bg-bg-elevated/60 border border-line rounded-lg p-3 max-h-[420px] overflow-auto leading-relaxed">
        {colorize(proposal.diff || "(no diff — proposal makes no changes)")}
      </pre>
    </section>
  );
}

/**
 * Light per-line coloring for a unified diff. We don't pull in a full
 * markdown/diff library because this is the only place we need it —
 * just walk the lines, paint added/removed/hunk-headers, and pass
 * everything else through.
 */
function colorize(diff: string) {
  return diff.split("\n").map((line, i) => {
    let cls = "";
    if (line.startsWith("+") && !line.startsWith("+++"))
      cls = "text-[color:var(--accent-emerald)]";
    else if (line.startsWith("-") && !line.startsWith("---"))
      cls = "text-[color:var(--critical)]";
    else if (line.startsWith("@@")) cls = "text-[color:var(--info)]";
    else if (line.startsWith("### ") || line.startsWith("+++") || line.startsWith("---"))
      cls = "text-muted font-semibold";
    return (
      <span key={i} className={cls}>
        {line}
        {"\n"}
      </span>
    );
  });
}

function ExecutorResultPanel({ proposal }: { proposal: ChangeProposal }) {
  const r = proposal.executor_result || {};
  if (!proposal.executed_at && Object.keys(r).length === 0) return null;
  const success = r.success === true;
  const stale = r.stale as Record<string, unknown> | undefined;
  return (
    <section className="surface p-4 space-y-2">
      <div className="text-[11px] uppercase tracking-wider text-muted">
        Executor result
      </div>
      {stale && (
        <div className="text-[12.5px] text-[color:var(--muted)]">
          Marked stale: <span className="italic">{pickString(stale.reason) || "(no reason recorded)"}</span>
        </div>
      )}
      {proposal.executed_at && (
        <div className="text-[12.5px] inline-flex items-center gap-2">
          {success ? (
            <Check className="h-4 w-4 text-[color:var(--accent)]" />
          ) : (
            <CircleX className="h-4 w-4 text-[color:var(--critical)]" />
          )}
          <span className={success ? "" : "text-[color:var(--critical)]"}>
            {success ? "Pushed successfully" : pickString(r.error) || "Push failed"}
          </span>
          {r.rollback_performed === true && (
            <span className="text-xs px-1.5 py-0.5 rounded-md border border-warning/60 text-[color:var(--warning)]">
              rollback performed
            </span>
          )}
        </div>
      )}
      <pre className="text-xs font-mono whitespace-pre-wrap break-words bg-bg-elevated/60 border border-line rounded-lg p-3 max-h-[260px] overflow-auto">
        {JSON.stringify(r, null, 2)}
      </pre>
    </section>
  );
}

// ── tiny helpers ──────────────────────────────────────────────────────

function pickString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}
function pickStringArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.length > 0);
}
function formatMetric(m: Record<string, unknown>): string {
  const name = pickString(m.name);
  const value = m.value !== undefined ? String(m.value) : null;
  const ts = pickString(m.ts);
  const source = pickString(m.source);
  const parts: string[] = [];
  if (name) parts.push(name);
  if (value !== null) parts.push(`= ${value}`);
  if (ts) parts.push(`@ ${ts}`);
  if (source) parts.push(`(${source})`);
  return parts.length > 0 ? parts.join(" ") : JSON.stringify(m);
}
