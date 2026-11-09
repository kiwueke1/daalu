"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeft, Check, CheckCircle2, ListChecks, Loader2, Siren } from "lucide-react";
import { api, type Alert, type Incident } from "@/lib/api";
import { AlertChatRail } from "@/components/alerts/alert-chat-rail";
import { AlertOccurrences } from "@/components/alerts/alert-occurrences";
import {
  InvestigationTabs,
  usePendingApprovalCount,
} from "@/components/alerts/investigation-tabs";
import { PromoteIncidentDialog } from "@/components/alerts/promote-incident-dialog";
import { enrichAlertTitle } from "@/components/alerts/alert-title";
import { formatRelative } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

const STRIPE_VAR: Record<Alert["severity"], string> = {
  critical: "var(--critical)",
  warning: "var(--warning)",
  info: "var(--info)",
};

export default function AlertDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const toast = useToast();
  const alertId = params.id;
  const toApprove = usePendingApprovalCount(alertId);

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
      toast.success("Alert acknowledged. It stays open while you work on it.");
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const resolve = useMutation({
    mutationFn: () => api.alerts.resolve(alertId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alert", alertId] });
      qc.invalidateQueries({ queryKey: ["alerts"] });
      toast.success("Alert resolved. Its investigation has stopped.");
    },
    onError: (e) => toast.error(errorText(e)),
  });

  // The thing that needs the user is the plan waiting for approval: jump to
  // the workflow tab (InvestigationTabs follows ?tab=).
  const reviewPlan = () => {
    router.replace(`/alerts/${alertId}?tab=workflow`, { scroll: false });
    window.setTimeout(() => document.getElementById("tab-workflow")?.focus(), 50);
  };

  const [promoteOpen, setPromoteOpen] = useState(false);
  const [promotedIncident, setPromotedIncident] = useState<Incident | null>(
    null
  );

  if (isLoading) {
    return (
      <div role="status" className="p-6 text-sm text-muted flex items-center gap-2">
        <Loader2 aria-hidden className="h-4 w-4 animate-spin" /> Loading alert…
      </div>
    );
  }
  if (error || !alert) {
    return (
      <div className="p-6 max-w-2xl">
        <Link
          href="/alerts"
          className="text-sm text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
        >
          <ArrowLeft aria-hidden className="h-3.5 w-3.5" /> Back to alerts
        </Link>
        <div role="alert" className="mt-4 text-sm">
          <div className="font-medium text-[color:var(--critical)]">Couldn&apos;t load this alert.</div>
          {error && <div className="text-muted mt-1">{errorText(error)}</div>}
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
            className="min-h-6 text-xs text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
          >
            <ArrowLeft aria-hidden className="h-3.5 w-3.5" /> Back to alerts
          </button>
        </div>

        {/* Header card — full content width. */}
        <section
          className="surface relative overflow-hidden p-5 mb-4"
          style={{ boxShadow: `inset 6px 0 0 ${stripe}` }}
        >
          <div className="flex flex-wrap items-start gap-4">
            {/* The title keeps a readable width; when the actions don't fit
                beside it they drop to their own row underneath. */}
            <div className="flex-1 min-w-[min(100%,28rem)]">
              {/* Gap-spaced, no literal "·" separators, so no wrapped line
                  ever starts with a dangling dot. */}
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mb-1.5">
                <span
                  className="text-[11px] uppercase tracking-wider font-semibold"
                  style={{ color: stripe }}
                >
                  {alert.severity}
                </span>
                <span className="text-[11px] uppercase tracking-wider text-muted">
                  {alert.module}
                </span>
                <span className="text-[11px] uppercase tracking-wider text-muted">
                  {alert.status}
                </span>
                {alert.occurrence_count > 1 && (
                  <span className="text-[11px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded-md border border-line text-fg">
                    fired ×{alert.occurrence_count}
                  </span>
                )}
                <span className="text-xs text-muted">
                  first fired {formatRelative(alert.created_at)}
                </span>
                {alert.last_seen_at &&
                  alert.last_seen_at !== alert.created_at && (
                    <span className="text-xs text-muted">
                      last {formatRelative(alert.last_seen_at)}
                    </span>
                  )}
              </div>
              <h1 className="text-lg font-semibold leading-tight">{title}</h1>
            </div>
            <div className="flex flex-wrap items-center gap-2 w-full sm:w-auto">
              {alert.status === "open" && (
                <Button
                  size="sm"
                  onClick={() => ack.mutate()}
                  pending={ack.isPending}
                  icon={<Check aria-hidden className="h-3.5 w-3.5" />}
                >
                  Acknowledge
                </Button>
              )}
              {!promotedIncident && (
                <Button
                  size="sm"
                  onClick={() => setPromoteOpen(true)}
                  icon={<Siren aria-hidden className="h-3.5 w-3.5" />}
                >
                  Promote to incident
                </Button>
              )}
              {alert.status !== "resolved" && (
                <ConfirmButton
                  label="Resolve"
                  confirmLabel="Resolve alert"
                  consequence="Closes the alert and stops the investigation."
                  onConfirm={() => resolve.mutate()}
                  pending={resolve.isPending}
                  icon={<CheckCircle2 aria-hidden className="h-3.5 w-3.5" />}
                />
              )}
              {toApprove > 0 && (
                <Button
                  variant="primary"
                  size="sm"
                  onClick={reviewPlan}
                  // On a phone this is what needs the user: first, full width.
                  className="order-first w-full sm:order-none sm:w-auto"
                  icon={<ListChecks aria-hidden className="h-3.5 w-3.5" />}
                >
                  Review plan
                  <span className="motion-attention rounded-full bg-bg-base/25 px-1.5 text-[11px] font-semibold leading-4">
                    {toApprove} to approve
                  </span>
                </Button>
              )}
            </div>
          </div>
          {promotedIncident && (
            <div className="motion-enter mt-3 text-xs inline-flex items-center gap-1.5 px-2 py-1 rounded-md border border-accent/40 bg-accent/10 text-accent">
              <Siren aria-hidden className="h-3 w-3" />
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
