"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Clock, Plus, Trash2 } from "lucide-react";
import { api, type ReportSchedule, type SavedReport } from "@/lib/api";
import { Button, buttonClass } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { LoadState } from "@/components/ui/load-state";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

export default function SchedulesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const schedulesQ = useQuery({
    queryKey: ["report-schedules"],
    queryFn: () => api.reports.schedules.list(),
  });
  const schedules = schedulesQ.data ?? [];
  const { data: saved = [] } = useQuery({
    queryKey: ["reports-saved"],
    queryFn: () => api.reports.saved.list(),
  });

  const [creating, setCreating] = useState(false);

  const remove = useMutation({
    mutationFn: (id: string) => api.reports.schedules.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["report-schedules"] });
      toast.success("Schedule deleted. It won't send again.");
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.reports.schedules.update(id, { enabled }),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ["report-schedules"] });
      toast.success(v.enabled ? "Schedule enabled." : "Schedule paused. Nothing sends until you enable it.");
    },
    onError: (e) => toast.error(errorText(e)),
  });

  return (
    <div className="space-y-6 max-w-[1100px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Link
          href="/reports?tab=query"
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-[color:var(--text)] transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> Back to Reports
        </Link>
        {!creating && saved.length > 0 && (
          <Button
            size="sm"
            variant="primary"
            onClick={() => setCreating(true)}
            icon={<Plus className="h-3.5 w-3.5" aria-hidden />}
          >
            New schedule
          </Button>
        )}
      </div>

      <header>
        <h1 className="text-2xl font-semibold inline-flex items-center gap-2">
          <Clock className="h-5 w-5 text-accent" aria-hidden /> Scheduled reports
        </h1>
        <p className="text-muted text-sm mt-1">
          Send a saved report to Slack or email on a schedule.
        </p>
      </header>

      {creating && (
        <CreateScheduleForm
          saved={saved}
          onClose={() => setCreating(false)}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["report-schedules"] });
            setCreating(false);
            toast.success("Schedule created. It sends at the next matching time.");
          }}
        />
      )}

      <LoadState
        query={schedulesQ}
        loadingText="Loading schedules…"
        empty={schedules.length === 0}
        emptyTitle="No schedules yet"
        emptyHint={
          saved.length === 0
            ? "Schedules send a saved report. Run a query on the Query tab, save it, then schedule it here."
            : "Pick one of your saved reports and choose when and where it's sent."
        }
        emptyAction={
          saved.length === 0 ? (
            <Link href="/reports?tab=query" className={buttonClass("primary", "sm")}>
              Go to Query
            </Link>
          ) : (
            !creating && (
              <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
                New schedule
              </Button>
            )
          )
        }
      >
        <ul className="space-y-3">
          {schedules.map((s) => (
            <ScheduleRow
              key={s.id}
              schedule={s}
              savedName={saved.find((r) => r.id === s.saved_report_id)?.name}
              onDelete={() => remove.mutate(s.id)}
              deleting={remove.isPending && remove.variables === s.id}
              onToggle={() => toggle.mutate({ id: s.id, enabled: !s.enabled })}
              toggling={toggle.isPending && toggle.variables?.id === s.id}
            />
          ))}
        </ul>
      </LoadState>
    </div>
  );
}

function ScheduleRow({
  schedule,
  savedName,
  onDelete,
  onToggle,
  deleting,
  toggling,
}: {
  schedule: ReportSchedule;
  savedName?: string;
  onDelete: () => void;
  onToggle: () => void;
  deleting?: boolean;
  toggling?: boolean;
}) {
  const status = schedule.last_status || (schedule.last_run_at ? "?" : "—");
  const statusClass =
    schedule.last_status === "ok"
      ? "text-[color:var(--accent)]"
      : schedule.last_status === "failed"
      ? "text-[color:var(--critical)]"
      : "text-muted";
  return (
    <li className="motion-enter rounded-xl border border-line bg-bg-card p-4 flex flex-col sm:flex-row sm:items-start gap-3 sm:gap-4">
      <div className="flex-1 min-w-0 space-y-1">
        <div className="flex items-center gap-2">
          <span className="font-medium">{schedule.name}</span>
          {!schedule.enabled && (
            <span className="text-[11px] uppercase tracking-wider text-muted border border-line rounded px-1 py-0.5">
              disabled
            </span>
          )}
        </div>
        <div className="text-xs text-muted">
          <span className="font-mono">{schedule.cron}</span> →{" "}
          {schedule.destination}
          {schedule.recipient ? ` (${schedule.recipient})` : ""} · {schedule.format}
        </div>
        <div className="text-xs text-muted">
          Report:{" "}
          <span className="text-[color:var(--text)]">
            {savedName ?? <em>missing</em>}
          </span>
        </div>
        <div className="text-xs">
          Last run: {schedule.last_run_at
            ? `${new Date(schedule.last_run_at).toLocaleString()} · `
            : "never · "}
          <span className={statusClass}>{status}</span>
          {schedule.next_run_at && (
            <>
              {" · next "}
              <span className="text-muted">
                {new Date(schedule.next_run_at).toLocaleString()}
              </span>
            </>
          )}
        </div>
        {schedule.last_error && (
          <div className="text-xs text-[color:var(--critical)]">
            {schedule.last_error}
          </div>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 flex-shrink-0">
        <Button size="sm" onClick={onToggle} pending={toggling}>
          {schedule.enabled ? "Pause" : "Enable"}
        </Button>
        <ConfirmButton
          label="Delete"
          confirmLabel="Delete schedule"
          consequence={`"${schedule.name}" stops sending.`}
          icon={<Trash2 className="h-3.5 w-3.5" aria-hidden />}
          onConfirm={onDelete}
          pending={deleting}
          tone="danger"
        />
      </div>
    </li>
  );
}

function CreateScheduleForm({
  saved,
  onClose,
  onSaved,
}: {
  saved: SavedReport[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [savedId, setSavedId] = useState("");
  const [cron, setCron] = useState("0 7 * * 1-5");
  const [destination, setDestination] = useState<"slack" | "email">("slack");
  const [recipient, setRecipient] = useState("");
  const [format, setFormat] = useState<"markdown" | "csv">("markdown");

  const create = useMutation({
    mutationFn: () =>
      api.reports.schedules.create({
        name,
        saved_report_id: savedId,
        cron,
        destination,
        recipient,
        format,
      }),
    onSuccess: onSaved,
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!name.trim() || !savedId || !cron.trim()) return;
        create.mutate();
      }}
      className="rounded-xl border border-line bg-bg-card p-4 space-y-3"
    >
      <div className="text-xs uppercase tracking-wider text-muted">New schedule</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
        <label className="space-y-1">
          <span className="text-xs font-medium">Name</span>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Daily NetOps standup"
            className="w-full bg-bg-elevated border border-line rounded-lg px-2 py-1.5"
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs font-medium">Saved report</span>
          <select
            value={savedId}
            onChange={(e) => setSavedId(e.target.value)}
            className="w-full bg-bg-elevated border border-line rounded-lg px-2 py-1.5"
          >
            <option value="">Pick one…</option>
            {saved.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-xs font-medium">Cron (5 fields, UTC)</span>
          <input
            type="text"
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            placeholder="0 7 * * 1-5"
            className="w-full bg-bg-elevated border border-line rounded-lg px-2 py-1.5 font-mono"
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs font-medium">Destination</span>
          <select
            value={destination}
            onChange={(e) => setDestination(e.target.value as "slack" | "email")}
            className="w-full bg-bg-elevated border border-line rounded-lg px-2 py-1.5"
          >
            <option value="slack">slack</option>
            <option value="email">email</option>
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-xs font-medium">
            {destination === "slack" ? "Slack channel (optional)" : "Email address"}
          </span>
          <input
            type="text"
            value={recipient}
            onChange={(e) => setRecipient(e.target.value)}
            placeholder={destination === "slack" ? "#netops" : "ops@example.com"}
            className="w-full bg-bg-elevated border border-line rounded-lg px-2 py-1.5"
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs font-medium">Format</span>
          <select
            value={format}
            onChange={(e) => setFormat(e.target.value as "markdown" | "csv")}
            className="w-full bg-bg-elevated border border-line rounded-lg px-2 py-1.5"
          >
            <option value="markdown">markdown</option>
            <option value="csv">csv</option>
          </select>
        </label>
      </div>
      {create.error && (
        <div role="alert" className="text-sm text-[color:var(--critical)]">
          {errorText(create.error)}
        </div>
      )}
      <div className="flex gap-2">
        <Button
          type="submit"
          size="sm"
          variant="primary"
          pending={create.isPending}
          disabled={!name.trim() || !savedId || !cron.trim()}
          disabledReason="Fill in a name, a saved report and a cron schedule"
        >
          Create schedule
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
