"use client";

import { useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  AlertTriangle,
  Bot,
  BookMarked,
  Boxes,
  Code2,
  Factory,
  FileText,
  FlaskConical,
  Gauge,
  HelpCircle,
  Home,
  LayoutDashboard,
  LayoutGrid,
  ListChecks,
  Receipt,
  ScrollText,
  SearchCheck,
  Settings,
  SlidersHorizontal,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useAutomationState } from "@/lib/use-automation-state";
import { cn } from "@/lib/utils";

type Tone = "critical" | "warning" | "accent" | "muted";
type Badge = { text: string; tone: Tone; attention?: boolean };
type Item = { href: string; label: string; icon: LucideIcon };

// The sidebar is grouped by what you came to do, most urgent first.
//
// Devices and Proposals are tabs inside /operations (the SoT hub); Integrations,
// Clusters & observability, Network & servers and Onboarding are tabs inside
// /managed-infra. Their standalone routes still resolve. Topology is deliberately
// not an entry: it belongs to one NV-CM environment and is reached from Managed
// infra → Network & servers → <environment> → Consoles. Workspace stays visible
// for every tenant; the page itself explains how to become eligible.
const HOME: Item = { href: "/", label: "Home", icon: Home };

const GROUPS: { label: string; items: Item[] }[] = [
  {
    label: "Respond",
    items: [
      { href: "/alerts", label: "Alerts", icon: AlertTriangle },
      { href: "/investigations", label: "Investigations", icon: SearchCheck },
      { href: "/workflows", label: "Workflows", icon: ListChecks },
    ],
  },
  {
    label: "Automate",
    items: [
      { href: "/automations", label: "Automations", icon: Workflow },
      { href: "/agents", label: "Agents", icon: Bot },
      { href: "/runbooks", label: "Runbooks", icon: BookMarked },
      { href: "/autonomy", label: "Autonomy policy", icon: SlidersHorizontal },
    ],
  },
  {
    label: "Infrastructure",
    items: [
      { href: "/managed-infra", label: "Managed infra", icon: Boxes },
      { href: "/ai-factory", label: "AI Factory", icon: Factory },
      { href: "/operations", label: "Operations", icon: LayoutGrid },
      { href: "/workspace", label: "Workspace", icon: Code2 },
    ],
  },
  {
    label: "Insights",
    items: [
      { href: "/dashboards", label: "Dashboards", icon: LayoutDashboard },
      { href: "/reports", label: "Reports", icon: FileText },
      // AI-Ops Maturity Model (docs/design/ai-ops-maturity.md §12).
      { href: "/maturity", label: "Maturity", icon: Gauge },
      { href: "/evals", label: "Evals", icon: FlaskConical },
      { href: "/audit", label: "Audit log", icon: ScrollText },
    ],
  },
];

const BOTTOM: Item[] = [
  { href: "/billing", label: "Usage & Pricing", icon: Receipt },
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/help", label: "Help & Feedback", icon: HelpCircle },
];

/**
 * What needs someone, keyed by route, so it shows from any page. Only things a
 * person can act on get a badge; nothing shows until the query has answered, and
 * a query the user can't read (403) just means no badge.
 */
function useNavBadges(): Record<string, Badge | undefined> {
  const { user } = useAuth();
  const { paused, tripped } = useAutomationState();
  // Same keys as the header and the rail, so these share fetches with what's
  // already on screen. 30 days is the Investigations page's default window.
  const { data: alerts } = useQuery({
    queryKey: ["alerts", "open-summary"],
    queryFn: () => api.alerts.list({ status: "open" }),
    refetchInterval: 60_000,
    enabled: !!user,
  });
  const { data: runs } = useQuery({
    queryKey: ["workflow-runs", "right-panel"],
    queryFn: () => api.workflows.runs(),
    refetchInterval: 60_000,
    enabled: !!user,
  });
  const { data: ungraded } = useQuery({
    queryKey: ["aiops-investigations", "nav-needs-verdict"],
    queryFn: () => api.aiops.investigations.list({ days: 30, needs_verdict: true }),
    refetchInterval: 60_000,
    enabled: !!user,
    retry: false,
  });

  const critical = (alerts ?? []).filter((a) => a.severity === "critical").length;
  const warning = (alerts ?? []).filter((a) => a.severity === "warning").length;
  const toApprove = (runs ?? []).filter((r) => r.status === "waiting_for_approval").length;
  const toGrade = ungraded?.length ?? 0;

  return {
    "/alerts": critical
      ? { text: `${critical} critical`, tone: "critical", attention: true }
      : warning
        ? { text: `${warning} warning${warning === 1 ? "" : "s"}`, tone: "warning" }
        : undefined,
    "/investigations": toGrade ? { text: `${toGrade} to grade`, tone: "muted" } : undefined,
    "/workflows": toApprove ? { text: `${toApprove} to approve`, tone: "accent", attention: true } : undefined,
    "/autonomy": paused
      ? { text: "Paused", tone: "warning", attention: true }
      : tripped
        ? { text: `${tripped} tripped`, tone: "warning", attention: true }
        : undefined,
  };
}

/**
 * The sidebar reads as a glowing operational conduit, not a panel.
 *
 * Layered, back to front:
 *   1. Deep graphite gradient.
 *   2. Large soft emerald glow anchored top-left, faintly washing down.
 *   3. Atmospheric circuitry — blurred SVG traces, so the user *feels*
 *      the wiring without ever seeing line art.
 *   4. Sharp surface highlight on the top edge (specular).
 *
 * Active nav item: emerald soft fill + 0px border (no hard ring), inner
 * top-edge glow, and a bloom dot. Inactive items get a faint hover
 * sheen only.
 */
export function Sidebar({
  mobileOpen = false,
  onMobileClose,
}: {
  mobileOpen?: boolean;
  onMobileClose?: () => void;
} = {}) {
  const pathname = usePathname();
  const badges = useNavBadges();

  // The drawer is an overlay on a phone: Escape closes it, like every other one.
  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onMobileClose?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mobileOpen, onMobileClose]);

  return (
    <>
      {/* Mobile backdrop — only present when the drawer is open. Tapping
       *  it dismisses the drawer. Hidden on md+ where the sidebar is
       *  a permanent flex item. */}
      <div
        aria-hidden={!mobileOpen}
        onClick={onMobileClose}
        className={cn(
          "md:hidden fixed inset-0 z-40 bg-black/55 backdrop-blur-[2px] transition-opacity duration-[220ms]",
          mobileOpen ? "opacity-100" : "pointer-events-none opacity-0"
        )}
      />
      <aside
        // Desktop: a normal 260px flex column that participates in the
        // page-level flex layout. Mobile: a fixed-position drawer that
        // slides in from the left, sitting above the backdrop.
        //
        // The sidebar must always be a positioned ancestor — its
        // circuit overlay and specular sheen children are absolutely
        // positioned inside it — so we switch between `fixed` on
        // mobile and `md:sticky` on desktop (never `static`).
        className={cn(
          "sidebar-shell flex flex-col w-[260px] shrink-0 overflow-hidden",
          "fixed inset-y-0 left-0 z-50 [transition:transform_260ms_ease-out,visibility_260ms]",
          // `invisible` keeps a closed drawer's links out of the keyboard's tab order.
          mobileOpen ? "translate-x-0" : "-translate-x-full max-md:invisible",
          // Stays on screen while a long page scrolls, so Settings is always reachable.
          "md:sticky md:top-0 md:h-dvh md:translate-x-0 md:z-auto md:transition-none"
        )}
      >
      {/* Photographic circuit overlay — paired light + dark variants
       *  rendered side-by-side and revealed by the theme attribute on
       *  <html>. Each image sits behind a graphite/paper tint so the
       *  nav stays readable. */}
      <SidebarCircuitry />

      {/* Top edge specular — 1px horizontal sheen, sells the polish. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-px sidebar-specular"
      />

      <div className="relative z-10 flex flex-col h-full">
        {/* Mobile-only close affordance — sits in the corner of the
         *  drawer when it's open on phones. md+ ignores it entirely. */}
        <button
          type="button"
          onClick={onMobileClose}
          aria-label="Close menu"
          className="md:hidden absolute top-3 right-3 z-20 h-9 w-9 rounded-lg flex items-center justify-center text-[color:var(--text)] opacity-80 hover:opacity-100 transition-opacity"
          style={{
            background: "rgba(255,255,255,0.04)",
            boxShadow: "inset 0 1px 0 rgba(255,255,255,0.06)",
          }}
        >
          <X className="h-4 w-4" />
        </button>

        {/* Logo — live text, not an image. The old PNG wordmark carried
         *  its own opaque rectangular background (a second circuit
         *  render) that masked the sidebar's real circuit wallpaper and
         *  baked the tagline in at an unreadable size. Rendering the
         *  wordmark and tagline as actual text means the background is
         *  transparent (the sidebar pattern shows through the gaps),
         *  each line is sized independently, and both colours come from
         *  the theme tokens — so light/dark "just work" with no asset
         *  swap. The brand face is Quicksand (var(--font-brand), wired
         *  in app/layout.tsx). */}
        <div className="px-4 pt-5 pb-3" aria-label="Daalu">
          <div
            className="leading-none text-[34px] font-bold tracking-[-0.02em] text-[color:var(--text)]"
            style={{ fontFamily: "var(--font-brand)" }}
          >
            daalu
          </div>
          <div
            className="mt-1.5 text-[12.5px] font-medium tracking-[0.04em] text-[color:var(--accent)]"
            style={{ fontFamily: "var(--font-brand)" }}
          >
            intelligent IT operations
          </div>
        </div>

        {/* The nav scrolls on short screens; the bottom block stays pinned. */}
        <nav aria-label="Main" className="flex-1 min-h-0 overflow-y-auto px-2 pb-2">
          <NavItem {...HOME} pathname={pathname} onClick={onMobileClose} />
          {GROUPS.map((group) => (
            <div key={group.label} role="group" aria-labelledby={`nav-${group.label}`} className="mt-3">
              <div
                id={`nav-${group.label}`}
                className="px-2.5 pb-1 text-[11px] uppercase tracking-wider font-semibold text-muted"
              >
                {group.label}
              </div>
              <div className="space-y-0.5">
                {group.items.map((item) => (
                  <NavItem
                    key={item.href}
                    {...item}
                    badge={badges[item.href]}
                    pathname={pathname}
                    onClick={onMobileClose}
                  />
                ))}
              </div>
            </div>
          ))}
        </nav>

        {/* Bottom nav: secondary destinations, quieter on purpose. */}
        <div className="shrink-0 px-2 py-2 space-y-0.5 relative">
          <div
            aria-hidden
            className="absolute inset-x-2 top-0 h-px pointer-events-none sidebar-divider"
          />
          {BOTTOM.map(({ href, label, icon: Icon }) => (
            <Link
              key={label}
              href={href}
              onClick={onMobileClose}
              aria-current={pathname.startsWith(href) ? "page" : undefined}
              className="group relative flex items-center gap-2 px-2.5 h-11 md:h-7 rounded-lg text-[13px] text-muted hover:text-[color:var(--text)] transition-colors"
            >
              <span
                aria-hidden
                className="absolute inset-0 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity duration-[220ms] sidebar-nav-hover"
              />
              <Icon className="relative h-4 w-4" />
              <span className="relative">{label}</span>
            </Link>
          ))}
        </div>
      </div>
      </aside>
    </>
  );
}

const BADGE_TONES: Record<Tone, string> = {
  critical: "var(--critical)",
  warning: "var(--warning)",
  accent: "var(--accent)",
  muted: "var(--text-muted)",
};

function NavItem({
  href,
  label,
  icon: Icon,
  badge,
  pathname,
  onClick,
}: Item & {
  badge?: Badge;
  pathname: string;
  onClick?: () => void;
}) {
  const active = pathname === href || (href !== "/" && pathname.startsWith(href));
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      onClick={onClick}
      className={cn(
        "group relative flex items-center gap-2 px-2.5 h-11 md:h-8 rounded-lg text-[13px] text-[color:var(--text)] transition-colors",
        active && "sidebar-nav-active"
      )}
    >
      {!active && (
        <span
          aria-hidden
          className="absolute inset-0 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity sidebar-nav-hover"
        />
      )}
      <Icon
        aria-hidden
        className="relative h-4 w-4 shrink-0"
        style={active ? { color: "var(--accent)" } : undefined}
      />
      <span className="relative flex-1 min-w-0 truncate font-medium">{label}</span>
      {badge && (
        <span
          className={cn(
            "relative shrink-0 rounded-full px-1.5 py-px text-xs font-semibold tabular-nums whitespace-nowrap",
            badge.attention && "motion-attention"
          )}
          style={{
            color: BADGE_TONES[badge.tone],
            // Opaque, so the text keeps its contrast over the circuit wallpaper.
            background: `color-mix(in srgb, ${BADGE_TONES[badge.tone]} 10%, var(--bg-card))`,
          }}
        >
          {badge.text}
        </span>
      )}
    </Link>
  );
}

/**
 * Sidebar background — two photographic circuit renders (one dark, one
 * light) stacked on top of each other. Each is gated by the theme
 * attribute so only one is visible at a time. A faint tint layer sits
 * on top to keep the nav text readable, and a top-to-bottom alpha mask
 * fades the image into the surrounding chrome rather than ending it
 * abruptly at the sidebar edge.
 */
function SidebarCircuitry() {
  // The .sidebar-bg / .sidebar-tint styles live in globals.css now
  // (so the right-side AlertChatRail can reuse the same look). This
  // component is just markup.
  return (
    <>
      <div
        aria-hidden
        className="sidebar-bg sidebar-bg-dark pointer-events-none absolute inset-0"
      />
      <div
        aria-hidden
        className="sidebar-bg sidebar-bg-light pointer-events-none absolute inset-0"
      />
      <div aria-hidden className="sidebar-tint pointer-events-none absolute inset-0" />
    </>
  );
}

