"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BookMarked, Plus, Sparkles } from "lucide-react";
import { api, type Runbook, type RunbookStatus } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, formatRelative } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";
import { Chip, PageHeader, Segmented, btnPrimary } from "@/components/aiops/ui";
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

  return (
    <div className="space-y-6 max-w-[1200px]">
      <PageHeader
        icon={BookMarked}
        title="Runbooks"
        actions={
          <button type="button" className={btnPrimary} onClick={() => setSelected("new")}>
            <Plus className="h-3.5 w-3.5" /> New runbook
          </button>
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
              const aiDraft = r.status === "draft" && r.source === "generated";
              return (
                <li key={r.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(r.id)}
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
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-sm flex-1 min-w-0 truncate">{r.title}</span>
                      {r.status === "published" ? (
                        <Chip tone="accent">published</Chip>
                      ) : aiDraft ? (
                        <Chip tone="warning">
                          <Sparkles className="h-3 w-3" /> AI-drafted, needs review
                        </Chip>
                      ) : (
                        <Chip>draft</Chip>
                      )}
                    </div>
                    <div className="text-[11px] text-muted mt-1 flex flex-wrap gap-x-2">
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

        <section className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5">
          {selected === null ? (
            <div className="text-sm text-muted">
              Pick a runbook to view or edit it, or create a new one.
            </div>
          ) : (
            <>
              <div className="text-[15px] font-medium mb-3">
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
            </>
          )}
        </section>
      </div>
    </div>
  );
}
