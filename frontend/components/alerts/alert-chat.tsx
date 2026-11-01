"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Send, ShieldAlert, Sparkles, Terminal, X } from "lucide-react";
import { api, type AlertAction, type AlertChatMessage } from "@/lib/api";
import { ActionPolicyInfo } from "@/components/aiops/action-policy";

interface AlertChatProps {
  alertId: string;
}

/**
 * Chat panel scoped to one alert. The transcript lives server-side; we
 * just GET + render. POST appends a user message and triggers a full
 * model turn (the backend may auto-run read tools and persist N tool
 * results before returning).
 *
 * Pending write actions surface as approval cards inline — the operator
 * sees the exact tool name + JSON arguments before clicking Run.
 */
export function AlertChat({ alertId }: AlertChatProps) {
  const qc = useQueryClient();
  const router = useRouter();
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const { data: rawMessages = [], isLoading } = useQuery({
    queryKey: ["alert-chat", alertId],
    queryFn: () => api.alerts.chat.list(alertId),
    refetchOnWindowFocus: false,
  });

  // Strip the auto-triage transcript. The triage kickoff lives in the
  // tiles above — replaying its user-prompt-and-LLM-loop inside the
  // chat panel is visual noise. We hide every message in a kickoff
  // chain (the kickoff user message itself, every assistant turn that
  // follows it, and every tool result attached to those turns) until
  // we hit a user message the operator actually typed. After that
  // human-typed message we resume showing everything normally — that's
  // a genuine human→LLM exchange, which is what the chat is for.
  const messages = useMemo(() => {
    const out: typeof rawMessages = [];
    let inAutoTriage = false;
    for (const m of rawMessages) {
      if (m.role === "user") {
        if (isAutoTriageKickoff(m.content)) {
          inAutoTriage = true;
          continue; // hide the kickoff prompt itself
        }
        inAutoTriage = false; // operator typed something — exit auto-triage scope
        out.push(m);
        continue;
      }
      // assistant or tool — visible iff we're not inside an auto-triage chain
      if (inAutoTriage) continue;
      out.push(m);
    }
    return out;
  }, [rawMessages]);

  const send = useMutation({
    mutationFn: (content: string) => api.alerts.chat.send(alertId, content),
    onSuccess: (data) => {
      qc.setQueryData(["alert-chat", alertId], data);
    },
  });

  // A multi-approver policy may only record this approval
  // (workflow_run_id === ""); then stay here and say how many are left.
  const [waitingFor, setWaitingFor] = useState<number | null>(null);
  const approve = useMutation({
    mutationFn: (actionId: string) => api.alerts.chat.approve(alertId, actionId),
    onSuccess: (data) => {
      qc.setQueryData(["alert-chat", alertId], data.messages);
      qc.invalidateQueries({ queryKey: ["aiops-investigation-by-alert", alertId] });
      if (!data.workflow_run_id) {
        setWaitingFor(data.approvals_remaining ?? 1);
        return;
      }
      setWaitingFor(null);
      router.push(`/workflows/${data.workflow_run_id}`);
    },
  });

  const reject = useMutation({
    mutationFn: (actionId: string) => api.alerts.chat.reject(alertId, actionId),
    onSuccess: (data) => qc.setQueryData(["alert-chat", alertId], data),
  });

  // Auto-scroll to bottom on new messages.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length, send.isPending, approve.isPending]);

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || send.isPending) return;
    setDraft("");
    send.mutate(text);
  };

  const busy = send.isPending || approve.isPending || reject.isPending;

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-6 py-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted">
        <Sparkles className="h-3.5 w-3.5" />
        Remediation copilot
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 pb-3 space-y-3">
        {isLoading && (
          <div className="text-xs text-muted">Loading conversation…</div>
        )}
        {!isLoading && messages.length === 0 && (
          <div className="text-[12px] text-muted leading-relaxed">
            <p className="mb-2">
              The agent already triaged this alert — see the tiles
              on the left for root cause, background, the remediation
              plan, and anything you need to do yourself.
            </p>
            <p>
              Use this chat to ask follow-ups: probe the evidence, run
              extra read commands, or propose a fix the system hasn't
              suggested. Anything that mutates the cluster still waits
              for your approval.
            </p>
          </div>
        )}
        {messages.map((m) => (
          <ChatBubble key={m.id} message={m} alertId={alertId}
            onApprove={(actionId) => approve.mutate(actionId)}
            onReject={(actionId) => reject.mutate(actionId)}
            approveBusy={approve.isPending}
            rejectBusy={reject.isPending}
          />
        ))}
        {send.isPending && (
          <div className="text-xs text-muted italic">Thinking…</div>
        )}
        {waitingFor !== null && (
          <div
            role="status"
            className="rounded-lg px-3 py-2 text-[12px]"
            style={{
              background: "color-mix(in srgb, var(--warning) 10%, var(--bg-base))",
              border: "1px solid color-mix(in srgb, var(--warning) 40%, var(--line))",
            }}
          >
            Your approval is recorded, waiting for {waitingFor} more.
          </div>
        )}
      </div>

      <form
        onSubmit={onSubmit}
        className="px-6 py-3 flex gap-2 items-end"
        style={{ borderTop: "1px solid var(--line)" }}
      >
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSubmit(e);
            }
          }}
          rows={2}
          placeholder="Ask the AI SRE — e.g. 'what's in the logs?'"
          className="flex-1 resize-none rounded-lg bg-bg-elevated/60 border border-line px-3 py-2 text-[13px] focus:outline-none focus:border-accent-blue/60"
          disabled={busy}
        />
        <button
          type="submit"
          disabled={!draft.trim() || busy}
          className="h-10 px-3 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base text-[13px] disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5"
        >
          <Send className="h-3.5 w-3.5" /> Send
        </button>
      </form>
    </div>
  );
}

interface ChatBubbleProps {
  message: AlertChatMessage;
  alertId: string;
  onApprove: (actionId: string) => void;
  onReject: (actionId: string) => void;
  approveBusy: boolean;
  rejectBusy: boolean;
}

function ChatBubble({
  message,
  onApprove,
  onReject,
  approveBusy,
  rejectBusy,
}: ChatBubbleProps) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div
          className="max-w-[80%] rounded-2xl rounded-br-sm px-3.5 py-2 text-[13px]"
          style={{
            background: "var(--accent-soft)",
            border: "1px solid color-mix(in srgb, var(--accent) 30%, transparent)",
          }}
        >
          {message.content}
        </div>
      </div>
    );
  }

  if (message.role === "tool") {
    return (
      <div className="flex justify-start">
        <div
          className="max-w-[92%] rounded-lg px-3 py-2 text-[12px] font-mono whitespace-pre-wrap break-words"
          style={{
            background: "var(--bg-elevated)",
            border: "1px solid var(--line)",
            color: "color-mix(in srgb, var(--text) 80%, transparent)",
          }}
        >
          <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted mb-1 font-sans">
            <Terminal className="h-3 w-3" /> tool output
          </div>
          {message.content}
        </div>
      </div>
    );
  }

  // assistant
  return (
    <div className="flex justify-start">
      <div className="max-w-[92%] space-y-2">
        {message.content && (
          <div
            className="rounded-2xl rounded-bl-sm px-3.5 py-2 text-[13px] alert-chat-msg"
            style={{
              background: "var(--bg-card)",
              border: "1px solid var(--line)",
            }}
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content}
            </ReactMarkdown>
          </div>
        )}
        {message.actions.map((a) => (
          <ActionCard
            key={a.id}
            action={a}
            onApprove={() => onApprove(a.id)}
            onReject={() => onReject(a.id)}
            busy={approveBusy || rejectBusy}
          />
        ))}
      </div>
      <style jsx global>{`
        .alert-chat-msg p {
          margin: 0;
        }
        .alert-chat-msg p + p {
          margin-top: 0.4em;
        }
        .alert-chat-msg ul,
        .alert-chat-msg ol {
          padding-left: 1.1em;
          margin: 0.25em 0;
        }
        .alert-chat-msg code {
          font-family:
            ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, monospace;
          font-size: 0.9em;
          background: var(--bg-elevated);
          border: 1px solid var(--line);
          padding: 0.05em 0.35em;
          border-radius: 4px;
        }
      `}</style>
    </div>
  );
}

interface ActionCardProps {
  action: AlertAction;
  onApprove: () => void;
  onReject: () => void;
  busy: boolean;
}

/**
 * Inline card for a single tool call. Read tools (auto-executed) just
 * confirm what happened; write tools show Approve / Reject buttons
 * until the operator decides.
 */
function ActionCard({ action, onApprove, onReject, busy }: ActionCardProps) {
  const isWrite = action.requires_approval;
  const isPending = action.status === "pending";
  const isFailed = action.status === "failed";

  const accent = isWrite
    ? "var(--warning)"
    : isFailed
      ? "var(--critical)"
      : "var(--info)";
  const accentSoft = isWrite
    ? "rgba(245,158,11,0.10)"
    : isFailed
      ? "rgba(239,68,68,0.10)"
      : "rgba(var(--accent-rgb),0.08)";

  return (
    <div
      className="rounded-lg overflow-hidden text-[12px]"
      style={{
        background: accentSoft,
        border: `1px solid color-mix(in srgb, ${accent} 38%, transparent)`,
        boxShadow: `inset 3px 0 0 ${accent}`,
      }}
    >
      <div className="px-3 py-2 flex items-center gap-2">
        {isWrite ? (
          <ShieldAlert className="h-3.5 w-3.5" style={{ color: accent }} />
        ) : (
          <Terminal className="h-3.5 w-3.5" style={{ color: accent }} />
        )}
        <span className="font-mono">{action.tool_name}</span>
        <span className="text-muted">
          ({renderArgs(action.tool_input)})
        </span>
        <span
          className="ml-auto text-[10px] uppercase tracking-wider"
          style={{ color: accent }}
        >
          {action.status}
        </span>
      </div>

      {isWrite && (
        <div className="px-3 pb-2">
          <ActionPolicyInfo action={action} compact={!isPending} />
        </div>
      )}

      {action.status === "executed" && action.result_output && (
        <pre
          className="px-3 py-2 m-0 font-mono whitespace-pre-wrap break-words text-[11px]"
          style={{
            background: "var(--bg-elevated)",
            borderTop: "1px solid var(--line)",
            color: "color-mix(in srgb, var(--text) 80%, transparent)",
          }}
        >
          {action.result_output}
        </pre>
      )}
      {action.status === "failed" && action.result_error && (
        <pre
          className="px-3 py-2 m-0 font-mono whitespace-pre-wrap break-words text-[11px]"
          style={{
            background: "var(--bg-elevated)",
            borderTop: "1px solid var(--line)",
            color: "var(--critical)",
          }}
        >
          {action.result_error}
        </pre>
      )}

      {isPending && isWrite && (
        <div
          className="px-3 py-2 flex items-center gap-2"
          style={{ borderTop: "1px solid var(--line)" }}
        >
          <span className="text-muted text-[11px] flex-1">
            This action mutates cluster state.
          </span>
          <button
            type="button"
            disabled={busy}
            onClick={onReject}
            className="h-7 px-2 rounded-md border border-line text-muted hover:text-[color:var(--text)] disabled:opacity-40 flex items-center gap-1"
          >
            <X className="h-3 w-3" /> Reject
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onApprove}
            className="h-7 px-2 rounded-md text-bg-base disabled:opacity-40 flex items-center gap-1"
            style={{
              background: `linear-gradient(90deg, ${accent}, color-mix(in srgb, ${accent} 70%, #000))`,
            }}
          >
            <Check className="h-3 w-3" /> Approve & run
          </button>
        </div>
      )}
    </div>
  );
}

function renderArgs(input: Record<string, unknown>): string {
  // Compact arg preview: key=value pairs joined by spaces. Avoids the
  // noisy {"key": ...} JSON braces in the inline card.
  const { reason: _reason, ...args } = input || {};
  return Object.entries(args)
    .map(([k, v]) =>
      typeof v === "string" ? `${k}=${v}` : `${k}=${JSON.stringify(v)}`
    )
    .join(" ");
}

/**
 * Match the backend's TRIAGE_KICKOFF preamble. The exact wording lives
 * in src/daalu_automation/api/routers/alert_chat.py — keep this regex
 * loose enough to survive minor copy-edits but tight enough that no
 * operator-typed message could accidentally match.
 */
function isAutoTriageKickoff(content: string): boolean {
  const trimmed = content.trim().toLowerCase();
  return (
    trimmed.startsWith("triage this alert end-to-end") ||
    // Earlier-generation kickoffs we may still have in the DB:
    trimmed.startsWith("triage this alert:")
  );
}
