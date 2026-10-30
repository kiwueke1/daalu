"use client";

import { Sparkles } from "lucide-react";
import { motion } from "framer-motion";
import { Area, AreaChart, ResponsiveContainer } from "recharts";

/**
 * AI insight callout — illuminated glass with an optional emerald
 * area-chart trend bleeding upward from the bottom edge.
 */
export function AIInsightCard({
  title,
  body,
  action,
  trend,
}: {
  title: string;
  body: React.ReactNode;
  action?: React.ReactNode;
  trend?: number[];
}) {
  const data = (trend ?? []).map((y, i) => ({ x: i, y }));

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="surface surface-bloom relative overflow-hidden p-5"
    >
      <div className="relative">
        <div
          className="flex items-center gap-2 text-[10px] uppercase tracking-[0.20em] mb-3"
          style={{ color: "var(--accent)" }}
        >
          <Sparkles className="h-3 w-3 animate-shimmer" />
          AI insight
        </div>
        <h3 className="text-base md:text-lg font-semibold mb-2 leading-snug">
          {title}
        </h3>
        <div className="text-[13.5px] text-muted leading-relaxed">{body}</div>
        {data.length > 1 && (
          <div className="h-[64px] -mx-1 mt-3 relative">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data}>
                <defs>
                  <linearGradient id="aiTrendFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <Area
                  type="monotone"
                  dataKey="y"
                  stroke="var(--accent)"
                  strokeWidth={1.6}
                  fill="url(#aiTrendFill)"
                />
              </AreaChart>
            </ResponsiveContainer>
            <div
              aria-hidden
              className="pointer-events-none absolute inset-x-0 bottom-0 h-px"
              style={{
                background:
                  "linear-gradient(90deg, transparent, var(--accent-glow), transparent)",
              }}
            />
          </div>
        )}
        {action && <div className="mt-4 flex gap-2">{action}</div>}
      </div>
    </motion.div>
  );
}
