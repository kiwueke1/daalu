"use client";

import { Fragment, useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, ChevronRight, ScrollText, ShieldCheck, XCircle } from "lucide-react";
import { api, type AuditEvent } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";
import { Chip, ErrorNote, PageHeader, fmtDateTime, inputCls } from "@/components/aiops/ui";

const PAGE = 100;

/** What each event kind means, in words. The raw id stays in the expanded row. */
const KIND_LABEL: Record<string, string> = {
  "action.proposed": "Proposed an action",
  "action.approval_recorded": "Approval recorded",
  "action.approved": "Approved",
  "action.rejected": "Rejected",
  "action.denied": "Blocked by policy",
  "action.executed": "Ran after approval",
  "action.auto_executed": "Ran automatically",
  "action.verified": "Fix verified",
  "tool.read": "Read data",
  "mcp.call": "Tool call (MCP)",
  "investigation.started": "Investigation started",
  "investigation.triaged": "Investigation triaged",
  "investigation.completed": "Investigation completed",
  "investigation.verdict": "Root cause graded",
  "policy.updated": "Policy changed",
  "policy.kill_switch": "Kill switch changed",
  "policy.breaker_tripped": "Breaker tripped",
  "runbook.drafted": "Runbook drafted",
  "runbook.published": "Runbook published",
  "eval.recorded": "Eval recorded",
  "action.*": "Any action",
  "policy.*": "Any policy change",
};

/** Unknown kinds read as words: "foo.bar_baz" → "Foo bar baz". */
function humaniseKind(k: string): string {
  const words = k.replace(/[._]+/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : k;
}

const kindLabel = (k: string) => KIND_LABEL[k] ?? humaniseKind(k);
const isAuto = (e: AuditEvent) => e.kind === "action.auto_executed" || e.actor.startsWith("policy:");

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The alert title from the payload when there is one, else a short id. */
function subjectLabel(e: AuditEvent): string {
  const d = e.data ?? {};
  for (const key of ["alert_title", "title", "name"]) {
    const v = d[key];
    if (typeof v === "string" && v.trim()) return v;
  }
  if (!e.subject) return "—";
  return UUID.test(e.subject) ? `…${e.subject.slice(-8)}` : e.subject;
}

function KindCell({ e }: { e: AuditEvent }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span>{kindLabel(e.kind)}</span>
      {isAuto(e) && (
        <Chip tone="accent" solid title="Run by the autonomy policy, without a person approving">
          Auto
        </Chip>
      )}
    </span>
  );
}

function EventDetail({ e }: { e: AuditEvent }) {
  return (
    <div className="space-y-1.5">
      <div className="text-xs text-muted flex flex-wrap gap-x-4 gap-y-1">
        <span>
          Kind <span className="font-mono text-fg">{e.kind}</span>
        </span>
        <span className="break-all">
          Subject <span className="font-mono text-fg">{e.subject || "—"}</span>
        </span>
      </div>
      <pre className="text-xs font-mono whitespace-pre-wrap break-words bg-bg-elevated/60 border border-line rounded p-2 max-h-[320px] overflow-auto">
        {JSON.stringify(e.data, null, 2)}
      </pre>
      <div className="text-[11px] text-muted font-mono break-all">
        prev {e.prev_hash || "(genesis)"} → {e.hash}
      </div>
    </div>
  );
}

/**
 * /audit — the append-only, hash-chained log of everything the AI and people
 * did: reads, proposals, approvals, executions, policy changes, verdicts.
 */
export default function AuditPage() {
  const [kind, setKind] = useState("");
  const [actor, setActor] = useState("");
  const [subject, setSubject] = useState("");
  // Applied filters (inputs apply on submit, not per keystroke).
  const [filters, setFilters] = useState({ kind: "", actor: "", subject: "" });
  const [older, setOlder] = useState<AuditEvent[]>([]);
  const [exhausted, setExhausted] = useState(false);
  const [open, setOpen] = useState<number | null>(null);

  const kinds = useQuery({ queryKey: ["aiops-audit-kinds"], queryFn: () => api.aiops.audit.kinds() });
  const q = useQuery({
    queryKey: ["aiops-audit", filters],
    queryFn: () => api.aiops.audit.list({ ...filters, limit: PAGE }),
  });
  useEffect(() => {
    setOlder([]);
    setExhausted(false);
  }, [filters]);

  const events = [...(q.data ?? []), ...older];
  const lastSeq = events.length ? events[events.length - 1].seq : null;

  const loadOlder = useMutation({
    mutationFn: () => api.aiops.audit.list({ ...filters, before_seq: lastSeq ?? undefined, limit: PAGE }),
    onSuccess: (rows) => {
      setOlder((o) => [...o, ...rows]);
      if (rows.length < PAGE) setExhausted(true);
    },
  });
  const verify = useMutation({ mutationFn: () => api.aiops.audit.verify() });

  return (
    <div className="space-y-6 max-w-[1200px]">
      <PageHeader
        icon={ScrollText}
        title="Audit log"
        actions={
          <Button
            size="sm"
            pending={verify.isPending}
            icon={<ShieldCheck className="h-3.5 w-3.5" aria-hidden />}
            onClick={() => verify.mutate()}
          >
            Verify chain
          </Button>
        }
      >
        Every read, proposal, approval, execution and policy change, in order.
        Each event carries the hash of the one before it, so any edit or gap
        breaks the chain.
      </PageHeader>

      {verify.data && (
        <div
          role="status"
          key={`${verify.data.ok}-${verify.data.checked}`}
          className="rounded-xl px-4 py-3 text-sm flex items-start gap-2 motion-enter"
          style={{
            background: `color-mix(in srgb, ${verify.data.ok ? "var(--accent)" : "var(--critical)"} 10%, var(--bg-card))`,
            border: `1px solid color-mix(in srgb, ${verify.data.ok ? "var(--accent)" : "var(--critical)"} 40%, var(--line))`,
          }}
        >
          {verify.data.ok ? (
            <CheckCircle2 aria-hidden className="h-4 w-4 mt-0.5 text-accent shrink-0 motion-success" />
          ) : (
            <XCircle aria-hidden className="h-4 w-4 mt-0.5 shrink-0" style={{ color: "var(--critical)" }} />
          )}
          <div>
            {verify.data.ok ? (
              <>Chain intact — {verify.data.checked} events verified.</>
            ) : (
              <>
                Chain broken at seq {verify.data.broken_at} ({verify.data.checked} events verified before it).
                <div className="text-xs text-muted mt-0.5">{verify.data.reason}</div>
              </>
            )}
          </div>
        </div>
      )}
      <ErrorNote error={verify.error} prefix="Couldn't verify:" />

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setFilters({ kind, actor: actor.trim(), subject: subject.trim() });
        }}
      >
        <label className="flex flex-col gap-1 text-xs text-muted">
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value)} className={inputCls}>
            <option value="">All kinds</option>
            {(kinds.data ?? []).map((k) => (
              <option key={k} value={k}>
                {kindLabel(k)}
              </option>
            ))}
            <option value="action.*">{kindLabel("action.*")}</option>
            <option value="policy.*">{kindLabel("policy.*")}</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Actor
          <input value={actor} onChange={(e) => setActor(e.target.value)} placeholder="Exact, e.g. agent" className={inputCls} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Subject
          <input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="e.g. an alert id" className={inputCls} />
        </label>
        <Button type="submit" size="sm">Apply filters</Button>
        {(filters.kind || filters.actor || filters.subject) && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setKind("");
              setActor("");
              setSubject("");
              setFilters({ kind: "", actor: "", subject: "" });
            }}
          >
            Clear filters
          </Button>
        )}
      </form>

      <LoadState
        query={q}
        loadingText="Loading the audit log…"
        empty={events.length === 0}
        emptyTitle="No audit events"
        emptyHint="Events are written as the AI investigates alerts and people act on them."
      >
        {/* Phone: stacked, so actor and subject stay on screen. */}
        <ul className="md:hidden space-y-2">
          {events.map((e) => {
            const expanded = open === e.seq;
            return (
              <li key={e.id} className="rounded-2xl border border-line bg-bg-card">
                <button
                  type="button"
                  aria-expanded={expanded}
                  onClick={() => setOpen(expanded ? null : e.seq)}
                  className="w-full text-left p-3 flex items-start gap-2"
                >
                  <ChevronRight
                    aria-hidden
                    className={cn("h-4 w-4 mt-0.5 shrink-0 text-muted transition-transform", expanded && "rotate-90")}
                  />
                  <div className="min-w-0 flex-1 text-sm">
                    <KindCell e={e} />
                    <div className="mt-1 text-xs text-muted break-words">
                      {e.actor} · {subjectLabel(e)}
                    </div>
                    <div className="mt-0.5 text-xs text-muted tabular-nums">
                      #{e.seq} · {fmtDateTime(e.created_at)}
                    </div>
                  </div>
                </button>
                {expanded && (
                  <div className="px-3 pb-3 motion-enter">
                    <EventDetail e={e} />
                  </div>
                )}
              </li>
            );
          })}
        </ul>

        <div className="hidden md:block rounded-2xl border border-line bg-bg-card relative overflow-x-auto">
          <table className="w-full min-w-[820px] text-sm">
            <thead className="text-[11px] uppercase tracking-wider text-muted bg-bg-elevated/50">
              <tr>
                <th className="px-2">
                  <span className="sr-only">Expand</span>
                </th>
                <th className="text-right px-3 py-2">Seq</th>
                <th className="text-left px-3 py-2">Time</th>
                <th className="text-left px-3 py-2">What happened</th>
                <th className="text-left px-3 py-2">Actor</th>
                <th className="text-left px-3 py-2">Subject</th>
                <th className="text-left px-3 py-2">Hash</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => {
                const expanded = open === e.seq;
                return (
                  <Fragment key={e.id}>
                    <tr
                      className="border-t border-line/60 hover:bg-bg-elevated/30 cursor-pointer"
                      onClick={() => setOpen(expanded ? null : e.seq)}
                    >
                      <td className="px-2 text-muted">
                        <button
                          type="button"
                          aria-expanded={expanded}
                          aria-label={`${expanded ? "Hide" : "Show"} details of event ${e.seq}`}
                          onClick={(ev) => {
                            ev.stopPropagation();
                            setOpen(expanded ? null : e.seq);
                          }}
                          className="h-7 w-7 inline-flex items-center justify-center rounded-md hover:bg-bg-elevated/60"
                        >
                          <ChevronRight aria-hidden className={cn("h-4 w-4 transition-transform", expanded && "rotate-90")} />
                        </button>
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted">{e.seq}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{fmtDateTime(e.created_at)}</td>
                      <td className="px-3 py-2 text-[13px]">
                        <KindCell e={e} />
                      </td>
                      <td className="px-3 py-2 text-xs truncate max-w-[14rem]">{e.actor}</td>
                      <td className="px-3 py-2 text-xs truncate max-w-[16rem]" title={e.subject}>
                        {subjectLabel(e)}
                      </td>
                      <td className="px-3 py-2 font-mono text-xs text-muted" title={e.hash}>
                        {e.hash.slice(0, 10)}
                      </td>
                    </tr>
                    {expanded && (
                      <tr className="bg-bg-elevated/20">
                        <td />
                        <td colSpan={6} className="px-3 py-3">
                          <div className="motion-enter">
                            <EventDetail e={e} />
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="flex items-center gap-3">
          {!exhausted && (q.data ?? []).length >= PAGE && (
            <Button size="sm" pending={loadOlder.isPending} onClick={() => loadOlder.mutate()}>
              Load older events
            </Button>
          )}
          <span className="text-xs text-muted">{events.length} events shown</span>
          <ErrorNote error={loadOlder.error} />
        </div>
      </LoadState>
    </div>
  );
}
