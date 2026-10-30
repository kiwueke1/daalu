"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2, X } from "lucide-react";
import { api, type Alert, type Incident } from "@/lib/api";

const SEVERITIES = ["sev1", "sev2", "sev3", "sev4"] as const;

const ALERT_TO_INCIDENT_SEVERITY: Record<Alert["severity"], string> = {
  critical: "sev2",
  warning: "sev3",
  info: "sev4",
};

export function PromoteIncidentDialog({
  alert,
  onClose,
  onPromoted,
}: {
  alert: Alert;
  onClose: () => void;
  onPromoted: (incident: Incident) => void;
}) {
  const [title, setTitle] = useState(alert.title);
  const [severity, setSeverity] = useState<string>(
    ALERT_TO_INCIDENT_SEVERITY[alert.severity]
  );
  const [summary, setSummary] = useState("");
  const [error, setError] = useState<string | null>(null);

  const promote = useMutation({
    mutationFn: () =>
      api.infra.promoteAlertToIncident(alert.id, {
        title: title.trim(),
        severity,
        summary: summary.trim() || undefined,
      }),
    onSuccess: (incident) => {
      onPromoted(incident);
      onClose();
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  function submit() {
    if (!title.trim()) {
      setError("Title is required");
      return;
    }
    setError(null);
    promote.mutate();
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-bg/70 backdrop-blur-sm flex items-start justify-center p-4 pt-16 overflow-y-auto"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-[520px] surface p-5 space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">Promote to incident</h2>
            <p className="text-xs text-muted mt-0.5">
              Create a new incident grouped under this alert. The alert stays
              open and is linked from the incident's evidence.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="h-8 w-8 rounded-lg border border-line text-muted hover:text-[color:var(--text)] flex items-center justify-center shrink-0"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-3">
          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-muted">
              Title
            </span>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="mt-1 w-full text-sm h-9 px-3 rounded-lg bg-bg-elevated border border-line focus:outline-none focus:border-accent-cyan/60"
            />
          </label>

          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-muted">
              Severity
            </span>
            <select
              value={severity}
              onChange={(e) => setSeverity(e.target.value)}
              className="mt-1 w-full text-sm h-9 px-3 rounded-lg bg-bg-elevated border border-line focus:outline-none focus:border-accent-cyan/60"
            >
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s.toUpperCase()}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="text-[11px] uppercase tracking-wider text-muted">
              Summary <span className="normal-case text-muted/70">(optional)</span>
            </span>
            <textarea
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              rows={3}
              placeholder="What's the scope? Who's impacted?"
              className="mt-1 w-full text-sm px-3 py-2 rounded-lg bg-bg-elevated border border-line focus:outline-none focus:border-accent-cyan/60 resize-none"
            />
          </label>
        </div>

        {error && (
          <div className="text-xs text-[color:var(--critical)]">{error}</div>
        )}

        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--text)]"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={promote.isPending}
            className="text-xs h-9 px-3 rounded-lg bg-accent-cyan/15 border border-accent-cyan/40 text-accent-cyan disabled:opacity-50 flex items-center gap-1.5"
          >
            {promote.isPending && (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            )}
            Promote
          </button>
        </div>
      </div>
    </div>
  );
}
