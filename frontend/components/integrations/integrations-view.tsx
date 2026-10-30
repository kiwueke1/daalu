"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  GitPullRequest,
  RefreshCw,
  Settings as SettingsIcon,
} from "lucide-react";
import { api, type IntegrationConfig } from "@/lib/api";
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

export function IntegrationsView() {
  const qc = useQueryClient();
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
      setIngestResult((r) => ({
        ...r,
        [provider]: {
          ok: true,
          text: n === 0 ? "Ran: no new events." : `Ran: ${n.toLocaleString()} new event${n === 1 ? "" : "s"}.`,
        },
      }));
    },
    onError: (e, provider) =>
      setIngestResult((r) => ({
        ...r,
        [provider]: {
          ok: false,
          text: e instanceof Error ? e.message : "Ingest failed.",
        },
      })),
  });

  const [editing, setEditing] = useState<IntegrationStep | null>(null);

  const channelSteps = STEPS.filter((s) => CHANNEL_STEP_IDS.includes(s.id));
  const configByProvider: Record<string, IntegrationConfig> = {};
  for (const c of tenantConfigs ?? []) {
    configByProvider[c.provider] = c;
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
        <p className="text-[12px] text-muted -mt-2">
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
                <GitPullRequest className="h-4 w-4 text-accent-cyan" />
              </div>
              <div className="flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-base font-medium">
                    Source of Truth (Nautobot)
                  </span>
                  <ArrowRight className="h-4 w-4 text-muted group-hover:text-accent-cyan transition-colors" />
                </div>
                <div className="text-[10px] uppercase tracking-wider text-muted mt-1">
                  wizard · 3 steps
                </div>
                <p className="text-[12.5px] text-fg/70 mt-2 leading-relaxed">
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
        <p className="text-[12px] text-muted -mt-2">
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
                      <span
                        className={`whitespace-nowrap text-[10px] uppercase tracking-wider rounded px-1.5 py-0.5 ${
                          configured
                            ? "bg-accent-emerald/15 text-accent-emerald"
                            : "bg-accent-amber/15 text-accent-amber"
                        }`}
                      >
                        {configured ? "configured" : "needs setup"}
                      </span>
                    </div>
                    <div className="text-[10px] uppercase tracking-wider text-muted mt-1">
                      channel · {step.provider}
                    </div>
                  </div>
                  <button
                    onClick={() => setEditing(step)}
                    className="shrink-0 text-xs h-8 px-3 rounded-lg border border-line hover:bg-bg-elevated/60 flex items-center gap-1"
                  >
                    <SettingsIcon className="h-3 w-3" />
                    {configured ? "Edit" : "Configure"}
                  </button>
                </div>
                <p className="text-sm text-fg/70 mt-2">
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
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {(adapters ?? []).map((i) => {
            const running = ingest.isPending && ingest.variables === i.provider;
            const result = ingestResult[i.provider];
            return (
            <div
              key={i.provider}
              className="rounded-2xl border border-line bg-bg-card p-4"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="text-base font-medium">{i.display_name}</span>
                    <span
                      className={`whitespace-nowrap text-[10px] uppercase tracking-wider rounded px-1.5 py-0.5 ${
                        i.configured
                          ? "bg-accent-emerald/15 text-accent-emerald"
                          : "bg-accent-amber/15 text-accent-amber"
                      }`}
                    >
                      {i.configured ? "configured" : "needs setup"}
                    </span>
                  </div>
                  <div className="text-[10px] uppercase tracking-wider text-muted mt-1">
                    {i.module} · {i.provider}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => ingest.mutate(i.provider)}
                  disabled={!i.configured || running}
                  title={
                    i.configured
                      ? "Pull new events from this source now"
                      : "Configure this source first"
                  }
                  className="shrink-0 text-xs h-8 px-3 rounded-lg border border-line hover:bg-bg-elevated/60 flex items-center gap-1 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                >
                  <RefreshCw className={`h-3 w-3 ${running ? "animate-spin" : ""}`} />
                  {running ? "Running…" : "Run ingest"}
                </button>
              </div>
              <p className="text-sm text-fg/70 mt-2">{i.description}</p>
              {!i.configured && (
                <div className="text-[11px] text-muted mt-2">
                  Configure this source before running it. Set it up in{" "}
                  <Link href="/onboarding" className="underline hover:text-fg">
                    Onboarding
                  </Link>
                  .
                </div>
              )}
              {result && (
                <div
                  className={`mt-2 text-[11px] flex items-start gap-1.5 ${
                    result.ok ? "text-accent" : "text-signal-critical"
                  }`}
                  role="status"
                >
                  {result.ok ? (
                    <Check className="h-3.5 w-3.5 shrink-0" />
                  ) : (
                    <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                  )}
                  <span className="min-w-0 break-words">{result.text}</span>
                </div>
              )}
            </div>
            );
          })}
        </div>
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
