"use client";

import { ArrowDownRight, ArrowUpRight } from "lucide-react";

/**
 * One stat tile. Uses the illuminated-glass `.surface` so it floats
 * slightly above the body's ambient lighting — no obvious border.
 */
export function MetricsCard({
  label,
  value,
  delta,
  hint,
  intent,
}: {
  label: string;
  value: string | number;
  delta?: string;
  hint?: string;
  intent?: "positive" | "negative" | "neutral";
}) {
  const positive =
    intent === "positive" || (delta && !delta.startsWith("-") && intent !== "negative");
  return (
    <div className="surface relative overflow-hidden p-4">
      <div className="text-[10px] uppercase tracking-[0.18em] text-muted">{label}</div>
      <div className="flex items-end gap-3 mt-2">
        <div className="text-[28px] leading-none font-semibold tracking-tight">
          {value}
        </div>
        {delta && (
          <div
            className="text-[11px] flex items-center gap-0.5 mb-1"
            style={{ color: positive ? "var(--accent)" : "var(--critical)" }}
          >
            {positive ? (
              <ArrowUpRight className="h-3 w-3" />
            ) : (
              <ArrowDownRight className="h-3 w-3" />
            )}
            {delta}
          </div>
        )}
      </div>
      {hint && <div className="text-[11px] text-muted mt-2 leading-snug">{hint}</div>}
    </div>
  );
}
