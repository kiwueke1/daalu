"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Fragment } from "react";
import Link from "next/link";
import { Receipt, Cpu, Cloud, Check, Info, Loader2 } from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, type RoutingPolicy, type Sku } from "@/lib/api";
import { cn } from "@/lib/utils";
import { errorText } from "@/lib/errors";
import { LoadState } from "@/components/ui/load-state";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { useToast } from "@/components/ui/toast";
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

  const toast = useToast();
  const changeSku = useMutation({
    mutationFn: api.billing.changeSku,
    onSuccess: (_d, slug) => {
      qc.invalidateQueries({ queryKey: ["billing"] });
      const name = skus.data?.find((s) => s.slug === slug)?.name ?? "the new plan";
      toast.success(`You're on ${name} now. New pricing applies from today.`);
    },
    onError: (err) => toast.error(errorText(err)),
  });

  const currentSlug = current.data?.sku?.slug ?? null;
  const totals = current.data?.totals;
  // Until the GPU status has loaded, don't claim either way.
  const gpuKnown = !!localGpu.data;
  const gpuConfigured = localGpu.data?.configured ?? false;

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
        {gpuKnown && (
          <div className="flex flex-col items-start sm:items-end gap-1.5 max-w-[320px] motion-enter">
            <LocalGpuPill
              configured={gpuConfigured}
              healthy={localGpu.data?.healthy ?? false}
              model={localGpu.data?.model_classifier ?? ""}
            />
            {!gpuConfigured && (
              <p className="text-xs text-muted sm:text-right">
                No local GPU is connected, so requests go to external models
                and are billed at external rates.{" "}
                <Link href="/ai-factory" className="underline hover:text-fg">
                  Connect a GPU in AI Factory
                </Link>{" "}
                to run them locally.
              </p>
            )}
          </div>
        )}
      </div>

      {/* ── Local-GPU onboarding ──────────────────────────────────── */}
      <GpuOnboardingCard />

      {/* ── Period totals ──────────────────────────────────────────── */}
      <LoadState query={current} loadingText="Loading current period…">
        {totals && <PeriodCard totals={totals} sku={current.data?.sku ?? null} />}
      </LoadState>

      {/* ── SKU picker ─────────────────────────────────────────────── */}
      <section>
        <h2 className="text-sm uppercase tracking-[0.18em] text-muted mb-3">
          Plans
        </h2>
        <LoadState
          query={skus}
          loadingText="Loading plans…"
          empty={!skus.data?.length}
          emptyTitle="No plans available"
          emptyHint="Plans couldn't be listed for your organisation. Contact support to choose one."
        >
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 motion-enter">
            {(skus.data ?? []).map((s) => (
              <SkuCard
                key={s.slug}
                sku={s}
                active={s.slug === currentSlug}
                switching={changeSku.isPending && changeSku.variables === s.slug}
                busy={changeSku.isPending || current.isLoading}
                gpuMissing={gpuKnown && !gpuConfigured}
                onSelect={() => changeSku.mutate(s.slug)}
              />
            ))}
          </div>
        </LoadState>
      </section>

      {/* ── Charts ─────────────────────────────────────────────────── */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <div className="lg:col-span-2 rounded-xl border border-line bg-bg-elevated/40 p-5">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">30-day spend</h3>
            <span className="text-xs text-muted">USD per day</span>
          </div>
          <div className="h-56 mt-3">
            {daily.isLoading ? (
              <div role="status" className="h-full flex items-center justify-center gap-2 text-sm text-muted">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Loading spend…
              </div>
            ) : daily.isError ? (
              <div className="h-full flex items-center justify-center text-sm text-muted text-center px-4">
                Couldn&apos;t load spend. {errorText(daily.error)}
              </div>
            ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={daily.data ?? []}>
                <defs>
                  <linearGradient id="spendFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.4} />
                    <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="var(--line)" vertical={false} />
                <XAxis dataKey="day" stroke="var(--text-muted)" fontSize={12} />
                <YAxis stroke="var(--text-muted)" fontSize={12} />
                <Tooltip
                  contentStyle={{
                    background: "var(--bg-card)",
                    border: "1px solid var(--line-strong)",
                    color: "var(--text)",
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
            )}
          </div>
        </div>
        <BreakdownPanel
          title="Where tokens were made"
          query={breakdown}
          rows={breakdown.data?.by_tier ?? []}
          labelMap={TIER_LABELS}
        />
      </section>

      <section>
        <BreakdownPanel
          title="Top sources this period"
          query={breakdown}
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
  totals: any;
  sku: Sku | null;
}) {
  const variable = Math.max(0, totals.cost_usd);
  const totalThisMonth = totals.base_usd + variable;
  const bundlePct = totals.included_events
    ? Math.min(100, (totals.included_events_used / totals.included_events) * 100)
    : 0;
  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-6 motion-enter">
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
  // Labels may wrap on a phone; values stay aligned along the bottom.
  return (
    <div className="flex flex-col justify-between">
      <div className="text-xs uppercase tracking-[0.18em] text-muted">
        {label}
      </div>
      <div className="text-lg font-medium mt-1 tabular-nums">{value}</div>
    </div>
  );
}

// ── SKU card ────────────────────────────────────────────────────────────

const POLICY_LABEL: Record<RoutingPolicy, string> = {
  local_first: "Local GPU first; external models for hard questions",
  hybrid: "Local first for everything; external on miss",
  external_only: "External models only, no local routing",
  sovereign: "Your own GPU, federated cluster",
};

function SkuCard({
  sku,
  active,
  switching,
  busy,
  gpuMissing,
  onSelect,
}: {
  sku: Sku;
  active: boolean;
  switching: boolean;
  busy: boolean;
  /** No local GPU connected: say where "local" traffic goes instead. */
  gpuMissing: boolean;
  onSelect: () => void;
}) {
  const price = `$${sku.monthly_base_usd.toFixed(0)}/mo`;
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
          <Check className="h-3.5 w-3.5" aria-hidden /> Current
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
      <div className="text-xs text-muted flex items-start gap-1.5 mt-1 leading-relaxed">
        {sku.routing_policy === "external_only" ? (
          <Cloud className="h-3.5 w-3.5 mt-px shrink-0" aria-hidden />
        ) : (
          <Cpu className="h-3.5 w-3.5 mt-px shrink-0" aria-hidden />
        )}
        {POLICY_LABEL[sku.routing_policy]}
      </div>
      {gpuMissing && (sku.routing_policy === "local_first" || sku.routing_policy === "hybrid") && (
        <div className="text-xs text-muted flex items-start gap-1.5 leading-relaxed">
          <Info className="h-3.5 w-3.5 mt-px shrink-0" aria-hidden />
          No GPU connected yet, so local requests go to external models for now.
        </div>
      )}
      <PriceTable sku={sku} />
      <div className="mt-auto pt-2">
        {active ? (
          <div className="h-8 flex items-center text-xs text-muted">Your current plan</div>
        ) : (
          <ConfirmButton
            label={`Switch to ${sku.name} · ${price}`}
            confirmLabel={`Switch to ${sku.name}`}
            consequence={`Switch to ${sku.name} — ${price} from today.`}
            pending={switching}
            disabled={busy}
            onConfirm={onSelect}
            className="w-full justify-center h-auto min-h-[32px] py-1.5 border-accent/50 hover:border-accent"
          />
        )}
      </div>
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
      <div className="text-xs text-muted">No metered tiers on this plan.</div>
    );
  return (
    <div className="text-xs mt-1 grid grid-cols-[1fr_auto_auto] gap-x-2 gap-y-1">
      <span className="text-muted" title="Price in US dollars per million tokens">Tier · $/M tokens</span>
      <span className="text-muted text-right">In</span>
      <span className="text-muted text-right">Out</span>
      {rows.map((r) => (
        <Fragment key={r.label}>
          <span className="leading-snug whitespace-nowrap">{r.label}</span>
          <span className="text-right tabular-nums">${r.in_.toFixed(2)}</span>
          <span className="text-right tabular-nums">${r.out_.toFixed(2)}</span>
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
  query,
  rows,
  labelMap,
}: {
  title: string;
  query: { isLoading: boolean; isError: boolean; error?: unknown };
  rows: { key: string; events: number; cost_usd: number }[];
  labelMap?: Record<string, string>;
}) {
  const total = rows.reduce((acc, r) => acc + r.cost_usd, 0);
  return (
    <div className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <h3 className="text-sm font-medium mb-3">{title}</h3>
      {query.isLoading ? (
        <div role="status" className="text-sm text-muted flex items-center gap-2">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> Loading…
        </div>
      ) : query.isError ? (
        <div className="text-sm text-muted">Couldn&apos;t load this. {errorText(query.error)}</div>
      ) : rows.length === 0 ? (
        <div className="text-sm text-muted">
          No usage yet this period. It appears here as soon as the agent or
          copilot makes a model call; ask the copilot something to see it.
        </div>
      ) : null}
      <div className="space-y-2.5 motion-enter">
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
