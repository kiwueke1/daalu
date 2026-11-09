"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ExternalLink, TerminalSquare, X } from "lucide-react";
import { api, type TopoNode } from "@/lib/api";
import { Button } from "@/components/ui/button";

function ReachBadge({ reachable }: { reachable: boolean | null }) {
  const map = {
    true: { label: "reachable", color: "var(--accent)" },
    false: { label: "unreachable", color: "var(--critical)" },
    null: { label: "not probed", color: "var(--text-muted)" },
  } as const;
  const k = (reachable === true ? "true" : reachable === false ? "false" : "null") as
    | "true"
    | "false"
    | "null";
  const { label, color } = map[k];
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] uppercase tracking-wider"
      style={{ borderColor: `color-mix(in srgb, ${color} 40%, transparent)`, color }}
    >
      <span
        aria-hidden
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: color }}
      />
      {label}
    </span>
  );
}

export function NodeDrawer({
  node,
  env,
  onClose,
  onOpenTerminal,
}: {
  node: TopoNode;
  /** NV-CM environment slug — the node id only resolves in its own Nautobot. */
  env?: string;
  onClose: () => void;
  onOpenTerminal: (device: { id: string; name: string }) => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["topology", "node", env ?? null, node.id],
    queryFn: () => api.network.node(node.id, env),
    refetchInterval: 12000,
  });

  const neighbors = data?.neighbors ?? [];
  const canTerminal = data?.can_terminal ?? node.can_terminal;

  return (
    <aside className="surface flex w-full max-w-[380px] flex-col self-stretch overflow-hidden">
      <div className="flex items-start justify-between border-b border-line px-4 py-3">
        <div>
          <div className="text-[15px] font-semibold text-[color:var(--text)]">
            {node.name}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-xs text-muted">
            <span className="uppercase tracking-wider">{node.tier}</span>
            {node.platform && <span>· {node.platform}</span>}
          </div>
        </div>
        <Button
          variant="icon"
          size="sm"
          onClick={onClose}
          aria-label={`Close ${node.name} details`}
          icon={<X className="h-4 w-4" aria-hidden />}
        />
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto scrollbar-thin px-4 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <ReachBadge reachable={data?.reachable ?? node.reachable} />
          {node.tags.map((t) => (
            <span
              key={t}
              className="rounded-full bg-bg-elevated px-2 py-0.5 text-[11px] text-muted"
            >
              {t}
            </span>
          ))}
        </div>

        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13px]">
          <dt className="text-muted">Mgmt IP</dt>
          <dd className="text-[color:var(--text)]">{node.primary_ip ?? "—"}</dd>
          <dt className="text-muted">Transport</dt>
          <dd className="text-[color:var(--text)]">{node.transport || "—"}</dd>
        </dl>

        {data?.probe_error && (
          <p className="rounded-lg border border-line bg-bg-card px-3 py-2 text-xs text-[color:var(--warning)]">
            {data.probe_error}
          </p>
        )}

        <div>
          <div className="mb-2 text-[11px] uppercase tracking-wider text-muted">
            LLDP neighbours{" "}
            {!isLoading && <span className="text-muted">· {neighbors.length}</span>}
          </div>
          {isLoading ? (
            <div role="status" className="text-sm text-muted">Loading neighbours…</div>
          ) : neighbors.length === 0 ? (
            <div className="rounded-lg border border-line bg-bg-card px-3 py-3 text-center text-xs text-muted">
              No neighbours observed
            </div>
          ) : (
            <ul className="space-y-1">
              {neighbors.map((n, i) => (
                <li
                  key={`${n.local_if}-${i}`}
                  className="flex items-center justify-between rounded-lg border border-line bg-bg-card px-2.5 py-1.5 text-xs"
                >
                  <span className="font-mono text-[color:var(--text)]">
                    {n.local_if || "?"}
                  </span>
                  <span className="text-muted">→</span>
                  <span className="truncate text-right">
                    <span className="text-[color:var(--accent)]">{n.remote_name}</span>
                    <span className="text-muted"> :{n.remote_if || "?"}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="space-y-2 border-t border-line px-4 py-3">
        <Button
          variant="primary"
          disabled={!canTerminal}
          disabledReason="This device has no SSH transport configured"
          onClick={() => onOpenTerminal({ id: node.id, name: node.name })}
          title="Open an SSH shell to this device"
          className="w-full"
          icon={<TerminalSquare className="h-4 w-4" aria-hidden />}
        >
          SSH into {node.name}
        </Button>
        <Link
          href={`/devices/${node.id}`}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-line px-3 py-2 text-sm text-muted hover:text-[color:var(--text)] transition-colors"
        >
          <ExternalLink className="h-4 w-4" />
          Open in source of truth
        </Link>
      </div>
    </aside>
  );
}
