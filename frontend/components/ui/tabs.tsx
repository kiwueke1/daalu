"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Page-level tab bar: the underline style from /managed-infra, /operations and
 * /settings, as one shared component. Use it when a page has several peer
 * sections of which the user reads one at a time.
 *
 * `badge` puts a small count or status next to the label, so something that
 * needs attention (e.g. an action waiting for approval) shows from any tab.
 */
export interface TabDef<T extends string> {
  id: T;
  label: string;
  /** Shorter label below the `sm` breakpoint, so more tabs fit on a phone. */
  shortLabel?: string;
  icon?: LucideIcon;
  badge?: React.ReactNode;
  /** Colour the badge with the accent (needs attention) instead of muted. */
  badgeTone?: "accent" | "muted";
}

export function TabBar<T extends string>({
  tabs,
  active,
  onChange,
  ariaLabel,
  trailing,
}: {
  tabs: TabDef<T>[];
  active: T;
  onChange: (id: T) => void;
  ariaLabel: string;
  /** Right-aligned content on the bar, e.g. a page action. */
  trailing?: React.ReactNode;
}) {
  // On a narrow screen the bar scrolls sideways; keep the active tab in view
  // (e.g. when a deep link opens a tab that starts off-screen).
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [active]);

  return (
    <div className="flex items-center gap-2 border-b border-line">
      <div
        ref={listRef}
        role="tablist"
        aria-label={ariaLabel}
        className="flex flex-1 min-w-0 items-center gap-1 overflow-x-auto"
      >
        {tabs.map(({ id, label, shortLabel, icon: Icon, badge, badgeTone = "muted" }) => {
          const selected = active === id;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-label={typeof badge === "string" || typeof badge === "number" ? `${label} (${badge})` : label}
              onClick={() => onChange(id)}
              className={cn(
                "relative inline-flex items-center gap-2 px-3 sm:px-4 py-2.5 text-sm whitespace-nowrap transition-colors",
                selected
                  ? "text-[color:var(--text)]"
                  : "text-muted hover:text-[color:var(--text)]"
              )}
            >
              {Icon && <Icon className={cn("h-4 w-4", shortLabel && "hidden sm:block")} />}
              {shortLabel ? (
                <>
                  <span className="sm:hidden">{shortLabel}</span>
                  <span className="hidden sm:inline">{label}</span>
                </>
              ) : (
                label
              )}
              {badge !== undefined && badge !== null && (
                <span
                  className="rounded-full px-1.5 py-px text-[10px] font-semibold leading-4"
                  style={
                    badgeTone === "accent"
                      ? {
                          background: "var(--accent)",
                          color: "var(--bg-base)",
                        }
                      : {
                          background: "var(--bg-elevated)",
                          color: "var(--text-muted)",
                          border: "1px solid var(--line)",
                        }
                  }
                >
                  {badge}
                </span>
              )}
              {selected && (
                <span
                  aria-hidden
                  className="absolute inset-x-2 -bottom-px h-0.5 rounded-full"
                  style={{
                    background: "var(--accent)",
                    boxShadow: "0 0 8px var(--accent-glow)",
                  }}
                />
              )}
            </button>
          );
        })}
      </div>
      {trailing && <div className="shrink-0 pb-1">{trailing}</div>}
    </div>
  );
}

/**
 * Active tab kept in `?tab=` so tabs are deep-linkable and survive a reload.
 * Needs a Suspense boundary above it (useSearchParams).
 */
export function useUrlTab<T extends string>(
  ids: readonly T[],
  fallback: T
): [T, (id: T) => void] {
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const urlTab = sp.get("tab");
  const valid = (v: string | null): v is T =>
    v !== null && (ids as readonly string[]).includes(v);
  const [tab, setTab] = useState<T>(valid(urlTab) ? urlTab : fallback);

  useEffect(() => {
    if (valid(urlTab) && urlTab !== tab) setTab(urlTab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlTab]);

  const select = (id: T) => {
    setTab(id);
    const params = new URLSearchParams(Array.from(sp.entries()));
    params.set("tab", id);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  };
  return [tab, select];
}
