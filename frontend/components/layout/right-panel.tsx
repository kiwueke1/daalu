"use client";

import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import Link from "next/link";
import {
  AlertTriangle,
  ChevronRight,
  Heart,
  MessageSquare,
  Send,
  Sparkles,
  TriangleAlert,
  Activity as ActivityIcon,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { errorText } from "@/lib/errors";
import { CopilotMarkdown } from "@/components/copilot/markdown";
import { Button } from "@/components/ui/button";
import { ThinkingDots } from "@/components/copilot/thinking-dots";
import { formatRelative } from "@/lib/utils";

/**
 * Right rail. Mirrors the cinematic surface system used everywhere else:
 * the rail itself is a deeper-tinted column with its own ambient wash,
 * each section is an illuminated-glass surface, and notifications carry
 * a left edge glow tinted by severity instead of a hard border colour.
 *
 * Every section is collapsed by default so the main column gets the
 * width — the rail reduces to a slim strip of labelled pills (an
 * activity-bar) and only claims its full column once the operator opens
 * something. The choice is remembered per browser; first visit is
 * always fully collapsed.
 */

type PanelId = "copilot" | "notifications" | "sessions" | "health";

const RAIL_STORAGE_KEY = "daalu-right-rail-open";

const PANELS: {
  id: PanelId;
  label: string;
  /** Fits under the icon in the collapsed strip. */
  shortLabel: string;
  icon: LucideIcon;
  tint: string;
}[] = [
  { id: "copilot", label: "AI Assistant", shortLabel: "Assistant", icon: MessageSquare, tint: "var(--accent)" },
  {
    id: "notifications",
    label: "Notifications",
    shortLabel: "Alerts",
    icon: TriangleAlert,
    tint: "var(--warning)",
  },
  { id: "sessions", label: "Active sessions", shortLabel: "Sessions", icon: ActivityIcon, tint: "var(--accent)" },
  { id: "health", label: "System health", shortLabel: "Health", icon: Heart, tint: "var(--accent)" },
];

function isPanelId(v: unknown): v is PanelId {
  return PANELS.some((p) => p.id === v);
}

export function RightPanel() {
  // Collapsed on first paint — and on the server — so the main column
  // never flashes narrow while we read the stored preference.
  const [open, setOpen] = useState<PanelId[]>([]);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(RAIL_STORAGE_KEY);
      if (!raw) return;
      const parsed: unknown = JSON.parse(raw);
      if (Array.isArray(parsed)) setOpen(parsed.filter(isPanelId));
    } catch {
      /* ignore storage/parse failures (private mode, stale value) */
    }
  }, []);

  function toggle(id: PanelId) {
    setOpen((cur) => {
      const next = cur.includes(id) ? cur.filter((p) => p !== id) : [...cur, id];
      try {
        localStorage.setItem(RAIL_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* ignore storage failures */
      }
      return next;
    });
  }

  // Render in rail order rather than click order so the stack doesn't
  // reshuffle as sections are opened and closed.
  const expanded = PANELS.filter((p) => open.includes(p.id));

  return (
    <aside
      // Theme tokens, not fixed dark rgba: the rail was a dark slab with
      // unreadable labels in the light theme. Full viewport height so it
      // doesn't stop partway down a short page.
      className="hidden xl:flex shrink-0 relative min-h-[calc(100dvh-4rem)]"
      style={{
        background:
          "linear-gradient(180deg, color-mix(in srgb, var(--bg-surface) 70%, transparent), var(--bg-surface))",
        boxShadow: "inset 1px 0 0 var(--line)",
      }}
    >
      {/* Rail ambient wash */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 50% at 90% 10%, rgba(var(--accent-rgb),0.08), transparent 60%), radial-gradient(40% 40% at 100% 100%, rgba(var(--accent-rgb),0.05), transparent 70%)",
        }}
      />

      {expanded.length > 0 && (
        <div className="relative w-[340px] px-4 py-6 space-y-5">
          {expanded.map((p) => (
            <RailSection
              key={p.id}
              icon={p.icon}
              tint={p.tint}
              label={p.label}
              bloom={p.id === "copilot" || p.id === "health"}
              action={
                p.id === "notifications" ? (
                  <Link
                    href="/alerts"
                    className="text-xs text-muted hover:text-[color:var(--text)]"
                  >
                    View all
                  </Link>
                ) : null
              }
              onCollapse={() => toggle(p.id)}
            >
              {p.id === "copilot" && <CopilotBody />}
              {p.id === "notifications" && <NotificationsBody />}
              {p.id === "sessions" && <ActiveSessionsBody />}
              {p.id === "health" && <SystemHealthBody />}
            </RailSection>
          ))}
        </div>
      )}

      <nav
        aria-label="Side panels"
        className="relative w-[76px] shrink-0 py-6 px-1.5 flex flex-col items-stretch gap-2"
      >
        {PANELS.map((p) => (
          <RailPill
            key={p.id}
            {...p}
            active={open.includes(p.id)}
            onClick={() => toggle(p.id)}
          />
        ))}
      </nav>
    </aside>
  );
}

/**
 * The collapsed affordance: an icon over a short horizontal label, so the
 * operator can tell what each strip entry is without opening it (vertical
 * 10px text was hard to read).
 */
function RailPill({
  label,
  shortLabel,
  icon: Icon,
  tint,
  active,
  onClick,
  id,
}: {
  label: string;
  shortLabel: string;
  icon: LucideIcon;
  tint: string;
  active: boolean;
  onClick: () => void;
  id: PanelId;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={active}
      aria-label={label}
      title={active ? `Collapse ${label}` : `Expand ${label}`}
      className="motion-press group relative flex flex-col items-center gap-1.5 rounded-xl px-1 py-2.5 transition-colors"
      style={{
        background: active
          ? "rgba(var(--accent-rgb),0.10)"
          : "color-mix(in srgb, var(--text) 3%, transparent)",
        boxShadow: active
          ? "inset 0 0 0 1px rgba(var(--accent-rgb),0.25)"
          : "inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent)",
      }}
    >
      <Icon
        aria-hidden
        className="h-4 w-4 shrink-0"
        style={{ color: active ? "var(--accent)" : tint }}
      />
      <span
        aria-hidden
        className={`text-[11px] font-medium leading-tight text-center transition-colors ${
          active ? "text-[color:var(--text)]" : "text-muted group-hover:text-[color:var(--text)]"
        }`}
      >
        {shortLabel}
      </span>
      {id === "notifications" && !active && <OpenAlertDot />}
    </button>
  );
}

/**
 * Severity dot on the collapsed Notifications pill. Collapsed-by-default
 * would otherwise hide a firing critical entirely — this keeps the
 * signal on screen without the column. Shares the panel's query key, so
 * react-query serves it from one fetch whether or not the panel is open.
 */
function OpenAlertDot() {
  const { data } = useQuery({
    queryKey: ["alerts", "open"],
    queryFn: () => api.alerts.list({ status: "open" }),
    refetchInterval: 15_000,
  });
  const open = data ?? [];
  if (open.length === 0) return null;
  const worst = open.some((a) => a.severity === "critical")
    ? "var(--critical)"
    : "var(--warning)";
  return (
    <span
      aria-hidden
      className="absolute top-1.5 right-2 h-2 w-2 rounded-full motion-attention"
      style={{ background: worst, boxShadow: `0 0 6px ${worst}` }}
    />
  );
}

/** Expanded shell: the surface, the header row, and the collapse control. */
function RailSection({
  icon: Icon,
  tint,
  label,
  bloom,
  action,
  onCollapse,
  children,
}: {
  icon: LucideIcon;
  tint: string;
  label: string;
  bloom?: boolean;
  action?: React.ReactNode;
  onCollapse: () => void;
  children: React.ReactNode;
}) {
  return (
    <section className={`surface p-4 ${bloom ? "surface-bloom" : ""}`}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Icon className="h-4 w-4" style={{ color: tint }} aria-hidden />
          <h2>{label}</h2>
        </div>
        <div className="flex items-center gap-2">
          {action}
          <button
            type="button"
            onClick={onCollapse}
            title={`Collapse ${label}`}
            aria-label={`Collapse ${label}`}
            className="motion-press h-7 w-7 -mr-1.5 inline-flex items-center justify-center rounded-md text-muted hover:text-[color:var(--text)] hover:bg-bg-elevated/60 transition-colors"
          >
            <ChevronRight className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      </div>
      {children}
    </section>
  );
}

const PROMPTS = [
  "What services are degraded?",
  "Summarise today's events.",
  "Which alerts need attention?",
];

/** `a: null` means the question is in flight. The question is appended to
 *  history BEFORE the request, so it appears the moment you hit Enter —
 *  previously the input cleared and, on the very first question, nothing
 *  rendered at all: both the transcript and the "Thinking…" indicator lived
 *  in the `history.length > 0` branch, so an empty history showed the
 *  suggested prompts and swallowed the message until the answer landed.
 *  A copilot turn can take tens of seconds (it makes real tool calls), which
 *  made that gap look like a dead chat. */
export type Turn = { q: string; a: string | null; error?: boolean };

/**
 * `comfortable` is the phone size: readable text, 44px controls, and a 16px
 * input, because iOS zooms the page when a smaller input takes focus. Pass
 * `history`/`onHistoryChange` when the conversation must outlive this body.
 */
export function CopilotBody({
  comfortable = false,
  history: heldHistory,
  onHistoryChange,
}: {
  comfortable?: boolean;
  history?: Turn[];
  onHistoryChange?: React.Dispatch<React.SetStateAction<Turn[]>>;
} = {}) {
  const [q, setQ] = useState("");
  const [localHistory, setLocalHistory] = useState<Turn[]>([]);
  const history = heldHistory ?? localHistory;
  const setHistory = onHistoryChange ?? setLocalHistory;
  const pending = history.some((t) => t.a === null);

  async function send(prompt?: string) {
    const query = (prompt ?? q).trim();
    if (!query || pending) return;
    setQ("");
    const idx = history.length;
    setHistory((h) => [...h, { q: query, a: null }]);
    const settle = (a: string, error = false) =>
      setHistory((h) => h.map((t, i) => (i === idx ? { ...t, a, error } : t)));
    try {
      const r = await api.copilot.ask(query);
      settle(r.answer?.trim() || "(the model returned an empty answer)");
    } catch (e) {
      // Surface the real reason as a plain sentence. A blanket "(copilot
      // unavailable)" hid auth failures, timeouts and 500s behind one
      // indistinguishable string.
      settle(`The assistant couldn't answer. ${errorText(e)}`, true);
    }
  }

  return (
    <div className={comfortable ? "text-sm flex-1 min-h-0 flex flex-col gap-3" : "text-xs space-y-3"}>
      {history.length === 0 ? (
        <div className="space-y-2">
          <p className="text-muted leading-relaxed">
            Ask anything about your live operational state.
          </p>
          <div className="space-y-1.5">
            {PROMPTS.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => send(p)}
                className={`motion-press w-full text-left px-3 rounded-lg text-muted hover:text-[color:var(--text)] transition-colors ${
                  comfortable ? "min-h-11 py-2.5 text-sm" : "py-2 text-xs"
                }`}
                style={{
                  background: "color-mix(in srgb, var(--text) 3%, transparent)",
                  boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent)",
                }}
              >
                {p}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div
          className={`overflow-auto scrollbar-thin space-y-3 ${comfortable ? "flex-1 min-h-0" : "max-h-[28rem]"}`}
          aria-live="polite"
          aria-relevant="additions text"
          aria-label="Conversation with the assistant"
          role="log"
        >
          {history.map((m, i) => (
            <div key={i} className="space-y-1 motion-enter">
              <div className="text-muted">You · {m.q}</div>
              <div
                className="rounded-lg px-3 py-2"
                style={{
                  background: "color-mix(in srgb, var(--text) 3%, transparent)",
                  boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent)",
                  color: m.error ? "var(--critical)" : undefined,
                }}
              >
                {m.a === null ? (
                  <ThinkingDots label="Querying your infrastructure…" />
                ) : (
                  <div className="motion-enter">
                    <CopilotMarkdown>{m.a}</CopilotMarkdown>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      <form
        className={comfortable ? "pt-1 mt-auto" : "pt-1"}
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <label htmlFor="rail-copilot-input" className="sr-only">
          Message the assistant
        </label>
        <div className="flex gap-2">
        <input
          id="rail-copilot-input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Message the assistant"
          className={`flex-1 min-w-0 rounded-lg px-2.5 transition-colors text-[color:var(--text)] placeholder:text-muted ${
            comfortable ? "h-11 text-base" : "h-9 text-xs"
          }`}
          style={{
            background: "color-mix(in srgb, var(--text) 3%, transparent)",
            boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent)",
          }}
        />
        <Button
          type="submit"
          variant="primary"
          aria-label="Send message"
          className={comfortable ? "h-11 w-11 px-0" : "w-9 px-0"}
          disabled={!q.trim() || pending}
          disabledReason={pending ? "Wait for the current answer" : "Type a message to send"}
          icon={<Send className="h-3.5 w-3.5" aria-hidden />}
        />
        </div>
      </form>
    </div>
  );
}

export function NotificationsBody() {
  const { data: alerts } = useQuery({
    queryKey: ["alerts", "open"],
    queryFn: () => api.alerts.list({ status: "open" }),
    refetchInterval: 15_000,
  });
  const { data: recs } = useQuery({
    queryKey: ["recommendations", "pending"],
    queryFn: () => api.recommendations.list({ status: "pending" }),
    refetchInterval: 20_000,
  });

  // Not "No critical alerts" until we actually know.
  if (alerts === undefined) return <p className="text-xs text-muted">Loading…</p>;

  const open = alerts;
  const critical = open.filter((a) => a.severity === "critical").slice(0, 2);
  const warning = open.filter((a) => a.severity === "warning").slice(0, 2);
  const aiGenerated = (recs ?? []).slice(0, 2);

  return (
    <>
      <NotificationGroup
        label="Critical"
        accent="var(--critical)"
        items={critical.map((a) => ({
          id: a.id,
          title: a.title,
          subtitle: formatRelative(a.created_at),
          href: `/alerts/${a.id}`,
        }))}
        emptyText="No critical alerts."
        icon="critical"
      />
      <NotificationGroup
        label="Warning"
        accent="var(--warning)"
        items={warning.map((a) => ({
          id: a.id,
          title: a.title,
          subtitle: formatRelative(a.created_at),
          href: `/alerts/${a.id}`,
        }))}
        emptyText="No warnings."
        icon="warning"
      />
      <NotificationGroup
        label="AI Generated"
        accent="var(--accent)"
        items={aiGenerated.map((r) => ({
          id: r.id,
          title: r.title,
          subtitle: r.suggested_action || "Tap to review",
          href: "/proposals",
        }))}
        emptyText="No new recommendations."
        icon="ai"
      />
    </>
  );
}

function NotificationGroup({
  label,
  accent,
  items,
  emptyText,
  icon,
}: {
  label: string;
  accent: string;
  items: { id: string; title: string; subtitle: string; href: string }[];
  emptyText: string;
  icon: "critical" | "warning" | "ai";
}) {
  const Icon =
    icon === "critical" ? AlertTriangle : icon === "warning" ? TriangleAlert : Sparkles;
  return (
    <div className="mb-3 last:mb-0">
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.14em] text-muted mb-1.5">
        <span
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ background: accent, boxShadow: `0 0 6px ${accent}` }}
        />
        {label}
      </div>
      {items.length === 0 ? (
        <div className="text-xs text-muted px-2 py-1">{emptyText}</div>
      ) : (
        <div className="space-y-1.5">
          {items.map((n) => (
            <motion.div
              key={n.id}
              initial={{ opacity: 0, x: 4 }}
              animate={{ opacity: 1, x: 0 }}
            >
              <Link
                href={n.href}
                className="relative block rounded-lg px-3 py-2 text-xs overflow-hidden transition-colors hover:bg-bg-elevated/60"
                style={{
                  background: "color-mix(in srgb, var(--text) 3%, transparent)",
                  boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent)",
                }}
              >
                {/* Left edge glow tinted by severity */}
                <span
                  aria-hidden
                  className="pointer-events-none absolute inset-y-0 left-0 w-px"
                  style={{
                    background: accent,
                    boxShadow: `0 0 12px ${accent}`,
                    opacity: 0.7,
                  }}
                />
                <div className="flex items-start gap-2 relative">
                  <Icon className="h-3 w-3 mt-0.5 shrink-0" style={{ color: accent }} aria-hidden />
                  <div className="min-w-0">
                    <div className="font-medium leading-snug truncate">{n.title}</div>
                    <div className="text-xs text-muted mt-0.5 truncate">
                      {n.subtitle}
                    </div>
                  </div>
                </div>
              </Link>
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}

function ActiveSessionsBody() {
  const { data } = useQuery({
    queryKey: ["workflow-runs", "right-panel"],
    queryFn: () => api.workflows.runs(),
    refetchInterval: 12_000,
  });
  const active = (data ?? [])
    .filter((r) => r.status === "running" || r.status === "waiting_for_approval")
    .slice(0, 4);

  return (
    <div className="space-y-1.5">
      {active.length === 0 && <div className="text-xs text-muted">Nothing running.</div>}
      {active.map((w) => (
        <div
          key={w.id}
          className="rounded-lg px-3 py-2 text-xs relative overflow-hidden"
          style={{
            background: "color-mix(in srgb, var(--text) 3%, transparent)",
            boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent)",
          }}
        >
          <div className="flex items-center justify-between">
            <span className="font-medium truncate">{prettyName(w.workflow_name)}</span>
            <span
              className="text-[11px] font-medium uppercase tracking-[0.12em]"
              style={{ color: "var(--accent)" }}
            >
              {w.status === "waiting_for_approval" ? "awaiting" : "running"}
            </span>
          </div>
          <div className="text-xs text-muted mt-0.5">
            started {formatRelative(w.started_at)}
          </div>
        </div>
      ))}
    </div>
  );
}

function prettyName(name: string) {
  return name
    .replace(/\./g, " · ")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function SystemHealthBody() {
  const { data: alerts } = useQuery({
    queryKey: ["alerts", "all", "health"],
    queryFn: () => api.alerts.list(),
    refetchInterval: 30_000,
  });
  // Not a reassuring 100% until we actually know.
  if (alerts === undefined) return <p className="text-xs text-muted">Loading…</p>;

  const open = alerts.filter((a) => a.status === "open");
  const score = Math.max(
    50,
    100 -
      open.filter((a) => a.severity === "critical").length * 8 -
      open.filter((a) => a.severity === "warning").length * 2
  );

  return (
    <div className="flex items-center gap-4">
      <Gauge value={score} />
      <div className="text-xs">
        <div className="font-medium text-base">
          {score}% <span className="text-muted text-xs font-normal">reliability</span>
        </div>
        <div className="text-muted leading-snug mt-1">
          {open.length === 0
            ? "No open alerts."
            : `${open.length} signals being watched.`}
        </div>
      </div>
    </div>
  );
}

function Gauge({ value }: { value: number }) {
  const radius = 28;
  const circumference = 2 * Math.PI * radius;
  const dash = (value / 100) * circumference;
  return (
    <div className="relative h-[72px] w-[72px] shrink-0">
      <div
        aria-hidden
        className="absolute inset-0 rounded-full"
        style={{
          background:
            "radial-gradient(50% 50% at 50% 50%, var(--accent-glow), transparent 70%)",
          filter: "blur(2px)",
        }}
      />
      <svg viewBox="0 0 72 72" className="relative h-full w-full -rotate-90">
        <circle
          cx="36"
          cy="36"
          r={radius}
          fill="none"
          stroke="color-mix(in srgb, var(--text) 8%, transparent)"
          strokeWidth="6"
        />
        <circle
          cx="36"
          cy="36"
          r={radius}
          fill="none"
          stroke="var(--accent)"
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference}`}
          style={{ filter: "drop-shadow(0 0 8px var(--accent-bloom))" }}
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center text-[11px] font-semibold">
        {value}%
      </div>
    </div>
  );
}
