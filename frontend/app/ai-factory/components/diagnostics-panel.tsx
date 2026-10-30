"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Loader2, Play, AlertTriangle } from "lucide-react";
import {
  api,
  type AiFactoryDiagKind,
  type AiFactoryDiagRun,
} from "@/lib/api";
import { Accordion, AccordionItem } from "@/components/ui/accordion";
import { LoadState } from "@/components/ui/load-state";
import { RunStateBadge, fmtWhen, useRecent } from "./run-state";

/**
 * Admin-only GPU diagnostics. "Run dcgmi diag" (level 1/2/3) and "Run NCCL
 * test" (multi-GPU only). The runs table polls every ~4s while any run is
 * pending/running so freshly-kicked diagnostics surface their result
 * without a manual refresh.
 */
type RunBody = { kind: AiFactoryDiagKind; level?: 1 | 2 | 3; acknowledged?: boolean };

export function DiagnosticsPanel({ gpuClass }: { gpuClass: string | null }) {
  const qc = useQueryClient();
  const [level, setLevel] = useState<1 | 2 | 3>(1);
  const [err, setErr] = useState<string | null>(null);
  // When a stressful run needs confirmation, the backend returns the warning
  // and we hold the pending request here to re-submit on acknowledge.
  const [ackPrompt, setAckPrompt] = useState<{
    body: RunBody;
    warning: string;
  } | null>(null);

  const runs = useQuery({
    queryKey: ["aiFactory", "diagnostics"],
    queryFn: api.aiFactory.diagnostics,
    refetchInterval: (q) => {
      const data = q.state.data as { runs?: AiFactoryDiagRun[] } | undefined;
      const busy = (data?.runs ?? []).some(
        (r) => r.state === "pending" || r.state === "running"
      );
      return busy ? 4_000 : 30_000;
    },
  });

  const run = useMutation({
    mutationFn: (body: RunBody) => api.aiFactory.runDiagnostic(body),
    onMutate: () => setErr(null),
    onSuccess: (result, body) => {
      if (result.requiresAck) {
        // Surface the warning + hold the request for an explicit ack.
        setAckPrompt({ body, warning: result.warning });
        return;
      }
      setAckPrompt(null);
      qc.invalidateQueries({ queryKey: ["aiFactory", "diagnostics"] });
    },
    onError: (e: unknown) =>
      setErr(e instanceof Error ? e.message : "failed to start diagnostic"),
  });

  // NCCL is multi-GPU only. We can't always know the GPU count; a
  // single-card class (anything not hinting "x2"/"x4"/"8x"/"multi") hides
  // the button and says why instead.
  const nccl = ncclEligible(gpuClass);

  const allRuns = runs.data?.runs ?? [];
  const recent = useRecent(allRuns);

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
        <div className="flex flex-wrap items-center gap-3">
          <div className="inline-flex items-center gap-2">
            <label className="text-xs text-muted" htmlFor="diag-level">
              Level
            </label>
            <select
              id="diag-level"
              value={level}
              onChange={(e) => setLevel(Number(e.target.value) as 1 | 2 | 3)}
              className="h-8 rounded-md border border-line bg-bg-base px-2 text-xs"
            >
              <option value={1}>1 — quick</option>
              <option value={2}>2 — medium</option>
              <option value={3}>3 — long</option>
            </select>
            <button
              type="button"
              disabled={run.isPending}
              onClick={() => run.mutate({ kind: "dcgmi_diag", level })}
              className="text-xs h-8 px-3 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base inline-flex items-center gap-1.5 disabled:opacity-60"
            >
              {run.isPending && run.variables?.kind === "dcgmi_diag" ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              Run dcgmi diag
            </button>
          </div>

          {nccl && (
            <button
              type="button"
              disabled={run.isPending}
              onClick={() => run.mutate({ kind: "nccl_test" })}
              className="text-xs h-8 px-3 rounded-lg border border-accent-emerald/60 text-[color:var(--text)] hover:bg-accent-emerald/15 disabled:opacity-60 inline-flex items-center gap-1.5 transition-colors"
            >
              {run.isPending && run.variables?.kind === "nccl_test" ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              Run NCCL test
            </button>
          )}
        </div>

        {!nccl && (
          <div className="mt-2 text-[11px] text-muted">
            The NCCL test measures the GPU-to-GPU interconnect, so it needs more
            than one GPU. This node has a single card.
          </div>
        )}

        {err && (
          <div className="mt-3 text-[11px] text-[color:var(--critical)] flex items-center gap-1.5">
            <AlertTriangle className="h-3 w-3" /> {err}
          </div>
        )}
      </section>

      <section className="space-y-2">
        <h2 className="text-sm font-medium">Past runs</h2>
        <LoadState
          query={runs}
          empty={allRuns.length === 0}
          loadingText="Loading diagnostic runs…"
          emptyTitle="No diagnostic runs yet"
          emptyHint="Run dcgmi diag above for a quick health pass of the card."
        >
          <Accordion>
            {recent.shown.map((r) => (
              <DiagRow key={r.id} run={r} gpuClass={gpuClass} />
            ))}
          </Accordion>
          {recent.toggle && <div className="pt-1">{recent.toggle}</div>}
        </LoadState>
      </section>

      {ackPrompt && (
        <AckModal
          warning={ackPrompt.warning}
          busy={run.isPending}
          onCancel={() => setAckPrompt(null)}
          onConfirm={() =>
            run.mutate({ ...ackPrompt.body, acknowledged: true })
          }
        />
      )}
    </div>
  );
}

function AckModal({
  warning,
  busy,
  onConfirm,
  onCancel,
}: {
  warning: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-[2px] p-4"
      role="dialog"
      aria-modal="true"
    >
      <div className="w-full max-w-md rounded-xl border border-signal-warning/40 bg-[color:var(--bg-elevated)] p-5 shadow-xl">
        <h4 className="text-sm font-semibold flex items-center gap-2 text-[color:var(--warning)]">
          <AlertTriangle className="h-4 w-4" /> This run stresses the GPU
        </h4>
        <p className="text-xs text-muted mt-3 leading-relaxed">{warning}</p>
        <p className="text-[11px] text-muted mt-3">
          This is a shared production card. Make sure you understand the impact
          before continuing.
        </p>
        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="text-xs h-8 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--text)]"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className="text-xs h-8 px-3 rounded-lg bg-signal-warning/90 text-bg-base font-medium inline-flex items-center gap-1.5 disabled:opacity-60"
          >
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            I understand — run it
          </button>
        </div>
      </div>
    </div>
  );
}

function ncclEligible(gpuClass: string | null): boolean {
  if (!gpuClass) return false;
  const c = gpuClass.toLowerCase();
  // Heuristic: a class that advertises a count > 1 or "multi". Single-card
  // classes (e.g. "ada-16", "a100-80") fall through to disabled.
  return /(?:^|[^0-9])(?:x|×)?(?:2|4|8)\b|multi|x2|x4|x8|8x/.test(c);
}

function summaryString(
  summary: Record<string, unknown> | null,
  key: string
): string | null {
  const v = summary?.[key];
  return typeof v === "string" ? v : null;
}

function summaryNumber(
  summary: Record<string, unknown> | null,
  key: string
): number | null {
  const v = summary?.[key];
  return typeof v === "number" ? v : null;
}

/**
 * One line saying why a run failed, for the folded row. Prefers the server's
 * error, then the dcgmi check counts, then the last line of the output.
 */
function failureReason(
  run: AiFactoryDiagRun,
  gpuClass: string | null
): string | null {
  if (run.state !== "failed" && run.state !== "error") return null;
  const error = summaryString(run.summary, "error");
  if (error) return error;
  const failed = summaryNumber(run.summary, "failed");
  const checks = summaryNumber(run.summary, "checks");
  if (failed !== null && checks !== null) {
    return `${failed} of ${checks} checks failed`;
  }
  if (run.kind === "nccl_test" && !ncclEligible(gpuClass)) {
    return "Needs 2 or more GPUs; this node has one";
  }
  const tail = summaryString(run.summary, "raw_tail");
  const lastLine = tail
    ?.trim()
    .split("\n")
    .filter((l) => l.trim())
    .pop();
  return lastLine ?? "No reason recorded; open the run for its output";
}

function DiagRow({
  run,
  gpuClass,
}: {
  run: AiFactoryDiagRun;
  gpuClass: string | null;
}) {
  const label =
    run.kind === "dcgmi_diag"
      ? `dcgmi diag${run.level ? ` · level ${run.level}` : ""}`
      : "NCCL interconnect test";
  const reason = failureReason(run, gpuClass);

  return (
    <AccordionItem
      id={run.id}
      tone={
        run.state === "failed"
          ? "var(--critical)"
          : run.state === "error"
            ? "var(--warning)"
            : undefined
      }
      title={
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{label}</span>
            <RunStateBadge state={run.state} />
          </div>
          {reason && (
            <div className="text-[11px] text-muted line-clamp-2 break-words">
              {reason}
            </div>
          )}
        </div>
      }
      meta={fmtWhen(run.started_at)}
    >
      <DiagDetail run={run} />
    </AccordionItem>
  );
}

function DiagDetail({ run }: { run: AiFactoryDiagRun }) {
  const busy = run.state === "pending" || run.state === "running";
  // Fetched only while the row is open (the accordion mounts this then).
  const detail = useQuery({
    queryKey: ["aiFactory", "diagnostic", run.id],
    queryFn: () => api.aiFactory.diagnostic(run.id),
    refetchInterval: busy ? 4_000 : false,
  });
  const errorMsg = summaryString(run.summary, "error");
  const command = summaryString(run.summary, "command");

  return (
    <div className="space-y-2">
      {command && (
        <div className="text-[11px] text-muted font-mono break-all">
          $ {command}
        </div>
      )}
      {detail.isLoading ? (
        <div className="text-[11px] text-muted inline-flex items-center gap-1.5">
          <Loader2 className="h-3 w-3 animate-spin" /> Loading output…
        </div>
      ) : (
        <pre className="text-[11px] font-mono bg-bg-base rounded-md border border-line p-3 overflow-x-auto whitespace-pre-wrap max-h-72 overflow-y-auto">
          {detail.data?.output ||
            errorMsg ||
            (run.summary ? JSON.stringify(run.summary, null, 2) : "(no output)")}
        </pre>
      )}
    </div>
  );
}
