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
import { CopilotMarkdown } from "@/components/copilot/markdown";
import { ThinkingDots } from "@/components/copilot/thinking-dots";
import { formatRelative } from "@/lib/utils";

/**
 * Right rail. Mirrors the cinematic surface system used everywhere else:
 * the rail itself is a deeper-tinted column with its own ambient wash,
 * each section is an illuminated-glass surface, and notifications carry
 * a left edge glow tinted by severity instead of a hard border colour.
 *
 * Every section is collapsed by default so the main column gets the
 * width — the rail reduces to a slim strip of vertical pills (an
 * activity-bar) and only claims its full column once the operator opens
 * something. The choice is remembered per browser; first visit is
 * always fully collapsed.
 */

type PanelId = "copilot" | "notifications" | "sessions" | "health";

const RAIL_STORAGE_KEY = "daalu-right-rail-open";

const PANELS: {
  id: PanelId;
  label: string;
  icon: LucideIcon;
  tint: string;
}[] = [
  { id: "copilot", label: "AI Assistant", icon: MessageSquare, tint: "var(--accent)" },
  {
    id: "notifications",
    label: "Notifications",
    icon: TriangleAlert,
    tint: "var(--warning)",
  },
  { id: "sessions", label: "Active sessions", icon: ActivityIcon, tint: "var(--accent)" },
  { id: "health", label: "System health", icon: Heart, tint: "var(--accent)" },
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
                  <a
                    href="/alerts"
                    className="text-[11px] text-muted hover:text-[color:var(--text)]"
                  >
                    View all
                  </a>
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
        className="relative w-[52px] shrink-0 py-6 flex flex-col items-center gap-2"
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
 * The collapsed affordance: an icon over a vertical label, so the
 * operator can tell what each strip entry is without opening it.
 */
function RailPill({
  label,
  icon: Icon,
  tint,
  active,
  onClick,
  id,
}: {
  label: string;
  icon: LucideIcon;
  tint: string;
  active: boolean;
  onClick: () => void;
  id: PanelId;
}) {
  return (
    <button
      onClick={onClick}
      aria-expanded={active}
      title={active ? `Collapse ${label}` : `Expand ${label}`}
      className="group relative flex flex-col items-center gap-2 rounded-full px-2 py-3 transition-colors"
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
        className="h-4 w-4 shrink-0"
        style={{ color: active ? "var(--accent)" : tint }}
      />
      <span
        className="text-[10px] uppercase tracking-[0.18em] text-muted group-hover:text-[color:var(--text)] transition-colors whitespace-nowrap"
        style={{ writingMode: "vertical-rl" }}
      >
        {label}
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
      className="absolute top-1.5 right-1.5 h-1.5 w-1.5 rounded-full"
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
          <Icon className="h-4 w-4" style={{ color: tint }} />
          {label}
        </div>
        <div className="flex items-center gap-2">
          {action}
          <button
            onClick={onCollapse}
            title={`Collapse ${label}`}
            aria-label={`Collapse ${label}`}
            className="text-muted hover:text-[color:var(--text)] transition-colors"
          >
            <ChevronRight className="h-3.5 w-3.5" />
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
type Turn = { q: string; a: string | null; error?: boolean };

function CopilotBody() {
  const [q, setQ] = useState("");
  const [history, setHistory] = useState<Turn[]>([]);
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
      // Surface the real reason. A blanket "(copilot unavailable)" hid auth
      // failures, timeouts and 500s behind one indistinguishable string.
      const msg = e instanceof Error ? e.message : String(e);
      settle(`Couldn't reach the copilot — ${msg}`, true);
    }
  }

  return (
    <div className="text-xs space-y-3">
      {history.length === 0 ? (
        <div className="space-y-2">
          <p className="text-muted leading-relaxed">
            Ask anything about your live operational state.
          </p>
          <div className="space-y-1.5">
            {PROMPTS.map((p) => (
              <button
                key={p}
                onClick={() => send(p)}
                className="w-full text-left px-3 py-2 rounded-lg text-[12px] text-muted hover:text-[color:var(--text)] transition-colors"
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
        <div className="max-h-[28rem] overflow-auto scrollbar-thin space-y-3">
          {history.map((m, i) => (
            <div key={i} className="space-y-1">
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
                  <CopilotMarkdown>{m.a}</CopilotMarkdown>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="flex gap-2 pt-1">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") send();
          }}
          placeholder="Message the copilot"
          className="flex-1 rounded-lg px-2.5 py-1.5 outline-none transition-colors text-[color:var(--text)] placeholder:text-muted"
          style={{
            background: "color-mix(in srgb, var(--text) 3%, transparent)",
            boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent)",
          }}
        />
        <button
          onClick={() => send()}
          className="rounded-lg px-2.5 transition-transform hover:scale-[1.04]"
          style={{
            background:
              "linear-gradient(180deg, color-mix(in srgb, var(--accent) 95%, white) 0%, var(--accent) 100%)",
            color: "var(--bg-base)",
            boxShadow:
              "inset 0 1px 0 rgba(255,255,255,0.35), 0 0 18px var(--accent-glow)",
          }}
        >
          <Send className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

function NotificationsBody() {
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

  const open = alerts ?? [];
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
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.20em] text-muted mb-1.5">
        <span
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ background: accent, boxShadow: `0 0 6px ${accent}` }}
        />
        {label}
      </div>
      {items.length === 0 ? (
        <div className="text-[11px] text-muted px-2 py-1">{emptyText}</div>
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
                  <Icon className="h-3 w-3 mt-0.5 shrink-0" style={{ color: accent }} />
                  <div className="min-w-0">
                    <div className="font-medium leading-snug truncate">{n.title}</div>
                    <div className="text-[10px] text-muted mt-0.5 truncate">
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
              className="text-[10px] uppercase tracking-[0.18em]"
              style={{ color: "var(--accent)" }}
            >
              {w.status === "waiting_for_approval" ? "awaiting" : "running"}
            </span>
          </div>
          <div className="text-[10px] text-muted mt-0.5">
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

function SystemHealthBody() {
  const { data: alerts } = useQuery({
    queryKey: ["alerts", "all", "health"],
    queryFn: () => api.alerts.list(),
    refetchInterval: 30_000,
  });
  const open = (alerts ?? []).filter((a) => a.status === "open");
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
            ? "All systems nominal."
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
