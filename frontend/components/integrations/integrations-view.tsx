"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronRight,
  FlaskConical,
  GitPullRequest,
  RefreshCw,
  Settings as SettingsIcon,
} from "lucide-react";
import { api, type IntegrationConfig, type IntegrationDescriptor } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";
import { ConnectModal } from "@/components/integrations/connect-modal";
import { STEPS, type IntegrationStep } from "@/components/integrations/steps";
import { LoadState } from "@/components/ui/load-state";

/**
 * The page renders two kinds of cards:
 *
 * 1. **Ingest adapters** — backed by the backend integration registry
 *    (`/integrations`). Each exposes a `Run ingest` button.
 * 2. **Channels** — config-only providers (SMTP, Slack) that don't have
 *    an ingest adapter; other code (notify.send_email,
 *    notify.send_slack) reads their config. These get a `Configure`
 *    button that opens the wizard step in a modal.
 */
const CHANNEL_STEP_IDS = ["email", "slack"];

/**
 * Plain-English card copy by provider. The backend descriptions are written
 * for developers (event names, API verbs); these say what the user gets.
 * Unknown providers fall back to the backend text.
 */
const SOURCE_COPY: Record<string, string> = {
  prometheus:
    "Brings your firing alerts into Daalu from Alertmanager, or from Prometheus' own alert rules when there's no Alertmanager.",
  pagerduty: "Brings your open PagerDuty incidents into the alerts feed.",
  aws: "Brings CloudWatch alarms that are firing into Daalu as alerts. Connect it with read-only AWS credentials.",
  loki: "Lets the agent read your logs from Grafana Loki while it investigates an alert.",
  thanos: "Lets the agent look back through older metrics while it investigates.",
  opensearch: "Lets the agent and the log console search your logs in OpenSearch.",
  "synthetic-infra": "Creates sample alerts so you can try Daalu without connecting a real system.",
};

/** Sources that generate fake data; shown apart so they're never mistaken for real ones. */
function isDemoSource(i: IntegrationDescriptor): boolean {
  return i.provider.startsWith("synthetic") || /\(demo\)/i.test(i.display_name);
}

function StatusChip({ configured }: { configured: boolean }) {
  return (
    <span
      className={`whitespace-nowrap text-[11px] font-medium uppercase tracking-wider rounded px-1.5 py-0.5 border ${
        configured
          ? "bg-accent/10 border-accent/30 text-accent"
          : "bg-signal-warning/10 border-signal-warning/40 text-signal-warning"
      }`}
    >
      {configured ? "Configured" : "Needs setup"}
    </span>
  );
}

export function IntegrationsView() {
  const qc = useQueryClient();
  const toast = useToast();
  const adaptersQ = useQuery({
    queryKey: ["integrations"],
    queryFn: () => api.integrations.list(),
  });
  const adapters = adaptersQ.data;
  const { data: tenantConfigs } = useQuery({
    queryKey: ["integrations", "config"],
    queryFn: () => api.integrations.listConfig(),
  });
  // Per-card result of the last ingest run, keyed by provider.
  const [ingestResult, setIngestResult] = useState<
    Record<string, { ok: boolean; text: string }>
  >({});
  const ingest = useMutation({
    mutationFn: (provider: string) => api.integrations.ingest(provider),
    onMutate: (provider) =>
      setIngestResult(({ [provider]: _drop, ...rest }) => rest),
    onSuccess: (res, provider) => {
      qc.invalidateQueries({ queryKey: ["events"] });
      const n = res.events_emitted;
      toast.success(n === 0 ? "Checked the source: no new alerts." : `Brought in ${n.toLocaleString()} new event${n === 1 ? "" : "s"}.`);
      setIngestResult((r) => ({
        ...r,
        [provider]: {
          ok: true,
          text: n === 0 ? "Checked just now: no new events." : `Brought in ${n.toLocaleString()} new event${n === 1 ? "" : "s"}.`,
        },
      }));
    },
    onError: (e, provider) => {
      toast.error(errorText(e));
      setIngestResult((r) => ({
        ...r,
        [provider]: {
          ok: false,
          text: errorText(e),
        },
      }));
    },
  });

  const [editing, setEditing] = useState<IntegrationStep | null>(null);

  const channelSteps = STEPS.filter((s) => CHANNEL_STEP_IDS.includes(s.id));
  const realSources = (adapters ?? []).filter((i) => !isDemoSource(i));
  const demoSources = (adapters ?? []).filter(isDemoSource);
  const configByProvider: Record<string, IntegrationConfig> = {};
  for (const c of tenantConfigs ?? []) {
    configByProvider[c.provider] = c;
  }

  function renderSource(i: IntegrationDescriptor) {
    const running = ingest.isPending && ingest.variables === i.provider;
    const result = ingestResult[i.provider];
    const demo = isDemoSource(i);
    return (
      <div key={i.provider} className="rounded-2xl border border-line bg-bg-card p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-base font-medium">{i.display_name.replace(/\s*\(demo\)\s*/i, "")}</span>
              {demo ? (
                <span className="whitespace-nowrap text-[11px] font-medium uppercase tracking-wider rounded px-1.5 py-0.5 border border-line-strong text-muted">
                  Sample data
                </span>
              ) : (
                <StatusChip configured={i.configured} />
              )}
            </div>
          </div>
          <Button
            size="sm"
            onClick={() => ingest.mutate(i.provider)}
            disabled={!i.configured}
            pending={running}
            disabledReason={i.configured ? undefined : "Set this source up first"}
            title="Check this source for new alerts now"
            className="shrink-0"
            icon={<RefreshCw className="h-3 w-3" aria-hidden />}
          >
            {running ? "Checking…" : "Check now"}
          </Button>
        </div>
        <p className="text-sm text-fg/80 mt-2">{SOURCE_COPY[i.provider] ?? i.description}</p>
        {!i.configured && (
          <div className="text-xs text-muted mt-2">
            Set this source up before checking it, in{" "}
            <Link href="/onboarding" className="underline hover:text-fg">
              Onboarding
            </Link>
            .
          </div>
        )}
        {result && (
          <div
            className={`mt-2 text-xs flex items-start gap-1.5 motion-enter ${
              result.ok ? "text-accent" : "text-signal-critical"
            }`}
            role="status"
          >
            {result.ok ? (
              <Check className="h-3.5 w-3.5 shrink-0 motion-success" aria-hidden />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
            )}
            <span className="min-w-0 break-words">{result.text}</span>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-8 max-w-[1200px]">
      <p className="text-muted text-sm">
        Connect the tools Daalu reads from and sends to.
      </p>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wider">
          Guided setup
        </h2>
        <p className="text-xs text-muted -mt-2">
          New to Nautobot? The guided setup walks you through it.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Link
            href="/onboarding/sot"
            className="rounded-2xl border border-line bg-bg-card p-4 hover:border-accent-cyan/60 hover:shadow-glow transition-all group block"
          >
            <div className="flex items-start gap-3">
              <div
                className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0"
                style={{
                  background:
                    "color-mix(in srgb, var(--accent-cyan) 14%, transparent)",
                }}
              >
                <GitPullRequest className="h-4 w-4 text-accent-cyan" aria-hidden />
              </div>
              <div className="flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-base font-medium">
                    Source of Truth (Nautobot)
                  </span>
                  <ArrowRight className="h-4 w-4 text-muted group-hover:text-accent-cyan transition-colors" aria-hidden />
                </div>
                <div className="text-[11px] uppercase tracking-wider text-muted mt-1">
                  Guided · 3 steps
                </div>
                <p className="text-sm text-fg/80 mt-2 leading-relaxed">
                  Pick hosted or bring-your-own Nautobot, wire the
                  connection, set up SSH credentials, then add your
                  first device. Sets up everything Operations →
                  Devices needs.
                </p>
              </div>
            </div>
          </Link>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wider">
          Notification channels
        </h2>
        <p className="text-xs text-muted -mt-2">
          Daalu sends invites, briefings and incident notifications through
          these. Email is needed to invite teammates.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {channelSteps.map((step) => {
            const cfg = step.provider ? configByProvider[step.provider] : undefined;
            const configured = !!cfg;
            return (
              <div
                key={step.id}
                className="rounded-2xl border border-line bg-bg-card p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="text-base font-medium">{step.title}</span>
                      <StatusChip configured={configured} />
                    </div>
                  </div>
                  <Button
                    size="sm"
                    variant={configured ? "secondary" : "primary"}
                    onClick={() => setEditing(step)}
                    className="shrink-0"
                    icon={<SettingsIcon className="h-3 w-3" aria-hidden />}
                  >
                    {configured ? `Edit ${step.title}` : `Set up ${step.title}`}
                  </Button>
                </div>
                <p className="text-sm text-fg/80 mt-2">
                  {step.description}
                </p>
              </div>
            );
          })}
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wider">
          Ingest sources
        </h2>
        <LoadState
          query={adaptersQ}
          empty={(adapters ?? []).length === 0}
          loadingText="Loading integrations…"
          emptyTitle="No ingest sources available"
          emptyHint="Ingest sources bring alerts and events into Daalu once they're configured."
        >
        <p className="text-xs text-muted -mt-2">
          Where Daalu&apos;s alerts and events come from.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {realSources.map(renderSource)}
        </div>
        {demoSources.length > 0 && (
          <details className="group rounded-2xl border border-dashed border-line-strong p-3">
            <summary className="cursor-pointer list-none flex items-center gap-2 text-sm text-muted hover:text-fg">
              <ChevronRight className="h-4 w-4 transition-transform group-open:rotate-90" aria-hidden />
              <FlaskConical className="h-4 w-4" aria-hidden />
              Demo sources ({demoSources.length}) · sample data for trying Daalu out
            </summary>
            <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3 motion-enter">
              {demoSources.map(renderSource)}
            </div>
          </details>
        )}
        </LoadState>
      </section>

      {editing && (
        <ConnectModal
          step={editing}
          existing={editing.provider ? configByProvider[editing.provider] : undefined}
          onClose={() => setEditing(null)}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["integrations", "config"] });
          }}
        />
      )}
    </div>
  );
}
