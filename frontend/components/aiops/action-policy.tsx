"use client";

import { Bot, CheckCircle2, ShieldCheck, Users, XCircle, Zap } from "lucide-react";
import type { AlertAction } from "@/lib/api";
import { Chip } from "./ui";

/**
 * What the autonomy policy decided for one write action, how many approvals
 * it has, what it will touch (preview) and whether it fixed the alert
 * (verification). Rendered inside the approve card and the "ran" list.
 * Shows nothing for an action from a backend without policy fields.
 */
export function ActionPolicyInfo({
  action,
  compact = false,
}: {
  action: AlertAction;
  compact?: boolean;
}) {
  const mode = action.policy_mode || "";
  const required = action.approvals_required ?? 1;
  const approvals = action.approvals ?? [];
  const preview = action.preview ?? {};
  const pods = preview.blast_radius?.pods;
  const deployments = preview.blast_radius?.deployments;
  const external = (action.actor ?? "").startsWith("mcp:");
  const hasAnything =
    mode || preview.summary || approvals.length > 0 || required > 1 || action.auto_executed ||
    (action.verified_resolved !== undefined && action.verified_resolved !== null) || external;
  if (!hasAnything) return null;

  return (
    <div className="mt-2 space-y-1.5 text-[11px]">
      <div className="flex flex-wrap items-center gap-1.5">
        {action.auto_executed ? (
          <Chip tone="accent" title={action.policy_reason}>
            <Zap className="h-3 w-3" /> Auto-executed by policy
            {action.policy_rule ? ` ${action.policy_rule}` : ""}
          </Chip>
        ) : (
          mode && (
            <Chip tone={mode === "auto" ? "accent" : mode === "deny" ? "critical" : "warning"}>
              <ShieldCheck className="h-3 w-3" /> {mode}
            </Chip>
          )
        )}
        {!action.auto_executed && mode && (
          <span className="text-muted">
            {action.policy_rule ? (
              <>
                rule <span className="font-mono text-fg/80">{action.policy_rule}</span>
              </>
            ) : (
              "policy defaults"
            )}
          </span>
        )}
        {(required > 1 || approvals.length > 0) && (
          <Chip tone={approvals.length >= required ? "accent" : "muted"}>
            <Users className="h-3 w-3" /> {approvals.length} / {required} approvals
          </Chip>
        )}
        {external && (
          <Chip tone="info" title="Proposed by an external agent over MCP">
            <Bot className="h-3 w-3" /> {action.actor}
          </Chip>
        )}
        {action.verified_resolved === true && (
          <Chip tone="accent">
            <CheckCircle2 className="h-3 w-3" /> Verified: workload recovered
          </Chip>
        )}
        {action.verified_resolved === false && (
          <Chip tone="critical">
            <XCircle className="h-3 w-3" /> Verification failed
          </Chip>
        )}
      </div>
      {!compact && action.policy_reason && (
        <div className="text-muted">Why: {action.policy_reason}</div>
      )}
      {approvals.length > 0 && (
        <div className="text-muted">
          Approved by {approvals.map((a) => a.email || a.user_id).join(", ")}
          {approvals.length < required &&
            ` — waiting for ${required - approvals.length} more`}
        </div>
      )}
      {!compact && (preview.summary || pods !== undefined || deployments !== undefined) && (
        <div
          className="rounded-md px-2.5 py-1.5"
          style={{
            background: "color-mix(in srgb, var(--info) 8%, var(--bg-base))",
            border: "1px solid color-mix(in srgb, var(--info) 28%, var(--line))",
          }}
        >
          <span className="font-semibold" style={{ color: "var(--info)" }}>
            Preview:
          </span>{" "}
          {preview.summary || "No preview available."}
          {(pods !== undefined || deployments !== undefined) && (
            <span className="text-muted">
              {" "}
              · blast radius{" "}
              {[
                pods !== undefined ? `${pods} pod${pods === 1 ? "" : "s"}` : null,
                deployments !== undefined
                  ? `${deployments} deployment${deployments === 1 ? "" : "s"}`
                  : null,
              ]
                .filter(Boolean)
                .join(", ")}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
