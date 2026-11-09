"use client";

import { useMutation } from "@tanstack/react-query";
import {
  ShieldCheck,
  Loader2,
  Check,
  X,
  Minus,
  AlertTriangle,
} from "lucide-react";
import { api, type AiFactoryValidateCheck } from "@/lib/api";
import { cn } from "@/lib/utils";
import { errorText } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";

/**
 * Admin-only "Validate observability stack" — the UI form of doc 02 §4A.
 * Runs the backend's self-check and renders the returned checklist with
 * pass/fail/skip badges + details.
 */
export function ValidatePanel() {
  const toast = useToast();
  const validate = useMutation({
    mutationFn: api.aiFactory.validateObservability,
    onMutate: () => toast.info("Observability check started. It takes about 10 seconds."),
    onSuccess: (r) => {
      const failed = r.checks.filter((c) => c.status === "fail").length;
      if (r.passed) toast.success("Observability check passed: GPU metrics are flowing.");
      else toast.error(`Observability check finished: ${failed} check${failed === 1 ? "" : "s"} failed. See the list below.`);
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const result = validate.data;

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-muted max-w-[560px]" role="status">
          {result
            ? `Last run at ${new Date(validate.submittedAt).toLocaleTimeString()}.`
            : validate.isPending
              ? "Checking exporters, scrape targets and queries…"
              : "Not run yet in this session. It checks the GPU exporters, the scrape targets and that the metrics can be queried."}
        </p>
        <Button
          variant="primary"
          size="sm"
          className="shrink-0"
          pending={validate.isPending}
          onClick={() => validate.mutate()}
          icon={<ShieldCheck className="h-3.5 w-3.5" aria-hidden />}
        >
          {validate.isPending ? "Running self-check…" : result ? "Run again (~10s)" : "Run self-check (~10s)"}
        </Button>
      </div>

      {validate.isPending && (
        <div className="mt-4 space-y-1.5" aria-hidden>
          {["GPU exporters", "Scrape targets", "Metric queries"].map((label) => (
            <div
              key={label}
              className="flex items-center gap-2.5 rounded-lg border border-line bg-bg-base/40 px-3 py-2 text-xs text-muted motion-enter"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Checking {label.toLowerCase()}…
            </div>
          ))}
        </div>
      )}

      {validate.isError && (
        <div role="alert" className="mt-3 text-xs text-[color:var(--critical)] flex items-center gap-1.5">
          <AlertTriangle className="h-3.5 w-3.5" aria-hidden /> {errorText(validate.error)}
        </div>
      )}

      {result && !validate.isPending && (
        <div className="mt-4 motion-enter" key={validate.submittedAt}>
          <div className="flex flex-wrap items-center gap-2 mb-3 text-xs">
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border",
                result.passed
                  ? "border-accent-emerald/40 bg-accent-emerald/10 text-[color:var(--text)]"
                  : "border-signal-critical/40 bg-signal-critical/10 text-[color:var(--critical)]"
              )}
            >
              {result.passed ? (
                <Check className="h-3.5 w-3.5 motion-success" aria-hidden />
              ) : (
                <X className="h-3.5 w-3.5" />
              )}
              {result.passed ? "All checks passed" : "Some checks failed"}
            </span>
            <span className="text-muted font-mono text-[11px]">
              {result.run_id}
            </span>
          </div>
          <div className="space-y-1.5">
            {result.checks.map((c, i) => (
              <CheckRow key={i} check={c} />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function CheckRow({ check }: { check: AiFactoryValidateCheck }) {
  const icon =
    check.status === "pass" ? (
      <Check className="h-3.5 w-3.5 text-accent-emerald" />
    ) : check.status === "fail" ? (
      <X className="h-3.5 w-3.5 text-[color:var(--critical)]" />
    ) : (
      <Minus className="h-3.5 w-3.5 text-muted" />
    );
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-line bg-bg-base/40 px-3 py-2">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0">
        <div className="text-xs font-medium">{check.name}</div>
        {check.detail && (
          <div className="text-[11px] text-muted">{check.detail}</div>
        )}
      </div>
    </div>
  );
}
