"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles, Check, X } from "lucide-react";
import { api, Recommendation } from "@/lib/api";
import { formatRelative } from "@/lib/utils";

export function AIRecommendationList({ module }: { module?: string }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["recommendations", module ?? "all", "pending"],
    queryFn: () => api.recommendations.list({ module, status: "pending" }),
    refetchInterval: 20_000,
  });

  const approve = useMutation({
    mutationFn: (id: string) => api.recommendations.approve(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["recommendations"] }),
  });
  const dismiss = useMutation({
    mutationFn: (id: string) => api.recommendations.dismiss(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["recommendations"] }),
  });

  if (!data || data.length === 0) return null;

  return (
    <div className="space-y-3">
      {data.slice(0, 4).map((r) => (
        <RecommendationCard
          key={r.id}
          rec={r}
          onApprove={() => approve.mutate(r.id)}
          onDismiss={() => dismiss.mutate(r.id)}
        />
      ))}
    </div>
  );
}

function RecommendationCard({
  rec,
  onApprove,
  onDismiss,
}: {
  rec: Recommendation;
  onApprove: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="surface surface-bloom relative overflow-hidden p-4">
      <div className="relative">
        <div className="flex items-center justify-between mb-1">
          <span
            className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.20em]"
            style={{ color: "var(--accent)" }}
          >
            <Sparkles className="h-3 w-3 animate-shimmer" /> AI recommendation
          </span>
          <span className="text-[10px] text-muted uppercase tracking-wider">
            {rec.module} · {Math.round(rec.confidence * 100)}% confident
          </span>
        </div>
        <h4 className="font-medium leading-snug">{rec.title}</h4>
        <p className="text-sm text-muted mt-1 leading-relaxed">{rec.rationale}</p>
        <p className="text-xs mt-2">
          <span className="text-muted">Suggested:&nbsp;</span>
          <span style={{ color: "var(--accent)" }}>{rec.suggested_action}</span>
        </p>
        <div className="flex items-center justify-between mt-3">
          <span className="text-[10px] text-muted">
            {formatRelative(rec.created_at)}
          </span>
          <div className="flex gap-2">
            <button
              onClick={onDismiss}
              className="h-8 px-3 rounded-lg text-xs text-muted hover:text-[color:var(--text)] flex items-center gap-1 transition-colors"
              style={{
                background: "rgba(255,255,255,0.02)",
                boxShadow: "inset 0 1px 0 rgba(255,255,255,0.04)",
              }}
            >
              <X className="h-3 w-3" /> Dismiss
            </button>
            <button
              onClick={onApprove}
              className="h-8 px-3 rounded-lg text-xs flex items-center gap-1 transition-transform hover:scale-[1.02]"
              style={{
                background:
                  "linear-gradient(180deg, color-mix(in srgb, var(--accent) 95%, white) 0%, var(--accent) 100%)",
                color: "#031814",
                boxShadow:
                  "inset 0 1px 0 rgba(255,255,255,0.35), 0 0 22px var(--accent-glow)",
              }}
            >
              <Check className="h-3 w-3" /> Approve
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
