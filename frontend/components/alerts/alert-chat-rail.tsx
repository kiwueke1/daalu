"use client";

import { useState } from "react";
import { ChevronLeft, ChevronRight, MessageSquare } from "lucide-react";
import { AlertChat } from "./alert-chat";

/**
 * Right-side rail for the alert detail page.
 *
 * Mirrors the photographic circuit pattern from the left sidebar so
 * the alert page feels framed on both sides. Two visual states:
 *
 * - Collapsed (default) — a thin 56px column. Just the circuit
 *   pattern, a vertical "Remediation copilot" label, and a chevron
 *   button to expand. Lets the operator focus on the tiles in the
 *   centre.
 * - Expanded — 420px column hosting the AlertChat. Click the
 *   chevron again to collapse.
 */
export function AlertChatRail({ alertId }: { alertId: string }) {
  const [open, setOpen] = useState(false);

  return (
    <aside
      className={`sidebar-shell chat-rail hidden lg:flex flex-col shrink-0 relative overflow-hidden transition-[width] duration-200 ease-out ${
        open ? "w-[420px]" : "w-[56px]"
      }`}
    >
      {/* Same per-theme circuit overlay as the left sidebar — the
       *  shared sidebar-bg-* classes pick up the global styles in
       *  globals.css automatically. */}
      <div
        aria-hidden
        className="sidebar-bg sidebar-bg-dark pointer-events-none absolute inset-0"
      />
      <div
        aria-hidden
        className="sidebar-bg sidebar-bg-light pointer-events-none absolute inset-0"
      />
      <div
        aria-hidden
        className="sidebar-tint pointer-events-none absolute inset-0"
      />

      <button
        type="button"
        onClick={() => setOpen(!open)}
        title={open ? "Collapse remediation chat" : "Open remediation chat"}
        aria-label={open ? "Collapse remediation chat" : "Open remediation chat"}
        aria-expanded={open}
        className="motion-press chat-rail-chevron relative z-10 self-end m-2 h-8 w-8 rounded-md inline-flex items-center justify-center"
      >
        {open ? (
          <ChevronRight aria-hidden className="h-4 w-4" />
        ) : (
          <ChevronLeft aria-hidden className="h-4 w-4" />
        )}
      </button>

      {!open && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="chat-rail-toggle relative z-10 mx-2 mt-1 flex-1 flex flex-col items-center justify-start gap-3 pt-3 pb-4 rounded-xl"
          aria-label="Open remediation chat"
        >
          <span
            aria-hidden
            className="flex h-9 w-9 items-center justify-center rounded-full chat-rail-msg-chip"
          >
            <MessageSquare
              className="h-4 w-4"
              style={{ color: "var(--accent)" }}
            />
          </span>
          <span
            className="text-[11px] uppercase tracking-[0.20em] font-semibold text-[color:var(--text)]"
            style={{ writingMode: "vertical-rl", textOrientation: "mixed" }}
          >
            Remediation copilot
          </span>
        </button>
      )}

      {/* Theme tokens only: --surface-top/--surface-bot and the hairlines
          already differ per theme, so no light-mode override is needed. */}
      <style jsx>{`
        .chat-rail-chevron {
          background: linear-gradient(180deg, var(--surface-top) 0%, var(--surface-bot) 100%);
          color: var(--text);
          box-shadow:
            inset 0 1px 0 var(--hairline-strong),
            inset 0 0 0 1px color-mix(in srgb, var(--accent) 28%, transparent),
            0 0 14px -4px var(--accent-glow);
          backdrop-filter: blur(4px);
          -webkit-backdrop-filter: blur(4px);
          transition:
            box-shadow 160ms ease,
            transform 160ms ease;
        }
        .chat-rail-chevron:hover {
          transform: translateY(-1px);
          box-shadow:
            inset 0 1px 0 var(--hairline-strong),
            inset 0 0 0 1px color-mix(in srgb, var(--accent) 55%, transparent),
            0 0 22px -4px var(--accent-glow);
        }

        .chat-rail-toggle {
          background: linear-gradient(
            180deg,
            color-mix(in srgb, var(--surface-top) 70%, transparent) 0%,
            color-mix(in srgb, var(--surface-bot) 75%, transparent) 100%
          );
          backdrop-filter: blur(4px) saturate(1.05);
          -webkit-backdrop-filter: blur(4px) saturate(1.05);
          box-shadow:
            inset 0 1px 0 var(--hairline),
            inset 0 0 0 1px color-mix(in srgb, var(--accent) 16%, var(--hairline));
          transition:
            box-shadow 220ms ease,
            transform 220ms ease;
        }
        .chat-rail-toggle:hover {
          box-shadow:
            inset 0 1px 0 var(--hairline-strong),
            inset 0 0 0 1px color-mix(in srgb, var(--accent) 38%, var(--hairline-strong)),
            0 0 18px -4px var(--accent-glow);
        }

        .chat-rail-msg-chip {
          background: color-mix(in srgb, var(--accent) 22%, transparent);
          box-shadow:
            inset 0 0 0 1px color-mix(in srgb, var(--accent) 40%, transparent),
            0 0 18px -4px var(--accent-glow);
        }
      `}</style>

      {open && (
        <div className="relative z-10 flex flex-col flex-1 min-h-0">
          <AlertChat alertId={alertId} />
        </div>
      )}
    </aside>
  );
}
