"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowUpRight,
  BookOpen,
  Bug,
  Check,
  GitBranch,
  HelpCircle,
  Lightbulb,
  Loader2,
  MessageSquare,
  ServerCog,
  Sparkles,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * /help — three blocks down the page:
 *
 *  1. Docs — curated links into the public handbook at docs.daalu.io,
 *     grouped by topic.
 *  2. Send feedback — single textarea + category radio that POSTs to
 *     /feedback. The form is intentionally small; longer triage
 *     happens out-of-band.
 *  3. System status — backend version, commit SHA, and build time
 *     read from GET /version. Lets the operator confirm which build
 *     they're looking at when something's misbehaving.
 */

const DOCS = "https://docs.daalu.io";

const DOC_GROUPS: { title: string; links: { label: string; href: string }[] }[] = [
  {
    title: "Getting started",
    links: [
      { label: "How Daalu works", href: `${DOCS}/introduction/how-it-works` },
      { label: "Connect your observability stack", href: `${DOCS}/getting-started/onboarding-your-observability-stack` },
      { label: "Source of truth (Nautobot)", href: `${DOCS}/core-concepts/source-of-truth` },
    ],
  },
  {
    title: "Using Daalu",
    links: [
      { label: "Integrations", href: `${DOCS}/core-concepts/integrations-and-adapters` },
      { label: "Alerts & incidents", href: `${DOCS}/feature-walkthroughs/alerts-and-incidents` },
      { label: "Local GPU & billing", href: `${DOCS}/core-concepts/local-gpu-and-llm-router` },
    ],
  },
  {
    title: "Support",
    links: [
      { label: "Troubleshooting", href: `${DOCS}/support/troubleshooting` },
      { label: "FAQ", href: `${DOCS}/support/faq` },
      { label: "Glossary", href: `${DOCS}/support/glossary` },
    ],
  },
];

const CATEGORIES: { id: string; label: string; icon: typeof Bug }[] = [
  { id: "bug", label: "Bug", icon: Bug },
  { id: "idea", label: "Idea", icon: Lightbulb },
  { id: "praise", label: "Praise", icon: Sparkles },
  { id: "general", label: "General", icon: MessageSquare },
];

export default function HelpPage() {
  return (
    <div className="space-y-6 max-w-[1100px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <HelpCircle className="h-5 w-5 text-accent-cyan" /> Help & Feedback
        </h1>
        <p className="text-muted text-sm mt-1">
          Documentation, and a direct channel back to the team.
        </p>
      </div>

      <DocsSection />
      <FeedbackSection />
      <StatusSection />
    </div>
  );
}

// ── Docs ─────────────────────────────────────────────────────────────

function DocsSection() {
  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <div className="flex items-center gap-2 mb-4">
        <BookOpen className="h-4 w-4 text-muted" />
        <h2 className="text-sm font-medium">Documentation</h2>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {DOC_GROUPS.map((g) => (
          <div key={g.title}>
            <div className="text-xs uppercase tracking-[0.16em] text-muted mb-2">
              {g.title}
            </div>
            <ul className="space-y-1.5">
              {g.links.map((l) => (
                <li key={l.href}>
                  <a
                    href={l.href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm flex items-center gap-1.5 text-[color:var(--text)] hover:text-accent-blue group"
                  >
                    <span className="truncate">{l.label}</span>
                    <ArrowUpRight className="h-3 w-3 opacity-50 group-hover:opacity-100 shrink-0" />
                  </a>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

// ── Feedback ─────────────────────────────────────────────────────────

function FeedbackSection() {
  const [category, setCategory] = useState<string>("general");
  const [message, setMessage] = useState("");
  const [ok, setOk] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: () =>
      api.feedback.send({
        message: message.trim(),
        category,
        page_url:
          typeof window !== "undefined" ? window.location.pathname : "",
      }),
    onSuccess: () => {
      setOk(true);
      setMessage("");
      setErr(null);
      setTimeout(() => setOk(false), 4000);
    },
    onError: (e: Error) => {
      setErr(e.message || "could not send");
      setOk(false);
    },
  });

  const canSend = message.trim().length > 0 && !send.isPending;

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <div className="flex items-center gap-2 mb-1">
        <MessageSquare className="h-4 w-4 text-muted" />
        <h2 className="text-sm font-medium">Send feedback</h2>
      </div>
      <p className="text-xs text-muted mb-4 max-w-[640px]">
        Goes straight to the platform team. Include URLs or steps to reproduce
        when reporting a bug.
      </p>

      <div className="flex flex-wrap gap-2 mb-3">
        {CATEGORIES.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setCategory(id)}
            className={cn(
              "text-xs h-8 px-3 rounded-lg border flex items-center gap-1.5 transition-colors",
              category === id
                ? "border-accent bg-accent/15 text-fg font-medium"
                : "border-line text-muted hover:text-[color:var(--text)]"
            )}
          >
            <Icon className="h-3.5 w-3.5" /> {label}
          </button>
        ))}
      </div>

      <textarea
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        rows={5}
        placeholder="Tell us what's on your mind…"
        className="w-full p-3 rounded-lg border border-line bg-[color:var(--bg-elevated)] text-sm focus:outline-none focus:border-accent transition-colors resize-y"
      />

      {err && (
        <div className="mt-2 text-xs text-[color:var(--critical)]">{err}</div>
      )}
      {ok && (
        <div className="mt-2 text-xs text-accent-blue flex items-center gap-1.5">
          <Check className="h-3.5 w-3.5" /> Thanks — your feedback is in.
        </div>
      )}

      <div className="flex justify-end mt-3">
        <button
          type="button"
          onClick={() => send.mutate()}
          disabled={!canSend}
          className="text-xs h-9 px-3 rounded-lg border border-accent-blue/60 bg-accent-blue/10 hover:bg-accent-blue/20 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
        >
          {send.isPending ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Sending…
            </>
          ) : (
            "Send feedback"
          )}
        </button>
      </div>
    </section>
  );
}

// ── System status ─────────────────────────────────────────────────────

function StatusSection() {
  const version = useQuery({
    queryKey: ["meta", "version"],
    queryFn: api.meta.version,
    refetchOnWindowFocus: false,
  });

  // Missing or "unknown" values read as "unknown", never as a bare dash.
  const known = (v: string | null | undefined) =>
    v && v !== "unknown" && v !== "—" ? v : null;
  const sha = known(version.data?.commit_sha);
  const built = known(version.data?.built_at);

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <div className="flex items-center gap-2 mb-4">
        <ServerCog className="h-4 w-4 text-muted" />
        <h2 className="text-sm font-medium">System status</h2>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <StatBlock
          label="Version"
          value={known(version.data?.version) ?? "unknown"}
          loading={version.isLoading}
        />
        <StatBlock
          label="Commit"
          value={sha ? sha.slice(0, 12) : "unknown"}
          mono
          loading={version.isLoading}
          icon={GitBranch}
        />
        <StatBlock
          label="Built"
          value={built ? formatBuilt(built) : "unknown"}
          loading={version.isLoading}
        />
      </div>

      {version.isError && (
        <div className="text-xs text-[color:var(--critical)] mt-3">
          Couldn&apos;t read the version. The API may be unavailable.
        </div>
      )}
    </section>
  );
}

function StatBlock({
  label,
  value,
  mono,
  loading,
  icon: Icon,
}: {
  label: string;
  value: string;
  mono?: boolean;
  loading?: boolean;
  icon?: typeof GitBranch;
}) {
  const body = (
    <>
      <div className="text-xs uppercase tracking-[0.16em] text-muted mb-1.5">
        {label}
      </div>
      <div
        className={cn(
          "text-sm flex items-center gap-1.5",
          mono && "font-mono"
        )}
      >
        {Icon && <Icon className="h-3.5 w-3.5 text-muted" />}
        {loading ? "Loading…" : value}
      </div>
    </>
  );
  return (
    <div className="p-3 rounded-lg border border-line bg-bg-elevated/30">
      {body}
    </div>
  );
}

function formatBuilt(s: string): string {
  try {
    return new Date(s).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return s;
  }
}
