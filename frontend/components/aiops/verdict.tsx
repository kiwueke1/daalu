"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, CircleSlash, Loader2, X } from "lucide-react";
import { api, type Investigation, type Verdict } from "@/lib/api";
import { Chip, ErrorNote, TONE_VAR, fmtDateTime, inputCls, type Tone } from "./ui";

export const VERDICT_META: Record<Verdict, { label: string; tone: Tone }> = {
  correct: { label: "Correct", tone: "accent" },
  partial: { label: "Partly right", tone: "warning" },
  wrong: { label: "Wrong", tone: "critical" },
};

export function VerdictChip({ verdict }: { verdict: Verdict | null }) {
  if (!verdict) return <span className="text-muted text-xs">—</span>;
  const m = VERDICT_META[verdict];
  return <Chip tone={m.tone}>{m.label}</Chip>;
}

/**
 * Grade the AI's root cause: correct / partly right / wrong, with an optional
 * note. The verdict is the ground truth behind RCA agreement and evals.
 */
export function VerdictButtons({
  investigation,
  onSaved,
}: {
  investigation: Investigation;
  onSaved?: (inv: Investigation) => void;
}) {
  const qc = useQueryClient();
  const [note, setNote] = useState(investigation.verdict_note ?? "");
  const [picked, setPicked] = useState<Verdict | null>(null);
  const [editing, setEditing] = useState(!investigation.verdict);

  const save = useMutation({
    mutationFn: (v: Verdict) => api.aiops.investigations.verdict(investigation.id, v, note),
    onSuccess: (inv) => {
      setEditing(false);
      setPicked(null);
      qc.invalidateQueries({ queryKey: ["aiops-investigations"] });
      qc.invalidateQueries({ queryKey: ["aiops-investigation"] });
      qc.invalidateQueries({ queryKey: ["aiops-investigation-by-alert", inv.alert_id] });
      onSaved?.(inv);
    },
  });

  if (!editing && investigation.verdict) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted">Your team said:</span>
        <VerdictChip verdict={investigation.verdict} />
        <span className="text-muted">
          by {investigation.verdict_by || "someone"} · {fmtDateTime(investigation.verdict_at)}
        </span>
        {investigation.verdict_note && (
          <span className="text-fg/80 basis-full">&ldquo;{investigation.verdict_note}&rdquo;</span>
        )}
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="text-muted hover:text-fg underline underline-offset-2"
        >
          Change
        </button>
      </div>
    );
  }

  const buttons: { v: Verdict; icon: React.ReactNode }[] = [
    { v: "correct", icon: <Check className="h-3.5 w-3.5" /> },
    { v: "partial", icon: <CircleSlash className="h-3.5 w-3.5" /> },
    { v: "wrong", icon: <X className="h-3.5 w-3.5" /> },
  ];

  return (
    <div className="space-y-2">
      <div className="text-xs text-muted">
        Was the AI&apos;s root cause right? Your answer feeds the RCA-agreement
        metric on the Maturity page.
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {buttons.map(({ v, icon }) => {
          const m = VERDICT_META[v];
          const color = TONE_VAR[m.tone];
          const active = picked === v || (!picked && investigation.verdict === v);
          return (
            <button
              key={v}
              type="button"
              disabled={save.isPending}
              onClick={() => {
                setPicked(v);
                save.mutate(v);
              }}
              className="h-8 px-3 rounded-lg text-xs font-medium inline-flex items-center gap-1.5 disabled:opacity-50"
              style={{
                color: active ? "var(--bg-base)" : color,
                background: active ? color : `color-mix(in srgb, ${color} 10%, transparent)`,
                border: `1px solid color-mix(in srgb, ${color} 40%, transparent)`,
              }}
            >
              {save.isPending && picked === v ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                icon
              )}
              {m.label}
            </button>
          );
        })}
        {investigation.verdict && (
          <button
            type="button"
            onClick={() => setEditing(false)}
            className="text-xs text-muted hover:text-fg"
          >
            Cancel
          </button>
        )}
      </div>
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        maxLength={4000}
        placeholder="Optional note — what was actually wrong?"
        className={`${inputCls} w-full`}
      />
      <ErrorNote error={save.error} prefix="Couldn't save the verdict:" />
    </div>
  );
}
