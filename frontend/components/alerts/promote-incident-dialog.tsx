"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Siren } from "lucide-react";
import { api, type Alert, type Incident } from "@/lib/api";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

const SEVERITIES = ["sev1", "sev2", "sev3", "sev4"] as const;

const ALERT_TO_INCIDENT_SEVERITY: Record<Alert["severity"], string> = {
  critical: "sev2",
  warning: "sev3",
  info: "sev4",
};

const FIELD =
  "mt-1 w-full text-sm rounded-lg bg-bg-elevated border border-line focus:border-accent/60";

export function PromoteIncidentDialog({
  alert,
  onClose,
  onPromoted,
}: {
  alert: Alert;
  onClose: () => void;
  onPromoted: (incident: Incident) => void;
}) {
  const toast = useToast();
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
      toast.success(`Incident opened at ${incident.severity.toUpperCase()}. The alert stays open and linked.`);
      onClose();
    },
    onError: (err) => setError(errorText(err)),
  });

  function submit() {
    if (!title.trim()) {
      setError("Give the incident a title.");
      return;
    }
    setError(null);
    promote.mutate();
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Promote to incident"
      description="Opens a new incident grouped under this alert. The alert stays open and is linked from the incident's evidence."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            onClick={submit}
            pending={promote.isPending}
            icon={<Siren aria-hidden className="h-3.5 w-3.5" />}
          >
            Open incident
          </Button>
        </>
      }
    >
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <label className="block">
          <span className="text-xs font-medium text-muted">Title</span>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className={`${FIELD} h-9 px-3`}
          />
        </label>

        <label className="block">
          <span className="text-xs font-medium text-muted">Severity</span>
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
            className={`${FIELD} h-9 px-3`}
          >
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s.toUpperCase()}
              </option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="text-xs font-medium text-muted">
            Summary <span className="font-normal">(optional)</span>
          </span>
          <textarea
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            rows={3}
            placeholder="What's the scope? Who's impacted?"
            className={`${FIELD} px-3 py-2 resize-none`}
          />
        </label>

        {error && (
          <div role="alert" className="text-sm text-[color:var(--critical)]">
            {error}
          </div>
        )}
      </form>
    </Dialog>
  );
}
