"use client";

import Link from "next/link";
import { ChevronRight, GitPullRequest, Sparkles, AlertOctagon, UserCheck } from "lucide-react";
import type { ChangeProposal, ChangeProposalKind, ChangeProposalStatus } from "@/lib/api";
import { cn, formatRelative } from "@/lib/utils";
import { buttonClass } from "@/components/ui/button";

/**
 * Compact list-row for a ChangeProposal. Modeled on AlertTile so the
 * Proposals page reads at the same visual weight as Alerts — these
 * surfaces are operator-approval cousins.
 *
 * The stripe colour encodes *status*, not severity, because severity
 * doesn't really apply to a proposal — the urgency is "needs human"
 * vs. "in motion" vs. "done." The icon encodes *kind*: drift vs.
 * manual vs. intended_change vs. (anything else).
 */

const STATUS_STRIPE: Record<ChangeProposalStatus, string> = {
  pending: "var(--warning)",   // operator action required
  approved: "var(--info)",     // in motion (executor will pick up)
  executed: "var(--accent)",   // success
  rejected: "var(--text-muted)",
  failed: "var(--critical)",
  stale: "var(--text-muted)",
};

const STATUS_LABEL: Record<ChangeProposalStatus, string> = {
  pending: "Needs approval",
  approved: "Approved",
  executed: "Applied",
  rejected: "Rejected",
  failed: "Failed",
  stale: "Stale",
};

const KIND_ICON: Record<ChangeProposalKind, typeof Sparkles> = {
  drift: AlertOctagon,
  manual: UserCheck,
  intended_change: GitPullRequest,
};

const KIND_LABEL: Record<ChangeProposalKind, string> = {
  drift: "drift",
  manual: "manual",
  intended_change: "intent",
};

const str = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : null);

/** A device's name when the evidence carries one, else null. */
export function proposalDeviceName(p: ChangeProposal): string | null {
  const e = p.evidence ?? {};
  return str(e.device_name) ?? str(e.hostname) ?? str(e.device);
}

/**
 * The device as a title: its name, or "Unnamed device" with the short id in
 * muted mono — never a bare hash as the title.
 */
export function ProposalDeviceLabel({ proposal }: { proposal: ChangeProposal }) {
  const name = proposalDeviceName(proposal);
  if (name) return <>{name}</>;
  const id = proposal.device_id;
  return (
    <>
      Unnamed device{" "}
      <span className="font-mono text-xs font-normal text-muted" title={id}>
        {id.length > 12 ? id.slice(0, 8) : id}
      </span>
    </>
  );
}

/** One line saying why this proposal exists, never a raw diff header. */
export function proposalReason(p: ChangeProposal): string {
  const reasoning = str(p.evidence?.llm_reasoning) ?? str(p.evidence?.reason);
  if (reasoning) {
    const first = reasoning.split(/(?<=[.!?])\s|\n/)[0].trim();
    return first.length > 160 ? first.slice(0, 157).trimEnd() + "…" : first;
  }
  const changed = (p.diff || "")
    .split("\n")
    .filter((l) => /^[+-]/.test(l) && !/^(\+\+\+|---)/.test(l)).length;
  const lines = changed ? ` (${changed} line${changed === 1 ? "" : "s"} differ)` : "";
  if (p.kind === "drift") return `The running config drifted from the intended config${lines}.`;
  if (p.kind === "intended_change") return `The intended config changed and needs pushing to the device${lines}.`;
  return `A manual config change was proposed${lines}.`;
}

interface ProposalTileProps {
  proposal: ChangeProposal;
}

export function ProposalTile({ proposal }: ProposalTileProps) {
  const stripe = STATUS_STRIPE[proposal.status] ?? "var(--text-muted)";
  const KindIcon = KIND_ICON[proposal.kind] ?? Sparkles;
  const kindLabel = KIND_LABEL[proposal.kind] ?? proposal.kind;
  const triggeredBy = str(proposal.evidence?.triggered_by);
  const pending = proposal.status === "pending";

  return (
    <article
      className="motion-enter relative rounded-2xl overflow-hidden transition-colors hover:border-[color:var(--line-strong)]"
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--line)",
        boxShadow: `inset 4px 0 0 ${stripe}`,
      }}
    >
      <div className="relative pl-6 pr-4 py-3.5">
        <div className="flex flex-wrap items-center gap-2 mb-1.5">
          <span
            className={cn(
              "text-[11px] uppercase tracking-wider font-semibold rounded-full px-2 py-0.5",
              pending && "motion-attention"
            )}
            style={{
              color: stripe,
              background: `color-mix(in srgb, ${stripe} 12%, transparent)`,
            }}
          >
            {STATUS_LABEL[proposal.status] ?? proposal.status}
          </span>
          <span className="text-[11px] uppercase tracking-wider text-muted inline-flex items-center gap-1">
            <KindIcon className="h-3 w-3" aria-hidden /> {kindLabel}
          </span>
          {triggeredBy && triggeredBy !== "user" && (
            <span
              className="text-[11px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded-md border border-line text-muted"
              title="Who created this proposal"
            >
              by {triggeredBy}
            </span>
          )}
          <span className="text-xs text-muted ml-auto">
            {formatRelative(proposal.created_at)}
          </span>
        </div>

        <h3 className="font-medium text-sm leading-snug text-[color:var(--text)] line-clamp-1">
          <Link
            href={`/proposals/${proposal.id}`}
            className="hover:underline after:absolute after:inset-0"
          >
            <ProposalDeviceLabel proposal={proposal} />
          </Link>
          {proposal.environment_slug && (
            <span className="ml-2 text-xs font-normal text-muted">{proposal.environment_slug}</span>
          )}
        </h3>

        <p className="text-sm text-muted mt-1 line-clamp-2">{proposalReason(proposal)}</p>

        <div className="relative z-10 flex items-center gap-2 mt-3">
          {proposal.status === "approved" && (
            <span className="text-xs text-muted">Queued for the executor</span>
          )}
          {proposal.status === "stale" && (
            <span className="text-xs text-muted">
              The intended config changed after approval; re-author or re-approve it.
            </span>
          )}
          {pending ? (
            <Link href={`/proposals/${proposal.id}`} className={cn(buttonClass("primary", "sm"), "ml-auto")}>
              Review &amp; approve <ChevronRight className="h-3.5 w-3.5" aria-hidden />
            </Link>
          ) : (
            <Link
              href={`/proposals/${proposal.id}`}
              className="ml-auto text-xs text-muted inline-flex items-center gap-1 hover:text-[color:var(--text)] transition-colors"
            >
              Open <ChevronRight className="h-3.5 w-3.5" aria-hidden />
            </Link>
          )}
        </div>
      </div>
    </article>
  );
}
