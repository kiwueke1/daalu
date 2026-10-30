"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  AlertTriangle,
  Bot,
  Boxes,
  Code2,
  Factory,
  FileText,
  HelpCircle,
  Home,
  LayoutDashboard,
  LayoutGrid,
  ListChecks,
  Receipt,
  Settings,
  Workflow,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";

// Devices and Proposals used to be top-level entries; they're now tabs
// inside /operations (the SoT hub). The /devices/[id] and /proposals/[id]
// detail pages still resolve directly, but the list views are reached
// through /operations.
const NAV = [
  { href: "/", label: "Home", icon: Home },
  { href: "/operations", label: "Operations", icon: LayoutGrid },
  { href: "/agents", label: "Agents", icon: Bot },
  { href: "/automations", label: "Automations", icon: Workflow },
  { href: "/reports", label: "Reports", icon: FileText },
  { href: "/alerts", label: "Alerts", icon: AlertTriangle },
  { href: "/workflows", label: "Workflows", icon: ListChecks },
  // Integrations is now a tab inside /managed-infra (its standalone route
  // redirects there) — wiring external systems is managed-infra work.
  // Coding assistant — browser IDE + chat. The page itself surfaces
  // an eligibility CTA for tenants whose LLM tier doesn't include a
  // coding-purpose route. Keeping the entry always visible is the
  // simplest UX; otherwise eligible-after-upgrade tenants wouldn't
  // know the feature exists.
  { href: "/workspace", label: "Workspace", icon: Code2 },
  { href: "/ai-factory", label: "AI Factory", icon: Factory },
  // Topology is deliberately NOT a top-level entry: the fabric belongs to one
  // NV-CM management environment, and there's no sensible environment-less
  // version of the page. It's reached per-environment from Managed infra →
  // Network & servers → <environment> → Consoles.
  { href: "/dashboards", label: "Dashboards", icon: LayoutDashboard },
  // Clusters & observability, Network & servers, and Onboarding are now
  // tabs inside /managed-infra (their standalone routes still resolve).
  { href: "/managed-infra", label: "Managed infra", icon: Boxes },
  { href: "/billing", label: "Usage & Pricing", icon: Receipt },
];

const BOTTOM = [
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/help", label: "Help & Feedback", icon: HelpCircle },
];

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
        // Desktop: a normal 240px flex column that participates in the
        // page-level flex layout. Mobile: a fixed-position drawer that
        // slides in from the left, sitting above the backdrop.
        //
        // The sidebar must always be a positioned ancestor — its
        // circuit overlay and specular sheen children are absolutely
        // positioned inside it — so we switch between `fixed` on
        // mobile and `md:relative` on desktop (never `static`).
        className={cn(
          "sidebar-shell flex flex-col w-[260px] shrink-0 overflow-hidden",
          "fixed inset-y-0 left-0 z-50 transition-transform duration-[260ms] ease-out",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
          "md:relative md:translate-x-0 md:w-[240px] md:z-auto md:transition-none"
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
        <div className="px-4 pt-6 pb-5" aria-label="Daalu">
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

        {/* Primary nav. Each row gets a frosted chip behind it so the
         *  label reads cleanly over the photographic circuit pattern.
         *  The chip's backdrop-filter blurs the bit of pattern under
         *  the button while leaving the gaps between buttons untouched
         *  — the user keeps the wallpaper, the labels stay legible. */}
        <nav className="px-3 py-1 space-y-1">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active =
              pathname === href || (href !== "/" && pathname.startsWith(href));
            return (
              <Link
                key={label}
                href={href}
                onClick={onMobileClose}
                className={cn(
                  "group relative flex items-center gap-3 px-3 py-2.5 md:py-2 rounded-lg text-[13px] transition-all duration-[220ms] sidebar-nav-chip",
                  active
                    ? "text-[color:var(--text)] sidebar-nav-active"
                    : "text-[color:var(--text)] opacity-90 hover:opacity-100"
                )}
              >
                {!active && (
                  <span
                    aria-hidden
                    className="absolute inset-0 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity duration-[220ms] sidebar-nav-hover"
                  />
                )}
                <Icon
                  className={cn(
                    "relative h-4 w-4 transition-transform duration-[220ms]",
                    active && "scale-105"
                  )}
                  style={active ? { color: "var(--accent)" } : undefined}
                />
                <span className="relative flex-1 font-medium">{label}</span>
                {active && (
                  <span
                    aria-hidden
                    className="relative h-1.5 w-1.5 rounded-full"
                    style={{
                      background: "var(--accent)",
                      boxShadow:
                        "0 0 8px var(--accent-bloom), 0 0 16px var(--accent-glow)",
                    }}
                  />
                )}
              </Link>
            );
          })}
        </nav>

        {/* Bottom nav — separated by light, not by a hard rule. Same
         *  chip treatment so the bottom block reads at the same weight
         *  as the primary nav (currently quieter on purpose — these
         *  are secondary destinations). */}
        <div className="mt-auto px-3 pb-5 pt-5 space-y-1 relative">
          <div
            aria-hidden
            className="absolute inset-x-3 top-0 h-px pointer-events-none sidebar-divider"
          />
          {BOTTOM.map(({ href, label, icon: Icon }) => (
            <Link
              key={label}
              href={href}
              onClick={onMobileClose}
              className="group relative flex items-center gap-3 px-3 py-2.5 md:py-2 rounded-lg text-[13px] text-[color:var(--text)] opacity-80 hover:opacity-100 transition-opacity sidebar-nav-chip"
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

