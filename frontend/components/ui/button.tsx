"use client";

import { forwardRef } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * The one button. Label it with the verb and the consequence
 * ("Approve & run", "Save new version"), never "OK" or "Submit".
 *
 *   <Button onClick={save} pending={m.isPending}>Save new version</Button>
 *   <Button variant="secondary" icon={<RefreshCw />}>Re-run</Button>
 *   <Button variant="icon" aria-label="Close panel" icon={<X />} />
 *
 * - primary: the one main action on a screen or card.
 * - secondary: everything else.
 * - danger: removes or stops something (pair it with ConfirmButton's ask).
 * - ghost: low-emphasis inline actions.
 * - icon: square, icon only; `aria-label` is required.
 *
 * `disabledReason` explains why it's off (shown as the tooltip and read by
 * screen readers), so a greyed button never leaves the user guessing.
 * Press feedback (`motion-press`) confirms the click.
 */
type Variant = "primary" | "secondary" | "danger" | "ghost" | "icon";
type Size = "sm" | "md";

type Props = Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children"> & {
  variant?: Variant;
  size?: Size;
  icon?: React.ReactNode;
  pending?: boolean;
  disabledReason?: string;
  children?: React.ReactNode;
} & ({ variant: "icon"; "aria-label": string } | { variant?: Exclude<Variant, "icon"> });

export const buttonClass = (variant: Variant = "secondary", size: Size = "md") =>
  cn(
    "motion-press inline-flex items-center justify-center gap-1.5 rounded-lg font-medium whitespace-nowrap",
    "disabled:cursor-not-allowed",
    variant === "icon"
      ? size === "sm" ? "h-8 w-8" : "h-9 w-9"
      : size === "sm" ? "h-8 px-3 text-xs" : "h-9 px-3.5 text-sm",
    // Disabled reads as clearly off in both themes: no accent fill (which
    // still looks clickable in dark), a neutral surface and muted text that
    // keeps 4.5:1 so the label stays readable.
    variant === "primary" &&
      "bg-accent text-bg-base font-semibold shadow-glowSoft hover:brightness-110 disabled:bg-bg-elevated disabled:text-muted disabled:shadow-none disabled:border disabled:border-line-strong disabled:hover:brightness-100",
    variant !== "primary" && "disabled:opacity-50",
    variant === "secondary" &&
      "border border-line-strong bg-bg-elevated/40 text-fg hover:bg-bg-elevated hover:border-accent/40",
    variant === "danger" &&
      "border border-signal-critical/50 text-signal-critical hover:bg-signal-critical/10",
    variant === "ghost" && "text-muted hover:text-fg hover:bg-bg-elevated/60",
    variant === "icon" && "text-muted hover:text-fg hover:bg-bg-elevated/60"
  );

export const Button = forwardRef<HTMLButtonElement, Props>(function Button(
  { variant = "secondary", size = "md", icon, pending = false, disabledReason, className, children, disabled, type, title, ...rest },
  ref
) {
  const off = disabled || pending;
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      disabled={off}
      aria-busy={pending || undefined}
      title={off && disabledReason ? disabledReason : title}
      className={cn(buttonClass(variant, size), className)}
      {...rest}
    >
      {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : icon}
      {children}
      {off && disabledReason && <span className="sr-only">({disabledReason})</span>}
    </button>
  );
});
