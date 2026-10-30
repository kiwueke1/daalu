"use client";

import Link from "next/link";
import { ChevronLeft } from "lucide-react";
import { HUB } from "@/lib/hub-routes";

/**
 * The one header every onboarding page uses: an optional "‹ Onboarding" back
 * link, the title, a one-line description, an optional right-hand action and
 * a thin progress bar with a "Step x of y" label. Wraps on a phone.
 */
export function OnboardingHeader({
  icon: Icon,
  title,
  description,
  backLink = false,
  action,
  progress,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  description: React.ReactNode;
  /** Show the "‹ Onboarding" link back to the main wizard. */
  backLink?: boolean;
  action?: React.ReactNode;
  /** value is 0..1; label sits under the bar ("Step 2 of 3"). */
  progress?: { value: number; label: string };
}) {
  const pct = progress ? Math.round(Math.min(1, Math.max(0, progress.value)) * 100) : 0;
  return (
    <div>
      {backLink && (
        <Link
          href={HUB.onboarding}
          className="text-[11px] uppercase tracking-wider text-muted hover:text-fg inline-flex items-center gap-1 mb-1"
        >
          <ChevronLeft className="h-3 w-3" /> Onboarding
        </Link>
      )}
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <div className="min-w-0 flex-1 basis-[320px]">
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <Icon className="h-5 w-5 text-accent shrink-0" /> {title}
          </h1>
          <p className="text-muted text-sm mt-1">{description}</p>
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
      {progress && (
        <div className="mt-4">
          <div
            className="h-1.5 rounded-full bg-bg-elevated overflow-hidden"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
            aria-label={progress.label}
          >
            <div
              className="h-full rounded-full bg-accent transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
          <div className="mt-1.5 text-[11px] text-muted">{progress.label}</div>
        </div>
      )}
    </div>
  );
}
