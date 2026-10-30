"use client";

/**
 * /dashboards/[uid] — a single Grafana dashboard rendered natively.
 *
 * The panel *definitions* (title, type, PromQL/LogQL) come from Grafana; the
 * *data* is fetched per-panel from the tenant's own Prometheus/Loki via
 * /dashboards/query and drawn with the same recharts components the AI-factory
 * GPU panels use. A single range switcher drives every panel.
 */

import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ArrowLeft, LayoutDashboard } from "lucide-react";
import {
  api,
  type DashboardPanel,
  type DashboardRange,
  type DashboardSeries,
} from "@/lib/api";

const RANGES: DashboardRange[] = ["15m", "1h", "6h", "24h", "7d"];

// A small, legible categorical palette for multi-series charts.
const PALETTE = [
  "#34d399",
  "#60a5fa",
  "#f472b6",
  "#fbbf24",
  "#a78bfa",
  "#22d3ee",
  "#fb7185",
  "#4ade80",
];

export default function DashboardDetailPage() {
  const params = useParams<{ uid: string }>();
  const uid = params.uid;
  const [range, setRange] = useState<DashboardRange>("6h");

  const { data: dash, isLoading, error } = useQuery({
    queryKey: ["dashboard", uid],
    queryFn: () => api.dashboards.get(uid),
    enabled: !!uid,
  });

  if (isLoading) {
    return <div className="p-6 text-sm text-muted">Loading dashboard…</div>;
  }
  if (error || !dash) {
    return (
      <div className="p-6 text-sm text-muted">
        Couldn&apos;t load this dashboard.{" "}
        <Link href="/dashboards" className="underline">
          Back to dashboards
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-5 max-w-[1280px]">
      <div>
        <Link
          href="/dashboards"
          className="text-xs text-muted hover:text-fg inline-flex items-center gap-1.5"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Dashboards
        </Link>
        <div className="mt-2 flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-3">
            <LayoutDashboard className="h-5 w-5 text-accent-cyan" />
            <h1 className="text-2xl font-semibold">{dash.title}</h1>
          </div>
          <RangeSwitcher value={range} onChange={setRange} />
        </div>
        {dash.description && (
          <p className="text-xs text-muted mt-1 max-w-[760px]">
            {dash.description}
          </p>
        )}
      </div>

      {dash.panels.length === 0 && (
        <div className="rounded-2xl border border-line bg-bg-card p-8 text-center text-muted text-sm">
          This dashboard has no PromQL/LogQL panels Daalu can render natively
          (text or external-only panels are skipped).
        </div>
      )}

      <div className="grid grid-cols-12 gap-3 auto-rows-min">
        {dash.panels.map((p) => (
          <div
            key={p.id}
            className="col-span-12"
            style={{ gridColumn: `span ${spanFor(p.w)} / span ${spanFor(p.w)}` }}
          >
            <Panel panel={p} range={range} />
          </div>
        ))}
      </div>
    </div>
  );
}

// Grafana's grid is 24 columns; ours is 12. Map width, clamped so a panel is
// never thinner than a third of the row on desktop.
function spanFor(w: number): number {
  const cols = Math.round((w / 24) * 12);
  return Math.min(12, Math.max(4, cols || 6));
}

function RangeSwitcher({
  value,
  onChange,
}: {
  value: DashboardRange;
  onChange: (r: DashboardRange) => void;
}) {
  return (
    <div className="flex rounded-lg border border-line overflow-hidden">
      {RANGES.map((r) => (
        <button
          key={r}
          onClick={() => onChange(r)}
          className={
            "h-8 px-3 text-xs " +
            (value === r
              ? "bg-accent-cyan/15 text-accent-cyan"
              : "text-muted hover:text-fg")
          }
        >
          {r}
        </button>
      ))}
    </div>
  );
}

function Panel({
  panel,
  range,
}: {
  panel: DashboardPanel;
  range: DashboardRange;
}) {
  const datasource = panel.datasource === "loki" ? "loki" : "prometheus";
  const q = useQuery({
    queryKey: ["dashboard-panel", panel.id, panel.expr, range],
    queryFn: () =>
      api.dashboards.query({
        datasource,
        expr: panel.expr,
        kind: panel.type,
        range,
      }),
    refetchInterval: 60_000,
  });

  return (
    <div className="rounded-2xl border border-line bg-bg-card p-4 h-full flex flex-col">
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="text-sm font-medium truncate" title={panel.title}>
          {panel.title}
        </div>
        <span className="text-[10px] uppercase tracking-wider text-muted shrink-0">
          {panel.type}
        </span>
      </div>
      <div className="flex-1 min-h-[140px]">
        {q.isLoading && (
          <div className="text-xs text-muted">Querying…</div>
        )}
        {q.data?.error && (
          <div className="text-xs text-accent-amber">{q.data.error}</div>
        )}
        {q.data && !q.data.error && (
          <PanelBody panel={panel} data={q.data} />
        )}
      </div>
      <div className="mt-2 text-[10px] font-mono text-muted truncate" title={panel.expr}>
        {panel.expr}
      </div>
    </div>
  );
}

function PanelBody({
  panel,
  data,
}: {
  panel: DashboardPanel;
  data: import("@/lib/api").DashboardQueryResult;
}) {
  if (panel.type === "logs") {
    if (data.logs.length === 0)
      return <div className="text-xs text-muted">No log lines.</div>;
    return (
      <div className="h-56 overflow-auto font-mono text-[11px] space-y-0.5">
        {data.logs.map((l, i) => (
          <div key={i} className="whitespace-pre-wrap break-all">
            <span className="text-muted">
              {new Date(l.ts).toLocaleTimeString()}{" "}
            </span>
            {l.line}
          </div>
        ))}
      </div>
    );
  }

  if (panel.type === "stat" || panel.type === "table") {
    if (data.instant.length === 0)
      return <div className="text-xs text-muted">No data.</div>;
    if (panel.type === "stat" && data.instant.length <= 1) {
      const v = data.instant[0];
      return (
        <div className="flex items-baseline gap-1">
          <span className="text-3xl font-mono">{fmt(v.value)}</span>
          {panel.unit && (
            <span className="text-sm text-muted">{panel.unit}</span>
          )}
        </div>
      );
    }
    return (
      <div className="h-56 overflow-auto">
        <table className="w-full text-xs">
          <tbody>
            {data.instant.slice(0, 50).map((v, i) => (
              <tr key={i} className="border-t border-line/60">
                <td className="py-1 pr-2 text-muted truncate max-w-[200px]">
                  {v.name}
                </td>
                <td className="py-1 font-mono text-right">{fmt(v.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  // timeseries
  return <TimeseriesChart series={data.series} unit={panel.unit} />;
}

function TimeseriesChart({
  series,
  unit,
}: {
  series: DashboardSeries[];
  unit: string | null;
}) {
  if (series.length === 0)
    return <div className="text-xs text-muted">No data in this range.</div>;

  // Cap series to keep the legend + chart readable.
  const shown = series.slice(0, 8);

  // Merge points across series into one row-per-timestamp dataset.
  const byTs: Record<number, Record<string, number | string>> = {};
  shown.forEach((s, idx) => {
    const key = s.name || `series ${idx + 1}`;
    for (const p of s.points) {
      (byTs[p.ts] ??= { ts: p.ts })[key] = p.value;
    }
  });
  const rows = Object.values(byTs)
    .sort((a, b) => (a.ts as number) - (b.ts as number))
    .map((r) => ({
      ...r,
      t: new Date((r.ts as number) * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
    }));

  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows}>
          <CartesianGrid stroke="rgba(255,255,255,0.04)" vertical={false} />
          <XAxis dataKey="t" stroke="rgba(255,255,255,0.3)" fontSize={10} />
          <YAxis
            stroke="rgba(255,255,255,0.3)"
            fontSize={10}
            width={44}
            tickFormatter={(v: number) => `${fmt(v)}${unit ? "" : ""}`}
          />
          <Tooltip
            contentStyle={{
              background: "rgba(15,15,18,0.95)",
              border: "1px solid rgba(255,255,255,0.08)",
              fontSize: 12,
            }}
            formatter={(v: number) => `${fmt(v)}${unit ? ` ${unit}` : ""}`}
          />
          {shown.length > 1 && <Legend wrapperStyle={{ fontSize: 10 }} />}
          {shown.map((s, idx) => (
            <Line
              key={s.name || idx}
              type="monotone"
              dataKey={s.name || `series ${idx + 1}`}
              stroke={PALETTE[idx % PALETTE.length]}
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function fmt(v: number): string {
  if (!isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(2) + "G";
  if (abs >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (v / 1e3).toFixed(2) + "k";
  if (abs > 0 && abs < 0.01) return v.toExponential(1);
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}
