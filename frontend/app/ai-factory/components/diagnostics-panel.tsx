"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { Loader2, Play, AlertTriangle } from "lucide-react";
import {
  api,
  type AiFactoryDiagKind,
  type AiFactoryDiagRun,
} from "@/lib/api";
import { Accordion, AccordionItem } from "@/components/ui/accordion";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";
import { cn } from "@/lib/utils";
import { RunStateBadge, When, useRecent, useRunWatch } from "./run-state";

/**
 * Admin-only GPU diagnostics. "Run dcgmi diag" (level 1/2/3) and "Run NCCL
 * test" (multi-GPU only). The runs table polls every ~4s while any run is
 * pending/running so freshly-kicked diagnostics surface their result
 * without a manual refresh.
 */
type RunBody = { kind: AiFactoryDiagKind; level?: 1 | 2 | 3; acknowledged?: boolean };

export function DiagnosticsPanel({ gpuClass }: { gpuClass: string | null }) {
  const qc = useQueryClient();
  const toast = useToast();
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
      toast.success(
        body.kind === "nccl_test"
          ? "Interconnect test started. It appears in Past runs below."
          : `Health check (level ${body.level ?? 1}) started. It appears in Past runs below.`
      );
      qc.invalidateQueries({ queryKey: ["aiFactory", "diagnostics"] });
    },
    onError: (e: unknown) => {
      setErr(errorText(e));
      toast.error(errorText(e));
    },
  });

  // NCCL is multi-GPU only. We can't always know the GPU count; a
  // single-card class (anything not hinting "x2"/"x4"/"8x"/"multi") hides
  // the button and says why instead.
  const nccl = ncclEligible(gpuClass);

  const allRuns = runs.data?.runs ?? [];
  const recent = useRecent(allRuns);
  const describe = useCallback(
    (r: AiFactoryDiagRun) => (r.kind === "nccl_test" ? "Interconnect test" : "GPU health check"),
    []
  );
  const fresh = useRunWatch(runs.data?.runs, describe);

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex flex-wrap items-center gap-2 min-w-0">
            <label className="text-xs text-muted whitespace-nowrap" htmlFor="diag-level">
              Check depth
            </label>
            <select
              id="diag-level"
              value={level}
              onChange={(e) => setLevel(Number(e.target.value) as 1 | 2 | 3)}
              className="h-8 rounded-md border border-line bg-bg-base px-2 text-xs"
            >
              <option value={1}>Level 1: quick (seconds)</option>
              <option value={2}>Level 2: medium (minutes)</option>
              <option value={3}>Level 3: long (stresses the GPU)</option>
            </select>
            <Button
              variant="primary"
              size="sm"
              disabled={run.isPending}
              pending={run.isPending && run.variables?.kind === "dcgmi_diag"}
              onClick={() => run.mutate({ kind: "dcgmi_diag", level })}
              icon={<Play className="h-3.5 w-3.5" aria-hidden />}
            >
              Run health check
            </Button>
          </div>

          <Button
            size="sm"
            disabled={!nccl || run.isPending}
            disabledReason={nccl ? undefined : "Needs 2 or more GPUs on one node; your GPU class has one"}
            pending={run.isPending && run.variables?.kind === "nccl_test"}
            onClick={() => run.mutate({ kind: "nccl_test" })}
            icon={<Play className="h-3.5 w-3.5" aria-hidden />}
          >
            Run interconnect test
          </Button>
        </div>

        <div className="mt-2 text-xs text-muted">
          The health check tests each GPU (dcgmi diag).{" "}
          {nccl
            ? "The interconnect test measures GPU-to-GPU bandwidth (NCCL)."
            : "The interconnect test (NCCL) measures GPU-to-GPU bandwidth, so it needs 2 or more GPUs on one node; your GPU class has one."}
        </div>

        {err && (
          <div role="alert" className="mt-3 text-xs text-[color:var(--critical)] flex items-center gap-1.5">
            <AlertTriangle className="h-3.5 w-3.5" aria-hidden /> {err}
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
          emptyHint="Run a level 1 health check above for a quick pass over the card."
        >
          <Accordion>
            {recent.shown.map((r) =>
              fresh.has(r.id) ? (
                <div key={r.id} className="motion-enter">
                  <DiagRow run={r} gpuClass={gpuClass} fresh />
                </div>
              ) : (
                <DiagRow key={r.id} run={r} gpuClass={gpuClass} />
              )
            )}
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
    <Dialog
      open
      onClose={onCancel}
      size="sm"
      title={
        <span className="flex items-center gap-2 text-[color:var(--warning)]">
          <AlertTriangle className="h-4 w-4" aria-hidden /> This run stresses the GPU
        </span>
      }
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="danger" pending={busy} onClick={onConfirm}>
            Run it on the shared GPU
          </Button>
        </>
      }
    >
      <p className="text-sm text-muted leading-relaxed">{warning}</p>
      <p className="text-xs text-muted mt-3">
        This is a shared production card. Requests served from it may slow
        down while the test runs.
      </p>
    </Dialog>
  );
}

/**
 * An "error" that is really the environment saying no (a single-GPU node, a
 * tool the image doesn't ship), not a fault: shown as "Skipped".
 */
function environmentLimit(run: AiFactoryDiagRun, gpuClass: string | null): string | null {
  if (run.state !== "error") return null;
  if (run.kind === "nccl_test" && !ncclEligible(gpuClass)) {
    return "Needs 2 or more GPUs on one node; this node has one";
  }
  const error = summaryString(run.summary, "error") ?? "";
  if (
    /not (installed|available|supported|found)|unsupported|requires (2|two|more|multiple)|single[- ]gpu|no (gpus?|such file)|disabled/i.test(
      error
    )
  ) {
    return error;
  }
  return null;
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
  fresh = false,
}: {
  run: AiFactoryDiagRun;
  gpuClass: string | null;
  /** Just appeared from a run started on this page: tint it once. */
  fresh?: boolean;
}) {
  const label =
    run.kind === "dcgmi_diag"
      ? `GPU health check${run.level ? ` · level ${run.level}` : ""}`
      : "Interconnect test";
  const skipped = environmentLimit(run, gpuClass);
  const reason = skipped ?? failureReason(run, gpuClass);

  return (
    <AccordionItem
      id={run.id}
      tone={
        skipped
          ? undefined
          : run.state === "failed"
            ? "var(--critical)"
            : run.state === "error"
              ? "var(--warning)"
              : undefined
      }
      title={
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn("font-medium px-1 -mx-1", fresh && "motion-update")}>{label}</span>
            <RunStateBadge
              state={run.state}
              label={skipped ? "Skipped" : undefined}
              title={skipped ?? undefined}
            />
          </div>
          {reason && (
            <div className="text-xs text-muted line-clamp-2 break-words">
              {reason}
            </div>
          )}
        </div>
      }
      meta={<When iso={run.started_at} />}
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
        <div role="status" className="text-xs text-muted inline-flex items-center gap-1.5">
          <Loader2 className="h-3 w-3 animate-spin" aria-hidden /> Loading output…
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
