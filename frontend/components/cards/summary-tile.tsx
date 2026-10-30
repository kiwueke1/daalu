"use client";

import Link from "next/link";
import { ArrowUpRight } from "lucide-react";

/**
 * A summary tile. The middle column of the dashboard is composed entirely
 * of these — each tile is a single bite-sized signal that links through
 * to its detail page.
 *
 * Hover: subtle lift + a soft emerald bloom on the bottom edge, so the
 * tile feels reactive without becoming a button.
 */
export function SummaryTile({
  href,
  icon: Icon,
  label,
  value,
  delta,
  hint,
  intent,
  spark,
}: {
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string | number;
  delta?: string;
  hint?: string;
  intent?: "positive" | "negative" | "neutral";
  spark?: number[];
}) {
  const positive =
    intent === "positive" || (delta && !delta.startsWith("-") && intent !== "negative");

  return (
    <Link
      href={href}
      className="surface group relative overflow-hidden p-5 flex flex-col gap-3 transition-all duration-300 hover:translate-y-[-1px]"
      style={{ minHeight: 140 }}
    >
      {/* On-hover emerald bottom bloom */}
      <span
        aria-hidden
        className="pointer-events-none absolute inset-x-0 bottom-0 h-[60%] opacity-0 group-hover:opacity-100 transition-opacity duration-300"
        style={{
          background:
            "radial-gradient(70% 100% at 50% 110%, var(--accent-soft), transparent 70%)",
        }}
      />

      <div className="relative flex items-start justify-between">
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.18em] text-muted">
          <span
            className="inline-flex h-6 w-6 items-center justify-center rounded-md"
            style={{
              background: "rgba(255,255,255,0.025)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.05)",
            }}
          >
            <Icon className="h-3.5 w-3.5" />
          </span>
          {label}
        </div>
        <ArrowUpRight
          className="h-3.5 w-3.5 text-muted opacity-0 group-hover:opacity-100 transition-opacity"
          style={{ color: "var(--accent)" }}
        />
      </div>

      <div className="relative flex items-end justify-between gap-3">
        <div>
          <div className="text-[30px] leading-none font-semibold tracking-tight">
            {value}
          </div>
          {(delta || hint) && (
            <div className="mt-2 flex items-center gap-2 text-[11px]">
              {delta && (
                <span
                  className="font-medium"
                  style={{ color: positive ? "var(--accent)" : "var(--critical)" }}
                >
                  {delta}
                </span>
              )}
              {hint && <span className="text-muted">{hint}</span>}
            </div>
          )}
        </div>
        {spark && spark.length > 1 && <Sparkline data={spark} />}
      </div>
    </Link>
  );
}

function Sparkline({ data }: { data: number[] }) {
  const w = 60;
  const h = 32;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - ((v - min) / range) * h;
      return `${x},${y}`;
    })
    .join(" ");
  const lastY = h - ((data[data.length - 1] - min) / range) * h;
  return (
    <svg width={w} height={h} className="overflow-visible">
      <defs>
        <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.35} />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
        </linearGradient>
      </defs>
      <polyline
        points={`0,${h} ${pts} ${w},${h}`}
        fill="url(#sparkFill)"
        stroke="none"
      />
      <polyline
        points={pts}
        fill="none"
        stroke="var(--accent)"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        style={{ filter: "drop-shadow(0 0 4px var(--accent-glow))" }}
      />
      <circle cx={w} cy={lastY} r={2} fill="var(--accent)" />
    </svg>
  );
}
