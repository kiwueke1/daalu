"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Activity } from "lucide-react";
import { useEventStream } from "@/lib/sse";
import { formatRelative } from "@/lib/utils";

/**
 * Live operational feed. Glass surface; row separators are barely-
 * visible gradients (not hard rules) so the list feels like one
 * continuous illuminated plane.
 */
export function EventFeedCard({ module, max }: { module?: string; max?: number }) {
  const events = useEventStream({ module, cap: max ?? 30 });
  return (
    <div className="surface relative overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4" style={{ color: "var(--accent)" }} />
          <span className="text-sm font-medium">Live operational feed</span>
        </div>
        <span
          className="text-[10px] uppercase tracking-[0.18em] animate-shimmer"
          style={{ color: "var(--accent)" }}
        >
          listening
        </span>
      </div>
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-[44px] h-px"
        style={{
          background:
            "linear-gradient(90deg, transparent, rgba(255,255,255,0.06), transparent)",
        }}
      />
      <div className="max-h-[440px] overflow-auto scrollbar-thin">
        <AnimatePresence initial={false}>
          {events.length === 0 && (
            <div className="px-4 py-6 text-sm text-muted">
              No events yet — events stream in here as they happen.
            </div>
          )}
          {events.map((e) => (
            <motion.div
              key={e.id}
              initial={{ opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="relative flex items-start gap-3 px-4 py-2.5"
            >
              <div
                aria-hidden
                className="pointer-events-none absolute inset-x-2 bottom-0 h-px"
                style={{
                  background:
                    "linear-gradient(90deg, transparent, rgba(255,255,255,0.04), transparent)",
                }}
              />
              <div className="pt-1.5">
                <span
                  className="inline-block h-1.5 w-1.5 rounded-full"
                  style={{
                    background:
                      e.severity === "critical"
                        ? "var(--critical)"
                        : e.severity === "warning"
                        ? "var(--warning)"
                        : "var(--accent)",
                    boxShadow:
                      e.severity === "critical"
                        ? "0 0 10px var(--critical)"
                        : e.severity === "warning"
                        ? "0 0 10px var(--warning)"
                        : "0 0 10px var(--accent-bloom)",
                  }}
                />
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[13px] leading-snug truncate">{e.summary}</div>
                <div className="text-[10px] mt-0.5 text-muted flex items-center gap-2">
                  <span className="uppercase tracking-wider">{e.module}</span>
                  <span>·</span>
                  <span>{e.source}</span>
                  <span>·</span>
                  <span>{formatRelative(e.occurred_at)}</span>
                </div>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}
