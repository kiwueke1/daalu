"use client";

import { useEffect, useId, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bookmark, Download, Pin, Play, Plus, Save, Sparkles, Trash2, X, Wand2 } from "lucide-react";
import Link from "next/link";
import {
  api,
  type ReportsEntityDescriptor,
  type ReportsQueryRequest,
  type ReportsQueryResponse,
  type SavedReport,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";
import { cn } from "@/lib/utils";

// Example query definitions surfaced as "starter" chips. These are
// client-side only; clicking one populates the builder, doesn't save
// anything to the server. The user can hit "Save" once they've tweaked.
const EXAMPLES: Array<{ name: string; definition: ReportsQueryRequest }> = [
  {
    name: "Critical events (24h)",
    definition: {
      entity: "events",
      filters: { severity: "critical" },
      since_hours: 24,
      limit: 100,
      display: "table",
    },
  },
  {
    name: "Open alerts",
    definition: {
      entity: "alerts",
      filters: { status: "open" },
      since_hours: null,
      limit: 100,
      display: "table",
    },
  },
  {
    name: "Pending change proposals",
    definition: {
      entity: "change_proposals",
      filters: { status: "pending" },
      since_hours: null,
      limit: 100,
      display: "table",
    },
  },
  {
    name: "Active incidents",
    definition: {
      entity: "incidents",
      filters: { status: "open" },
      since_hours: 168,
      limit: 100,
      display: "table",
    },
  },
  {
    name: "Count: failing integrations",
    definition: {
      entity: "integrations",
      filters: { status: "error" },
      since_hours: null,
      limit: 500,
      display: "count",
    },
  },
];

const ENTITY_HAS_TIME = (timeField: string | null) => Boolean(timeField);

interface FilterRow {
  key: string;
  value: string;
}

type Mode = "builder" | "ask";

export function ReportsQueryTab() {
  const { data: schema } = useQuery({
    queryKey: ["reports-schema"],
    queryFn: () => api.reports.schema(),
    staleTime: 5 * 60 * 1000,
  });

  const entities = schema?.entities ?? [];
  const [mode, setMode] = useState<Mode>("builder");

  return (
    <div className="space-y-4">
      <div className="flex gap-1 text-xs" role="group" aria-label="Query mode">
        <ModeButton active={mode === "builder"} onClick={() => setMode("builder")}>
          Builder
        </ModeButton>
        <ModeButton active={mode === "ask"} onClick={() => setMode("ask")}>
          <Sparkles className="h-3 w-3" aria-hidden /> Ask
        </ModeButton>
      </div>

      {mode === "builder" ? (
        <BuilderMode entities={entities} />
      ) : (
        <AskMode entities={entities} />
      )}
    </div>
  );
}

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`motion-press h-8 px-3 rounded-md inline-flex items-center gap-1 transition-colors ${
        active
          ? "bg-bg-elevated text-[color:var(--text)] border border-line"
          : "text-muted hover:text-[color:var(--text)]"
      }`}
    >
      {children}
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Builder mode
// ─────────────────────────────────────────────────────────────────────

function BuilderMode({ entities }: { entities: ReportsEntityDescriptor[] }) {
  const qc = useQueryClient();
  const toast = useToast();
  const fieldId = useId();
  // Naming a new saved report happens inline, not in a browser prompt.
  const [naming, setNaming] = useState(false);
  const [newName, setNewName] = useState("");

  const [entityName, setEntityName] = useState<string>("events");
  const entity = useMemo(
    () => entities.find((e) => e.name === entityName) ?? entities[0],
    [entities, entityName]
  );

  useEffect(() => {
    if (!entities.length) return;
    if (!entities.some((e) => e.name === entityName)) {
      setEntityName(entities[0].name);
    }
  }, [entities, entityName]);

  const [filters, setFilters] = useState<FilterRow[]>([]);
  const [sinceHours, setSinceHours] = useState<number>(24);
  const [display, setDisplay] = useState<"table" | "count">("table");
  const [runRequest, setRunRequest] = useState<ReportsQueryRequest | null>(null);
  // Tracks whether the user is editing an existing saved report (so "Save"
  // updates instead of creating a new row).
  const [loadedSavedId, setLoadedSavedId] = useState<string | null>(null);
  // Suppresses the "reset on entity change" effect when we're populating
  // state from a saved report — otherwise the filters would be wiped.
  const [populatingFromSaved, setPopulatingFromSaved] = useState(false);

  useEffect(() => {
    if (populatingFromSaved) {
      setPopulatingFromSaved(false);
      return;
    }
    setFilters([]);
    setRunRequest(null);
    setLoadedSavedId(null);
  }, [entityName, populatingFromSaved]);

  const { data: savedList = [] } = useQuery({
    queryKey: ["reports-saved"],
    queryFn: () => api.reports.saved.list(),
    staleTime: 30 * 1000,
  });

  const loadDefinition = (def: ReportsQueryRequest, savedId: string | null) => {
    setPopulatingFromSaved(true);
    setEntityName(def.entity);
    setFilters(
      Object.entries(def.filters ?? {}).map(([k, v]) => ({ key: k, value: String(v ?? "") }))
    );
    setSinceHours(def.since_hours ?? 24);
    setDisplay(def.display ?? "table");
    setLoadedSavedId(savedId);
    // Auto-run so clicking a chip surfaces results immediately.
    setRunRequest({ ...def, limit: def.limit ?? 200 });
  };

  const currentDefinition = (): ReportsQueryRequest | null => {
    if (!entity) return null;
    const filterObj: Record<string, string> = {};
    for (const row of filters) {
      if (row.key && row.value) filterObj[row.key] = row.value;
    }
    return {
      entity: entity.name,
      filters: filterObj,
      since_hours: ENTITY_HAS_TIME(entity.time_field) ? sinceHours : null,
      limit: 200,
      display,
    };
  };

  const saveReport = useMutation({
    mutationFn: async () => {
      const def = currentDefinition();
      if (!def) throw new Error("nothing to save");
      if (loadedSavedId) {
        return api.reports.saved.update(loadedSavedId, { definition: def });
      }
      return api.reports.saved.create({ name: newName.trim(), definition: def });
    },
    onSuccess: (row) => {
      qc.invalidateQueries({ queryKey: ["reports-saved"] });
      setLoadedSavedId(row.id);
      setNaming(false);
      setNewName("");
      toast.success(
        loadedSavedId
          ? `Saved report "${row.name}" updated.`
          : `Saved as "${row.name}". Add it to a dashboard or a schedule.`
      );
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const deleteSaved = useMutation({
    mutationFn: (id: string) => api.reports.saved.remove(id),
    onSuccess: (_v, id) => {
      qc.invalidateQueries({ queryKey: ["reports-saved"] });
      if (loadedSavedId === id) setLoadedSavedId(null);
      toast.success("Saved report deleted.");
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const { data: result, isFetching, error } = useQuery<ReportsQueryResponse>({
    queryKey: ["reports-query", runRequest],
    queryFn: () => api.reports.runQuery(runRequest!),
    enabled: !!runRequest,
  });

  const run = () => {
    const def = currentDefinition();
    if (def) setRunRequest(def);
  };

  const addFilter = () => {
    if (!entity) return;
    const used = new Set(filters.map((f) => f.key));
    const next = entity.filter_fields.find((f) => !used.has(f));
    if (!next) return;
    setFilters([...filters, { key: next, value: "" }]);
  };

  const updateFilter = (i: number, patch: Partial<FilterRow>) => {
    setFilters(filters.map((f, idx) => (idx === i ? { ...f, ...patch } : f)));
  };

  const removeFilter = (i: number) => {
    setFilters(filters.filter((_, idx) => idx !== i));
  };

  return (
    <div className="space-y-4">
      {(savedList.length > 0 || EXAMPLES.length > 0) && (
        <SavedChipRail
          saved={savedList}
          loadedSavedId={loadedSavedId}
          onLoad={(def, id) => loadDefinition(def, id)}
          onDelete={(id) => deleteSaved.mutate(id)}
        />
      )}

      <div className="rounded-xl border border-line bg-bg-card p-4 space-y-4">
        <div>
          <label htmlFor={`${fieldId}-from`} className="block text-xs uppercase tracking-wider text-muted mb-2">
            From
          </label>
          {entities.length === 0 ? (
            <div className="text-sm text-muted">Loading entities…</div>
          ) : (
            <select
              id={`${fieldId}-from`}
              value={entityName}
              onChange={(e) => setEntityName(e.target.value)}
              className="bg-bg-elevated border border-line rounded-lg px-2 py-1.5 text-sm"
            >
              {entities.map((e) => (
                <option key={e.name} value={e.name}>
                  {e.name}
                </option>
              ))}
            </select>
          )}
        </div>

        {entity && (
          <div>
            <div className="text-xs uppercase tracking-wider text-muted mb-2">Where</div>
            <div className="space-y-2">
              {filters.map((row, i) => (
                <div key={i} className="flex gap-2 items-center">
                  <select
                    aria-label={`Filter ${i + 1} field`}
                    value={row.key}
                    onChange={(e) => updateFilter(i, { key: e.target.value, value: "" })}
                    className="bg-bg-elevated border border-line rounded-lg px-2 py-1.5 text-xs"
                  >
                    {entity.filter_fields.map((f) => (
                      <option key={f} value={f}>
                        {f}
                      </option>
                    ))}
                  </select>
                  <span className="text-xs text-muted">=</span>
                  <input
                    type="text"
                    aria-label={`Value for ${row.key}`}
                    value={row.value}
                    onChange={(e) => updateFilter(i, { value: e.target.value })}
                    placeholder="value"
                    className="flex-1 bg-bg-elevated border border-line rounded-lg px-2 py-1.5 text-xs"
                  />
                  <Button
                    variant="icon"
                    size="sm"
                    onClick={() => removeFilter(i)}
                    aria-label={`Remove filter on ${row.key}`}
                    icon={<X className="h-3.5 w-3.5" aria-hidden />}
                  />
                </div>
              ))}
              {filters.length < entity.filter_fields.length && (
                <button
                  type="button"
                  onClick={addFilter}
                  className="text-xs inline-flex items-center gap-1 text-muted hover:text-[color:var(--text)]"
                >
                  <Plus className="h-3.5 w-3.5" aria-hidden /> Add filter
                </button>
              )}
              {ENTITY_HAS_TIME(entity.time_field) && (
                <label className="text-xs flex items-center gap-2 pt-2">
                  <span className="text-muted">
                    Last <span className="font-mono">{entity.time_field}</span> within
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={720}
                    value={sinceHours}
                    onChange={(e) =>
                      setSinceHours(Math.max(1, Math.min(720, Number(e.target.value) || 24)))
                    }
                    className="w-20 bg-bg-elevated border border-line rounded-lg px-2 py-1 text-xs"
                  />
                  <span className="text-muted">hours</span>
                </label>
              )}
            </div>
          </div>
        )}

        <div>
          <div id={`${fieldId}-display-label`} className="text-xs uppercase tracking-wider text-muted mb-2">Display</div>
          <div className="flex gap-3 text-sm" role="radiogroup" aria-labelledby={`${fieldId}-display-label`}>
            <label className="flex items-center gap-1.5">
              <input type="radio" name={`${fieldId}-display`} checked={display === "table"} onChange={() => setDisplay("table")} />
              <span>table</span>
            </label>
            <label className="flex items-center gap-1.5">
              <input type="radio" name={`${fieldId}-display`} checked={display === "count"} onChange={() => setDisplay("count")} />
              <span>count</span>
            </label>
          </div>
        </div>

        <div className="pt-1 flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            onClick={run}
            pending={isFetching}
            disabled={!entity}
            icon={<Play className="h-3.5 w-3.5" aria-hidden />}
          >
            {isFetching ? "Running…" : "Run query"}
          </Button>
          <Button
            size="sm"
            onClick={() => (loadedSavedId ? saveReport.mutate() : setNaming(true))}
            pending={saveReport.isPending}
            disabled={!entity}
            icon={<Save className="h-3.5 w-3.5" aria-hidden />}
          >
            {loadedSavedId ? "Update saved report" : "Save as report…"}
          </Button>
          {result && (
            <>
              <ExportButton
                getDefinition={currentDefinition}
                format="csv"
              />
              <ExportButton
                getDefinition={currentDefinition}
                format="json"
              />
            </>
          )}
          <div className="flex-1" />
          <Link
            href="/reports/schedules"
            className="text-xs text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1"
          >
            Schedules →
          </Link>
        </div>
      </div>

      {naming && !loadedSavedId && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (newName.trim()) saveReport.mutate();
          }}
          className="motion-enter rounded-xl border border-line bg-bg-card p-4 flex flex-wrap items-end gap-2"
        >
          <div className="flex-1 min-w-[200px]">
            <label htmlFor={`${fieldId}-name`} className="block text-xs font-medium mb-1">
              Report name
            </label>
            <input
              id={`${fieldId}-name`}
              autoFocus
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={`e.g. ${entity?.name ?? "events"} this week`}
              className="w-full bg-bg-elevated border border-line rounded-lg px-3 py-1.5 text-sm"
            />
          </div>
          <Button
            type="submit"
            size="sm"
            variant="primary"
            pending={saveReport.isPending}
            disabled={!newName.trim()}
            disabledReason="Name the report"
          >
            Save report
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setNaming(false)}>
            Cancel
          </Button>
        </form>
      )}

      {runRequest && (
        <ResultPanel key={JSON.stringify(runRequest)} result={result} error={error} isFetching={isFetching} />
      )}
    </div>
  );
}

function ExportButton({
  getDefinition,
  format,
}: {
  getDefinition: () => ReportsQueryRequest | null;
  format: "csv" | "json";
}) {
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  return (
    <Button
      size="sm"
      aria-label={`Export as ${format.toUpperCase()}`}
      pending={busy}
      icon={<Download className="h-3.5 w-3.5" aria-hidden />}
      onClick={async () => {
        const def = getDefinition();
        if (!def) return;
        setBusy(true);
        try {
          const blob = await api.reports.export(def, format);
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = `${def.entity}-${Date.now()}.${format}`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
        } catch (e) {
          toast.error(errorText(e));
        } finally {
          setBusy(false);
        }
      }}
    >
      {format.toUpperCase()}
    </Button>
  );
}

function SavedChipRail({
  saved,
  loadedSavedId,
  onLoad,
  onDelete,
}: {
  saved: SavedReport[];
  loadedSavedId: string | null;
  onLoad: (def: ReportsQueryRequest, savedId: string | null) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="space-y-2">
      {saved.length > 0 && (
        <div>
          <div className="text-[11px] uppercase tracking-wider text-muted mb-1.5 flex items-center gap-1.5">
            <Bookmark className="h-3 w-3" aria-hidden /> Saved
          </div>
          <div className="flex flex-wrap gap-2">
            {saved.map((s) => (
              <div
                key={s.id}
                className={cn(
                  "inline-flex items-center gap-1 rounded-lg border text-xs",
                  loadedSavedId === s.id ? "border-accent/60 bg-accent/10" : "border-line hover:border-accent/40"
                )}
              >
                <button
                  type="button"
                  onClick={() => onLoad(s.definition, s.id)}
                  aria-pressed={loadedSavedId === s.id}
                  className="h-7 px-2.5 inline-flex items-center gap-1"
                >
                  {s.pinned && <Pin className="h-3 w-3 text-accent" aria-label="Pinned" />}
                  {s.name}
                </button>
                <ConfirmButton
                  label={<span className="sr-only">Delete {s.name}</span>}
                  confirmLabel="Delete"
                  consequence={`Delete "${s.name}"? Dashboards and schedules using it lose it.`}
                  icon={<Trash2 className="h-3 w-3" aria-hidden />}
                  onConfirm={() => onDelete(s.id)}
                  tone="danger"
                  className="h-7 border-0 px-1.5 text-muted hover:text-[color:var(--critical)]"
                />
              </div>
            ))}
          </div>
        </div>
      )}
      <div>
        <div className="text-[11px] uppercase tracking-wider text-muted mb-1.5">Examples</div>
        <div className="flex flex-wrap gap-2">
          {EXAMPLES.map((ex) => (
            <button
              key={ex.name}
              type="button"
              onClick={() => onLoad(ex.definition, null)}
              className="text-xs px-2.5 py-1 rounded-lg border border-line text-muted hover:text-[color:var(--text)] hover:border-accent/40 transition-colors"
            >
              {ex.name}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Ask mode (natural language)
// ─────────────────────────────────────────────────────────────────────

function AskMode({ entities }: { entities: ReportsEntityDescriptor[] }) {
  const [question, setQuestion] = useState<string>("");
  const [request, setRequest] = useState<ReportsQueryRequest | null>(null);
  const [rationale, setRationale] = useState<string>("");
  const askId = useId();

  const translate = useMutation({
    mutationFn: () => api.reports.translate({ question }),
    onSuccess: (resp) => {
      setRequest(resp.query);
      setRationale(resp.rationale);
    },
  });

  const { data: result, isFetching, error: runError } = useQuery<ReportsQueryResponse>({
    queryKey: ["reports-query", request],
    queryFn: () => api.reports.runQuery(request!),
    enabled: !!request,
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!question.trim()) return;
    setRequest(null);
    translate.mutate();
  };

  const entitySuggestions = entities.length
    ? entities.map((e) => e.name).join(", ")
    : "events, alerts, incidents…";

  return (
    <div className="space-y-4">
      <form onSubmit={submit} className="rounded-xl border border-line bg-bg-card p-4 space-y-3">
        <label htmlFor={askId} className="text-xs uppercase tracking-wider text-muted block">
          Ask in plain English
        </label>
        <textarea
          id={askId}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={`e.g. critical events from prometheus in the last hour\n     count open alerts in infra\n     change proposals waiting for approval`}
          rows={3}
          className="w-full bg-bg-elevated border border-line rounded-lg px-3 py-2 text-sm font-mono"
        />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-xs text-muted">
            Entities the assistant knows about: {entitySuggestions}
          </div>
          <Button
            type="submit"
            size="sm"
            variant="primary"
            pending={translate.isPending}
            disabled={!question.trim()}
            disabledReason="Type a question first"
            icon={<Wand2 className="h-3.5 w-3.5" aria-hidden />}
          >
            {translate.isPending ? "Thinking…" : "Translate & run"}
          </Button>
        </div>
        {translate.error && (
          <div role="alert" className="text-sm text-[color:var(--critical)]">
            {errorText(translate.error)}
          </div>
        )}
      </form>

      {request && (
        <div key={JSON.stringify(request)} className="motion-enter rounded-xl border border-line bg-bg-card p-3 space-y-2">
          <div className="text-[11px] uppercase tracking-wider text-muted">
            Interpreted as
          </div>
          {rationale && <div className="text-sm text-muted italic">{rationale}</div>}
          <pre className="text-xs bg-bg-elevated rounded-md p-2 overflow-x-auto">
{JSON.stringify(request, null, 2)}
          </pre>
        </div>
      )}

      {request && (
        <ResultPanel key={JSON.stringify(request)} result={result} error={runError} isFetching={isFetching} />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Shared result panel + tiny helper
// ─────────────────────────────────────────────────────────────────────

function ResultPanel({
  result,
  error,
  isFetching,
}: {
  result?: ReportsQueryResponse;
  error: unknown;
  isFetching: boolean;
}) {
  if (error) {
    return (
      <div role="alert" className="motion-enter rounded-xl border border-line bg-bg-card p-4 text-sm text-[color:var(--critical)]">
        Couldn&apos;t run this query. {errorText(error)}
      </div>
    );
  }
  if (!result) {
    return (
      <div role="status" className="rounded-xl border border-line bg-bg-card p-4 text-sm text-muted">
        {isFetching ? "Running…" : "No result yet."}
      </div>
    );
  }
  if (result.display === "count") {
    return (
      <div className="motion-enter rounded-xl border border-line bg-bg-card p-6 text-center">
        <div className="text-3xl font-semibold">{result.total}</div>
        <div className="text-xs text-muted mt-1">{result.entity} matching</div>
      </div>
    );
  }
  if (result.rows.length === 0) {
    return (
      <div role="status" className="motion-enter rounded-xl border border-line bg-bg-card p-4 text-sm text-muted">
        Nothing matched. Try a longer time window, remove a filter, or check a
        filter value&apos;s spelling (values match exactly).
      </div>
    );
  }
  return (
    <div className="motion-enter rounded-xl border border-line bg-bg-card overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-bg-elevated/60 text-muted text-xs uppercase tracking-wider">
            <tr>
              {result.columns.map((c) => (
                <th key={c.key} className="text-left px-3 py-2 whitespace-nowrap">
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row) => (
              <tr key={row.id} className="border-t border-line">
                {result.columns.map((c) => (
                  <td key={c.key} className="px-3 py-2 align-top">
                    {formatCell(row[c.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="px-3 py-2 text-xs text-muted bg-bg-elevated/30 border-t border-line">
        {result.total} row{result.total === 1 ? "" : "s"}
      </div>
    </div>
  );
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") {
    if (/^\d{4}-\d{2}-\d{2}T/.test(v)) {
      try {
        return new Date(v).toLocaleString();
      } catch {
        return v;
      }
    }
    return v;
  }
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
