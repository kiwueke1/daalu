"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BookMarked, Plus, Sparkles } from "lucide-react";
import { api, type Runbook, type RunbookStatus } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatRelative } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";
import { Button } from "@/components/ui/button";
import { Chip, PageHeader, Segmented } from "@/components/aiops/ui";
import { RunbookEditor } from "@/components/aiops/runbook-editor";

type Filter = "" | RunbookStatus;

/**
 * /runbooks — runbooks as code. Published runbooks are injected into the
 * AI's triage for matching alerts; drafts the AI wrote after a verified fix
 * wait here for a human to review and publish.
 */
export default function RunbooksPage() {
  const { user } = useAuth();
  const isAdmin = !!(user?.is_admin || user?.is_superuser);
  const [status, setStatus] = useState<Filter>("");
  // "new" = create form; a runbook id = that one open; null = none.
  const [selected, setSelected] = useState<string | null>(null);

  const q = useQuery({
    queryKey: ["aiops-runbooks", status],
    queryFn: () => api.aiops.runbooks.list(status || undefined),
  });
  const rows = q.data ?? [];
  const current: Runbook | null =
    selected && selected !== "new" ? rows.find((r) => r.id === selected) ?? null : null;
  const needsReview = (r: Runbook) => r.status === "draft" && r.source === "generated";

  // On a wide screen, open the first AI draft waiting for review, so the
  // thing that needs the user is already on screen. Once per filter.
  const autoPicked = useRef<string | null>(null);
  useEffect(() => {
    if (!q.data || selected !== null || autoPicked.current === status) return;
    autoPicked.current = status;
    if (!window.matchMedia("(min-width: 1024px)").matches) return;
    const first = q.data.find(needsReview);
    if (first) setSelected(first.id);
  }, [q.data, selected, status]);

  // Stacked layout: bring the editor into view when a runbook is picked.
  const editorRef = useRef<HTMLElement>(null);
  const pick = (id: string) => {
    setSelected(id);
    if (!window.matchMedia("(min-width: 1024px)").matches) {
      window.requestAnimationFrame(() =>
        editorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
      );
    }
  };

  return (
    <div className="space-y-6 max-w-[1200px]">
      <PageHeader
        icon={BookMarked}
        title="Runbooks"
        actions={
          <Button
            variant="primary"
            size="sm"
            icon={<Plus className="h-3.5 w-3.5" aria-hidden />}
            onClick={() => pick("new")}
          >
            New runbook
          </Button>
        }
      >
        Your team&apos;s fixes, written down where the AI can use them. A published
        runbook that matches an alert goes into the AI&apos;s triage context, and
        its allowed actions guide the fix it proposes.
      </PageHeader>

      <Segmented
        ariaLabel="Status"
        value={status}
        onChange={(v) => {
          setStatus(v);
          setSelected(null);
        }}
        options={[
          { value: "", label: "All" },
          { value: "draft", label: "Drafts" },
          { value: "published", label: "Published" },
        ]}
      />

      <div className="grid lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)] gap-4 items-start">
        <LoadState
          query={q}
          loadingText="Loading runbooks…"
          empty={rows.length === 0}
          emptyTitle={status === "draft" ? "No drafts to review" : "No runbooks yet"}
          emptyHint="Write one here, import a folder with `daalu runbooks import <dir>`, or let the AI draft one after a verified fix."
        >
          <ul className="space-y-2">
            {rows.map((r) => {
              const aiDraft = needsReview(r);
              return (
                <li key={r.id}>
                  <button
                    type="button"
                    onClick={() => pick(r.id)}
                    aria-current={selected === r.id ? "true" : undefined}
                    className={cn(
                      "w-full text-left rounded-xl border p-3 bg-bg-card hover:bg-bg-elevated/40 transition-colors",
                      selected === r.id ? "border-accent/60" : "border-line"
                    )}
                    style={
                      aiDraft
                        ? { boxShadow: "inset 3px 0 0 var(--warning)" }
                        : undefined
                    }
                  >
                    <div className="font-medium text-sm break-words">{r.title}</div>
                    <div className="mt-1.5">
                      {r.status === "published" ? (
                        <Chip tone="accent">published</Chip>
                      ) : aiDraft ? (
                        <Chip tone="warning">
                          <Sparkles aria-hidden className="h-3 w-3" /> AI-drafted, needs review
                        </Chip>
                      ) : (
                        <Chip>draft</Chip>
                      )}
                    </div>
                    <div className="text-xs text-muted mt-1.5 flex flex-wrap gap-x-2">
                      <span className="font-mono">{r.slug}</span>
                      <span>v{r.version}</span>
                      <span>{r.source}</span>
                      {(r.match?.alertname ?? []).length > 0 && (
                        <span className="font-mono">{r.match.alertname!.join(", ")}</span>
                      )}
                      <span>updated {formatRelative(r.updated_at)}</span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        </LoadState>

        <section
          ref={editorRef}
          className={cn(
            "rounded-2xl border border-line bg-bg-card p-4 lg:p-5 scroll-mt-4",
            selected === null && "hidden md:block"
          )}
        >
          {selected === null ? (
            <div className="text-sm text-muted">
              Pick a runbook to view or edit it, or create a new one.
            </div>
          ) : (
            <div key={selected} className="motion-enter">
              <div className="text-[15px] font-medium mb-3 break-words">
                {selected === "new" ? "New runbook" : current?.title ?? "Runbook"}
                {current?.published_by && (
                  <span className="text-xs text-muted font-normal">
                    {" "}
                    · published by {current.published_by} {formatRelative(current.published_at)}
                  </span>
                )}
              </div>
              {selected !== "new" && !current ? (
                <div className="text-sm text-muted">This runbook isn&apos;t in the current list.</div>
              ) : (
                <RunbookEditor
                  key={`${selected}-${current?.updated_at ?? ""}`}
                  runbook={current}
                  isAdmin={isAdmin}
                  onDone={(rb) => setSelected(rb ? rb.id : null)}
                />
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
