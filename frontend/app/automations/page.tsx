"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Workflow as WorkflowIcon, Play, CheckCircle2, AlertTriangle } from "lucide-react";
import { api, type WorkflowDescriptor } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatRelative } from "@/lib/utils";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { LoadState } from "@/components/ui/load-state";
import { buttonClass } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

/**
 * What each code-registered workflow does, in plain English, and what it
 * needs from the operator before it can run. The API only returns the
 * workflow's name and module, so the copy lives here.
 */
const CATALOG: Record<
  string,
  { title: string; does: string; runLabel: string; needsTitle?: boolean }
> = {
  "infra.incident.coordinate": {
    title: "Coordinate an incident",
    does:
      "Opens a new incident, marks it as investigating, drafts a status update and posts it to your incidents Slack channel (and email, if set).",
    runLabel: "Open incident",
    needsTitle: true,
  },
};

export default function AutomationsPage() {
  const qc = useQueryClient();
  const workflowsQ = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.workflows.list(),
  });
  const runsQ = useQuery({
    queryKey: ["workflow-runs"],
    queryFn: () => api.workflows.runs(),
    refetchInterval: 10_000,
  });
  const runs = runsQ.data ?? [];

  return (
    <div className="space-y-6 max-w-[1200px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <WorkflowIcon className="h-5 w-5 text-accent" aria-hidden /> Automations
        </h1>
        <p className="text-muted text-sm mt-1">
          Automations you can start by hand, and the history of every run.
          Remediations the agent runs for an alert are listed separately, under{" "}
          <Link href="/workflows" className="underline hover:text-fg">
            agent remediations
          </Link>
          .
        </p>
      </div>

      <section>
        <h2 className="text-sm uppercase tracking-wider text-muted mb-3">
          Automations you can start
        </h2>
        <LoadState
          query={workflowsQ}
          loadingText="Loading automations…"
          empty={(workflowsQ.data ?? []).length === 0}
          emptyTitle="No automations available"
          emptyHint="Automations appear here when a module that provides them is enabled."
        >
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {(workflowsQ.data ?? []).map((w) => (
              <WorkflowCard
                key={w.name}
                w={w}
                onRan={() => qc.invalidateQueries({ queryKey: ["workflow-runs"] })}
              />
            ))}
          </div>
        </LoadState>
      </section>

      <section>
        <h2 className="text-sm uppercase tracking-wider text-muted mb-3">
          Recent runs
        </h2>
        <LoadState
          query={runsQ}
          loadingText="Loading runs…"
          empty={runs.length === 0}
          emptyTitle="No runs yet"
          emptyHint="Start an automation above and its run shows here."
        >
          <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead className="text-[11px] uppercase tracking-wider text-muted bg-bg-elevated/50">
                <tr>
                  <th className="text-left px-4 py-2.5">Automation</th>
                  <th className="text-left px-4 py-2.5">Module</th>
                  <th className="text-left px-4 py-2.5">Status</th>
                  <th className="text-left px-4 py-2.5">Started</th>
                  <th className="text-left px-4 py-2.5">Finished</th>
                </tr>
              </thead>
              <tbody>
                {runs.slice(0, 25).map((r) => (
                  <tr key={r.id} className="motion-enter border-t border-line/60">
                    <td className="px-4 py-2.5">
                      <div className="font-medium">
                        {CATALOG[r.workflow_name]?.title ?? r.workflow_name}
                      </div>
                      {r.error_message && (
                        <div
                          className="text-xs text-muted line-clamp-1"
                          title={r.error_message}
                        >
                          {r.error_message}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-muted">{r.module}</td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`text-[11px] uppercase tracking-wider rounded px-1.5 py-0.5 ${
                          r.status === "succeeded"
                            ? "bg-accent/15 text-accent"
                            : r.status === "failed"
                            ? "bg-signal-critical/15 text-signal-critical"
                            : "bg-signal-info/15 text-signal-info"
                        }`}
                      >
                        {r.status}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-muted">{formatRelative(r.started_at)}</td>
                    <td className="px-4 py-2.5 text-muted">{formatRelative(r.finished_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </LoadState>
      </section>
    </div>
  );
}

function WorkflowCard({ w, onRan }: { w: WorkflowDescriptor; onRan: () => void }) {
  const { user } = useAuth();
  const toast = useToast();
  const inputId = useId();
  const meta = CATALOG[w.name];
  const [title, setTitle] = useState("");
  const [result, setResult] = useState<"ok" | "error" | null>(null);

  const trigger = useMutation({
    mutationFn: () =>
      api.workflows.run(
        w.name,
        meta?.needsTitle
          ? { summary: title.trim(), tenant_id: user?.tenant_id }
          : {}
      ),
    onSuccess: () => {
      setResult("ok");
      setTitle("");
      toast.success(`${meta?.title ?? w.name} started. Its run is in Recent runs.`);
      onRan();
    },
    onError: (e) => {
      setResult("error");
      toast.error(errorText(e));
      onRan();
    },
  });

  const ready = !meta?.needsTitle || title.trim().length > 0;

  return (
    <div className="rounded-2xl border border-line bg-bg-card p-4 space-y-3">
      <div>
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted">
          <WorkflowIcon className="h-3.5 w-3.5 text-accent" />
          {w.module}
        </div>
        <div className="text-sm font-medium mt-1">{meta?.title ?? w.name}</div>
        <p className="text-xs text-muted mt-1 leading-relaxed">
          {meta?.does ?? "Runs this workflow with no input."}
        </p>
      </div>

      {meta?.needsTitle && (
        <div>
          <label htmlFor={inputId} className="block text-xs font-medium mb-1">
            Incident title
          </label>
          <input
            id={inputId}
            value={title}
            onChange={(e) => {
              setTitle(e.target.value);
              setResult(null);
            }}
            placeholder="e.g. Checkout API returning 502s"
            className="w-full h-9 rounded-lg border border-line bg-bg-elevated/40 px-3 text-sm placeholder:text-muted focus:outline-none focus:border-accent/60"
          />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <ConfirmButton
          label={meta?.runLabel ?? "Run"}
          confirmLabel={meta?.needsTitle ? "Open & post to Slack" : "Run now"}
          className={cn(buttonClass("primary", "sm"), "border-0")}
          consequence={
            meta?.needsTitle
              ? "Posts to your incidents channel."
              : "Starts it right away."
          }
          icon={<Play className="h-3 w-3" aria-hidden />}
          onConfirm={() => trigger.mutate()}
          pending={trigger.isPending}
          disabled={!ready}
        />
        {result === "ok" && (
          <span className="motion-enter text-xs inline-flex items-center gap-1 text-accent">
            <CheckCircle2 className="h-3.5 w-3.5 motion-success" aria-hidden /> Started. See Recent runs below.
          </span>
        )}
        {result === "error" && (
          <span className="motion-enter text-xs inline-flex items-center gap-1" style={{ color: "var(--critical)" }}>
            <AlertTriangle className="h-3.5 w-3.5" aria-hidden /> It failed. The run below has the reason.
          </span>
        )}
      </div>
    </div>
  );
}
