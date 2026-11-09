"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * Whether the AI can currently act: paused by the kill switch, or held back by
 * tripped circuit breakers. Shared by the header pill and the sidebar badge so
 * the two can't disagree, and keyed like the Autonomy page, so flipping the
 * switch there updates both.
 */
export function useAutomationState() {
  const { user } = useAuth();
  const { data: policy } = useQuery({
    queryKey: ["aiops-policy"],
    queryFn: () => api.aiops.policy.get(),
    refetchInterval: 60_000,
    enabled: !!user,
    retry: false,
  });
  const now = Date.now();
  const tripped = Object.values(policy?.breaker ?? {}).filter((until) => Date.parse(until) > now).length;
  return { paused: !!policy?.kill_switch, tripped };
}
