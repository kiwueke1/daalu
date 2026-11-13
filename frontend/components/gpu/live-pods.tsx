"use client";

/**
 * Live `kubectl get pods` for what Add GPU installs — the GPU Operator, or
 * the served model — read over the tunnel, with each pod's logs one click
 * away. Rows read red while a pod is coming up or stuck and turn green when
 * it's ready, like k9s, so the install is visible in Daalu and matches what
 * `kubectl` shows on the cluster.
 */

import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp, Loader2, Terminal } from "lucide-react";
import { api, type GpuPodRow, type GpuPodsTarget } from "@/lib/api";
import { cn } from "@/lib/utils";
import { errorText } from "@/lib/errors";

/** kubectl-style age: 45s, 3m, 2h, 4d. */
export function kubeAge(seconds?: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 120) return `${seconds}s`;
  if (seconds < 7200) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 172800) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

const HEALTH_TEXT: Record<GpuPodRow["health"], string> = {
  ready: "text-[color:var(--accent)]",
  done: "text-muted",
  starting: "text-[color:var(--critical)]",
  failing: "text-[color:var(--critical)] font-semibold",
};

export function LivePods({
  target,
  clusterId,
  title,
  defaultOpen = false,
}: {
  target: GpuPodsTarget;
  clusterId?: string | null;
  /** e.g. "GPU Operator pods" */
  title: string;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [selected, setSelected] = useState<string | null>(null);

  const pods = useQuery({
    queryKey: ["gpu", "pods", target, clusterId ?? null],
    queryFn: () => api.onboarding.gpu.pods(target, clusterId),
    // Each read crosses the tunnel to the cluster's API: fast while you're
    // watching, slow when folded (the folded line still shows the counts).
    refetchInterval: open ? 3_000 : 10_000,
    placeholderData: keepPreviousData,
    retry: false,
  });

  const rows = pods.data?.pods ?? [];
  const namespace = pods.data?.namespace ?? (target === "operator" ? "gpu-operator" : "daalu");
  const ready = rows.filter((r) => r.health === "ready" || r.health === "done").length;
  const failing = rows.filter((r) => r.health === "failing").length;
  const selectedRow = rows.find((r) => r.name === selected) ?? null;

  return (
    <div className="rounded-lg border border-line bg-bg-base/40">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center justify-between gap-2 px-3 py-2 text-left text-xs"
      >
        <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <Terminal className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
          <span className="font-semibold">{title}</span>
          <span className="font-mono text-muted">kubectl get pods -n {namespace}</span>
        </span>
        <span className="flex items-center gap-2">
          {rows.length > 0 && (
            <span className="text-muted">
              {ready} of {rows.length} ready
              {failing > 0 && (
                <span className="text-[color:var(--critical)]"> · {failing} failing</span>
              )}
            </span>
          )}
          {open ? (
            <ChevronUp className="h-3.5 w-3.5 text-muted" aria-hidden />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 text-muted" aria-hidden />
          )}
        </span>
      </button>

      {open && (
        <div className="space-y-2 border-t border-line px-3 py-3">
          {pods.isError && (
            <div role="status" className="flex items-start gap-1.5 text-xs text-[color:var(--warning)]">
              <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" aria-hidden />
              <span>
                Can&apos;t reach the cluster&apos;s API right now ({errorText(pods.error)}).
                {rows.length > 0 ? " Showing the last answer; " : " "}retrying every few seconds.
              </span>
            </div>
          )}
          {pods.isLoading && (
            <div role="status" className="flex items-center gap-1.5 text-xs text-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> Reading pods…
            </div>
          )}
          {pods.isSuccess && rows.length === 0 && (
            <div className="text-xs text-muted">No pods in {namespace} yet.</div>
          )}

          {rows.length > 0 && (
            <div className="relative overflow-x-auto rounded border border-line bg-bg-elevated/40">
              <table className="w-full font-mono text-xs">
                <thead>
                  <tr className="text-left text-muted">
                    <th className="px-2 py-1 font-normal">NAME</th>
                    <th className="px-2 py-1 font-normal">READY</th>
                    <th className="px-2 py-1 font-normal">STATUS</th>
                    <th className="px-2 py-1 font-normal">RESTARTS</th>
                    <th className="px-2 py-1 font-normal">AGE</th>
                    <th className="px-2 py-1 font-normal">NODE</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <PodRowView
                      key={r.name}
                      row={r}
                      open={selected === r.name}
                      onToggle={() => setSelected((s) => (s === r.name ? null : r.name))}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {selectedRow ? (
            <PodLogs
              key={selectedRow.name}
              row={selectedRow}
              target={target}
              clusterId={clusterId}
              namespace={namespace}
            />
          ) : (
            rows.length > 0 && <p className="text-xs text-muted">Click a pod to see its logs.</p>
          )}
        </div>
      )}
    </div>
  );
}

function PodRowView({
  row,
  open,
  onToggle,
}: {
  row: GpuPodRow;
  open: boolean;
  onToggle: () => void;
}) {
  const tone = HEALTH_TEXT[row.health];
  return (
    <tr className={cn("border-t border-line/50", open && "bg-bg-elevated")}>
        <td className="px-2 py-1">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            className={cn("inline-flex items-center gap-1 whitespace-nowrap text-left hover:underline", tone)}
          >
            {row.health === "failing" && (
              <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden />
            )}
            {row.name}
          </button>
        </td>
        <td className={cn("px-2 py-1", tone)}>{row.ready}</td>
        <td className={cn("px-2 py-1 whitespace-nowrap", tone)}>{row.status}</td>
        <td className={cn("px-2 py-1", row.restarts > 0 ? "text-[color:var(--warning)]" : "text-muted")}>
          {row.restarts}
        </td>
        <td className="px-2 py-1 text-muted">{kubeAge(row.age_seconds)}</td>
        <td className="px-2 py-1 whitespace-nowrap text-muted">{row.node ?? "—"}</td>
    </tr>
  );
}

function PodLogs({
  row,
  target,
  clusterId,
  namespace,
}: {
  row: GpuPodRow;
  target: GpuPodsTarget;
  clusterId?: string | null;
  namespace: string;
}) {
  const [container, setContainer] = useState<string | null>(row.log_container ?? null);
  const logs = useQuery({
    queryKey: ["gpu", "podLogs", target, clusterId ?? null, row.name, container],
    queryFn: () => api.onboarding.gpu.podLogs(target, row.name, container, clusterId),
    refetchInterval: 3_000,
    placeholderData: keepPreviousData,
    retry: false,
  });
  const pre = useRef<HTMLPreElement>(null);
  const text = logs.data?.logs ?? "";
  // Follow the tail, like `kubectl logs -f`.
  useEffect(() => {
    if (pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [text]);

  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <span className="font-mono break-all">
          kubectl logs -n {namespace} {row.name}
          {container ? ` -c ${container}` : ""}
        </span>
        {row.containers.length > 1 && (
          <label className="flex items-center gap-1">
            <span>Container</span>
            <select
              value={container ?? ""}
              onChange={(e) => setContainer(e.target.value || null)}
              className="rounded border border-line bg-bg-base px-1 py-0.5 font-mono text-xs"
            >
              {row.containers.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <pre
        ref={pre}
        className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded border border-line bg-bg-base px-2 py-1.5 font-mono text-xs leading-relaxed text-[color:var(--text)]"
      >
        {logs.isLoading
          ? "Reading logs…"
          : logs.isError
            ? `Can't read logs right now: ${errorText(logs.error)}`
            : text.trim() || "No log output yet (the container hasn't started)."}
      </pre>
    </div>
  );
}
