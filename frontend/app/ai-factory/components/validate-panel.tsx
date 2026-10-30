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

/**
 * Admin-only "Validate observability stack" — the UI form of doc 02 §4A.
 * Runs the backend's self-check and renders the returned checklist with
 * pass/fail/skip badges + details.
 */
export function ValidatePanel() {
  const validate = useMutation({
    mutationFn: api.aiFactory.validateObservability,
  });

  const result = validate.data;

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-muted max-w-[560px]">
          {result
            ? `Last run at ${new Date(validate.submittedAt).toLocaleTimeString()}.`
            : validate.isPending
              ? "Checking exporters, scrape targets and queries…"
              : "Not run yet in this session. It checks the GPU exporters, the scrape targets and that the metrics can be queried."}
        </p>
        <button
          type="button"
          disabled={validate.isPending}
          onClick={() => validate.mutate()}
          className="text-xs h-8 px-3 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base inline-flex items-center gap-1.5 disabled:opacity-60 shrink-0"
        >
          {validate.isPending ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <ShieldCheck className="h-3.5 w-3.5" />
          )}
          {validate.isPending ? "Running self-check…" : result ? "Run again (~10s)" : "Run self-check (~10s)"}
        </button>
      </div>

      {validate.isError && (
        <div className="mt-3 text-[11px] text-[color:var(--critical)] flex items-center gap-1.5">
          <AlertTriangle className="h-3 w-3" />{" "}
          {validate.error instanceof Error
            ? validate.error.message
            : "validation failed to run"}
        </div>
      )}

      {result && (
        <div className="mt-4">
          <div className="flex items-center gap-2 mb-3 text-xs">
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 border",
                result.passed
                  ? "border-accent-emerald/40 bg-accent-emerald/10 text-[color:var(--text)]"
                  : "border-signal-critical/40 bg-signal-critical/10 text-[color:var(--critical)]"
              )}
            >
              {result.passed ? (
                <Check className="h-3.5 w-3.5" />
              ) : (
                <X className="h-3.5 w-3.5" />
              )}
              {result.passed ? "All checks passed" : "Some checks failed"}
            </span>
            <span className="text-muted font-mono text-[10px]">
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
