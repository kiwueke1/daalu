"use client";

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Bot, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { LoadState } from "@/components/ui/load-state";
import { formatRelative } from "@/lib/utils";

export default function AgentsPage() {
  const agentsQ = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
  });
  const { data: runs } = useQuery({
    queryKey: ["agent-runs"],
    queryFn: () => api.agents.runs(),
    refetchInterval: 10_000,
  });

  const agents = agentsQ.data;

  return (
    <div className="space-y-6 max-w-[1200px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <Bot className="h-5 w-5 text-accent" aria-hidden /> Agents
        </h1>
        <p className="text-muted text-sm mt-1">
          AI workers continuously observing the event stream and acting on
          your behalf.
        </p>
      </div>

      <LoadState
        query={agentsQ}
        loadingText="Loading agents…"
        empty={(agents ?? []).length === 0}
        emptyTitle="No agents running"
        emptyHint="Agents start with the modules you enable; they then watch the event stream."
      >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {(agents ?? []).map((a) => {
          const mine = (runs ?? []).filter((r) => r.agent_name === a.name);
          const last = mine[0];
          const succ = mine.filter((r) => r.status === "ok").length;
          const total = mine.length;
          const failedRuns = mine.filter((r) => r.status === "error");
          const rate = total ? `${Math.round((succ / total) * 100)}%` : "—";
          return (
            <div
              key={a.name}
              className="motion-enter rounded-2xl border border-line bg-bg-card p-5 relative overflow-hidden"
            >
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-2">
                  <div className="h-9 w-9 rounded-lg bg-accent/15 text-accent flex items-center justify-center">
                    <Bot className="h-4 w-4" aria-hidden />
                  </div>
                  <div>
                    <div className="text-base font-medium">{a.name}</div>
                    <div className="text-[11px] uppercase tracking-wider text-muted">
                      {a.module}
                    </div>
                  </div>
                </div>
                <span
                  className={`text-[11px] uppercase tracking-wider rounded px-2 py-1 ${
                    last?.status === "error"
                      ? "bg-signal-critical/15 text-signal-critical"
                      : "bg-accent/15 text-accent"
                  }`}
                >
                  {last?.status ?? "idle"}
                </span>
              </div>
              <p className="text-sm text-muted mt-3">{a.description}</p>
              <div className="grid grid-cols-3 gap-2 mt-4 text-center">
                <Stat label="Success rate" value={rate} />
                <Stat label="Runs (24h)" value={total} />
                <Stat label="Last seen" value={formatRelative(last?.started_at)} />
              </div>
              {failedRuns.length > 0 && (
                <details className="group mt-3 text-xs">
                  <summary
                    className="cursor-pointer list-none inline-flex items-center gap-1.5 font-medium hover:underline"
                    style={{ color: "var(--warning)" }}
                  >
                    <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
                    View failed run{failedRuns.length === 1 ? "" : `s (${failedRuns.length})`}
                    <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90" aria-hidden />
                  </summary>
                  <ul className="mt-2 space-y-1.5">
                    {failedRuns.slice(0, 5).map((r) => (
                      <li key={r.id} className="rounded-lg bg-bg-elevated px-2.5 py-1.5">
                        <div className="text-muted text-xs">
                          {formatRelative(r.started_at)}
                          {r.activity ? ` · ${r.activity}` : ""}
                        </div>
                        <div className="break-words">
                          {r.error_message || "No error message recorded."}
                        </div>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              <details className="mt-3 text-xs text-muted">
                <summary className="cursor-pointer hover:text-fg">Details</summary>
                <div className="mt-1.5">
                  Listens to {a.subscribed_event_types.length} event type
                  {a.subscribed_event_types.length === 1 ? "" : "s"}:
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {a.subscribed_event_types.map((t) => (
                    <code key={t} className="rounded bg-bg-elevated px-1.5 py-0.5">
                      {t}
                    </code>
                  ))}
                </div>
              </details>
            </div>
          );
        })}
      </div>
      </LoadState>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg bg-bg-elevated py-2">
      <div className="text-sm font-medium">{value}</div>
      <div className="text-[11px] text-muted uppercase tracking-wider">{label}</div>
    </div>
  );
}
