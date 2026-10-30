"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import {
  Receipt,
  Cpu,
  Cloud,
  CloudOff,
  Check,
  Loader2,
  ArrowRight,
  Server,
  Plus,
  Trash2,
  AlertTriangle,
  X,
  Terminal,
  ChevronUp,
  ChevronDown,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  api,
  type RoutingPolicy,
  type Sku,
  type GpuCluster,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { GpuOnboardingCard, LocalGpuPill } from "@/components/gpu/connect-gpu";

const compact = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 2,
});

/**
 * /billing — the tenant's window into what they are paying for and
 * where the tokens are physically being manufactured.
 *
 * Four blocks down the page:
 *  1. Header + Local-GPU status pill (configured? healthy? which models?)
 *  2. Period totals — base + variable + included-bundle progress
 *  3. SKU picker — four cards, the current one highlighted
 *  4. Charts: 30-day cost trend + by-tier + by-source breakdowns
 */
export default function BillingPage() {
  const qc = useQueryClient();
  const skus = useQuery({ queryKey: ["billing", "skus"], queryFn: api.billing.skus });
  const current = useQuery({
    queryKey: ["billing", "current"],
    queryFn: api.billing.current,
    refetchInterval: 30_000,
  });
  const breakdown = useQuery({
    queryKey: ["billing", "breakdown"],
    queryFn: api.billing.breakdown,
    refetchInterval: 60_000,
  });
  const daily = useQuery({
    queryKey: ["billing", "daily"],
    queryFn: () => api.billing.daily(30),
    refetchInterval: 60_000,
  });
  const localGpu = useQuery({
    queryKey: ["billing", "localGpu"],
    queryFn: api.billing.localGpu,
    refetchInterval: 30_000,
  });

  const changeSku = useMutation({
    mutationFn: api.billing.changeSku,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["billing"] });
    },
  });

  const currentSlug = current.data?.sku?.slug ?? null;
  const totals = current.data?.totals;

  return (
    <div className="space-y-8 max-w-[1200px]">
      {/* ── Header ──────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-2">
            <Receipt className="h-5 w-5 text-accent-blue" /> Usage & Pricing
          </h1>
          <p className="text-muted text-sm mt-1 max-w-[640px]">
            Where your tokens are made, what you pay for them, and which
            plan you are on. Switch plans at any time — pricing applies
            from the moment you change.
          </p>
        </div>
        <LocalGpuPill
          configured={localGpu.data?.configured ?? false}
          healthy={localGpu.data?.healthy ?? false}
          model={localGpu.data?.model_classifier ?? ""}
        />
      </div>

      {/* ── Local-GPU onboarding ──────────────────────────────────── */}
      <GpuOnboardingCard />

      {/* ── Period totals ──────────────────────────────────────────── */}
      <PeriodCard totals={totals} sku={current.data?.sku ?? null} />

      {/* ── SKU picker ─────────────────────────────────────────────── */}
      <section>
        <h2 className="text-sm uppercase tracking-[0.18em] text-muted mb-3">
          Plans
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          {(skus.data ?? []).map((s) => (
            <SkuCard
              key={s.slug}
              sku={s}
              active={s.slug === currentSlug}
              switching={changeSku.isPending && changeSku.variables === s.slug}
              onSelect={() => changeSku.mutate(s.slug)}
            />
          ))}
        </div>
      </section>

      {/* ── Charts ─────────────────────────────────────────────────── */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <div className="lg:col-span-2 rounded-xl border border-line bg-bg-elevated/40 p-5">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">30-day spend</h3>
            <span className="text-xs text-muted">USD per day</span>
          </div>
          <div className="h-56 mt-3">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={daily.data ?? []}>
                <defs>
                  <linearGradient id="spendFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.4} />
                    <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="rgba(255,255,255,0.04)" vertical={false} />
                <XAxis dataKey="day" stroke="rgba(255,255,255,0.3)" fontSize={10} />
                <YAxis stroke="rgba(255,255,255,0.3)" fontSize={10} />
                <Tooltip
                  contentStyle={{
                    background: "rgba(15,15,18,0.95)",
                    border: "1px solid rgba(255,255,255,0.08)",
                    fontSize: 12,
                  }}
                  formatter={(v: number) => `$${v.toFixed(4)}`}
                />
                <Area
                  type="monotone"
                  dataKey="cost_usd"
                  stroke="var(--accent)"
                  strokeWidth={1.5}
                  fill="url(#spendFill)"
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
        <BreakdownPanel
          title="Where tokens were made"
          rows={breakdown.data?.by_tier ?? []}
          labelMap={TIER_LABELS}
        />
      </section>

      <section>
        <BreakdownPanel
          title="Top sources this period"
          rows={breakdown.data?.by_source ?? []}
        />
      </section>
    </div>
  );
}

// ── Period card ─────────────────────────────────────────────────────────

function PeriodCard({
  totals,
  sku,
}: {
  totals: any | undefined;
  sku: Sku | null;
}) {
  if (!totals)
    return (
      <div className="rounded-xl border border-line p-6 text-muted text-sm">
        Loading current period…
      </div>
    );
  const variable = Math.max(0, totals.cost_usd);
  const totalThisMonth = totals.base_usd + variable;
  const bundlePct = totals.included_events
    ? Math.min(100, (totals.included_events_used / totals.included_events) * 100)
    : 0;
  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-6">
      <div className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <div className="text-xs uppercase tracking-[0.18em] text-muted">
            Current period
          </div>
          <div className="text-3xl font-semibold mt-1">
            ${totalThisMonth.toFixed(2)}
          </div>
          <div className="text-xs text-muted mt-1">
            ${totals.base_usd.toFixed(2)} base + ${variable.toFixed(2)} usage
            {sku && (
              <>
                {" · "}
                <span className="text-[color:var(--text)]">{sku.name}</span>
              </>
            )}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-6 text-right">
          <Stat label="Events" value={totals.events.toLocaleString()} />
          <Stat
            label="Tokens in"
            value={compact.format(totals.prompt_tokens)}
          />
          <Stat
            label="Tokens out"
            value={compact.format(totals.completion_tokens)}
          />
        </div>
      </div>
      {totals.included_events > 0 && (
        <div className="mt-5">
          <div className="flex justify-between text-xs text-muted mb-1.5">
            <span>
              Bundle: {totals.included_events_used.toLocaleString()} /{" "}
              {totals.included_events.toLocaleString()} included events
            </span>
            <span>{bundlePct.toFixed(0)}%</span>
          </div>
          <div className="h-1.5 rounded-full bg-fg/10 overflow-hidden">
            <div
              className="h-full rounded-full"
              style={{
                width: `${bundlePct}%`,
                background: "var(--accent)",
                boxShadow: "0 0 10px var(--accent-glow)",
              }}
            />
          </div>
        </div>
      )}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-[0.18em] text-muted">
        {label}
      </div>
      <div className="text-lg font-medium mt-1">{value}</div>
    </div>
  );
}

// ── SKU card ────────────────────────────────────────────────────────────

const POLICY_LABEL: Record<RoutingPolicy, string> = {
  local_first: "Local first, external model for quality",
  hybrid: "Local first for everything; external on miss",
  external_only: "External models only, no local routing",
  sovereign: "Your own GPU, federated cluster",
};

function SkuCard({
  sku,
  active,
  switching,
  onSelect,
}: {
  sku: Sku;
  active: boolean;
  switching: boolean;
  onSelect: () => void;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border p-5 flex flex-col gap-3 relative",
        active
          ? "border-accent-blue/60 bg-accent-blue/10 shadow-glow"
          : "border-line bg-bg-elevated/30"
      )}
    >
      {active && (
        <div className="absolute top-3 right-3 text-xs flex items-center gap-1 text-accent-blue">
          <Check className="h-3.5 w-3.5" /> Current
        </div>
      )}
      <div>
        <div className="text-base font-semibold">{sku.name}</div>
        <div className="text-xs text-muted mt-1 min-h-[32px]">
          {sku.tagline}
        </div>
      </div>
      <div className="flex items-baseline gap-1.5">
        <span className="text-2xl font-semibold">
          ${sku.monthly_base_usd.toFixed(0)}
        </span>
        <span className="text-xs text-muted">/ month base</span>
      </div>
      {sku.included_events_per_month > 0 && (
        <div className="text-xs text-muted">
          Includes {sku.included_events_per_month.toLocaleString()} events
        </div>
      )}
      <div className="text-[11px] text-muted flex items-start gap-1.5 mt-1 leading-relaxed">
        {sku.routing_policy === "external_only" ? (
          <Cloud className="h-3.5 w-3.5 mt-px shrink-0" />
        ) : (
          <Cpu className="h-3.5 w-3.5 mt-px shrink-0" />
        )}
        {POLICY_LABEL[sku.routing_policy]}
      </div>
      <PriceTable sku={sku} />
      <button
        type="button"
        disabled={active || switching}
        onClick={onSelect}
        className={cn(
          "text-xs px-3 py-2 rounded-lg border transition-all",
          active
            ? "border-line text-muted cursor-default"
            : "border-accent-blue/60 text-[color:var(--text)] hover:bg-accent-blue/20"
        )}
      >
        {switching ? (
          <span className="flex items-center justify-center gap-1.5">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Switching…
          </span>
        ) : active ? (
          "Active plan"
        ) : (
          <span className="flex items-center justify-center gap-1.5">
            Switch to {sku.name} <ArrowRight className="h-3.5 w-3.5" />
          </span>
        )}
      </button>
    </div>
  );
}

function PriceTable({ sku }: { sku: Sku }) {
  const rows: { label: string; in_: number; out_: number }[] = [];
  if (sku.price_local_in_per_mtok > 0 || sku.price_local_out_per_mtok > 0)
    rows.push({
      label: "Local GPU",
      in_: sku.price_local_in_per_mtok,
      out_: sku.price_local_out_per_mtok,
    });
  if (
    sku.price_external_classifier_in_per_mtok > 0 ||
    sku.price_external_classifier_out_per_mtok > 0
  )
    rows.push({
      label: "External classifier",
      in_: sku.price_external_classifier_in_per_mtok,
      out_: sku.price_external_classifier_out_per_mtok,
    });
  if (
    sku.price_external_quality_in_per_mtok > 0 ||
    sku.price_external_quality_out_per_mtok > 0
  )
    rows.push({
      label: "External quality",
      in_: sku.price_external_quality_in_per_mtok,
      out_: sku.price_external_quality_out_per_mtok,
    });
  if (rows.length === 0)
    return (
      <div className="text-[11px] text-muted">No metered tiers on this plan.</div>
    );
  return (
    <div className="text-[11px] mt-1 grid grid-cols-[1fr_auto_auto] gap-x-3 gap-y-1">
      <span className="text-muted">Tier</span>
      <span className="text-muted text-right">In $/M</span>
      <span className="text-muted text-right">Out $/M</span>
      {rows.map((r) => (
        <Fragment key={r.label}>
          <span className="min-w-0 leading-snug">{r.label}</span>
          <span className="text-right">${r.in_.toFixed(2)}</span>
          <span className="text-right">${r.out_.toFixed(2)}</span>
        </Fragment>
      ))}
    </div>
  );
}

// ── Breakdown panel ─────────────────────────────────────────────────────

const TIER_LABELS: Record<string, string> = {
  local: "Local GPU",
  external_classifier: "External classifier",
  external_quality: "External quality",
  sovereign: "Sovereign",
};

function BreakdownPanel({
  title,
  rows,
  labelMap,
}: {
  title: string;
  rows: { key: string; events: number; cost_usd: number }[];
  labelMap?: Record<string, string>;
}) {
  const total = rows.reduce((acc, r) => acc + r.cost_usd, 0);
  return (
    <div className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <h3 className="text-sm font-medium mb-3">{title}</h3>
      {rows.length === 0 && (
        <div className="text-xs text-muted">No activity this period.</div>
      )}
      <div className="space-y-2.5">
        {rows.map((r) => {
          const pct = total ? (r.cost_usd / total) * 100 : 0;
          return (
            <div key={r.key}>
              <div className="flex justify-between text-xs">
                <span>{labelMap?.[r.key] ?? r.key}</span>
                <span className="text-muted">
                  ${r.cost_usd.toFixed(2)} · {r.events.toLocaleString()} evt
                </span>
              </div>
              <div className="h-1 mt-1 rounded-full bg-fg/10 overflow-hidden">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${pct}%`,
                    background: "var(--accent)",
                    opacity: 0.8,
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
