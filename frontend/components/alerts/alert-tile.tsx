"use client";

import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronRight } from "lucide-react";
import type { Alert } from "@/lib/api";
import { enrichAlertTitle } from "@/components/alerts/alert-title";
import { formatRelative } from "@/lib/utils";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { Button } from "@/components/ui/button";

/**
 * Map alert severity → the CSS variable that drives the tile's side
 * stripe. The variables come from globals.css and already adjust per
 * theme, so a single line picks up both light + dark.
 */
const STRIPE_VAR: Record<Alert["severity"], string> = {
  critical: "var(--critical)",
  warning: "var(--warning)",
  info: "var(--info)",
};

const STRIPE_GLOW: Record<Alert["severity"], string> = {
  critical: "color-mix(in srgb, var(--critical) 45%, transparent)",
  warning: "color-mix(in srgb, var(--warning) 40%, transparent)",
  info: "color-mix(in srgb, var(--info) 35%, transparent)",
};

interface AlertTileProps {
  alert: Alert;
  onAcknowledge?: () => void;
  onResolve?: () => void;
  /** A resolve for this alert is in flight. */
  resolving?: boolean;
}

export function AlertTile({
  alert,
  onAcknowledge,
  onResolve,
  resolving = false,
}: AlertTileProps) {
  const stripe = STRIPE_VAR[alert.severity];
  const glow = STRIPE_GLOW[alert.severity];

  // Markdown preview — trim to a single short paragraph so tiles stay
  // compact. The full body shows up on the detail page.
  const preview = alert.body.length > 240
    ? alert.body.slice(0, 240).trimEnd() + "…"
    : alert.body;

  return (
    // The tile is a div, not one big link: the action buttons must not sit
    // inside an <a> (a button in a link navigates on click).
    <div
      className="alert-tile group relative block w-full text-left rounded-2xl overflow-hidden"
      style={{
        // The faux-3D look is three layered shadows: an inner top edge
        // for the highlight, a soft drop shadow below the tile, and a
        // colored bloom along the severity stripe so it reads as a
        // glowing inlay rather than a flat rule.
        background: "var(--bg-card)",
        border: "1px solid var(--line)",
        boxShadow: `
          inset 5px 0 0 ${stripe},
          inset 6px 0 12px -6px ${glow},
          0 8px 24px -14px color-mix(in srgb, var(--text) 25%, transparent),
          -2px 0 18px -8px ${glow}
        `,
        transition: "transform 180ms ease, box-shadow 180ms ease",
      }}
    >
      <div className="relative pl-6 pr-4 py-3.5">
        <Link href={`/alerts/${alert.id}`} className="block">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mb-1.5">
          <span
            className="text-[11px] uppercase tracking-wider font-semibold"
            style={{ color: stripe }}
          >
            {alert.severity}
          </span>
          <span className="text-[11px] uppercase tracking-wider text-muted">
            {alert.module}
          </span>
          {alert.occurrence_count > 1 && (
            <span
              className="text-xs font-medium px-1.5 py-0.5 rounded-md border border-line text-fg"
              title={`This alert has fired ${alert.occurrence_count} times`}
            >
              fired {alert.occurrence_count.toLocaleString()}×
            </span>
          )}
          <span className="text-xs text-muted ml-auto">
            {formatRelative(alert.last_seen_at ?? alert.created_at)}
          </span>
        </div>

        <h3 className="font-medium text-[14px] leading-snug text-fg line-clamp-2">
          {enrichAlertTitle(alert)}
        </h3>

        <div className="text-[13px] text-muted mt-1.5 line-clamp-3 alert-tile-preview">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              // Strip block-level styling — we render markdown only so
              // bold / italic / code spans don't show literal **; the
              // tile is a preview, not a document.
              p: ({ children }) => <span>{children} </span>,
              h1: ({ children }) => <span>{children} </span>,
              h2: ({ children }) => <span>{children} </span>,
              h3: ({ children }) => <span>{children} </span>,
              ul: ({ children }) => <span>{children} </span>,
              li: ({ children }) => <span>{children}. </span>,
              code: ({ children }) => (
                <code className="font-mono text-[12px] bg-bg-elevated/60 rounded px-1">
                  {children}
                </code>
              ),
              a: ({ children }) => <span>{children}</span>,
            }}
          >
            {preview}
          </ReactMarkdown>
        </div>
        </Link>

        <div className="flex flex-wrap items-center gap-2 mt-3">
          {alert.status === "open" && onAcknowledge && (
            <Button
              size="sm"
              onClick={onAcknowledge}
              title="Mark that someone is looking at it. It stays open."
            >
              Acknowledge
            </Button>
          )}
          {alert.status !== "resolved" && onResolve && (
            <ConfirmButton
              label="Resolve"
              confirmLabel="Resolve alert"
              consequence="Closes the alert and stops the investigation."
              onConfirm={onResolve}
              pending={resolving}
            />
          )}
          <Link
            href={`/alerts/${alert.id}`}
            className="ml-auto min-h-8 text-xs flex items-center gap-1 transition-colors hover:underline"
            style={{ color: "var(--accent)" }}
          >
            Open details <ChevronRight aria-hidden className="h-3.5 w-3.5" />
          </Link>
        </div>
      </div>

      <style jsx>{`
        .alert-tile:hover {
          transform: translateY(-1px);
          box-shadow:
            inset 5px 0 0 ${stripe},
            inset 6px 0 16px -4px ${glow},
            0 14px 28px -14px color-mix(in srgb, var(--text) 30%, transparent),
            -2px 0 28px -6px ${glow} !important;
        }
        .alert-tile :global(.alert-tile-preview p) {
          margin: 0;
        }
      `}</style>
    </div>
  );
}
