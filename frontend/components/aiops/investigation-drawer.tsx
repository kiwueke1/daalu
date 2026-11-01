"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ExternalLink, X, Zap } from "lucide-react";
import { api } from "@/lib/api";
import { severityColor } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";
import { Chip, fmtDateTime, fmtDuration } from "./ui";
import { ModeChip } from "./autonomy-parts";
import { RESOLUTION_LABEL, RESOLUTION_TONE } from "./investigation-panel";
import { VerdictButtons } from "./verdict";

/** Right-side drawer with one investigation: root cause, actions with their
 *  policy decision and verification, and the verdict buttons. */
export function InvestigationDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useQuery({
    queryKey: ["aiops-investigation", id],
    queryFn: () => api.aiops.investigations.get(id),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const inv = q.data;
  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-label="Investigation"
        className="fixed inset-y-0 right-0 z-50 w-full max-w-[640px] bg-bg-base border-l border-line overflow-y-auto p-5 space-y-4 animate-fadeIn"
      >
        <div className="flex items-start gap-2">
          <div className="flex-1 min-w-0">
            <div className="text-[10px] uppercase tracking-wider text-muted">Investigation</div>
            <h2 className="text-lg font-semibold leading-tight mt-0.5">
              {inv?.alert_title ?? "Loading…"}
            </h2>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="h-8 w-8 rounded-lg border border-line inline-flex items-center justify-center">
            <X className="h-4 w-4" />
          </button>
        </div>
        <LoadState query={q} loadingText="Loading investigation…">
          {inv && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="uppercase tracking-wider font-semibold text-[10px]" style={{ color: severityColor[inv.alert_severity] }}>
                  {inv.alert_severity}
                </span>
                <Chip tone={RESOLUTION_TONE[inv.resolution] ?? "muted"}>
                  {RESOLUTION_LABEL[inv.resolution] ?? inv.resolution}
                </Chip>
                {inv.verified === true && <Chip tone="accent">fix verified</Chip>}
                {inv.verified === false && <Chip tone="critical">fix did not verify</Chip>}
                <Link href={`/alerts/${inv.alert_id}`} className="ml-auto inline-flex items-center gap-1 text-muted hover:text-fg">
                  Open alert <ExternalLink className="h-3 w-3" />
                </Link>
              </div>
              <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                <div>
                  <dt className="text-muted">Alert fired</dt>
                  <dd>{fmtDateTime(inv.alert_created_at)}</dd>
                </div>
                <div>
                  <dt className="text-muted">Time to diagnosis</dt>
                  <dd className="tabular-nums">{fmtDuration(inv.time_to_diagnosis_s)}</dd>
                </div>
                <div>
                  <dt className="text-muted">Time to resolve</dt>
                  <dd className="tabular-nums">{fmtDuration(inv.time_to_resolve_s)}</dd>
                </div>
                <div>
                  <dt className="text-muted">Model</dt>
                  <dd className="truncate">{inv.model || "—"}</dd>
                </div>
              </dl>

              <section className="surface p-4">
                <div className="text-[10px] uppercase tracking-wider text-muted mb-2">Root cause</div>
                {inv.root_cause ? (
                  <article className="prose prose-sm dark:prose-invert max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{inv.root_cause}</ReactMarkdown>
                  </article>
                ) : (
                  <div className="text-xs text-muted">The AI hasn&apos;t named a root cause yet.</div>
                )}
              </section>

              {inv.diagnosed_at && (
                <section className="surface p-4">
                  <VerdictButtons investigation={inv} />
                </section>
              )}

              <section className="space-y-2">
                <div className="text-[10px] uppercase tracking-wider text-muted">
                  Actions ({inv.actions.length})
                </div>
                {inv.actions.length === 0 && (
                  <div className="text-xs text-muted">No write actions were proposed.</div>
                )}
                {inv.actions.map((a) => (
                  <div key={a.id} className="rounded-lg border border-line bg-bg-card px-3 py-2 text-xs space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono font-semibold text-[13px]">{a.tool}</span>
                      <span className="text-muted font-mono truncate max-w-[18rem]">
                        {Object.entries(a.input || {})
                          .filter(([k]) => k !== "reason")
                          .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
                          .join(" ")}
                      </span>
                      <span className="ml-auto uppercase tracking-wider text-[10px] text-muted">{a.status}</span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      {a.auto_executed ? (
                        <Chip tone="accent">
                          <Zap className="h-3 w-3" /> auto-executed
                        </Chip>
                      ) : (
                        a.policy_mode && <ModeChip mode={a.policy_mode} />
                      )}
                      <span className="text-muted">
                        rule <span className="font-mono">{a.policy_rule || "defaults"}</span>
                      </span>
                      {a.approvals.length > 0 && (
                        <span className="text-muted">
                          approved by {a.approvals.map((x) => x.email || x.user_id).join(", ")}
                        </span>
                      )}
                      {a.actor && a.actor !== "agent" && <Chip tone="info">{a.actor}</Chip>}
                      {a.verified_resolved === true && <Chip tone="accent">verified</Chip>}
                      {a.verified_resolved === false && <Chip tone="critical">verification failed</Chip>}
                    </div>
                  </div>
                ))}
              </section>
            </>
          )}
        </LoadState>
      </aside>
    </>
  );
}
