"use client";

/**
 * Modal wrapper around <StepView /> for the managed-infra page.
 *
 * Lets a user connect (or update) one provider without walking the full
 * onboarding wizard. Pre-fills from /integrations/config if a row
 * already exists; otherwise starts blank. Saves via
 * `PUT /integrations/config/{provider}` and tests via
 * `POST /onboarding/test/{provider}` — same endpoints the wizard uses.
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, X } from "lucide-react";
import { api, IntegrationConfig } from "@/lib/api";
import {
  IntegrationStep,
  StepState,
  StepView,
  emptyState,
  payloadFromValues,
  valuesFromConfig,
} from "@/components/integrations/steps";

export function ConnectModal({
  step,
  existing,
  onClose,
  onSaved,
}: {
  step: IntegrationStep;
  existing: IntegrationConfig | undefined;
  onClose: () => void;
  onSaved: () => void;
}) {
  const qc = useQueryClient();

  // Seed state from any existing config. `enabled` is always true inside
  // the modal — opening the modal IS the consent; the toggle is hidden.
  const initial = useMemo<StepState>(() => {
    const base = emptyState(step);
    const values = valuesFromConfig(step, existing);
    // The cluster name lives on the integration row (`name`), not in config,
    // so seed the nameField from it when editing.
    if (step.nameField && existing?.name) {
      values[step.nameField] = existing.name;
    }
    return {
      ...base,
      enabled: true,
      values,
      saved: !!existing,
      clusterTunnelId: existing?.cluster_tunnel_id ?? null,
    };
  }, [step, existing]);

  // Only fetch clusters for steps that can be cluster-scoped — saves a
  // request on slack/smtp/cloud cards. The dropdown stays empty (and
  // hidden) when this returns no rows.
  const { data: clusters } = useQuery({
    queryKey: ["clusters"],
    queryFn: () => api.clusters.list(),
    enabled: !!step.supportsCluster,
  });

  const [state, setState] = useState<StepState>(initial);

  // If the user switches between providers without unmounting (unlikely
  // but possible if we ever reuse the modal across cards), reset.
  useEffect(() => {
    setState(initial);
  }, [initial]);

  const putIntegration = useMutation({
    mutationFn: async (input: {
      provider: string;
      config: Record<string, unknown>;
      name?: string;
      cluster_id?: string;
      cluster_tunnel_id: string | null;
    }) =>
      api.integrations.putConfig(input.provider, {
        config: input.config,
        name: input.name,
        cluster_id: input.cluster_id,
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

  function updateValue(key: string, value: string) {
    setState((prev) => ({
      ...prev,
      values: { ...prev.values, [key]: value },
      saved: false,
      test: null,
    }));
  }

  async function runTest() {
    if (!step.provider) return;
    const { config } = payloadFromValues(step, state.values);
    try {
      const result = await testIntegration.mutateAsync({
        provider: step.provider,
        config,
        cluster_tunnel_id: state.clusterTunnelId,
      });
      setState((prev) => ({
        ...prev,
        test: result,
        error: result.ok ? null : result.message,
      }));
    } catch (e) {
      setState((prev) => ({ ...prev, error: (e as Error).message }));
    }
  }

  async function runSave() {
    if (!step.provider) return;
    const { config, missing } = payloadFromValues(step, state.values);
    if (missing.length > 0) {
      setState((prev) => ({
        ...prev,
        error: `Required: ${missing.join(", ")}`,
      }));
      return;
    }
    // Lift the name field out of config into the row's `name` (cluster name).
    let name: string | undefined;
    if (step.nameField) {
      name = (config[step.nameField] as string) || undefined;
      delete config[step.nameField];
    }
    try {
      await putIntegration.mutateAsync({
        provider: step.provider,
        config,
        name,
        // Editing an existing instance → send its id so the backend updates
        // that row instead of creating a duplicate.
        cluster_id: existing?.id,
        cluster_tunnel_id: state.clusterTunnelId,
      });
      setState((prev) => ({ ...prev, saved: true, error: null }));
      onSaved();
      onClose();
    } catch (e) {
      setState((prev) => ({ ...prev, error: (e as Error).message }));
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-bg/70 backdrop-blur-sm flex items-start justify-center p-4 pt-16 overflow-y-auto"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-[720px] space-y-3">
        <div className="flex items-center justify-end">
          <button
            type="button"
            onClick={onClose}
            className="h-8 w-8 rounded-lg border border-line text-muted hover:text-fg flex items-center justify-center"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <StepView
          step={step}
          state={state}
          existing={existing}
          hideToggle
          onToggle={() => {
            /* hidden — opening the modal implies enabled */
          }}
          onChange={updateValue}
          onTest={runTest}
          testing={testIntegration.isPending}
          clusters={clusters ?? []}
          onClusterChange={(id) =>
            setState((prev) => ({ ...prev, clusterTunnelId: id, test: null }))
          }
        />

        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-fg"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={runSave}
            disabled={putIntegration.isPending}
            className="text-xs h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan disabled:opacity-50 flex items-center gap-1.5"
          >
            {putIntegration.isPending && (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            )}
            {existing ? "Update" : "Connect"}
          </button>
        </div>
      </div>
    </div>
  );
}
