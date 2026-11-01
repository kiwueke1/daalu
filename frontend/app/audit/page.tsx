"use client";

import { Fragment, useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, ChevronRight, Loader2, ScrollText, ShieldCheck, XCircle } from "lucide-react";
import { api, type AuditEvent } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";
import { ErrorNote, PageHeader, btnSecondary, fmtDateTime, inputCls } from "@/components/aiops/ui";

const PAGE = 100;

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
          <button type="button" className={btnSecondary} disabled={verify.isPending} onClick={() => verify.mutate()}>
            {verify.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5" />}
            Verify chain
          </button>
        }
      >
        Every read, proposal, approval, execution and policy change, in order.
        Each event carries the hash of the one before it, so any edit or gap
        breaks the chain.
      </PageHeader>

      {verify.data && (
        <div
          role="status"
          className="rounded-xl px-4 py-3 text-sm flex items-start gap-2"
          style={{
            background: `color-mix(in srgb, ${verify.data.ok ? "var(--accent)" : "var(--critical)"} 10%, var(--bg-card))`,
            border: `1px solid color-mix(in srgb, ${verify.data.ok ? "var(--accent)" : "var(--critical)"} 40%, var(--line))`,
          }}
        >
          {verify.data.ok ? (
            <CheckCircle2 className="h-4 w-4 mt-0.5 text-accent shrink-0" />
          ) : (
            <XCircle className="h-4 w-4 mt-0.5 shrink-0" style={{ color: "var(--critical)" }} />
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
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setFilters({ kind, actor: actor.trim(), subject: subject.trim() });
        }}
      >
        <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value)} className={inputCls}>
          <option value="">All kinds</option>
          {(kinds.data ?? []).map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
          <option value="action.*">action.*</option>
          <option value="policy.*">policy.*</option>
        </select>
        <input aria-label="Actor" value={actor} onChange={(e) => setActor(e.target.value)} placeholder="Actor (exact)" className={inputCls} />
        <input aria-label="Subject" value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="Subject, e.g. alert id" className={inputCls} />
        <button type="submit" className={btnSecondary}>Apply</button>
        {(filters.kind || filters.actor || filters.subject) && (
          <button
            type="button"
            className="text-xs text-muted hover:text-fg"
            onClick={() => {
              setKind("");
              setActor("");
              setSubject("");
              setFilters({ kind: "", actor: "", subject: "" });
            }}
          >
            Clear
          </button>
        )}
      </form>

      <LoadState
        query={q}
        loadingText="Loading the audit log…"
        empty={events.length === 0}
        emptyTitle="No audit events"
        emptyHint="Events are written as the AI investigates alerts and people act on them."
      >
        <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
          <table className="w-full min-w-[820px] text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-muted bg-bg-elevated/50">
              <tr>
                <th className="px-2" />
                <th className="text-right px-3 py-2">Seq</th>
                <th className="text-left px-3 py-2">Time</th>
                <th className="text-left px-3 py-2">Kind</th>
                <th className="text-left px-3 py-2">Actor</th>
                <th className="text-left px-3 py-2">Subject</th>
                <th className="text-left px-3 py-2">Hash</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <Fragment key={e.id}>
                  <tr
                    className="border-t border-line/60 hover:bg-bg-elevated/30 cursor-pointer"
                    onClick={() => setOpen(open === e.seq ? null : e.seq)}
                  >
                    <td className="px-2 text-muted">
                      <ChevronRight className={cn("h-4 w-4 transition-transform", open === e.seq && "rotate-90")} />
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted">{e.seq}</td>
                    <td className="px-3 py-2 whitespace-nowrap">{fmtDateTime(e.created_at)}</td>
                    <td className="px-3 py-2 font-mono text-[12px]">{e.kind}</td>
                    <td className="px-3 py-2 text-[12px] truncate max-w-[14rem]">{e.actor}</td>
                    <td className="px-3 py-2 font-mono text-[12px] truncate max-w-[14rem]">{e.subject}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-muted" title={e.hash}>
                      {e.hash.slice(0, 10)}
                    </td>
                  </tr>
                  {open === e.seq && (
                    <tr className="bg-bg-elevated/20">
                      <td />
                      <td colSpan={6} className="px-3 py-3">
                        <pre className="text-[11px] font-mono whitespace-pre-wrap break-words bg-bg-elevated/60 border border-line rounded p-2 max-h-[320px] overflow-auto">
                          {JSON.stringify(e.data, null, 2)}
                        </pre>
                        <div className="text-[10px] text-muted font-mono mt-1 break-all">
                          prev {e.prev_hash || "(genesis)"} → {e.hash}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex items-center gap-3">
          {!exhausted && (q.data ?? []).length >= PAGE && (
            <button type="button" className={btnSecondary} disabled={loadOlder.isPending} onClick={() => loadOlder.mutate()}>
              {loadOlder.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Load older
            </button>
          )}
          <span className="text-xs text-muted">{events.length} events shown</span>
          <ErrorNote error={loadOlder.error} />
        </div>
      </LoadState>
    </div>
  );
}
