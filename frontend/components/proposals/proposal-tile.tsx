"use client";

import Link from "next/link";
import { ChevronRight, GitPullRequest, Sparkles, AlertOctagon, UserCheck } from "lucide-react";
import type { ChangeProposal, ChangeProposalKind, ChangeProposalStatus } from "@/lib/api";
import { formatRelative } from "@/lib/utils";

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
  rejected: "var(--muted)",
  failed: "var(--critical)",
  stale: "var(--muted)",
};

const STATUS_GLOW: Record<ChangeProposalStatus, string> = {
  pending: "rgba(245,158,11,0.40)",
  approved: "rgba(var(--accent-rgb),0.30)",
  executed: "rgba(var(--accent-rgb),0.45)",
  rejected: "rgba(148,163,184,0.20)",
  failed: "rgba(239,68,68,0.45)",
  stale: "rgba(148,163,184,0.20)",
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

interface ProposalTileProps {
  proposal: ChangeProposal;
}

export function ProposalTile({ proposal }: ProposalTileProps) {
  const stripe = STATUS_STRIPE[proposal.status] ?? "var(--muted)";
  const glow = STATUS_GLOW[proposal.status] ?? "rgba(148,163,184,0.20)";
  const KindIcon = KIND_ICON[proposal.kind] ?? Sparkles;
  const kindLabel = KIND_LABEL[proposal.kind] ?? proposal.kind;

  // Try to surface the LLM's reasoning as the body preview when it's
  // there — it's the most operator-relevant single string on the
  // proposal. Fall back to the first chunk of the diff.
  const reasoning =
    typeof proposal.evidence?.llm_reasoning === "string"
      ? (proposal.evidence.llm_reasoning as string)
      : null;
  const triggeredBy =
    typeof proposal.evidence?.triggered_by === "string"
      ? (proposal.evidence.triggered_by as string)
      : null;
  const previewSource = reasoning || proposal.diff || proposal.intended_config || "(no preview)";
  const preview =
    previewSource.length > 240
      ? previewSource.slice(0, 240).trimEnd() + "…"
      : previewSource;

  return (
    <Link
      href={`/proposals/${proposal.id}`}
      className="proposal-tile group relative block w-full text-left rounded-2xl overflow-hidden"
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--line)",
        boxShadow: `
          inset 0 1px 0 rgba(255,255,255,0.06),
          inset 5px 0 0 ${stripe},
          inset 6px 0 12px -6px ${glow},
          0 1px 0 rgba(0,0,0,0.25),
          0 8px 24px -12px rgba(0,0,0,0.55),
          -2px 0 18px -8px ${glow}
        `,
        transition: "transform 180ms ease, box-shadow 180ms ease",
      }}
    >
      <div className="relative pl-6 pr-4 py-3.5">
        <div className="flex items-center gap-2 mb-1.5">
          <span
            className="text-[10px] uppercase tracking-wider font-semibold"
            style={{ color: stripe }}
          >
            {proposal.status}
          </span>
          <span className="text-[10px] uppercase tracking-wider text-muted inline-flex items-center gap-1">
            <KindIcon className="h-3 w-3" /> {kindLabel}
          </span>
          {triggeredBy && triggeredBy !== "user" && (
            <span
              className="text-[10px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded-md border border-line text-fg/80"
              title="Who created this proposal"
            >
              by {triggeredBy}
            </span>
          )}
          <span className="text-[10px] text-muted ml-auto">
            {formatRelative(proposal.created_at)}
          </span>
        </div>

        <h3 className="font-medium text-[13.5px] leading-snug text-[color:var(--text)] line-clamp-1">
          Device <span className="font-mono">{proposal.device_id}</span>
        </h3>

        <p className="text-[12.5px] text-fg/65 mt-1.5 line-clamp-3 whitespace-pre-line">
          {preview}
        </p>

        <div className="flex items-center gap-2 mt-3">
          {proposal.status === "pending" && (
            <span
              className="text-[11px] h-7 px-2.5 rounded-md border border-warning/60 text-[color:var(--warning)] flex items-center"
              title="This proposal is waiting for an operator decision"
            >
              Needs approval
            </span>
          )}
          {proposal.status === "approved" && (
            <span className="text-[11px] h-7 px-2.5 rounded-md border border-line text-muted flex items-center">
              Queued for executor
            </span>
          )}
          {proposal.status === "stale" && (
            <span
              className="text-[11px] h-7 px-2.5 rounded-md border border-line text-muted flex items-center"
              title="SoT intent changed between approval and execution; re-author or re-approve."
            >
              Stale — re-author needed
            </span>
          )}
          <span className="ml-auto text-[11px] text-muted flex items-center gap-1 group-hover:text-[color:var(--text)] transition-colors">
            Open <ChevronRight className="h-3.5 w-3.5" />
          </span>
        </div>
      </div>

      <style jsx>{`
        .proposal-tile:hover {
          transform: translateY(-1px);
          box-shadow:
            inset 0 1px 0 rgba(255, 255, 255, 0.08),
            inset 5px 0 0 ${stripe},
            inset 6px 0 16px -4px ${glow},
            0 1px 0 rgba(0, 0, 0, 0.25),
            0 14px 28px -14px rgba(0, 0, 0, 0.6),
            -2px 0 28px -6px ${glow} !important;
        }
      `}</style>
    </Link>
  );
}
