"use client";

import { useMemo } from "react";
import { Box, Globe, Network, Router, Server } from "lucide-react";
import type { TopoLink, TopoLinkStatus, TopoNode, TopoTier } from "@/lib/api";
import { cn } from "@/lib/utils";

// Link colours read straight from the design tokens so the graph lives in the
// same palette as the rest of the app (emerald = healthy, critical = down).
export const LINK_COLOR: Record<TopoLinkStatus, string> = {
  up: "var(--accent)",
  down: "var(--critical)",
  mismatch: "var(--warning)",
  // Neutral tokens, so both stay visible on the light theme too.
  mgmt: "var(--text-muted)",
  unknown: "var(--text-muted)",
};

// Stroke treatment per status — exported so the page legend renders the SAME
// dash/width the graph draws (a colour chip alone can't explain why "up" is
// dotted and "miscabled" is solid).
export const LINK_STYLE: Record<
  TopoLinkStatus,
  { dash?: string; width: number; opacity: number }
> = {
  up: { dash: "1 7", width: 1.6, opacity: 0.8 }, // dotted; dashes animate = live traffic
  down: { dash: "5 5", width: 1.6, opacity: 0.8 }, // dashed, pulsing
  mismatch: { width: 1.6, opacity: 0.8 }, // solid — reads "heavier" on purpose
  mgmt: { dash: "2 5", width: 1, opacity: 0.35 }, // faint — out-of-band, not fabric
  unknown: { width: 1.6, opacity: 0.45 },
};

const TIER_ROW: Record<TopoTier, number> = {
  spine: 0,
  gateway: 0,
  leaf: 1,
  server: 2,
  unknown: 2,
};

const TIER_ICON: Record<TopoTier, typeof Server> = {
  spine: Network,
  gateway: Globe,
  leaf: Router,
  server: Server,
  unknown: Box,
};

const TIER_LABEL: Record<TopoTier, string> = {
  spine: "Spine",
  gateway: "Gateway",
  leaf: "Leaf",
  server: "Server",
  unknown: "Node",
};

const COL_W = 208;
const ROW_H = 168;
const PAD = 56;
const NODE_W = 168;
const NODE_H = 76;

interface Placed {
  node: TopoNode;
  x: number; // top-left
  y: number;
  cx: number; // centre
  cy: number;
}

function reachDot(node: TopoNode): { color: string; title: string } {
  if (node.reachable === true) return { color: "var(--accent)", title: "reachable" };
  if (node.reachable === false) return { color: "var(--critical)", title: "unreachable" };
  return { color: "var(--line-strong)", title: "not probed" };
}

export function FabricGraph({
  nodes,
  links,
  selectedId,
  onSelect,
}: {
  nodes: TopoNode[];
  links: TopoLink[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const { placed, byId, width, height } = useMemo(() => {
    // Bucket nodes into their tier rows, keep a stable order (spine→server).
    const rows = new Map<number, TopoNode[]>();
    for (const n of nodes) {
      const r = TIER_ROW[n.tier] ?? 2;
      (rows.get(r) ?? rows.set(r, []).get(r)!).push(n);
    }
    const rowKeys = [...rows.keys()].sort((a, b) => a - b);
    const maxCount = Math.max(1, ...rowKeys.map((k) => rows.get(k)!.length));
    const totalW = maxCount * COL_W;

    const placed: Placed[] = [];
    rowKeys.forEach((rk, rowIndex) => {
      const row = rows
        .get(rk)!
        .slice()
        .sort((a, b) => a.name.localeCompare(b.name));
      const rowW = row.length * COL_W;
      const xStart = (totalW - rowW) / 2;
      row.forEach((node, i) => {
        const x = PAD + xStart + i * COL_W + (COL_W - NODE_W) / 2;
        const y = PAD + rowIndex * ROW_H;
        placed.push({ node, x, y, cx: x + NODE_W / 2, cy: y + NODE_H / 2 });
      });
    });
    const byId = new Map(placed.map((p) => [p.node.id, p]));
    return {
      placed,
      byId,
      width: totalW + 2 * PAD,
      height: rowKeys.length * ROW_H + 2 * PAD,
    };
  }, [nodes]);

  return (
    <div className="overflow-auto scrollbar-thin rounded-2xl border border-line bg-bg-card">
      <div className="relative mx-auto" style={{ width, height, minWidth: "100%" }}>
        {/* Links layer */}
        <svg
          width={width}
          height={height}
          className="absolute inset-0 pointer-events-none"
        >
          {links.map((lk) => {
            const a = byId.get(lk.source);
            const b = byId.get(lk.target);
            if (!a || !b) return null;
            const color = LINK_COLOR[lk.status];
            const style = LINK_STYLE[lk.status];
            const isUp = lk.status === "up";
            const selected =
              selectedId != null &&
              (lk.source === selectedId || lk.target === selectedId);
            return (
              <g key={lk.id}>
                <line
                  x1={a.cx}
                  y1={a.cy}
                  x2={b.cx}
                  y2={b.cy}
                  stroke={color}
                  strokeWidth={selected ? style.width + 1 : style.width}
                  strokeOpacity={
                    selected ? Math.min(1, style.opacity + 0.3) : style.opacity
                  }
                  strokeLinecap="round"
                  strokeDasharray={style.dash}
                >
                  {isUp && (
                    // Live "flow" — dashes drift toward the target so healthy
                    // links read as carrying traffic, not just present.
                    <animate
                      attributeName="stroke-dashoffset"
                      from="16"
                      to="0"
                      dur="1s"
                      repeatCount="indefinite"
                    />
                  )}
                  {lk.status === "down" && (
                    <animate
                      attributeName="stroke-opacity"
                      values="0.35;0.9;0.35"
                      dur="1.6s"
                      repeatCount="indefinite"
                    />
                  )}
                </line>
              </g>
            );
          })}
        </svg>

        {/* Nodes layer */}
        {placed.map(({ node, x, y }) => {
          const Icon = TIER_ICON[node.tier] ?? Box;
          const dot = reachDot(node);
          const active = node.id === selectedId;
          return (
            <button
              key={node.id}
              type="button"
              onClick={() => onSelect(node.id)}
              title={node.note || node.name}
              className={cn(
                "absolute text-left rounded-xl border bg-bg-elevated px-3 py-2.5 transition-all duration-200",
                "hover:-translate-y-0.5 focus:outline-none",
                active
                  ? "border-accent-emerald/70 shadow-glow"
                  : "border-line hover:border-accent-emerald/40"
              )}
              style={{ left: x, top: y, width: NODE_W, height: NODE_H }}
            >
              <div className="flex items-center gap-2">
                <Icon
                  className="h-4 w-4 shrink-0"
                  style={{ color: active ? "var(--accent)" : "var(--text-muted)" }}
                />
                <span className="flex-1 truncate text-[13px] font-semibold text-[color:var(--text)]">
                  {node.name}
                </span>
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  title={dot.title}
                  style={{ background: dot.color, boxShadow: `0 0 7px ${dot.color}` }}
                />
              </div>
              <div className="mt-1 flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-wider text-muted">
                  {TIER_LABEL[node.tier]}
                </span>
                <span className="truncate text-[11px] text-muted">
                  {node.primary_ip ?? "—"}
                </span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
