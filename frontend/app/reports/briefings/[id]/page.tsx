"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeft, FileText, Link2, Check } from "lucide-react";
import { api } from "@/lib/api";
import { Accordion, AccordionItem } from "@/components/ui/accordion";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";
import { parseSections, toneFor } from "@/components/hero-briefing";

const norm = (t: string) => t.replace(/[*_`#>\s]+/g, " ").trim().toLowerCase();

/** Body text before the first heading (often a restatement of the summary). */
function preambleOf(md: string): string {
  const lines = (md ?? "").split(/\r?\n/);
  const out: string[] = [];
  for (const line of lines) {
    if (/^\s{0,3}#{1,6}\s+\S/.test(line) || /^\s*\*\*(.+?)\*\*\s*:?\s*$/.test(line)) break;
    out.push(line);
  }
  return out.join("\n").trim();
}

export default function BriefingDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [copied, setCopied] = useState(false);

  const q = useQuery({
    queryKey: ["briefing", id],
    queryFn: () => api.briefings.get(id),
    enabled: !!id,
  });
  const briefing = q.data;

  const copyPermalink = async () => {
    if (typeof window === "undefined") return;
    await navigator.clipboard.writeText(window.location.href);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  if (!briefing) {
    return (
      <div className="space-y-4 max-w-[1000px]">
        <Link
          href="/reports"
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-fg transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> Back to Reports
        </Link>
        <LoadState
          query={q}
          loadingText="Loading briefing…"
          empty
          emptyTitle="Briefing not found"
          emptyHint="It may have been removed. The Reports page lists every briefing."
        >
          {null}
        </LoadState>
      </div>
    );
  }

  const sections = parseSections(briefing.body_markdown);
  const rawPreamble = sections.length > 0 ? preambleOf(briefing.body_markdown) : "";
  const summaryN = norm(briefing.summary ?? "");
  const preambleN = norm(rawPreamble);
  // The body often opens by restating the summary; show that text once.
  const preamble = rawPreamble && preambleN !== summaryN ? rawPreamble : "";
  const restated = (t: string) => !!summaryN && t.startsWith(summaryN.slice(0, 80));
  const showSummary =
    !!briefing.summary &&
    !(preamble && restated(preambleN)) &&
    !(sections.length === 0 && restated(norm(briefing.body_markdown ?? "")));

  // Open the section that needs the user (incidents first, then recommended
  // actions) instead of whatever happens to come first.
  const needsUser = (kind: string) =>
    sections.findIndex((sec) => toneFor(sec.title).tone === kind && sec.items > 0);
  const openIdx = [needsUser("critical"), needsUser("accent")].find((i) => i >= 0) ?? 0;

  return (
    <div className="space-y-6 max-w-[1000px] motion-enter">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <Link
          href="/reports"
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-fg transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> Back to Reports
        </Link>
        <Button
          size="sm"
          onClick={copyPermalink}
          icon={
            copied ? (
              <Check className="h-3.5 w-3.5 motion-success" aria-hidden />
            ) : (
              <Link2 className="h-3.5 w-3.5" aria-hidden />
            )
          }
        >
          <span role="status">{copied ? "Copied" : "Copy permalink"}</span>
        </Button>
      </div>

      <article className="gradient-border p-6 lg:p-8 bg-hero-grad">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-accent mb-2">
          <FileText className="h-3.5 w-3.5" aria-hidden /> {briefing.channel} ·{" "}
          {briefing.coverage_date}
        </div>
        <h1 className="text-2xl font-semibold mb-3">{briefing.title}</h1>
        {showSummary && <p className="text-muted mb-4">{briefing.summary}</p>}
        {preamble && (
          <div className="prose prose-sm dark:prose-invert max-w-none mb-4">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{preamble}</ReactMarkdown>
          </div>
        )}
        {sections.length > 0 ? (
          <Accordion defaultOpen={`s${openIdx}`}>
            {sections.map((sec, i) => {
              const tone = toneFor(sec.title);
              const Icon = tone.icon;
              return (
                <AccordionItem
                  key={i}
                  id={`s${i}`}
                  tone={tone.tone === "muted" ? undefined : tone.color}
                  leading={
                    <span style={{ color: tone.color }}>
                      <Icon className="h-4 w-4" />
                    </span>
                  }
                  title={<span className="font-medium">{sec.title}</span>}
                  meta={sec.items > 0 ? `${sec.items} item${sec.items === 1 ? "" : "s"}` : undefined}
                >
                  <div className="prose prose-sm dark:prose-invert max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{sec.content}</ReactMarkdown>
                  </div>
                </AccordionItem>
              );
            })}
          </Accordion>
        ) : (
          !preamble && (
            <div className="prose prose-sm dark:prose-invert max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {briefing.body_markdown}
              </ReactMarkdown>
            </div>
          )
        )}
      </article>

      {briefing.source_event_ids && briefing.source_event_ids.length > 0 && (
        <section className="rounded-xl border border-line bg-bg-card p-4">
          <h3 className="text-xs uppercase tracking-wider text-muted mb-2">
            Cited events
          </h3>
          <p className="text-sm text-muted">
            {briefing.source_event_ids.length} event
            {briefing.source_event_ids.length === 1 ? "" : "s"} fed this briefing.
            Open the{" "}
            <Link href="/reports?tab=query" className="text-accent hover:underline">
              Query tab
            </Link>{" "}
            to inspect them.
          </p>
        </section>
      )}
    </div>
  );
}
