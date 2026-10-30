"use client";

import { Children, Suspense, isValidElement, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Activity,
  AlertTriangle,
  BookOpen,
  Check,
  ChevronRight,
  Cloud,
  Code,
  Copy,
  Database,
  FileText,
  Layers,
  ListTree,
  Loader2,
  RefreshCw,
  Server,
  Sparkles,
  TerminalSquare,
  Workflow,
  X,
} from "lucide-react";
import {
  api,
  type Alert,
  type AlertAction,
  type AlertChatMessage,
} from "@/lib/api";
import { Accordion, AccordionItem } from "@/components/ui/accordion";
import { TabBar, useUrlTab, type TabDef } from "@/components/ui/tabs";

interface InvestigationTabsProps {
  alert: Alert;
  /** Content for the Details tab: occurrences, original body, raw metadata. */
  details?: React.ReactNode;
}

const TAB_IDS = ["root-cause", "background", "workflow", "evidence", "details"] as const;
type TabId = (typeof TAB_IDS)[number];

/**
 * The autonomous-triage surface for a single alert, as tabs:
 * Root cause · Background · Remediation workflow · Evidence · Details.
 * One section is on screen at a time, and detail inside a section is folded
 * (one item open at a time), so the page stays readable however much the
 * agent found.
 *
 * On mount the component fires POST /alerts/{id}/triage — that endpoint is
 * idempotent server-side, so reopening the page (or concurrent tabs) does not
 * re-spend a model turn. The agent emits three sections (Root cause /
 * Background / Remediation plan); the plan's numbered steps become the
 * Remediation workflow, each tagged `[system]` (runs on Approve) or
 * `[operator]` (the operator does it). Evidence comes from the agent's read
 * tool calls (logs, events, pod state, deployment, metrics).
 */
export function InvestigationTabs(props: InvestigationTabsProps) {
  // useUrlTab reads ?tab=, which needs a Suspense boundary.
  return (
    <Suspense fallback={null}>
      <InvestigationTabsInner {...props} />
    </Suspense>
  );
}

function InvestigationTabsInner({ alert, details }: InvestigationTabsProps) {
  const qc = useQueryClient();
  const { data: messages, isLoading } = useQuery({
    queryKey: ["alert-chat", alert.id],
    queryFn: () => api.alerts.chat.list(alert.id),
    refetchOnWindowFocus: false,
  });

  const triage = useMutation({
    mutationFn: (opts: { force?: boolean } = {}) =>
      api.alerts.chat.triage(alert.id, opts),
    onSuccess: (data) => qc.setQueryData(["alert-chat", alert.id], data),
  });

  // Fire the kickoff exactly once per page mount. The backend will
  // no-op if a previous tab already kicked off.
  useEffect(() => {
    if (isLoading) return;
    if (triage.isPending || triage.isSuccess) return;
    const hasAssistant = (messages ?? []).some((m) => m.role === "assistant");
    if (hasAssistant) return;
    triage.mutate({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoading, alert.id]);

  const [tab, setTab] = useUrlTab<TabId>(TAB_IDS, "root-cause");
  const groups = useMemo(() => groupActions(messages ?? []), [messages]);
  const sections = useMemo(() => parseSections(latestSummary(messages ?? [])), [messages]);

  const triageInFlight = triage.isPending;

  if (isLoading) {
    return (
      <div className="text-[12px] text-muted flex items-center gap-2">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Loading investigation…
      </div>
    );
  }

  const evidenceKinds = ORDER.filter((k) => (groups[k]?.actions.length ?? 0) > 0);
  const evidenceCount = evidenceKinds.reduce(
    (n, k) => n + (groups[k]?.actions.length ?? 0),
    0
  );
  const toApprove = groups.pendingWrites.length;
  const ran = groups.executedWrites.length;

  const tabs: TabDef<TabId>[] = [
    { id: "root-cause", label: "Root cause", shortLabel: "Cause", icon: Sparkles },
    { id: "background", label: "Background", shortLabel: "Context", icon: BookOpen },
    {
      id: "workflow",
      label: "Remediation workflow",
      shortLabel: "Workflow",
      icon: Workflow,
      badge: toApprove ? `${toApprove} to approve` : ran ? `${ran} ran` : undefined,
      badgeTone: toApprove ? "accent" : "muted",
    },
    {
      id: "evidence",
      label: "Evidence",
      icon: ListTree,
      badge: evidenceCount || undefined,
    },
    ...(details ? [{ id: "details" as const, label: "Details", icon: FileText }] : []),
  ];

  return (
    <div className="space-y-4">
      <TabBar
        tabs={tabs}
        active={tab}
        onChange={setTab}
        ariaLabel="Investigation"
        trailing={
          <button
            type="button"
            onClick={() => triage.mutate({ force: true })}
            disabled={triageInFlight}
            className="text-[11px] h-7 px-2 rounded-md border border-line text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5 disabled:opacity-50"
            title="Re-run the autonomous triage pass"
            aria-label="Re-triage"
          >
            {triageInFlight ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="h-3 w-3" />
            )}
            <span className="hidden sm:inline">
              {triageInFlight ? "Triaging…" : "Re-triage"}
            </span>
          </button>
        }
      />

      <div role="tabpanel">
        {tab === "root-cause" && (
          <NarrativePanel
            subtitle="What broke and why"
            body={sections.rootCause}
            pending={triageInFlight && !sections.rootCause}
            empty="No findings yet — hit Re-triage to start an investigation."
          />
        )}
        {tab === "background" && (
          <NarrativePanel
            subtitle="Context you may not have"
            body={sections.background}
            pending={triageInFlight && !sections.background}
            empty="Background context will appear here once the agent has investigated."
          />
        )}
        {tab === "workflow" && (
          <RemediationWorkflow
            alert={alert}
            pendingWrites={groups.pendingWrites}
            executedWrites={groups.executedWrites}
            plan={sections.remediationPlan}
            pending={triageInFlight && !sections.remediationPlan}
            messages={messages ?? []}
          />
        )}
        {tab === "evidence" && (
          <EvidencePanel
            kinds={evidenceKinds}
            groups={groups}
            pending={triageInFlight}
          />
        )}
        {tab === "details" && details}
      </div>

      <RemediationStyles />
    </div>
  );
}

// ── Root cause / Background ─────────────────────────────────────────────

function NarrativePanel({
  subtitle,
  body,
  pending,
  empty,
}: {
  subtitle: string;
  body: string;
  pending: boolean;
  empty: string;
}) {
  const hasBody = !!body && body.trim() !== "—";
  return (
    <section className="surface p-5">
      <div className="text-[10px] uppercase tracking-wider text-muted mb-2">
        {subtitle}
      </div>
      {hasBody ? (
        <article className="prose prose-sm dark:prose-invert max-w-none investigation-md">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
        </article>
      ) : (
        <PendingOrEmpty
          pending={pending}
          pendingText="Investigating — the agent is pulling logs, events and metrics…"
          empty={empty}
        />
      )}
    </section>
  );
}

function PendingOrEmpty({
  pending,
  pendingText,
  empty,
}: {
  pending: boolean;
  pendingText: string;
  empty: string;
}) {
  return (
    <div className="text-[12px] text-muted flex items-center gap-2">
      {pending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
      {pending ? pendingText : empty}
    </div>
  );
}

// ── Remediation workflow ────────────────────────────────────────────────
//
// The plan's numbered steps, each folded to its one-sentence summary (the
// command and details open on click, one step at a time), then the approval
// card for the steps the system runs. Nothing runs until the operator
// approves; approving opens the workflow run.

function RemediationWorkflow({
  alert,
  pendingWrites,
  executedWrites,
  plan,
  pending,
  messages,
}: {
  alert: Alert;
  pendingWrites: AlertAction[];
  executedWrites: AlertAction[];
  plan: string;
  pending: boolean;
  messages: AlertChatMessage[];
}) {
  const steps = useMemo(() => splitPlanSteps(plan), [plan]);
  const hasContent =
    !!plan || pendingWrites.length > 0 || executedWrites.length > 0;

  return (
    <div className="space-y-4">
      <p className="text-[12px] text-muted">
        The workflow the AI SRE proposes for this alert. Steps marked{" "}
        <span className="font-semibold text-[color:var(--accent)]">System</span> run
        only when you approve them; steps marked{" "}
        <span className="font-semibold text-[color:var(--warning)]">You</span> are
        yours to do.
      </p>

      {!hasContent && (
        <section className="surface p-5">
          <PendingOrEmpty
            pending={pending}
            pendingText="Investigating — the workflow will appear here once the agent has a fix to propose."
            empty="No workflow yet. Re-triage or ask the chat for next steps."
          />
        </section>
      )}

      {steps.intro && (
        <article className="prose prose-sm dark:prose-invert max-w-none investigation-md">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{steps.intro}</ReactMarkdown>
        </article>
      )}

      {steps.items.length > 0 ? (
        <Accordion>
          {steps.items.map((s, i) => (
            <AccordionItem
              key={i}
              id={`step-${i}`}
              tone={
                s.tag === "operator"
                  ? "var(--warning)"
                  : s.tag === "system"
                    ? "var(--accent)"
                    : undefined
              }
              leading={<StepNumber n={i + 1} tag={s.tag} />}
              title={
                <span className="remediation-md step-title">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={inlineMdComponents}
                  >
                    {s.title}
                  </ReactMarkdown>
                </span>
              }
              meta={<StepTag tag={s.tag} />}
            >
              {s.body ? (
                <article className="prose prose-sm dark:prose-invert max-w-none investigation-md remediation-md">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={remediationMdComponents}
                  >
                    {s.body}
                  </ReactMarkdown>
                </article>
              ) : (
                <div className="text-[12px] text-muted">No further detail.</div>
              )}
            </AccordionItem>
          ))}
        </Accordion>
      ) : (
        steps.rest && (
          <article className="prose prose-sm dark:prose-invert max-w-none investigation-md remediation-md">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={remediationMdComponents}
            >
              {steps.rest}
            </ReactMarkdown>
          </article>
        )
      )}

      {pendingWrites.length > 0 && (
        <ApprovalCard alert={alert} pendingWrites={pendingWrites} messages={messages} />
      )}

      {/* Heuristic guardrail: the agent sometimes writes "click Approve" in
       *  the plan but forgets to emit the matching tool_use, leaving the
       *  operator hunting for a button that can't exist. */}
      {pendingWrites.length === 0 &&
        executedWrites.length === 0 &&
        plan &&
        /\bapprov(e|al)\b/i.test(plan) && (
          <div
            className="rounded-xl p-3 text-[12px]"
            style={{
              background: "color-mix(in srgb, var(--warning) 12%, var(--bg-elevated))",
              border: "1px solid color-mix(in srgb, var(--warning) 50%, var(--line))",
              color: "color-mix(in srgb, var(--warning) 80%, var(--text))",
            }}
          >
            <div className="flex items-start gap-2">
              <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              <span>
                The workflow references an Approve step, but the agent didn't
                queue a matching action, so there is nothing to run from here.
                Hit <strong>Re-triage</strong> to regenerate it, or run the
                steps' commands yourself.
              </span>
            </div>
          </div>
        )}

      {executedWrites.length > 0 && (
        <Accordion>
          <AccordionItem
            id="ran"
            leading={<TerminalSquare className="h-4 w-4 text-[color:var(--accent)]" />}
            title={`${executedWrites.length} action${executedWrites.length === 1 ? "" : "s"} ran`}
            meta={summarizeStatuses(executedWrites)}
          >
            <ExecutedActionsList actions={executedWrites} />
          </AccordionItem>
        </Accordion>
      )}
    </div>
  );
}

function StepNumber({ n, tag }: { n: number; tag: StepTagKind }) {
  const color =
    tag === "operator" ? "var(--warning)" : tag === "system" ? "var(--accent)" : "var(--text-muted)";
  return (
    <span
      className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] font-bold"
      style={{
        background: `color-mix(in srgb, ${color} 20%, transparent)`,
        color,
      }}
    >
      {n}
    </span>
  );
}

function StepTag({ tag }: { tag: StepTagKind }) {
  if (!tag) return null;
  const operator = tag === "operator";
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider"
      style={
        operator
          ? { background: "var(--warning)", color: "var(--bg-base)" }
          : {
              background: "color-mix(in srgb, var(--accent) 18%, transparent)",
              color: "var(--accent)",
              border: "1px solid color-mix(in srgb, var(--accent) 40%, transparent)",
            }
      }
    >
      {operator ? (
        <>
          <AlertTriangle className="h-3 w-3" /> You
        </>
      ) : (
        <>
          <Sparkles className="h-3 w-3" /> System
        </>
      )}
    </span>
  );
}

function summarizeStatuses(actions: AlertAction[]): string {
  const failed = actions.filter((a) => a.status === "failed").length;
  const rejected = actions.filter((a) => a.status === "rejected").length;
  const running = actions.filter((a) => a.status === "approved").length;
  const parts = [];
  if (running) parts.push(`${running} running`);
  if (failed) parts.push(`${failed} failed`);
  if (rejected) parts.push(`${rejected} rejected`);
  return parts.join(" · ") || "no errors";
}

// ── Approval card ───────────────────────────────────────────────────────

function ApprovalCard({
  alert,
  pendingWrites,
  messages,
}: {
  alert: Alert;
  pendingWrites: AlertAction[];
  messages: AlertChatMessage[];
}) {
  const qc = useQueryClient();
  const router = useRouter();
  // message id → its text, so each action can fall back to the agent's own
  // explanation (from the message that proposed it) when it has no reason.
  const contentById = useMemo(() => {
    const m = new Map<string, string>();
    for (const msg of messages) if (msg.content) m.set(msg.id, msg.content);
    return m;
  }, [messages]);
  // Approving runs the remediation as a Workflow; jump to its page so the
  // operator watches it execute step by step.
  // Approving creates a workflow run and returns at once (the action and its
  // verification run in the background); we then open the run page, which
  // shows each step as it lands.
  const approveOne = useMutation({
    mutationFn: (actionId: string) => api.alerts.chat.approve(alert.id, actionId),
    onSuccess: (data) => {
      qc.setQueryData(["alert-chat", alert.id], data.messages);
      qc.invalidateQueries({ queryKey: ["workflow-runs"] });
      router.push(`/workflows/${data.workflow_run_id}`);
    },
  });
  const rejectOne = useMutation({
    mutationFn: (actionId: string) => api.alerts.chat.reject(alert.id, actionId),
    onSuccess: (data) => qc.setQueryData(["alert-chat", alert.id], data),
  });

  // "Approve all" runs every pending write in sequence. The backend exposes
  // one approve-per-action endpoint, so this fans out, then opens the first
  // workflow run it created.
  const [bulkApproving, setBulkApproving] = useState(false);
  const approveAll = async () => {
    setBulkApproving(true);
    let firstRunId: string | null = null;
    try {
      for (const a of pendingWrites) {
        const res = await api.alerts.chat.approve(alert.id, a.id);
        if (!firstRunId) firstRunId = res.workflow_run_id;
      }
      qc.invalidateQueries({ queryKey: ["alert-chat", alert.id] });
    } finally {
      setBulkApproving(false);
    }
    if (firstRunId) router.push(`/workflows/${firstRunId}`);
  };

  const busy = approveOne.isPending || rejectOne.isPending || bulkApproving;
  const starting = approveOne.isPending || bulkApproving || approveOne.isSuccess;
  const failure = (approveOne.error ?? rejectOne.error) as Error | null;

  return (
    <div
      className="rounded-xl p-4"
      style={{
        background: "color-mix(in srgb, var(--accent) 10%, var(--bg-elevated))",
        border: "1px solid color-mix(in srgb, var(--accent) 40%, var(--line))",
        boxShadow: "inset 4px 0 0 var(--accent), 0 4px 18px -10px var(--accent-glow)",
      }}
    >
      <div className="flex items-center gap-2 mb-3">
        <Sparkles className="h-3.5 w-3.5" style={{ color: "var(--accent)" }} />
        <div
          className="text-[12px] font-semibold uppercase tracking-wider"
          style={{ color: "var(--accent)" }}
        >
          {pendingWrites.length} action{pendingWrites.length === 1 ? "" : "s"} waiting for your approval
        </div>
        <span className="text-[11px] text-muted ml-auto">
          Changes the cluster — nothing runs until you approve.
        </span>
      </div>
      {starting && (
        <div
          role="status"
          className="mb-3 rounded-md px-3 py-2 text-[12px] flex items-center gap-2"
          style={{
            background: "color-mix(in srgb, var(--accent) 12%, var(--bg-base))",
            border: "1px solid color-mix(in srgb, var(--accent) 35%, var(--line))",
          }}
        >
          <Loader2 className="h-3.5 w-3.5 animate-spin" style={{ color: "var(--accent)" }} />
          {approveOne.isSuccess
            ? "Workflow started — opening it so you can watch each step…"
            : "Creating the workflow run and starting the approved action…"}
        </div>
      )}
      {failure && (
        <div
          role="alert"
          className="mb-3 rounded-md px-3 py-2 text-[12px] flex items-start gap-2"
          style={{
            background: "color-mix(in srgb, var(--critical) 10%, var(--bg-base))",
            border: "1px solid color-mix(in srgb, var(--critical) 40%, var(--line))",
          }}
        >
          <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" style={{ color: "var(--critical)" }} />
          <span>Couldn&apos;t start it: {failure.message}</span>
        </div>
      )}
      <div className="space-y-2">
        {pendingWrites.map((a) => (
          <div key={a.id} className="rounded-md border border-line bg-bg-base/40 px-3 py-2">
            <div className="flex items-center gap-2 text-[12px]">
              <span className="font-mono font-semibold">{a.tool_name}</span>
              <span className="text-muted font-mono text-[11px] truncate">
                {argTarget(a.tool_input)}
              </span>
            </div>
            {/* Why this action, and what happens when it runs: the agent's
                `reason` argument, else the prose it wrote around the call,
                else a mechanical summary. */}
            <div className="text-[12px] text-fg/80 mt-1.5 whitespace-pre-wrap">
              {actionReason(a) ?? actionRationale(a, contentById) ?? describeAction(a)}
            </div>
            <details className="mt-1.5 group">
              <summary className="cursor-pointer text-[11px] text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1">
                <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />
                Show exact command
              </summary>
              <pre className="mt-1 text-[11px] font-mono bg-bg-base/60 border border-line rounded p-2 overflow-x-auto whitespace-pre-wrap break-all">
                {a.tool_name}({JSON.stringify(commandArgs(a.tool_input), null, 2)})
              </pre>
            </details>
            <div className="flex items-center gap-2 mt-2 flex-wrap justify-end">
              <button
                type="button"
                disabled={busy}
                onClick={() => rejectOne.mutate(a.id)}
                className="h-8 px-3 rounded-md border border-line text-muted hover:text-[color:var(--text)] disabled:opacity-40 inline-flex items-center gap-1.5 text-[12px]"
              >
                <X className="h-3.5 w-3.5" /> Reject
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => approveOne.mutate(a.id)}
                className="h-8 px-3 rounded-md text-bg-base disabled:opacity-40 inline-flex items-center gap-1.5 text-[12px] font-semibold"
                style={{
                  background:
                    "linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 70%, #000))",
                }}
              >
                {approveOne.isPending && approveOne.variables === a.id ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" /> Starting…
                  </>
                ) : (
                  <>
                    <Check className="h-3.5 w-3.5" /> Approve & run
                  </>
                )}
              </button>
            </div>
          </div>
        ))}
      </div>
      {pendingWrites.length > 1 && (
        <button
          type="button"
          disabled={bulkApproving}
          onClick={approveAll}
          className="mt-3 w-full h-10 rounded-lg text-bg-base disabled:opacity-50 inline-flex items-center justify-center gap-2 text-[13px] font-semibold"
          style={{
            background:
              "linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 70%, #000))",
            boxShadow: "0 4px 18px -6px var(--accent-glow)",
          }}
        >
          {bulkApproving ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" /> Starting workflow…
            </>
          ) : (
            <>
              <Check className="h-4 w-4" /> Approve all and run workflow
            </>
          )}
        </button>
      )}
    </div>
  );
}

// ── Plan → steps ────────────────────────────────────────────────────────

type StepTagKind = "system" | "operator" | null;

interface PlanStep {
  tag: StepTagKind;
  /** The step's one-sentence summary (first line, marker stripped). */
  title: string;
  /** Everything under it: the command, the "→ where to run it" line. */
  body: string;
}

/**
 * Split the Remediation plan markdown into its numbered steps. Done on the
 * text rather than the rendered list because models often put a step's
 * fenced command at column 0, which ends the markdown list — the command
 * would otherwise land outside its step.
 *
 * Also drops "(And call rollout_undo(...))" paragraphs: the prompt's example
 * shape that models sometimes copy into the plan.
 */
function splitPlanSteps(md: string): { intro: string; items: PlanStep[]; rest: string } {
  const lines = (md || "").split("\n");
  const introLines: string[] = [];
  const raw: { head: string; body: string[] }[] = [];
  let inFence = false;
  for (const line of lines) {
    const fence = /^\s*(```|~~~)/.test(line);
    const m = !inFence && /^\s{0,3}\d+[.)]\s+(.*)$/.exec(line);
    if (fence) inFence = !inFence;
    if (m) {
      raw.push({ head: m[1], body: [] });
    } else if (raw.length) {
      raw[raw.length - 1].body.push(line);
    } else {
      introLines.push(line);
    }
  }
  const items = raw.map(({ head, body }) => {
    const tagMatch = /\[(system|operator)\]/i.exec(head);
    const title = head
      .replace(/^\s*\*{0,2}\[(system|operator)\]\*{0,2}\s*[—\-:]?\s*/i, "")
      .trim();
    return {
      tag: tagMatch ? (tagMatch[1].toLowerCase() as StepTagKind) : null,
      title,
      body: dropEchoedCalls(dedent(body.join("\n"))).trim(),
    };
  });
  return { intro: introLines.join("\n").trim(), items, rest: items.length ? "" : md };
}

/** Remove "(And call tool(...))" lines, through the end of that paragraph. */
function dropEchoedCalls(text: string): string {
  const out: string[] = [];
  let skipping = false;
  let inFence = false;
  for (const line of text.split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence;
    if (!inFence && /^\s*\(And (in the same response, )?call\b/i.test(line)) skipping = true;
    if (skipping && !line.trim()) skipping = false;
    if (!skipping) out.push(line);
  }
  return out.join("\n");
}

function dedent(text: string): string {
  const indents = text
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => /^ */.exec(l)![0].length);
  const cut = Math.min(4, ...(indents.length ? indents : [0]));
  return text
    .split("\n")
    .map((l) => l.slice(Math.min(cut, /^ */.exec(l)![0].length)))
    .join("\n");
}

// ── Evidence ────────────────────────────────────────────────────────────

function EvidencePanel({
  kinds,
  groups,
  pending,
}: {
  kinds: EvidenceKind[];
  groups: Groups;
  pending: boolean;
}) {
  if (kinds.length === 0) {
    return (
      <section className="surface p-5">
        <PendingOrEmpty
          pending={pending}
          pendingText="Collecting evidence — logs, events and metrics appear here as the agent reads them…"
          empty="No evidence collected yet."
        />
      </section>
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-[12px] text-muted">
        What the AI SRE read to reach its conclusion. Lines that look like
        errors are highlighted.
      </p>
      <Accordion>
        {kinds.map((kind) => {
          const meta = KIND_META[kind];
          const actions = groups[kind]!.actions;
          return (
            <AccordionItem
              key={kind}
              id={kind}
              leading={
                <span
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md shrink-0"
                  style={{
                    background: "color-mix(in srgb, var(--accent) 16%, transparent)",
                    color: meta.accent,
                  }}
                >
                  {meta.icon}
                </span>
              }
              title={<span className="font-medium">{meta.title}</span>}
              meta={`${actions.length} call${actions.length === 1 ? "" : "s"}`}
            >
              <div className="space-y-2">
                {actions.map((a) => (
                  <details
                    key={a.id}
                    open={actions.length === 1}
                    className="rounded-md border border-line bg-bg-elevated/50"
                  >
                    <summary className="cursor-pointer px-3 py-1.5 text-[11px] flex items-center gap-2">
                      <span className="font-mono">{a.tool_name}</span>
                      <span className="text-muted truncate">{renderArgs(a.tool_input)}</span>
                      <span
                        className="ml-auto text-[10px] uppercase tracking-wider"
                        style={{
                          color: a.status === "failed" ? "var(--critical)" : "var(--accent)",
                        }}
                      >
                        {a.status}
                      </span>
                    </summary>
                    <HighlightedLog
                      content={a.result_output || a.result_error || "(empty)"}
                      variant={a.status === "failed" ? "error" : "normal"}
                    />
                  </details>
                ))}
              </div>
            </AccordionItem>
          );
        })}
      </Accordion>
    </div>
  );
}

// ── Styles for markdown rendered inside the workflow ────────────────────

function RemediationStyles() {
  return (
    // Global so it reaches the elements ReactMarkdown emits.
    <style jsx global>{`
        .remediation-md ol,
        .remediation-md ul {
          padding-left: 0;
          margin: 0;
          list-style: none;
          counter-reset: rem-step;
        }
        .remediation-md > ol > li,
        .remediation-md > ul > li {
          counter-increment: rem-step;
          position: relative;
          padding: 0.65em 0.85em 0.75em 2.4em;
          margin: 0.5em 0;
          border-radius: 10px;
          background: var(--bg-elevated);
          border: 1px solid var(--line);
        }
        .remediation-md > ol > li::before {
          content: counter(rem-step);
          position: absolute;
          left: 0.6em;
          top: 0.65em;
          width: 1.45em;
          height: 1.45em;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          border-radius: 50%;
          background: color-mix(in srgb, var(--accent) 22%, transparent);
          color: var(--accent);
          font-size: 0.78em;
          font-weight: 700;
        }
        .remediation-md .step-operator {
          background: color-mix(in srgb, var(--warning) 10%, var(--bg-elevated));
          border-color: color-mix(in srgb, var(--warning) 60%, var(--line));
          box-shadow: inset 3px 0 0 var(--warning);
        }
        .remediation-md .step-operator::before {
          background: var(--warning);
          color: var(--bg-base);
        }
        .remediation-md .step-system {
          background: color-mix(in srgb, var(--accent) 6%, var(--bg-elevated));
          border-color: color-mix(in srgb, var(--accent) 28%, var(--line));
        }
        .remediation-md .step-tag {
          display: inline-flex;
          align-items: center;
          gap: 0.3em;
          font-size: 0.72em;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          padding: 0.15em 0.5em;
          border-radius: 999px;
          margin-right: 0.55em;
          vertical-align: middle;
        }
        .remediation-md .step-tag-operator {
          background: var(--warning);
          color: var(--bg-base);
        }
        .remediation-md .step-tag-system {
          background: color-mix(in srgb, var(--accent) 22%, transparent);
          color: var(--accent);
          border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
        }
        .remediation-md .step-where {
          display: block;
          margin: 0.45em 0 0.25em;
          padding: 0.35em 0.55em;
          font-size: 0.85em;
          background: color-mix(in srgb, var(--warning) 14%, transparent);
          border-left: 2px solid var(--warning);
          border-radius: 4px;
          color: color-mix(in srgb, var(--warning) 85%, var(--text));
        }
        .remediation-md p {
          margin: 0.25em 0;
        }
        .remediation-md code {
          font-family: ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, monospace;
          font-size: 0.9em;
          background: var(--bg-base);
          border: 1px solid var(--line);
          padding: 0.05em 0.35em;
          border-radius: 4px;
        }
        .remediation-md .code-block-wrap {
          position: relative;
          margin: 0.5em 0;
          border-radius: 8px;
          overflow: hidden;
          background: var(--bg-base);
          border: 1px solid var(--line);
        }
        .remediation-md .code-block-wrap pre {
          margin: 0;
          padding: 0.7em 3.4em 0.7em 0.85em;
          font-family: ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, monospace;
          font-size: 0.85em;
          white-space: pre-wrap;
          word-break: break-word;
          background: transparent;
          border: none;
        }
        .remediation-md .code-block-wrap pre code {
          background: transparent;
          border: none;
          padding: 0;
          font-size: inherit;
          color: inherit;
        }
        .remediation-md .code-block-copy {
          position: absolute;
          top: 0.4em;
          right: 0.4em;
          height: 1.7em;
          padding: 0 0.6em;
          display: inline-flex;
          align-items: center;
          gap: 0.3em;
          border-radius: 5px;
          background: color-mix(in srgb, var(--accent) 18%, transparent);
          border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
          color: var(--accent);
          font-size: 0.75em;
          font-weight: 600;
          cursor: pointer;
          transition:
            background 140ms ease,
            color 140ms ease;
        }
        .remediation-md .code-block-copy:hover {
          background: color-mix(in srgb, var(--accent) 32%, transparent);
          color: var(--bg-base);
        }
        .remediation-md .code-block-copy.copied {
          background: var(--accent);
          color: var(--bg-base);
          border-color: var(--accent);
        }
      
      .remediation-md.step-title p {
        display: inline;
        margin: 0;
      }
    `}</style>
  );
}

// ── ReactMarkdown component overrides for the Remediation workflow ──────────

/**
 * Walk a React children tree and collect its raw text. Used to peek
 * at a list-item's content so we can decide whether it's a
 * [system] or [operator] step.
 */
function flattenText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(flattenText).join("");
  if (isValidElement(node)) {
    const props = node.props as { children?: unknown };
    return flattenText(props.children);
  }
  return "";
}

/**
 * Strip the leading `**[operator]**` / `**[system]**` token off a
 * list item's first child so the badge we render doesn't double up
 * with the marker text. Returns the modified children tree.
 */
function stripStepMarker(children: React.ReactNode): React.ReactNode {
  const arr = Children.toArray(children);
  for (let i = 0; i < arr.length; i++) {
    const node = arr[i];
    if (typeof node === "string") {
      const stripped = node.replace(
        /^\s*\*?\*?\[(operator|system)\]\*?\*?\s*[—\-:]?\s*/i,
        ""
      );
      if (stripped !== node) {
        arr[i] = stripped;
        return arr;
      }
      // Empty after strip — keep walking.
      if (node.trim() === "") continue;
      return arr;
    }
    if (isValidElement(node)) {
      // The marker is usually wrapped in <strong>**[operator]**</strong>
      // by remark-gfm; check if this element's text is exactly the
      // marker and drop it.
      const inner = flattenText((node.props as { children?: unknown }).children).trim();
      if (/^\[(operator|system)\]$/i.test(inner)) {
        // Also drop a trailing "—" or ":" separator that follows the
        // marker, so we don't get a dangling em-dash before the body.
        const next = arr[i + 1];
        if (typeof next === "string") {
          arr[i + 1] = next.replace(/^\s*[—\-:]\s*/, "");
        }
        arr.splice(i, 1);
        return arr;
      }
    }
    return arr;
  }
  return arr;
}

/**
 * react-markdown v9 dropped the `inline` prop on the `code` component
 * override, so we can no longer distinguish inline vs fenced code from
 * inside the `code` handler. The correct hook now is the `pre` element:
 * fenced code blocks are wrapped in `<pre><code>…</code></pre>`, while
 * inline backticks emit a bare `<code>` with no `<pre>` parent. We
 * therefore put the copy-button affordance on the `pre` override and
 * leave `code` as a plain inline span — otherwise every `NET_ADMIN` /
 * `delete_pod` / `o-hm0` token written with single-backticks would
 * render as a full-width copy-button block, which is what the user
 * was seeing.
 */
function PreWithCopy({ children }: { children?: React.ReactNode }) {
  const [copied, setCopied] = useState(false);
  const text = String(flattenText(children)).replace(/\n$/, "");
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      // No clipboard permission — degrade gracefully.
    }
  };
  return (
    <div className="code-block-wrap">
      <pre>{children}</pre>
      <button
        type="button"
        onClick={onCopy}
        className={`code-block-copy ${copied ? "copied" : ""}`}
        title="Copy to clipboard"
      >
        {copied ? (
          <>
            <Check className="h-3 w-3" /> Copied
          </>
        ) : (
          <>
            <Copy className="h-3 w-3" /> Copy
          </>
        )}
      </button>
    </div>
  );
}

function InlineCode({
  className,
  children,
}: {
  className?: string;
  children?: React.ReactNode;
}) {
  return <code className={className}>{children}</code>;
}

function RemediationListItem({ children }: { children?: React.ReactNode }) {
  const text = flattenText(children);
  const isOperator = /\[operator\]/i.test(text);
  const isSystem = /\[system\]/i.test(text);
  const cls = isOperator
    ? "step-operator"
    : isSystem
      ? "step-system"
      : "";
  const stripped = isOperator || isSystem ? stripStepMarker(children) : children;
  return (
    <li className={cls}>
      {(isOperator || isSystem) && (
        <span
          className={`step-tag ${
            isOperator ? "step-tag-operator" : "step-tag-system"
          }`}
        >
          {isOperator ? (
            <>
              <AlertTriangle className="h-3 w-3" /> You
            </>
          ) : (
            <>
              <Sparkles className="h-3 w-3" /> System
            </>
          )}
        </span>
      )}
      {stripped}
    </li>
  );
}

/**
 * Wrap a paragraph that starts with "→" in the step-where pill so the
 * "where to run this" line pops visually inside a [operator] step.
 * Lines that don't start with → render as normal <p>s.
 */
function RemediationParagraph({ children }: { children?: React.ReactNode }) {
  const text = flattenText(children).trim();
  if (text.startsWith("→")) {
    return <span className="step-where">{children}</span>;
  }
  return <p>{children}</p>;
}

const remediationMdComponents = {
  // Inline `code` is rendered as a plain span; fenced code blocks
  // come in as <pre><code>…</code></pre> and our PreWithCopy override
  // adds the Copy button + chrome around them.
  code: InlineCode,
  pre: PreWithCopy,
  li: RemediationListItem,
  p: RemediationParagraph,
} as Parameters<typeof ReactMarkdown>[0]["components"];

/** For one-line markdown (a step title): no block paragraph wrapper. */
const inlineMdComponents = {
  code: InlineCode,
  p: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
} as Parameters<typeof ReactMarkdown>[0]["components"];

// ── Evidence tiles (logs, events, etc.) ─────────────────────────────────

const ORDER: EvidenceKind[] = [
  "logs",
  "events",
  "pod",
  "deployment",
  "metrics",
  "cloud_compute",
  "cloud_db",
  "cloud_function",
  "external",
];

const KIND_META: Record<
  EvidenceKind,
  {
    title: string;
    icon: React.ReactNode;
    accent: string;
  }
> = {
  logs: {
    title: "Logs",
    icon: <FileText className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
  events: {
    title: "Cluster events",
    icon: <ListTree className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
  pod: {
    // "Resource state" rather than "Pod state" — this tile now covers
    // any inspected resource (a k8s pod via describe_pod, but the system
    // also reasons over cloud VMs, network devices, BMCs), so the k8s-
    // specific label no longer fits.
    title: "Resource state",
    icon: <Layers className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
  deployment: {
    title: "Deployment / rollout",
    icon: <TerminalSquare className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
  metrics: {
    title: "Metrics",
    icon: <Activity className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
  cloud_compute: {
    title: "Cloud instances",
    icon: <Server className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
  cloud_db: {
    title: "Cloud databases",
    icon: <Database className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
  cloud_function: {
    title: "Serverless functions",
    icon: <Code className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
  external: {
    title: "External API",
    icon: <Cloud className="h-3.5 w-3.5" />,
    accent: "var(--accent)",
  },
};

/**
 * Tool-output panel that lights up lines matching common error
 * keywords in red. The model can't tag specific lines (evidence tiles
 * render directly from tool stdout), so we lean on a regex over each
 * line — ERROR / WARN / FATAL / OOMKilled / CrashLoopBackOff /
 * Traceback / HTTP 4xx-5xx / "connection refused" etc.
 *
 * Conservative on purpose: we don't want to redden a benign info line
 * just because it mentions "error code 0" — the regex requires a
 * keyword that's actually a status, not arbitrary substring matches.
 */
function HighlightedLog({
  content,
  variant,
}: {
  content: string;
  variant: "normal" | "error";
}) {
  const lines = content.split("\n");
  return (
    <pre
      className={`px-3 py-2 m-0 font-mono whitespace-pre-wrap break-words text-[11px] max-h-[320px] overflow-auto border-t border-line ${
        variant === "error" ? "log-pre-error" : "log-pre-normal"
      }`}
    >
      {lines.map((line, i) => (
        <div
          key={i}
          className={isErrorLine(line) ? "log-line-error" : undefined}
        >
          {line || " "}
        </div>
      ))}
      <style jsx>{`
        .log-line-error {
          color: var(--critical);
          font-weight: 500;
        }
        .log-pre-error {
          color: color-mix(in srgb, var(--critical) 90%, var(--text));
        }
      `}</style>
    </pre>
  );
}

// Recognised "this looks like a problem" patterns. Anchored to word
// boundaries so we don't redden every line that happens to contain
// the substring "Failed" inside a noun.
const ERROR_KEYWORDS =
  /\b(ERROR|ERR|WARN|WARNING|FATAL|CRITICAL|PANIC|FAILED?|FAILURE|EXCEPTION|TRACEBACK|OOMKilled|CrashLoopBackOff|ImagePullBackOff|ErrImagePull|ImagePullErr|CreateContainerError|ContainerCannotRun|Evicted|NodeNotReady|BackOff|Killed)\b/;
const HTTP_FAIL = /\b(HTTP\/\S+\s+)?[45]\d{2}\b/;
const NETWORK_FAIL = /\b(connection refused|connection timed out|no route to host|name or service not known|max retries exceeded|i\/o timeout|context deadline exceeded)\b/i;

function isErrorLine(line: string): boolean {
  if (!line) return false;
  return ERROR_KEYWORDS.test(line) || HTTP_FAIL.test(line) || NETWORK_FAIL.test(line);
}

// ── Helpers / extraction logic ─────────────────────────────────────────

type EvidenceKind =
  | "logs"
  | "events"
  | "pod"
  | "deployment"
  | "metrics"
  | "external"
  | "cloud_compute"
  | "cloud_db"
  | "cloud_function";

interface EvidenceGroup {
  actions: AlertAction[];
}

interface Groups extends Record<EvidenceKind, EvidenceGroup | undefined> {
  pendingWrites: AlertAction[];
  executedWrites: AlertAction[];
}

const KIND_OF: Record<string, EvidenceKind> = {
  // k8s read tools
  get_pod_logs: "logs",
  query_loki: "logs",
  get_pod_events: "events",
  list_pods: "events",
  describe_pod: "pod",
  get_deployment: "deployment",
  rollout_history: "deployment",
  query_prometheus: "metrics",
  call_external_api: "external",
  // AWS read tools
  aws_describe_instances: "cloud_compute",
  aws_get_cloudwatch_logs: "logs",
  aws_query_cloudwatch_metric: "metrics",
  aws_describe_rds_instances: "cloud_db",
  aws_describe_lambda: "cloud_function",
  // GCP read tools
  gcp_list_instances: "cloud_compute",
  gcp_query_logging: "logs",
  gcp_query_monitoring: "metrics",
  gcp_describe_sql_instance: "cloud_db",
  gcp_describe_function: "cloud_function",
  // Azure read tools
  azure_list_vms: "cloud_compute",
  azure_query_log_analytics: "logs",
  azure_query_metrics: "metrics",
  azure_describe_sql_db: "cloud_db",
  azure_describe_function: "cloud_function",
};

const WRITE_TOOL_NAMES = new Set([
  "rollout_undo",
  "scale_deployment",
  "restart_deployment",
  "delete_pod",
  "patch_resource",
]);

function isWriteTool(name: string, requiresApproval: boolean): boolean {
  if (WRITE_TOOL_NAMES.has(name)) return true;
  // call_external_api with a non-GET method shows up as
  // requires_approval=true at action-creation time.
  return requiresApproval;
}

const NOT_CONNECTED = /^error: \w+Unavailable: no \w+ integration registered/;

function groupActions(messages: AlertChatMessage[]): Groups {
  const out: Groups = {
    logs: undefined,
    events: undefined,
    pod: undefined,
    deployment: undefined,
    metrics: undefined,
    external: undefined,
    cloud_compute: undefined,
    cloud_db: undefined,
    cloud_function: undefined,
    pendingWrites: [],
    executedWrites: [],
  };
  for (const m of messages) {
    for (const a of m.actions || []) {
      if (a.status === "pending" && a.requires_approval) {
        out.pendingWrites.push(a);
        continue;
      }
      // Write tools that have run (executed / failed / rejected) get
      // bundled into their own "Actions ran" section inside the
      // Remediation tile, so the operator can see the kubectl/cloud
      // call we fired plus the API's response. Read-tool calls land
      // in the evidence tiles below as before.
      if (
        isWriteTool(a.tool_name, a.requires_approval) &&
        a.status !== "pending"
      ) {
        out.executedWrites.push(a);
        continue;
      }
      const k = KIND_OF[a.tool_name];
      if (!k) continue;
      // A tool for an integration the tenant hasn't connected (e.g. GCP)
      // found nothing; it isn't evidence, so don't show an error group.
      if (NOT_CONNECTED.test(a.result_output || a.result_error || "")) continue;
      out[k] ??= { actions: [] };
      out[k]!.actions.push(a);
    }
  }
  return out;
}

function latestSummary(messages: AlertChatMessage[]): string {
  // The triage summary is the most recent assistant message that
  // carries the three `## Root cause / Background / Remediation
  // plan` section headers. After approval the agent posts follow-up
  // notes (e.g. "patch applied, pod recreated"); those are useful
  // but they do NOT replace the triage summary — if we treated them
  // as the source for the three top tiles, rootCause / background /
  // plan would all go empty and the click-to-expand buttons would
  // disable. So we walk the transcript backwards looking for a
  // header-carrying assistant message first, falling back to the
  // bare-text latest only when no triage has been produced yet.
  const HEADER_RE =
    /^##\s+(root cause|background|context|remediation|plan)\b/im;
  // Headings are normalized first: some models write "Root cause" on its
  // own line (or bold, or with a colon) instead of "## Root cause".
  let fallback = "";
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "assistant") continue;
    const body = m.content.trim();
    if (!body) continue;
    if (HEADER_RE.test(normalizeHeadings(body))) return m.content;
    if (!fallback) fallback = m.content;
  }
  return fallback;
}

/**
 * Slice the agent's final markdown into the three expected sections.
 *
 * The system prompt locks the model to exactly three `## ` headers in
 * a fixed order: Root cause / Background / Remediation plan. We still
 * parse forgivingly — match by case-insensitive header text so a
 * stray capitalisation doesn't blank the tile. Any content before
 * the first header is discarded. Manual steps now live inside the
 * Remediation plan section (tagged `[operator]`); legacy
 * "What you need to do" or "Likely operator questions" sections from
 * messages generated before the prompt change are dropped silently.
 */
interface Sections {
  rootCause: string;
  background: string;
  remediationPlan: string;
}

/**
 * Rewrite a section title that sits alone on its line ("Root cause",
 * "**Background:**", "# Remediation plan") as a "## " heading, so a model
 * that skips the markdown still fills the tiles.
 */
const SECTION_LINE_RE =
  /^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?(root cause|background|context|remediation plan)(?:\*\*)?[ \t]*:?[ \t]*(?:\*\*)?[ \t]*$/gim;

function normalizeHeadings(md: string): string {
  return md.replace(SECTION_LINE_RE, (_line, title: string) => `## ${title}`);
}

function parseSections(md: string): Sections {
  const out: Sections = {
    rootCause: "",
    background: "",
    remediationPlan: "",
  };
  if (!md) return out;
  // Split on lines that start with "## " so we get
  // [pre, header1, body1, header2, body2, ...].
  const parts = normalizeHeadings(md).split(/^##\s+(.+)$/m);
  let manualBody = "";
  for (let i = 1; i < parts.length; i += 2) {
    const header = (parts[i] || "").trim().toLowerCase();
    const body = (parts[i + 1] || "").trim();
    if (header.startsWith("root cause")) out.rootCause = body;
    else if (header.startsWith("background") || header.startsWith("context"))
      out.background = body;
    else if (header.startsWith("remediation") || header.startsWith("plan"))
      out.remediationPlan = body;
    else if (
      header.startsWith("what you need to do") ||
      header.startsWith("operator") ||
      header.startsWith("manual") ||
      header.startsWith("you need")
    ) {
      // Legacy bucket — keep the body so we can append it to the
      // remediation plan if the new section is empty.
      manualBody = body;
    }
    // Evidence is rendered from tool calls. "Likely operator
    // questions" and any other stray section is dropped.
  }
  // Back-compat: if a message was generated before the prompt change,
  // it'll have a separate "What you need to do" section. Fold it back
  // into the Remediation plan body so the new tile still renders it.
  if (manualBody && !/\[operator\]/i.test(out.remediationPlan)) {
    out.remediationPlan = out.remediationPlan
      ? `${out.remediationPlan}\n\n${manualBody}`
      : manualBody;
  }
  return out;
}

function renderArgs(input: Record<string, unknown>): string {
  return Object.entries(commandArgs(input))
    .map(([k, v]) =>
      typeof v === "string" ? `${k}=${v}` : `${k}=${JSON.stringify(v)}`
    )
    .join(" ");
}

/**
 * One-line "what this action will do" summary for the Approve panel,
 * keyed off the tool name so the operator doesn't have to mentally
 * translate the raw kwargs into impact ("patch_resource …" alone is
 * opaque without context).
 */
/**
 * Render the post-Approve "Actions ran" panel — one card per executed
 * write tool, expanded by default so the operator sees the kubectl /
 * cloud call we actually fired and whatever stdout / error message
 * came back. Failed actions are flagged red so a botched patch can't
 * hide behind a green checkmark.
 */
function ExecutedActionsList({ actions }: { actions: AlertAction[] }) {
  return (
    <>
      <div className="text-[11px] text-muted mb-2">
        The exact call Daalu made and what the cluster / cloud API returned.
      </div>
      <div className="space-y-2">
        {actions.map((a) => (
          <details
            key={a.id}
            open={actions.length === 1}
            className="rounded-md border border-line bg-bg-base/40"
          >
            <summary className="cursor-pointer px-3 py-2 flex items-center gap-2 text-[12px]">
              <span className="font-mono font-semibold">{a.tool_name}</span>
              <span className="text-muted truncate flex-1 min-w-0">
                {renderArgs(a.tool_input)}
              </span>
              <span
                className="text-[10px] uppercase tracking-wider font-semibold shrink-0"
                style={{
                  color:
                    a.status === "failed"
                      ? "var(--critical)"
                      : a.status === "rejected"
                        ? "var(--text-muted)"
                        : "var(--accent)",
                }}
              >
                {a.status}
              </span>
            </summary>
            <div className="border-t border-line">
              <HighlightedLog
                content={a.result_error || a.result_output || "(no output captured)"}
                variant={a.status === "failed" ? "error" : "normal"}
              />
            </div>
          </details>
        ))}
      </div>
    </>
  );
}

/** The agent's one-line "why + expected outcome" for a write action. */
function actionReason(a: AlertAction): string | null {
  const r = (a.tool_input as Record<string, unknown>)?.reason;
  return typeof r === "string" && r.trim() ? r.trim() : null;
}

/** Tool arguments minus `reason`, which is for the operator, not the API. */
function commandArgs(input: Record<string, unknown>): Record<string, unknown> {
  const { reason: _reason, ...rest } = input || {};
  return rest;
}

function describeAction(a: AlertAction): string {
  const i = a.tool_input as Record<string, unknown>;
  const ns = typeof i.namespace === "string" ? i.namespace : "?";
  const name = typeof i.name === "string" ? i.name : "?";
  switch (a.tool_name) {
    case "rollout_undo":
      return `Roll deployment ${ns}/${name} back to its previous revision.`;
    case "scale_deployment":
      return `Scale ${ns}/${name} to ${i.replicas} replicas.`;
    case "restart_deployment":
      return `Rolling restart of deployment ${ns}/${name}.`;
    case "delete_pod":
      return `Delete pod ${ns}/${name}; the controller reschedules a replacement.`;
    case "patch_resource": {
      const kind = (typeof i.kind === "string" ? i.kind : "resource").toLowerCase();
      const fields = patchContainerFields(i.patch);
      return fields
        ? `Patch ${kind} ${ns}/${name} — ${fields}.`
        : `Strategic-merge patch ${kind} ${ns}/${name}.`;
    }
    case "call_external_api":
      return `${String(i.method ?? "GET").toUpperCase()} ${i.provider}${i.path ?? ""}`;
    default:
      return "Mutates cluster state — runs on approval.";
  }
}

/**
 * Summarize which container fields a strategic-merge patch changes, e.g.
 * "set livenessProbe, readinessProbe on container 'app'", so the operator can
 * tell what a patch_resource action does without reading the raw JSON. Returns
 * null when the patch isn't the usual deployment/container shape.
 */
function patchContainerFields(patch: unknown): string | null {
  try {
    const containers = (patch as Record<string, any>)?.spec?.template?.spec
      ?.containers;
    if (Array.isArray(containers) && containers.length) {
      return containers
        .map((c: Record<string, unknown>) => {
          const keys = Object.keys(c).filter((k) => k !== "name");
          if (!keys.length) return null;
          // Spell out env changes (the usual "wrong setting" fix) so the
          // summary says GPU_MEM_UTIL=0.10, not just "env".
          const parts = keys.map((k) =>
            k === "env" && Array.isArray(c.env)
              ? `env ${(c.env as Record<string, unknown>[])
                  .map((e) => `${e.name}=${e.value ?? "…"}`)
                  .join(", ")}`
              : k
          );
          return `set ${parts.join(", ")} on container '${c.name ?? "?"}'`;
        })
        .filter(Boolean)
        .join("; ");
    }
  } catch {
    /* fall through to null */
  }
  return null;
}

/**
 * The agent's own explanation for a proposed action — the prose it wrote in the
 * message that proposed it (each "[system]" step explains one tool call). This
 * is the real "what is this trying to do", far better than the mechanical
 * describeAction() fallback. Returns null if we can't find it.
 */
function actionRationale(
  a: AlertAction,
  byId: Map<string, string>
): string | null {
  const content = byId.get(a.message_id)?.trim();
  if (!content) return null;
  // Steps are narrated as "[system] <explanation> ... (And call <tool>(…))".
  const blocks = content.split(/\n(?=\[system\])/i);
  const block =
    blocks.find((b) => b.includes(`${a.tool_name}(`)) ??
    (blocks.length === 1 ? blocks[0] : null);
  if (!block) return null;
  const prose = block
    .replace(/^\s*\[system\]\s*/i, "")
    .split(/```|\(And call/)[0]
    .trim();
  return prose || null;
}

/** Short target identifier for an action's header (namespace/name). */
function argTarget(input: Record<string, unknown>): string {
  const ns = typeof input.namespace === "string" ? input.namespace : null;
  const name = typeof input.name === "string" ? input.name : null;
  if (ns && name) return `${ns}/${name}`;
  if (name) return name;
  const scalar = Object.entries(input).find(([, v]) => typeof v !== "object");
  return scalar ? `${scalar[0]}=${String(scalar[1])}` : "";
}
