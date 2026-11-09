"use client";

/**
 * General onboarding wizard.
 *
 * A guided, step-by-step walk through every integration the operator
 * normally sets up by hand via `PUT /integrations/config/{provider}`
 plus an optional cluster
 * tunnel (VPN) step that delegates to the dedicated workflow at
 * `/onboarding/cluster` via the shared <ClusterWorkflow /> component.
 *
 * Server contract:
 *
 *   • GET  /onboarding/status            — per-step "already done?" map
 *   • POST /onboarding/test/{provider}   — verify creds before saving
 *   • POST /onboarding/validate/{provider} — schema preflight
 *   • PUT  /integrations/config/{prov}   — actual save (per step)
 *   • POST /clusters                     — VPN step, owned by <ClusterWorkflow />
 *
 * The wizard owns no server-side state. Quitting mid-flow leaves
 * already-saved steps intact; re-running pre-fills from
 * GET /onboarding/status + /integrations/config.
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Boxes,
  Check,
  ChevronLeft,
  ChevronRight,
  Workflow,
  X,
} from "lucide-react";
import Link from "next/link";
import { api } from "@/lib/api";
import { HUB } from "@/lib/hub-routes";
import { errorText } from "@/lib/errors";
import { Button, buttonClass } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { ClusterWorkflow } from "@/components/onboarding/cluster-workflow";
import { OnboardingHeader } from "@/components/onboarding/onboarding-header";
import {
  IntegrationStep,
  STEPS,
  StepState,
  StepView,
  emptyState,
  emptyValues,
  payloadFromValues,
} from "@/components/integrations/steps";

const TOTAL_STEPS = STEPS.length + 1;

export default function OnboardingPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data: status } = useQuery({
    queryKey: ["onboarding", "status"],
    queryFn: () => api.onboarding.status(),
  });
  const { data: existingConfigs } = useQuery({
    queryKey: ["integrations", "config"],
    queryFn: () => api.integrations.listConfig(),
  });

  const [stepIdx, setStepIdx] = useState<number>(-1);
  const [states, setStates] = useState<Record<string, StepState>>(() =>
    Object.fromEntries(STEPS.map((s) => [s.id, emptyState(s)]))
  );

  // Pre-seed enabled state from /integrations/config + /onboarding/status.
  useEffect(() => {
    if (!existingConfigs) return;
    setStates((prev) => {
      const next = { ...prev };
      for (const cfg of existingConfigs) {
        const step = STEPS.find((s) => s.provider === cfg.provider);
        if (!step) continue;
        if (next[step.id].enabled) continue; // user has typed something
        const merged: Record<string, string> = { ...emptyValues(step) };
        for (const [k, v] of Object.entries(cfg.config || {})) {
          if (typeof v === "string") merged[k] = v;
          else if (typeof v === "number") merged[k] = String(v);
        }
        next[step.id] = {
          ...next[step.id],
          enabled: true,
          values: merged,
          saved: true,
          clusterTunnelId: cfg.cluster_tunnel_id ?? null,
        };
      }
      return next;
    });
  }, [existingConfigs]);

  // Status drives the welcome screen — but also auto-marks the cluster
  // step as configured if there's any cluster_tunnel row on the tenant.
  useEffect(() => {
    if (!status) return;
    const clusterStep = status.steps.find((s) => s.id === "cluster");
    if (clusterStep?.configured) {
      setStates((prev) => ({
        ...prev,
        cluster: { ...prev.cluster, enabled: true, saved: true },
      }));
    }
  }, [status]);

  const { data: clusters } = useQuery({
    queryKey: ["clusters"],
    queryFn: () => api.clusters.list(),
  });

  const putIntegration = useMutation({
    mutationFn: async (input: {
      provider: string;
      config: Record<string, unknown>;
      cluster_tunnel_id: string | null;
    }) =>
      api.integrations.putConfig(input.provider, {
        config: input.config,
        cluster_tunnel_id: input.cluster_tunnel_id,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["integrations", "config"] });
      qc.invalidateQueries({ queryKey: ["onboarding", "status"] });
    },
  });

  const testIntegration = useMutation({
    mutationFn: async (input: {
      provider: string;
      config: Record<string, unknown>;
      cluster_tunnel_id: string | null;
    }) =>
      api.onboarding.test(input.provider, input.config, input.cluster_tunnel_id),
  });

  // A step counts as done when the server says so (it may have been set up
  // from Integrations directly) or it was saved in this session.
  const configuredIds = useMemo(
    () => new Set(status?.steps.filter((s) => s.configured).map((s) => s.id) ?? []),
    [status]
  );
  const isDone = (id: string) => configuredIds.has(id) || states[id].saved;
  // "Set up the remaining" walks only the steps that aren't done yet.
  const [skipDone, setSkipDone] = useState(false);
  function nextIdx(from: number): number {
    let i = from + 1;
    while (skipDone && i < STEPS.length && isDone(STEPS[i].id)) i++;
    return Math.min(i, STEPS.length);
  }
  function prevIdx(from: number): number {
    let i = from - 1;
    while (skipDone && i >= 0 && isDone(STEPS[i].id)) i--;
    return Math.max(i, -1);
  }

  function update(stepId: string, patch: Partial<StepState>) {
    setStates((prev) => ({ ...prev, [stepId]: { ...prev[stepId], ...patch } }));
  }

  function updateValue(stepId: string, key: string, value: string) {
    setStates((prev) => ({
      ...prev,
      [stepId]: {
        ...prev[stepId],
        values: { ...prev[stepId].values, [key]: value },
        saved: false,
        test: null,
      },
    }));
  }

  function payloadFor(step: IntegrationStep) {
    return payloadFromValues(step, states[step.id].values);
  }

  async function runTest() {
    if (stepIdx < 0 || stepIdx >= STEPS.length) return;
    const step = STEPS[stepIdx];
    if (!step.provider) return;
    const { config } = payloadFor(step);
    try {
      const result = await testIntegration.mutateAsync({
        provider: step.provider,
        config,
        cluster_tunnel_id: states[step.id].clusterTunnelId,
      });
      update(step.id, { test: result, error: result.ok ? null : result.message });
    } catch (e) {
      update(step.id, { error: errorText(e) });
    }
  }

  async function saveCurrentStep(): Promise<boolean> {
    if (stepIdx < 0 || stepIdx >= STEPS.length) return true;
    const step = STEPS[stepIdx];
    const state = states[step.id];

    if (!state.enabled) return true;

    // Cluster step is handled by <ClusterWorkflow /> — saving here is a
    // no-op; the workflow component does its own POST /clusters.
    if (step.id === "cluster") return true;

    const { config, missing } = payloadFor(step);
    if (missing.length > 0) {
      update(step.id, { error: `Required: ${missing.join(", ")}` });
      return false;
    }
    try {
      await putIntegration.mutateAsync({
        provider: step.provider!,
        config,
        cluster_tunnel_id: state.clusterTunnelId,
      });
      update(step.id, { saved: true, error: null });
      toast.success(`${step.title} saved.`);
      return true;
    } catch (e) {
      update(step.id, { error: errorText(e) });
      toast.error(errorText(e));
      return false;
    }
  }

  async function goNext() {
    const ok = await saveCurrentStep();
    if (!ok) return;
    setStepIdx(nextIdx(stepIdx));
  }

  function goBack() {
    setStepIdx(prevIdx(stepIdx));
  }

  // One list drives every count on the page (header, welcome card, button),
  // so "N of M" always matches the rows the user can see.
  const doneCount = STEPS.filter((s) => isDone(s.id)).length;
  const progress =
    stepIdx < 0
      ? status
        ? doneCount / STEPS.length
        : 0
      : (stepIdx + 1) / TOTAL_STEPS;
  const progressLabel =
    stepIdx < 0
      ? status
        ? `${doneCount} of ${STEPS.length} set up`
        : "Checking what's already set up…"
      : stepIdx < STEPS.length
        ? `Step ${stepIdx + 1} of ${STEPS.length}: ${STEPS[stepIdx].title}`
        : "All done";

  return (
    <div className="space-y-6 max-w-[860px]">
      <OnboardingHeader
        icon={Workflow}
        title="Onboarding"
        description="Set up notifications, observability and the cluster tunnel in one sequence. Skip any step you don't need."
        action={
          <Link
            href="/managed-infra?tab=integrations"
            className={buttonClass("secondary", "sm")}
          >
            Open Integrations <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        }
        progress={{ value: progress, label: progressLabel }}
      />

      {stepIdx === -1 && (
        <WelcomeStep
          isDone={isDone}
          remaining={STEPS.length - doneCount}
          loaded={!!status}
          onOpen={(i) => {
            setSkipDone(false);
            setStepIdx(i);
          }}
          onStartRemaining={() => {
            setSkipDone(true);
            const first = STEPS.findIndex((s) => !isDone(s.id));
            setStepIdx(first === -1 ? STEPS.length : first);
          }}
        />
      )}

      {stepIdx >= 0 && stepIdx < STEPS.length && (
        <>
          {STEPS[stepIdx].id === "cluster" ? (
            <ClusterStepView
              state={states["cluster"]}
              onToggle={(enabled) => update("cluster", { enabled })}
              onComplete={() => {
                update("cluster", { saved: true });
                setStepIdx(nextIdx(stepIdx));
              }}
            />
          ) : (
            <StepView
              step={STEPS[stepIdx]}
              state={states[STEPS[stepIdx].id]}
              existing={existingConfigs?.find(
                (c) => c.provider === STEPS[stepIdx].provider
              )}
              onToggle={(enabled) => update(STEPS[stepIdx].id, { enabled })}
              onChange={(key, val) => updateValue(STEPS[stepIdx].id, key, val)}
              onTest={runTest}
              testing={testIntegration.isPending}
              clusters={clusters ?? []}
              onClusterChange={(id) =>
                update(STEPS[stepIdx].id, { clusterTunnelId: id, test: null })
              }
            />
          )}
        </>
      )}

      {stepIdx === STEPS.length && <DoneStep states={states} />}

      {stepIdx >= 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 pt-2">
          <Button
            variant="ghost"
            onClick={goBack}
            disabled={stepIdx < 0}
            icon={<ChevronLeft className="h-3.5 w-3.5" aria-hidden />}
          >
            Back
          </Button>
          {stepIdx < STEPS.length ? (
            STEPS[stepIdx].id === "cluster" ? (
              // The cluster step advances itself via onComplete on the
              // <ClusterWorkflow />. Offer a "Skip" affordance so users
              // who don't want the VPN can move on.
              !states.cluster.enabled || states.cluster.saved ? (
                <Button variant="primary" onClick={() => setStepIdx(nextIdx(stepIdx))}>
                  {states.cluster.saved ? "Continue" : "Skip cluster tunnel"}
                  <ChevronRight className="h-3.5 w-3.5" aria-hidden />
                </Button>
              ) : (
                <div className="text-xs text-muted">
                  Finish the cluster workflow above, or untick the toggle to skip.
                </div>
              )
            ) : (
              <Button
                variant="primary"
                onClick={goNext}
                pending={putIntegration.isPending}
                disabled={testIntegration.isPending}
                disabledReason={testIntegration.isPending ? "Waiting for the connection test" : undefined}
              >
                {states[STEPS[stepIdx].id].enabled
                  ? "Save & continue"
                  : "Skip & continue"}
                <ChevronRight className="h-3.5 w-3.5" aria-hidden />
              </Button>
            )
          ) : (
            <Link href="/" className={buttonClass("primary")}>
              Go to dashboard <ChevronRight className="h-3.5 w-3.5" aria-hidden />
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

// ── Welcome step ────────────────────────────────────────────────────────

function WelcomeStep({
  isDone,
  remaining,
  loaded,
  onOpen,
  onStartRemaining,
}: {
  isDone: (id: string) => boolean;
  remaining: number;
  loaded: boolean;
  onOpen: (idx: number) => void;
  onStartRemaining: () => void;
}) {
  return (
    <div className="rounded-2xl border border-line bg-bg-card p-4 sm:p-6 space-y-4">
      <div className="text-base font-medium">What you&apos;ll set up</div>
      <p className="text-sm text-muted">
        Each step is optional and independent. Pick one below to set it up
        or change it, or walk through everything that isn&apos;t done yet.
        The cluster tunnel can also be set up on its own from{" "}
        <Link href="/onboarding/cluster" className="underline">
          Cluster tunnel onboarding
        </Link>
        .
      </p>
      {/* To-do first, done after (dimmed), so the eye lands on what's left. */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
        {STEPS.map((step, i) => ({ step, i, done: isDone(step.id) }))
          .sort((a, b) => Number(a.done) - Number(b.done) || a.i - b.i)
          .map(({ step, i, done }) => {
          const Icon = step.icon;
          return (
            <button
              key={step.id}
              type="button"
              onClick={() => onOpen(i)}
              className={`group text-left flex items-center gap-2 rounded-lg border px-3 py-2 hover:border-accent/50 hover:bg-bg-elevated/60 transition-colors ${
                done ? "border-line bg-transparent text-muted" : "border-line-strong bg-bg-elevated/30"
              }`}
            >
              <Icon className={`h-4 w-4 shrink-0 ${done ? "text-muted" : "text-accent"}`} aria-hidden />
              <span className="flex-1 min-w-0">{step.title}</span>
              {done ? (
                <span className="text-[11px] uppercase tracking-wider text-accent flex items-center gap-1 shrink-0">
                  <Check className="h-3 w-3" aria-hidden /> Done
                </span>
              ) : (
                <ChevronRight className="h-3.5 w-3.5 text-muted group-hover:text-fg shrink-0" />
              )}
            </button>
          );
        })}
      </div>
      <div className="sticky bottom-0 -mx-4 sm:mx-0 -mb-4 sm:mb-0 px-4 sm:px-0 py-3 sm:py-0 sm:pt-2 bg-bg-card border-t border-line sm:border-0 flex flex-wrap items-center justify-end gap-2">
        {remaining > 0 ? (
          <Button
            variant="primary"
            onClick={onStartRemaining}
            disabled={!loaded}
            disabledReason={loaded ? undefined : "Checking what's already set up"}
          >
            {loaded ? `Set up the ${remaining} remaining` : "Loading…"}
            <ChevronRight className="h-3.5 w-3.5" aria-hidden />
          </Button>
        ) : (
          <span className="text-xs text-muted">
            Everything is set up. Pick a step above to change it.
          </span>
        )}
      </div>
    </div>
  );
}

// ── Cluster step — delegates to the shared workflow ─────────────────────

function ClusterStepView({
  state,
  onToggle,
  onComplete,
}: {
  state: StepState;
  onToggle: (enabled: boolean) => void;
  onComplete: () => void;
}) {
  return (
    <div className="rounded-2xl border border-line bg-bg-card p-5 space-y-4">
      <div className="flex items-start gap-3">
        <div
          className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0"
          style={{
            background: "color-mix(in srgb, var(--accent) 14%, transparent)",
          }}
        >
          <Boxes className="h-4 w-4 text-accent-cyan" />
        </div>
        <div className="flex-1">
          <div className="text-base font-medium">Cluster tunnel (VPN)</div>
          <p className="text-sm text-muted mt-0.5">
            Optional secure tunnel from your cluster to Daalu. Turning this
            step on runs the cluster tunnel setup here; you can also run it
            on its own from{" "}
            <Link href="/onboarding/cluster" className="underline">
              Cluster tunnel onboarding
            </Link>
            .
          </p>
        </div>
        <label className="flex items-center gap-2 text-xs cursor-pointer shrink-0">
          <input
            type="checkbox"
            checked={state.enabled}
            onChange={(e) => onToggle(e.target.checked)}
            className="h-4 w-4 accent-accent-cyan"
          />
          <span className="text-muted">
            {state.enabled ? "Enabled" : "Skipped"}
          </span>
        </label>
      </div>

      {state.enabled ? (
        <ClusterWorkflow onComplete={onComplete} allowAnother={false} />
      ) : (
        <div className="text-xs text-muted">
          Leave unchecked to skip the cluster tunnel for now. You can always
          set one up later on the{" "}
          <Link href={HUB.clusters} className="underline">
            Clusters &amp; observability tab
          </Link>
          .
        </div>
      )}
    </div>
  );
}

// ── Done step ───────────────────────────────────────────────────────────

function DoneStep({ states }: { states: Record<string, StepState> }) {
  const enabled = STEPS.filter((s) => states[s.id].enabled);
  const skipped = STEPS.filter((s) => !states[s.id].enabled);
  return (
    <div className="rounded-2xl border border-line bg-bg-card p-6 space-y-4 motion-enter">
      <div className="text-base font-medium flex items-center gap-2">
        <Check className="h-4 w-4 text-accent-emerald motion-success" aria-hidden /> Onboarding complete
      </div>
      <p className="text-sm text-muted">
        You can change any of these later from{" "}
        <Link href="/managed-infra?tab=integrations" className="underline">
          Integrations
        </Link>{" "}
        or come back to this page.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
        {enabled.length > 0 && (
          <div className="rounded-lg border border-line bg-bg-elevated/30 p-3 space-y-1">
            <div className="text-[11px] uppercase tracking-wider text-muted">
              Configured
            </div>
            {enabled.map((s) => (
              <div key={s.id} className="flex items-center gap-2 text-sm">
                <Check className="h-3 w-3 text-accent-emerald" /> {s.title}
              </div>
            ))}
          </div>
        )}
        {skipped.length > 0 && (
          <div className="rounded-lg border border-line bg-bg-elevated/30 p-3 space-y-1">
            <div className="text-[11px] uppercase tracking-wider text-muted">
              Skipped
            </div>
            {skipped.map((s) => (
              <div
                key={s.id}
                className="flex items-center gap-2 text-sm text-muted"
              >
                <X className="h-3 w-3" /> {s.title}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
