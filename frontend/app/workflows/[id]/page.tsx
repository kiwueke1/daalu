"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  Workflow as WorkflowIcon,
  AlertTriangle,
  ChevronLeft,
  CheckCircle2,
  XCircle,
  Wrench,
  Search,
  Loader2,
} from "lucide-react";
import { api, type WorkflowStep } from "@/lib/api";
import { formatRelative } from "@/lib/utils";
import { StatusBadge } from "@/components/workflows/status-badge";

/**
 * /workflows/[id] — step-by-step replay of one remediation run: each tool the
 * agent executed, its input, output and outcome, plus a link back to the alert.
 */
export default function WorkflowDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;

  const { data: run, isLoading } = useQuery({
    queryKey: ["workflow-run", id],
    queryFn: () => api.workflows.runDetail(id),
    // Poll while running so the steps stream in as they execute.
    refetchInterval: (q) =>
      q.state.data?.status === "running" ? 1500 : false,
  });

  return (
    <div className="space-y-6 max-w-[1000px]">
      <Link
        href="/workflows"
        className="inline-flex items-center gap-1 text-xs text-muted hover:text-fg"
      >
        <ChevronLeft className="h-4 w-4" /> Workflows
      </Link>

      {isLoading && <div className="text-muted text-sm">Loading…</div>}
      {!isLoading && !run && (
        <div className="text-muted text-sm">Workflow run not found.</div>
      )}

      {run && (
        <>
          <div>
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className="text-2xl font-semibold flex items-center gap-2">
                <WorkflowIcon className="h-5 w-5 text-accent-cyan" />
                {run.workflow_name}
              </h1>
              <StatusBadge status={run.status} />
            </div>
            <div className="text-muted text-sm mt-1 flex items-center gap-3 flex-wrap">
              <span
                className="font-mono text-xs px-1.5 py-0.5 rounded bg-bg-elevated/60 border border-line"
                title={run.id}
              >
                #{run.id.slice(0, 8)}
              </span>
              <span>Started {formatRelative(run.started_at)}</span>
              {run.finished_at && (
                <span>· Finished {formatRelative(run.finished_at)}</span>
              )}
              {run.alert_id && (
                <Link
                  href={`/alerts/${run.alert_id}`}
                  className="inline-flex items-center gap-1 hover:text-accent-cyan hover:underline"
                >
                  <AlertTriangle className="h-3.5 w-3.5" />
                  {run.alert_title ?? "source alert"}
                </Link>
              )}
            </div>
          </div>

          {run.error_message && (
            <div className="rounded-xl border border-accent-red/40 bg-accent-red/5 p-3 text-sm text-accent-red">
              {run.error_message}
            </div>
          )}

          <section className="space-y-3">
            <h2 className="text-sm uppercase tracking-wider text-muted">
              Steps
            </h2>
            <ol className="space-y-3">
              {(run.steps ?? []).map((s) => (
                <StepCard key={s.order} step={s} />
              ))}
              {run.status === "running" && (
                <li className="rounded-2xl border border-dashed border-line bg-bg-card px-4 py-3 text-sm text-muted flex items-center gap-2">
                  <Loader2 className="h-4 w-4 animate-spin" style={{ color: "var(--accent)" }} />
                  {(run.steps ?? []).length === 0
                    ? "Starting — running the approved action…"
                    : "Verifying — waiting for the workload to recover (this can take a minute)…"}
                </li>
              )}
              {run.status !== "running" && (run.steps ?? []).length === 0 && (
                <li className="text-muted text-sm">No steps recorded.</li>
              )}
            </ol>
          </section>
        </>
      )}
    </div>
  );
}

function StepCard({ step }: { step: WorkflowStep }) {
  const isError = step.status === "error";
  const KindIcon = step.kind === "verify" ? Search : Wrench;
  return (
    <li className="rounded-2xl border border-line bg-bg-card overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-line/60">
        <div className="flex items-center gap-3 min-w-0">
          <span
            className="h-7 w-7 rounded-lg flex items-center justify-center shrink-0 text-[11px] font-mono text-muted"
            style={{
              background: "color-mix(in srgb, var(--accent) 10%, transparent)",
            }}
          >
            {step.order}
          </span>
          <div className="min-w-0">
            <div className="text-sm font-medium truncate">{step.title}</div>
            <div className="text-[11px] text-muted flex items-center gap-1">
              <KindIcon className="h-3 w-3" />
              <span className="font-mono">
                {step.tool}({renderArgs(step.input)})
              </span>
            </div>
          </div>
        </div>
        {isError ? (
          <XCircle className="h-4 w-4 text-accent-red shrink-0" />
        ) : (
          <CheckCircle2 className="h-4 w-4 text-accent-emerald shrink-0" />
        )}
      </div>
      {step.output && (
        <pre
          className={`text-xs px-4 py-3 overflow-x-auto whitespace-pre-wrap font-mono ${
            isError ? "text-accent-red" : "text-fg/80"
          }`}
        >
          {step.output}
        </pre>
      )}
    </li>
  );
}

function renderArgs(input: Record<string, unknown>): string {
  const { reason: _reason, ...args } = input || {};
  return Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}
