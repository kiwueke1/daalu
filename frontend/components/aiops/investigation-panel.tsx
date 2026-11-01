"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ClipboardCheck, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { Chip, fmtDuration, isStatus, type Tone } from "./ui";
import { VerdictButtons } from "./verdict";

export const RESOLUTION_TONE: Record<string, Tone> = {
  open: "muted",
  auto: "accent",
  approved: "accent",
  manual: "warning",
  unresolved: "critical",
};

export const RESOLUTION_LABEL: Record<string, string> = {
  open: "Open",
  auto: "Auto-resolved",
  approved: "Resolved (approved fix)",
  manual: "Resolved manually",
  unresolved: "Unresolved",
};

/**
 * The learning-loop record for one alert: time to diagnosis, outcome, and
 * the verdict buttons. 404 = no investigation yet (the AI hasn't started).
 */
export function AlertInvestigationPanel({ alertId }: { alertId: string }) {
  const q = useQuery({
    queryKey: ["aiops-investigation-by-alert", alertId],
    queryFn: () => api.aiops.investigations.byAlert(alertId),
    retry: (n, err) => !isStatus(err, 404) && n < 2,
    refetchInterval: 30_000,
  });

  if (q.isLoading) {
    return (
      <section className="surface p-4 text-[12px] text-muted flex items-center gap-2">
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading investigation record…
      </section>
    );
  }
  if (isStatus(q.error, 404)) {
    return (
      <section className="surface p-4 text-[12px] text-muted">
        No investigation record yet — one is created when the AI starts triaging
        this alert.
      </section>
    );
  }
  // Anything but an investigation object (an empty body, a proxy's error
  // page parsed as JSON) is "couldn't load", never a crash of the alert page.
  if (q.isError || !q.data || Array.isArray(q.data) || typeof q.data !== "object") {
    return (
      <section className="surface p-4 text-[12px] text-muted">
        Couldn&apos;t load the investigation record.
      </section>
    );
  }

  const inv = q.data;
  const diagnosed = !!inv.diagnosed_at;
  return (
    <section className="surface p-5 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <ClipboardCheck className="h-4 w-4 text-accent" />
        <div className="text-[10px] uppercase tracking-wider text-muted">Investigation record</div>
        <Chip tone={RESOLUTION_TONE[inv.resolution] ?? "muted"}>
          {RESOLUTION_LABEL[inv.resolution] ?? inv.resolution}
        </Chip>
        {inv.verified === true && <Chip tone="accent">Fix verified</Chip>}
        {inv.verified === false && <Chip tone="critical">Fix did not verify</Chip>}
        <Link
          href={`/investigations?open=${inv.id}`}
          className="ml-auto text-[11px] text-muted hover:text-fg underline underline-offset-2"
        >
          All investigations
        </Link>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-[12px]">
        <span>
          <span className="text-muted">Time to diagnosis </span>
          <span className="font-semibold tabular-nums">
            {diagnosed ? fmtDuration(inv.time_to_diagnosis_s) : "not diagnosed yet"}
          </span>
        </span>
        {inv.time_to_resolve_s !== null && (
          <span>
            <span className="text-muted">Time to resolve </span>
            <span className="font-semibold tabular-nums">{fmtDuration(inv.time_to_resolve_s)}</span>
          </span>
        )}
        <span className="text-muted">
          Actions {inv.actions_proposed} proposed · {inv.actions_approved} approved ·{" "}
          {inv.actions_rejected} rejected · {inv.actions_auto} auto
        </span>
        {(inv.runbook_ids?.length ?? 0) > 0 && (
          <span className="text-muted">
            {inv.runbook_ids.length} runbook{inv.runbook_ids.length === 1 ? "" : "s"} used
          </span>
        )}
      </div>
      {diagnosed ? (
        <VerdictButtons investigation={inv} />
      ) : (
        <div className="text-[12px] text-muted">
          You can grade the root cause once the AI has named one.
        </div>
      )}
    </section>
  );
}
