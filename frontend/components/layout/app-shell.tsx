"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { Sidebar } from "@/components/layout/sidebar";
import { Header } from "@/components/layout/header";
import { RightPanel } from "@/components/layout/right-panel";
import { AssistantSheet } from "@/components/layout/assistant-sheet";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

// Exact-match public routes (no auth, no app chrome).
const PUBLIC_ROUTES = new Set(["/login", "/signup", "/verify-email"]);

// Public route *prefixes* — for dynamic segments like /accept-invite/<token>.
// These must bypass the authenticated shell entirely; otherwise the invitee
// (who has no session yet) renders the full app, which fires authed API calls
// that 401 and crash the page with a client-side exception.
const PUBLIC_ROUTE_PREFIXES = ["/accept-invite"];

// Signed-in screens that need the user's full attention (a security consent
// step): no sidebar, header or rail around them, like the login page.
const FOCUSED_ROUTES = new Set(["/cli/activate"]);

function isPublicRoute(pathname: string): boolean {
  return (
    PUBLIC_ROUTES.has(pathname) ||
    PUBLIC_ROUTE_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`))
  );
}

/**
 * Routes that own their right rail and don't want the generic
 * AI Assistant + Notifications + System health stack rendered next to
 * them. Currently just the alert detail page, which renders its own
 * remediation-chat rail in place of the global one.
 */
function ownsRightRail(pathname: string): boolean {
  // Match /alerts/<anything-but-/>  — list page (/alerts) keeps the
  // generic right panel.
  return /^\/alerts\/[^/]+/.test(pathname);
}

/** Full-screen tools with no room for the floating Assistant button. */
function hidesAssistantButton(pathname: string): boolean {
  return ownsRightRail(pathname) || pathname.startsWith("/workspace");
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { loading } = useAuth();
  // Mobile nav drawer state. Lives here so both Header (the hamburger
  // trigger) and Sidebar (the drawer) can share it. Auto-closes on
  // route change so tapping a link from the drawer dismisses it.
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  useEffect(() => {
    setMobileNavOpen(false);
  }, [pathname]);
  // Lock body scroll while the drawer is open so the page beneath
  // doesn't bounce when the user swipes inside the nav.
  useEffect(() => {
    if (typeof document === "undefined") return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = mobileNavOpen ? "hidden" : prev;
    return () => {
      document.body.style.overflow = prev;
    };
  }, [mobileNavOpen]);

  if (isPublicRoute(pathname)) {
    return <>{children}</>;
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-muted text-sm">
        Loading…
      </div>
    );
  }

  if (FOCUSED_ROUTES.has(pathname)) {
    return (
      <main id="main" className="min-h-screen flex items-center justify-center px-4 py-10">
        <div className="w-full max-w-lg motion-enter">{children}</div>
      </main>
    );
  }

  const skipGlobalRail = ownsRightRail(pathname);
  const showAssistantButton = !hidesAssistantButton(pathname);

  return (
    <div className="flex min-h-screen">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <Sidebar
        mobileOpen={mobileNavOpen}
        onMobileClose={() => setMobileNavOpen(false)}
      />
      <div className="flex-1 min-w-0">
        <Header onMobileNavOpen={() => setMobileNavOpen(true)} />
        <div className="flex">
          <main
            id="main"
            tabIndex={-1}
            className={cn(
              "flex-1 min-w-0 px-4 py-5 md:px-6 md:py-6 lg:px-10 lg:py-8 focus:outline-none",
              // Room to scroll the last row out from under the Assistant button.
              showAssistantButton && "max-xl:pb-24"
            )}
          >
            {/* Re-keyed per route: each page fades up as it arrives, so a
                navigation reads as one soft change rather than a hard cut. */}
            <div key={pathname} className="motion-enter">
              {children}
            </div>
          </main>
          {!skipGlobalRail && <RightPanel />}
        </div>
      </div>
      {showAssistantButton && <AssistantSheet />}
    </div>
  );
}
