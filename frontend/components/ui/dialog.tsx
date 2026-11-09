"use client";

import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A modal dialog or side drawer that behaves the same everywhere:
 * role="dialog" + aria-modal + a labelled title, Escape and the backdrop
 * close it, Tab stays inside it, focus moves in on open and returns to
 * whatever opened it on close, and the page behind doesn't scroll.
 *
 *   <Dialog open={open} onClose={() => setOpen(false)} title="Connect AWS"
 *           description="Daalu gets read-only access to the account.">
 *     …form…
 *   </Dialog>
 *
 * Motion: the backdrop fades and the panel settles in (`motion-dialog`), or
 * slides from the right for `variant="drawer"`, so the eye follows where it
 * came from; `variant="sheet"` rises from the bottom edge, for phone panels
 * within thumb reach. Off under prefers-reduced-motion.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  variant = "modal",
  size = "md",
  className,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  variant?: "modal" | "drawer" | "sheet";
  size?: "sm" | "md" | "lg" | "xl";
  className?: string;
  children: React.ReactNode;
  /** Action row, pinned to the bottom (primary action last). */
  footer?: React.ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";

    // Focus the first field (or the panel) once it's on screen.
    const t = window.setTimeout(() => {
      const panel = panelRef.current;
      if (!panel) return;
      // A sheet is a phone panel: focusing its field would throw up the keyboard
      // over what the person opened it to read.
      const first =
        variant === "sheet"
          ? null
          : panel.querySelector<HTMLElement>(
              "[autofocus], input:not([disabled]), select:not([disabled]), textarea:not([disabled])"
            );
      (first ?? panel).focus();
    }, 0);

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const items = Array.from(
        panelRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )
      ).filter((el) => el.offsetParent !== null);
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey, true);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = overflow;
      opener?.focus?.();
    };
  }, [open, onClose, variant]);

  if (!open || typeof document === "undefined") return null;

  const widths = { sm: "max-w-sm", md: "max-w-lg", lg: "max-w-2xl", xl: "max-w-4xl" };
  const drawer = variant === "drawer";
  const sheet = variant === "sheet";

  return createPortal(
    <div
      className={cn(
        "fixed inset-0 z-50 flex",
        drawer ? "justify-end" : sheet ? "items-end justify-center" : "items-center justify-center p-4"
      )}
    >
      <div
        aria-hidden
        className="motion-overlay absolute inset-0 bg-black/50 backdrop-blur-[2px]"
        onClick={onClose}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={cn(
          "relative flex flex-col bg-bg-card border border-line shadow-2xl focus:outline-none",
          drawer
            ? "motion-drawer h-full w-full sm:max-w-xl border-y-0 border-r-0"
            : sheet
              ? "motion-sheet w-full sm:max-w-xl h-[76dvh] rounded-t-2xl border-b-0"
              : cn("motion-dialog w-full max-h-[calc(100dvh-2rem)] rounded-2xl", widths[size]),
          className
        )}
      >
        <div className="flex items-start gap-3 px-5 pt-4 pb-3 border-b border-line">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-base font-semibold leading-snug">
              {title}
            </h2>
            {description && (
              <p id={descId} className="mt-1 text-sm text-muted">
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="motion-press -mr-1 h-9 w-9 shrink-0 rounded-lg inline-flex items-center justify-center text-muted hover:text-fg hover:bg-bg-elevated/60"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <div className="flex flex-wrap items-center justify-end gap-2 px-5 py-3 border-t border-line">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body
  );
}
