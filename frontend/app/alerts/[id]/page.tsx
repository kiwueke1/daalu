"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeft, Check, CheckCircle2, Siren } from "lucide-react";
import { api, type Alert, type Incident } from "@/lib/api";
import { AlertChatRail } from "@/components/alerts/alert-chat-rail";
import { AlertOccurrences } from "@/components/alerts/alert-occurrences";
import { InvestigationTabs } from "@/components/alerts/investigation-tabs";
import { PromoteIncidentDialog } from "@/components/alerts/promote-incident-dialog";
import { enrichAlertTitle } from "@/components/alerts/alert-title";
import { formatRelative } from "@/lib/utils";

const STRIPE_VAR: Record<Alert["severity"], string> = {
  critical: "var(--critical)",
  warning: "var(--warning)",
  info: "var(--info)",
};

export default function AlertDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const alertId = params.id;

  const { data: alert, isLoading, error } = useQuery({
    queryKey: ["alert", alertId],
    queryFn: () => api.alerts.get(alertId),
    enabled: !!alertId,
    refetchInterval: 30_000,
  });

  const ack = useMutation({
    mutationFn: () => api.alerts.acknowledge(alertId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alert", alertId] });
      qc.invalidateQueries({ queryKey: ["alerts"] });
    },
  });
  const resolve = useMutation({
    mutationFn: () => api.alerts.resolve(alertId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alert", alertId] });
      qc.invalidateQueries({ queryKey: ["alerts"] });
    },
  });

  const [promoteOpen, setPromoteOpen] = useState(false);
  const [promotedIncident, setPromotedIncident] = useState<Incident | null>(
    null
  );

  if (isLoading) {
    return <div className="p-6 text-sm text-muted">Loading alert…</div>;
  }
  if (error || !alert) {
    return (
      <div className="p-6 max-w-2xl">
        <Link
          href="/alerts"
          className="text-sm text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Back to alerts
        </Link>
        <div className="mt-4 text-sm text-[color:var(--critical)]">
          Couldn't load this alert.
        </div>
      </div>
    );
  }

  const stripe = STRIPE_VAR[alert.severity];
  const title = enrichAlertTitle(alert);

  return (
    // -mx-6 / -mx-10 bleeds past the AppShell main padding so the
    // chat rail's circuit pattern hugs the right edge of the viewport
    // like the left sidebar hugs the left. Inner content keeps its
    // padding via the child wrapper.
    // The height is the viewport minus the 4rem header, not h-full: <main>
    // has no height of its own, so h-full collapsed to the chat rail's height
    // and squeezed the investigation into a short scroll box.
    // Negative margins cancel <main>'s padding at every breakpoint (px-4 /
    // md:px-6 / lg:px-10); a fixed -mx-6 overshot phones by 8px.
    <div className="flex h-[calc(100dvh-4rem)] min-h-0 -mx-4 md:-mx-6 lg:-mx-10 -my-5 md:-my-6 lg:-my-8">
      <div className="flex-1 min-w-0 flex flex-col overflow-hidden px-4 md:px-6 lg:px-10 py-5 md:py-6 lg:py-8">
        <div className="pb-3">
          <button
            type="button"
            onClick={() => router.push("/alerts")}
            className="text-xs text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> Back to alerts
          </button>
        </div>

        {/* Header card — full content width. */}
        <section
          className="surface relative overflow-hidden p-5 mb-4"
          style={{ boxShadow: `inset 6px 0 0 ${stripe}` }}
        >
          <div className="flex flex-wrap items-start gap-4">
            <div className="flex-1 min-w-[16rem]">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mb-1.5">
                <span
                  className="text-[10px] uppercase tracking-wider font-semibold"
                  style={{ color: stripe }}
                >
                  {alert.severity}
                </span>
                <span className="text-[10px] uppercase tracking-wider text-muted">
                  {alert.module}
                </span>
                <span className="text-[10px] uppercase tracking-wider text-muted">
                  · {alert.status}
                </span>
                {alert.occurrence_count > 1 && (
                  <span className="text-[10px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded-md border border-line text-fg/80">
                    fired ×{alert.occurrence_count}
                  </span>
                )}
                <span className="text-[10px] text-muted">
                  · first fired {formatRelative(alert.created_at)}
                </span>
                {alert.last_seen_at &&
                  alert.last_seen_at !== alert.created_at && (
                    <span className="text-[10px] text-muted">
                      · last {formatRelative(alert.last_seen_at)}
                    </span>
                  )}
              </div>
              <h1 className="text-lg font-semibold leading-tight">{title}</h1>
            </div>
            <div className="flex flex-wrap gap-2">
              {alert.status === "open" && (
                <button
                  onClick={() => ack.mutate()}
                  disabled={ack.isPending}
                  className="text-xs h-8 px-3 rounded-lg border border-line hover:bg-bg-elevated/60 inline-flex items-center gap-1.5"
                >
                  <Check className="h-3.5 w-3.5" /> Acknowledge
                </button>
              )}
              {!promotedIncident && (
                <button
                  onClick={() => setPromoteOpen(true)}
                  className="text-xs h-8 px-3 rounded-lg border border-line hover:bg-bg-elevated/60 inline-flex items-center gap-1.5"
                >
                  <Siren className="h-3.5 w-3.5" /> Promote to incident
                </button>
              )}
              {alert.status !== "resolved" && (
                <button
                  onClick={() => resolve.mutate()}
                  disabled={resolve.isPending}
                  className="text-xs h-8 px-3 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base inline-flex items-center gap-1.5"
                >
                  <CheckCircle2 className="h-3.5 w-3.5" /> Resolve
                </button>
              )}
            </div>
          </div>
          {promotedIncident && (
            <div className="mt-3 text-xs inline-flex items-center gap-1.5 px-2 py-1 rounded-md border border-accent-cyan/40 bg-accent-cyan/10 text-accent-cyan">
              <Siren className="h-3 w-3" />
              Promoted to incident {promotedIncident.id.slice(0, 8)} —{" "}
              {promotedIncident.severity.toUpperCase()}
            </div>
          )}
        </section>

        {/* Investigation tabs take the full content width; the chat lives
         *  in the right rail (collapsed by default). Occurrences, the
         *  original alert body and raw metadata sit on the Details tab. */}
        <div className="flex-1 min-h-0 overflow-y-auto pr-1">
          <InvestigationTabs
            alert={alert}
            details={
              <div className="space-y-4">
                <AlertOccurrences
                  alertId={alert.id}
                  occurrenceCount={alert.occurrence_count}
                />

                <details className="surface p-4">
                  <summary className="cursor-pointer text-[11px] uppercase tracking-wider text-muted">
                    Original alert body
                  </summary>
                  <article className="prose prose-sm dark:prose-invert max-w-none alert-detail-body mt-3">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {alert.body}
                    </ReactMarkdown>
                  </article>
                </details>

                <details className="surface p-4">
                  <summary className="cursor-pointer text-[11px] uppercase tracking-wider text-muted">
                    Raw metadata
                  </summary>
                  <pre className="text-[12px] font-mono whitespace-pre-wrap break-words bg-bg-elevated/60 border border-line rounded-lg p-3 mt-3 max-h-[280px] overflow-auto">
                    {JSON.stringify(alert.metadata_json, null, 2)}
                  </pre>
                </details>
              </div>
            }
          />
        </div>
      </div>

      <AlertChatRail alertId={alert.id} />

      {promoteOpen && (
        <PromoteIncidentDialog
          alert={alert}
          onClose={() => setPromoteOpen(false)}
          onPromoted={(incident) => {
            setPromotedIncident(incident);
            qc.invalidateQueries({ queryKey: ["incidents"] });
          }}
        />
      )}

      <style jsx global>{`
        .alert-detail-body h1,
        .alert-detail-body h2,
        .alert-detail-body h3 {
          color: var(--text);
        }
        .alert-detail-body strong {
          color: var(--text);
        }
        .alert-detail-body p,
        .alert-detail-body li {
          color: color-mix(in srgb, var(--text) 78%, transparent);
        }
        .alert-detail-body code {
          font-family:
            ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, monospace;
          font-size: 0.85em;
          background: var(--bg-elevated);
          border: 1px solid var(--line);
          padding: 0.05em 0.35em;
          border-radius: 4px;
        }
        .alert-detail-body pre {
          background: var(--bg-elevated);
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 0.75rem 1rem;
          overflow-x: auto;
        }
      `}</style>
    </div>
  );
}
