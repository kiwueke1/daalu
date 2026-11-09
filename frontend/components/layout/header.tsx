"use client";

import { useState, useRef, useEffect } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Bell, LogOut, Menu, Moon, PauseCircle, ShieldCheck, Sun } from "lucide-react";
import { CommandBar } from "@/components/command-bar";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { useAutomationState } from "@/lib/use-automation-state";

export function Header({ onMobileNavOpen }: { onMobileNavOpen?: () => void } = {}) {
  const { user, logout } = useAuth();
  const { theme, toggle } = useTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, []);

  const initial = (user?.full_name || user?.email || "?").charAt(0).toUpperCase();

  // Status comes from the open alerts, never a fixed "all nominal". The
  // ["alerts", …] key is refreshed whenever an alert is acked or resolved.
  const { data: openAlerts } = useQuery({
    queryKey: ["alerts", "open-summary"],
    queryFn: () => api.alerts.list({ status: "open" }),
    refetchInterval: 60_000,
    enabled: !!user,
  });
  const critical = (openAlerts ?? []).filter((a) => a.severity === "critical").length;
  const warning = (openAlerts ?? []).filter((a) => a.severity === "warning").length;
  const status =
    openAlerts === undefined
      ? null
      : critical
        ? { tone: "var(--critical)", text: `${critical} critical alert${critical === 1 ? "" : "s"}` }
        : warning
          ? { tone: "var(--warning)", text: `${warning} warning${warning === 1 ? "" : "s"}` }
          : { tone: "var(--accent)", text: "No open alerts" };

  // Automation state, shown on every page: when someone pauses the AI or a
  // circuit breaker trips, everyone should see it without opening /autonomy.
  const { paused, tripped } = useAutomationState();
  const automation = paused
    ? { text: "Automation paused", tone: "var(--warning)" }
    : tripped
      ? { text: `${tripped} breaker${tripped === 1 ? "" : "s"} tripped`, tone: "var(--warning)" }
      : null;

  return (
    <header
      className="sticky top-0 z-30 h-[64px] backdrop-blur-xl relative"
      style={{
        background: "color-mix(in srgb, var(--bg-base) 70%, transparent)",
        boxShadow:
          "inset 0 -1px 0 var(--line), 0 8px 30px -10px rgba(0,0,0,0.4)",
      }}
    >
      {/* Bottom-edge hairline replaced by a soft horizontal sheen */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 bottom-0 h-px"
        style={{
          background:
            "linear-gradient(90deg, transparent, color-mix(in srgb, var(--text) 8%, transparent), transparent)",
        }}
      />
      <div className="h-full flex items-center gap-2 sm:gap-4 px-3 sm:px-4 md:px-6 lg:px-8 relative">
        {/* Hamburger — mobile only. Opens the sidebar drawer. Same
         *  frosted-chip vibe as the other header buttons so it doesn't
         *  feel bolted on. */}
        <button
          type="button"
          onClick={onMobileNavOpen}
          aria-label="Open menu"
          className="md:hidden h-9 w-9 rounded-lg flex items-center justify-center text-[color:var(--text)] hover:text-[color:var(--accent)] transition-colors"
          style={{ background: "color-mix(in srgb, var(--text) 3%, transparent)" }}
        >
          <Menu className="h-5 w-5" />
        </button>
        {status && (
          <Link
            href="/alerts"
            aria-label={status.text}
            className="flex items-center gap-2 text-xs text-muted hover:text-fg shrink-0"
          >
            {critical || warning ? (
              <AlertTriangle aria-hidden className="h-4 w-4" style={{ color: status.tone }} />
            ) : (
              <ShieldCheck aria-hidden className="h-4 w-4" style={{ color: status.tone }} />
            )}
            {/* Full text from md up; on a phone just the count, so the
                alert signal never disappears. */}
            <span className="hidden md:inline">{status.text}</span>
            {(critical > 0 || warning > 0) && (
              <span className="md:hidden font-semibold tabular-nums" style={{ color: status.tone }}>
                {critical || warning}
              </span>
            )}
          </Link>
        )}
        {automation && (
          <Link
            href="/autonomy"
            role="status"
            aria-label={`${automation.text}. Open the autonomy policy.`}
            className="motion-attention shrink-0 inline-flex items-center gap-1.5 h-7 px-2.5 rounded-full text-xs font-medium border"
            style={{
              color: automation.tone,
              borderColor: `color-mix(in srgb, ${automation.tone} 45%, var(--line))`,
              background: `color-mix(in srgb, ${automation.tone} 10%, transparent)`,
            }}
          >
            <PauseCircle aria-hidden className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">{automation.text}</span>
          </Link>
        )}
        <div className="flex-1 min-w-0 max-w-xl mx-auto">
          <CommandBar />
        </div>
        <div className="ml-auto flex items-center gap-1 sm:gap-2 text-muted">
          <button
            onClick={toggle}
            className="h-9 w-9 rounded-lg hover:text-[color:var(--text)] flex items-center justify-center transition-colors"
            style={{ background: "color-mix(in srgb, var(--text) 3%, transparent)" }}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          >
            {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </button>
          <Link
            href="/alerts"
            aria-label={
              openAlerts?.length ? `${openAlerts.length} open alerts` : "Alerts"
            }
            className="relative h-9 w-9 rounded-lg hover:text-[color:var(--text)] flex items-center justify-center transition-colors"
            style={{ background: "color-mix(in srgb, var(--text) 3%, transparent)" }}
          >
            <Bell className="h-4 w-4" />
            {(critical > 0 || warning > 0) && (
              <span
                aria-hidden
                className="absolute top-2 right-2 h-1.5 w-1.5 rounded-full motion-attention"
                style={{
                  background: critical ? "var(--critical)" : "var(--warning)",
                  boxShadow: `0 0 6px ${critical ? "var(--critical)" : "var(--warning)"}`,
                }}
              />
            )}
          </Link>
          <div className="relative" ref={ref}>
            <button
              onClick={() => setOpen((v) => !v)}
              className="h-8 w-8 rounded-full text-[11px] font-semibold flex items-center justify-center"
              style={{
                background:
                  "linear-gradient(140deg, var(--accent), color-mix(in srgb, var(--accent) 55%, var(--bg-base)))",
                color: "var(--bg-base)",
                boxShadow: "0 0 16px var(--accent-glow)",
              }}
              aria-label="Account menu"
            >
              {initial}
            </button>
            {open && (
              <div className="surface absolute right-0 mt-2 w-56 py-1 text-sm">
                <div className="px-3 py-2">
                  <div className="text-[color:var(--text)] truncate">
                    {user?.full_name || user?.email}
                  </div>
                  {user?.full_name && (
                    <div className="text-xs text-muted truncate">{user.email}</div>
                  )}
                </div>
                <div
                  aria-hidden
                  className="pointer-events-none mx-3 h-px"
                  style={{
                    background:
                      "linear-gradient(90deg, transparent, color-mix(in srgb, var(--text) 8%, transparent), transparent)",
                  }}
                />
                <button
                  onClick={() => logout()}
                  className="w-full px-3 py-2 flex items-center gap-2 text-left hover:text-[color:var(--accent)] transition-colors"
                >
                  <LogOut className="h-3.5 w-3.5" /> Sign out
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}
