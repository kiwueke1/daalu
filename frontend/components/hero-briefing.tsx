"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  Sparkles,
  RotateCcw,
  AlertTriangle,
  ListChecks,
  Activity,
  Gauge,
  FileText,
  ChevronDown,
  ArrowRight,
} from "lucide-react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

type Tone = "critical" | "warning" | "accent" | "blue" | "muted";

export interface Section {
  title: string;
  content: string;
  items: number;
}

/**
 * The daily AI briefing, rendered as a compact header + a grid of
 * clickable section tiles. Each tile shows a one-glance preview (item
 * count / first line); clicking expands it in place to reveal the full
 * section. This replaces the old "dump the entire markdown body inline"
 * layout, which buried the signal in a wall of text.
 */
export function HeroBriefing({ channel = "infra" }: { channel?: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { data, isLoading } = useQuery({
    queryKey: ["briefing", "latest", channel],
    queryFn: () => api.briefings.latest(channel),
    retry: false,
  });

  const generate = useMutation({
    mutationFn: () => api.briefings.generate(channel),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["briefing"] });
      qc.invalidateQueries({ queryKey: ["briefings"] });
      toast.success("New briefing written from the latest events.");
    },
    onError: (e) => toast.error(`Couldn't write a briefing. ${errorText(e)}`),
  });

  const [showRest, setShowRest] = useState(false);
  const sections = data ? parseSections(data.body_markdown) : [];
  // Zero-valued counters ("0 incidents opened") next to "6 active incidents"
  // read as a contradiction; only show the ones that say something.
  const metrics = data
    ? Object.entries(data.metrics ?? {}).filter(
        ([, v]) => v !== 0 && v !== "0" && v !== null && v !== ""
      )
    : [];
  const [headline, rest, fullHeadline] = shortHeadline(data?.summary || data?.title || "");

  return (
    <motion.section
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="gradient-border p-6 lg:p-7 bg-hero-grad relative overflow-hidden"
    >
      <div className="absolute -top-24 -right-24 h-72 w-72 rounded-full bg-accent-blue/20 blur-3xl pointer-events-none" />
      <div className="absolute -bottom-32 -left-12 h-72 w-72 rounded-full bg-accent-violet/20 blur-3xl pointer-events-none" />

      <div className="relative">
        {/* Header row */}
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
          <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] uppercase tracking-[0.18em] text-accent-blue">
            <Sparkles aria-hidden className="h-3 w-3" /> AI briefing · {channel}
            {data?.coverage_date && (
              <span className="text-muted normal-case tracking-normal">
                · {data.coverage_date}
              </span>
            )}
          </span>
          {!isLoading && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => generate.mutate()}
              pending={generate.isPending}
              title="Ask the agent to write a fresh briefing from the latest events"
              icon={<RotateCcw aria-hidden className="h-3.5 w-3.5" />}
            >
              {generate.isPending
                ? "Writing a new briefing…"
                : data
                ? "Regenerate"
                : "Generate today's briefing"}
            </Button>
          )}
        </div>

        {isLoading && (
          <div role="status" className="animate-shimmer text-muted">
            Loading the latest briefing…
          </div>
        )}

        {!data && !isLoading && (
          <div>
            <h2 className="text-2xl font-semibold leading-tight mb-3">
              No briefing yet for the {channel} channel.
            </h2>
            <p className="text-fg/70 max-w-2xl">
              Daalu writes a briefing every day from the events your connected
              sources send. Click{" "}
              <span className="text-fg">Generate today&apos;s briefing</span> to
              write one now, or{" "}
              <Link href="/integrations" className="underline hover:text-fg">
                connect a source
              </Link>{" "}
              to give it more to work with.
            </p>
          </div>
        )}

        {data && (
          <>
            {/* Headline: the summary's first sentence. The rest is folded. */}
            <h2 key={data.id} title={fullHeadline ?? undefined} className="motion-enter text-base md:text-lg lg:text-xl leading-snug font-semibold tracking-tight max-w-4xl">
              {headline}
            </h2>
            <div className="mt-1.5 mb-4 max-w-4xl">
              {rest && (
                <p
                  className={`text-sm text-muted leading-relaxed ${
                    showRest ? "" : "line-clamp-2"
                  }`}
                >
                  {rest}
                </p>
              )}
              {rest && (
                <button
                  type="button"
                  onClick={() => setShowRest((v) => !v)}
                  aria-expanded={showRest}
                  className="mt-1 min-h-6 text-xs text-muted underline hover:text-fg"
                >
                  {showRest ? "Show less" : "Read the full summary"}
                </button>
              )}
            </div>

            {/* Metric stat chips */}
            {metrics.length > 0 && (
              // Say whose window these figures cover, so they don't read as
              // contradicting the live tiles below.
              <div className="text-[11px] uppercase tracking-wider text-muted mb-1.5">
                In this briefing&apos;s window
                {data.coverage_date && (
                  <span className="normal-case tracking-normal"> · {data.coverage_date}</span>
                )}
              </div>
            )}
            {metrics.length > 0 && (
              // One swipeable row on a phone instead of four wrapped rows.
              <div className="-mx-6 px-6 sm:mx-0 sm:px-0 flex flex-nowrap sm:flex-wrap gap-2.5 mb-5 overflow-x-auto sm:overflow-visible [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                {metrics.map(([k, v]) => (
                  <div
                    key={k}
                    className="shrink-0 flex items-baseline gap-2 rounded-lg border border-line bg-bg-elevated/50 px-3 py-1.5"
                  >
                    <span className="text-lg font-semibold leading-none tabular-nums">
                      {String(v)}
                    </span>
                    <span className="text-[11px] uppercase tracking-wider text-muted">
                      {k.replace(/_/g, " ")}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {/* Section tiles */}
            {sections.length > 0 ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 items-start">
                {sections.map((s, i) => (
                  <SectionTile key={i} section={s} />
                ))}
              </div>
            ) : (
              // Fallback: no parseable sections → render the body as before.
              <article className="prose prose-sm dark:prose-invert max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {data.body_markdown}
                </ReactMarkdown>
              </article>
            )}

            {data.id && (
              <div className="mt-4">
                <Link
                  href={`/reports/briefings/${data.id}`}
                  className="inline-flex items-center gap-1 min-h-6 text-xs text-muted hover:text-fg transition-colors"
                >
                  Open the full briefing <ArrowRight aria-hidden className="h-3 w-3" />
                </Link>
              </div>
            )}
          </>
        )}
      </div>
    </motion.section>
  );
}

/**
 * One collapsible section card. Collapsed it shows an icon, title, and a
 * one-line preview (item count or first line). Clicking reveals the full
 * section markdown in place.
 */
function SectionTile({ section }: { section: Section }) {
  const [open, setOpen] = useState(false);
  const meta = toneFor(section.title);
  const Icon = meta.icon;

  return (
    <div
      className={`surface relative overflow-hidden transition-colors ${
        open ? "ring-1 ring-[color:var(--accent-soft)]" : ""
      }`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full text-left p-4 flex items-start gap-3 group"
      >
        <span
          className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
          style={{ background: meta.bg, color: meta.color }}
        >
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium text-[color:var(--text)]">
              {section.title}
            </span>
            <span className="flex items-center gap-2 shrink-0">
              {section.items > 0 && (
                <span
                  className="text-[11px] font-medium rounded-full px-2 py-0.5"
                  style={{ background: meta.bg, color: meta.color }}
                >
                  {section.items}
                </span>
              )}
              <ChevronDown
                aria-hidden
                className={`h-4 w-4 text-muted transition-transform duration-200 ${
                  open ? "rotate-180" : ""
                }`}
              />
            </span>
          </div>
          {!open && (
            <p className="mt-1 text-xs text-muted line-clamp-2">
              {previewOf(section)}
            </p>
          )}
        </div>
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: "easeInOut" }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 pt-0">
              <div className="border-t border-line pt-3 prose prose-sm dark:prose-invert max-w-none prose-p:my-1.5 prose-li:my-0.5 prose-ul:my-1">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {section.content}
                </ReactMarkdown>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/* ---------- helpers ---------- */

/**
 * Split a summary into its first sentence and the rest. A "sentence" ends
 * at . ! or ? followed by whitespace and a capital/digit, so "v1.2" and
 * "e.g. foo" don't split.
 */
export function splitFirstSentence(text: string): [string, string] {
  const t = text.trim();
  const m = t.match(/^(.+?[.!?])\s+(?=[A-Z0-9"'(])/);
  if (!m) return [t, ""];
  return [m[1], t.slice(m[0].length).trim()];
}

/**
 * The headline is the summary's complete first sentence. Only when that runs
 * past `max` chars is it cut, at a word boundary, with a trailing "…"; the
 * body then continues from the next sentence (it never starts with "…").
 * The third value is the full first sentence when it was cut, for a tooltip.
 */
export function shortHeadline(text: string, max = 140): [string, string, string | null] {
  const [first, rest] = splitFirstSentence(text);
  if (first.length <= max) return [first, rest, null];
  const cut = first.slice(0, max).lastIndexOf(" ");
  if (cut <= 0) return [first, rest, null];
  const headline = first.slice(0, cut).replace(/[,;:—\s]+$/, "") + "…";
  return [headline, rest, first];
}

/**
 * Split a briefing's markdown body into sections. Recognises both
 * markdown headings (`## Title`) and bold-only lines acting as headings
 * (`**Title**`). Everything before the first heading is ignored (it's
 * usually the summary, already shown above).
 */
export function parseSections(md: string): Section[] {
  if (!md) return [];
  const lines = md.split(/\r?\n/);
  const sections: Section[] = [];
  let cur: Section | null = null;
  const headingRe = /^\s{0,3}#{1,6}\s+(.*\S)\s*$/;
  const boldHeadingRe = /^\s*\*\*(.+?)\*\*\s*:?\s*$/;

  for (const line of lines) {
    let title: string | null = null;
    const h = line.match(headingRe);
    if (h) {
      title = h[1].replace(/[*_`#]/g, "").trim();
    } else {
      const b = line.match(boldHeadingRe);
      if (b) title = b[1].replace(/[*_`]/g, "").trim();
    }

    if (title) {
      cur = { title, content: "", items: 0 };
      sections.push(cur);
    } else if (cur) {
      cur.content += (cur.content ? "\n" : "") + line;
      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) cur.items += 1;
    }
  }

  return sections.filter((s) => s.content.trim().length > 0 || s.items > 0);
}

function previewOf(s: Section): string {
  if (s.items > 0) {
    // First list item, cleaned of markdown bullet syntax.
    const first = s.content
      .split(/\r?\n/)
      .find((l) => /^\s*([-*+]|\d+\.)\s+/.test(l));
    if (first) {
      return first
        .replace(/^\s*([-*+]|\d+\.)\s+/, "")
        .replace(/[*_`]/g, "")
        .trim();
    }
  }
  const firstLine = s.content
    .split(/\r?\n/)
    .map((l) => l.trim())
    .find((l) => l.length > 0);
  return (firstLine ?? "").replace(/[*_`#>]/g, "").trim();
}

/**
 * Pick an icon + colour tone for a section, by keyword in its title.
 */
export function toneFor(title: string): {
  icon: React.ComponentType<{ className?: string }>;
  tone: Tone;
  color: string;
  bg: string;
} {
  const t = title.toLowerCase();
  const make = (
    icon: React.ComponentType<{ className?: string }>,
    color: string,
    bg: string,
    tone: Tone
  ) => ({ icon, color, bg, tone });

  if (/(incident|alert|critical|issue|outage)/.test(t))
    return make(AlertTriangle, "var(--critical)", "color-mix(in srgb, var(--critical) 12%, transparent)", "critical");
  if (/(recommend|action|next step|remediat|fix)/.test(t))
    return make(ListChecks, "var(--accent)", "var(--accent-soft)", "accent");
  if (/(capacity|cost|budget|spend|usage)/.test(t))
    return make(Gauge, "var(--text)", "var(--bg-elevated)", "muted");
  if (/(24h|last|activity|log|summary|change|event)/.test(t))
    return make(Activity, "var(--info)", "color-mix(in srgb, var(--info) 12%, transparent)", "blue");
  return make(FileText, "var(--text)", "var(--bg-elevated)", "muted");
}
