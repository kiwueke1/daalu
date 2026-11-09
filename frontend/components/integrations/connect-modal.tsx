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
import { api, IntegrationConfig } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";
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
  const toast = useToast();

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
      setState((prev) => ({ ...prev, error: errorText(e) }));
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
      toast.success(existing ? `${step.title} updated.` : `${step.title} connected.`);
      onSaved();
      onClose();
    } catch (e) {
      setState((prev) => ({ ...prev, error: errorText(e) }));
      toast.error(errorText(e));
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      size="xl"
      title={existing ? `Edit ${step.title}` : `Connect ${step.title}`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={runSave} pending={putIntegration.isPending}>
            {existing ? "Save changes" : `Connect ${step.title}`}
          </Button>
        </>
      }
    >
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
    </Dialog>
  );
}
