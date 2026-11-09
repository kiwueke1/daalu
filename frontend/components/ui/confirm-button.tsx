"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A button for an action that changes or removes something. The first click
 * asks, in place: it shows what will happen and a confirm / cancel pair. No
 * modal, no layout jump. The question resets after a few seconds.
 *
 *   <ConfirmButton
 *     label="Resolve"
 *     confirmLabel="Resolve alert"
 *     consequence="Closes it even if the rule is still firing."
 *     onConfirm={() => resolve.mutate()}
 *     pending={resolve.isPending}
 *   />
 */
export function ConfirmButton({
  label,
  confirmLabel,
  consequence,
  onConfirm,
  pending = false,
  disabled = false,
  icon,
  tone = "default",
  className,
}: {
  label: React.ReactNode;
  confirmLabel: string;
  /** One short sentence: what happens when it runs. */
  consequence: string;
  onConfirm: () => void;
  pending?: boolean;
  disabled?: boolean;
  icon?: React.ReactNode;
  tone?: "default" | "danger";
  className?: string;
}) {
  const [asking, setAsking] = useState(false);
  const confirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!asking) return;
    // Keyboard users land on the confirm button; Escape backs out.
    confirmRef.current?.focus();
    const t = window.setTimeout(() => setAsking(false), 8000);
    return () => window.clearTimeout(t);
  }, [asking]);

  const color = tone === "danger" ? "var(--critical)" : "var(--accent)";

  if (!asking) {
    return (
      <button
        type="button"
        disabled={disabled || pending}
        onClick={(e) => {
          e.stopPropagation();
          setAsking(true);
        }}
        className={cn(
          "motion-press h-8 px-3 rounded-lg border border-line text-xs inline-flex items-center gap-1.5 whitespace-nowrap hover:bg-bg-elevated/60 disabled:opacity-50",
          className
        )}
      >
        {pending ? <Loader2 aria-hidden className="h-3.5 w-3.5 animate-spin" /> : icon}
        {label}
      </button>
    );
  }

  return (
    <span
      role="group"
      aria-label={confirmLabel}
      className="motion-enter inline-flex flex-wrap items-center gap-2 text-xs"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === "Escape") setAsking(false);
      }}
    >
      <span className="text-muted" aria-live="polite">{consequence}</span>
      <button
        type="button"
        onClick={() => {
          setAsking(false);
          onConfirm();
        }}
        ref={confirmRef}
        className="motion-press h-8 px-3 rounded-lg font-semibold inline-flex items-center gap-1.5 whitespace-nowrap"
        style={{ background: color, color: "var(--bg-base)" }}
      >
        {icon}
        {confirmLabel}
      </button>
      <button
        type="button"
        onClick={() => setAsking(false)}
        className="motion-press h-8 px-2 rounded-lg text-muted hover:text-fg"
      >
        Cancel
      </button>
    </span>
  );
}
