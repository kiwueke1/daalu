"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileCode2, ListChecks, Loader2, Save, Send, Trash2 } from "lucide-react";
import { api, type Runbook, type RunbookIn } from "@/lib/api";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { Segmented, ErrorNote, btnPrimary, btnSecondary, inputCls } from "./ui";
import { WRITE_TOOLS } from "./starter-policies";

const NEW_BODY = `## Symptoms

## Diagnosis

## Fix
`;

/** A runbook file: YAML frontmatter + markdown body (the import format). */
export function toRunbookFile(f: {
  slug: string;
  title: string;
  alertnames: string[];
  labels: Record<string, string>;
  allowed: string[];
  body: string;
}): string {
  const q = (s: string) => (/^[\w.-]+$/.test(s) ? s : JSON.stringify(s));
  const lines = ["---"];
  if (f.slug) lines.push(`id: ${q(f.slug)}`);
  lines.push(`title: ${JSON.stringify(f.title)}`);
  lines.push("match:");
  lines.push(`  alertname: [${f.alertnames.map(q).join(", ")}]`);
  const lbl = Object.entries(f.labels);
  lines.push(`  labels: {${lbl.map(([k, v]) => `${q(k)}: ${q(v)}`).join(", ")}}`);
  lines.push(`allowed_actions: [${f.allowed.map(q).join(", ")}]`);
  lines.push("---");
  return `${lines.join("\n")}\n${f.body}`;
}

function parseLabels(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split(/[\n,]/)) {
    const i = line.indexOf("=");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

const splitList = (s: string) =>
  s
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

/**
 * View / edit / create a runbook, either as fields (title, match, allowed
 * actions, markdown body) or as the whole file with frontmatter.
 */
export function RunbookEditor({
  runbook,
  isAdmin,
  onDone,
}: {
  runbook: Runbook | null; // null = create
  isAdmin: boolean;
  onDone: (rb: Runbook | null) => void;
}) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"fields" | "file">("fields");
  const [title, setTitle] = useState(runbook?.title ?? "");
  const [slug, setSlug] = useState(runbook?.slug ?? "");
  const [alertnames, setAlertnames] = useState((runbook?.match?.alertname ?? []).join(", "));
  const [labels, setLabels] = useState(
    Object.entries(runbook?.match?.labels ?? {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n")
  );
  const [allowed, setAllowed] = useState<string[]>(runbook?.allowed_actions ?? []);
  const [body, setBody] = useState(runbook?.body ?? NEW_BODY);
  const [file, setFile] = useState("");

  const fields = () => ({
    slug,
    title,
    alertnames: splitList(alertnames),
    labels: parseLabels(labels),
    allowed,
    body,
  });

  const switchMode = (m: "fields" | "file") => {
    if (m === "file") setFile(toRunbookFile(fields()));
    setMode(m);
  };

  const invalidate = () => qc.invalidateQueries({ queryKey: ["aiops-runbooks"] });

  const save = useMutation({
    mutationFn: () => {
      const f = fields();
      const payload: RunbookIn =
        mode === "file"
          ? { markdown: file }
          : {
              title: f.title,
              body: f.body,
              match: { alertname: f.alertnames, labels: f.labels },
              allowed_actions: f.allowed,
              ...(f.slug ? { slug: f.slug } : {}),
            };
      return runbook ? api.aiops.runbooks.update(runbook.id, payload) : api.aiops.runbooks.create(payload);
    },
    onSuccess: (rb) => {
      invalidate();
      onDone(rb);
    },
  });
  const publish = useMutation({
    mutationFn: () => api.aiops.runbooks.publish(runbook!.id),
    onSuccess: (rb) => {
      invalidate();
      onDone(rb);
    },
  });
  const remove = useMutation({
    mutationFn: () => api.aiops.runbooks.remove(runbook!.id),
    onSuccess: () => {
      invalidate();
      onDone(null);
    },
  });

  const busy = save.isPending || publish.isPending || remove.isPending;

  return (
    <div className="space-y-3">
      <Segmented
        ariaLabel="Editor mode"
        value={mode}
        onChange={switchMode}
        options={[
          { value: "fields", label: "Fields" },
          { value: "file", label: "Edit as file" },
        ]}
      />
      {mode === "fields" ? (
        <div className="space-y-3 text-xs">
          <div className="grid sm:grid-cols-2 gap-2">
            <label className="space-y-1">
              <span className="text-muted">Title</span>
              <input value={title} onChange={(e) => setTitle(e.target.value)} className={`${inputCls} w-full`} placeholder="DummyApp is down" />
            </label>
            <label className="space-y-1">
              <span className="text-muted">Id (slug)</span>
              <input value={slug} onChange={(e) => setSlug(e.target.value)} className={`${inputCls} w-full font-mono`} placeholder="dummy-app-down" />
            </label>
            <label className="space-y-1">
              <span className="text-muted">Match alert names (comma-separated)</span>
              <input value={alertnames} onChange={(e) => setAlertnames(e.target.value)} className={`${inputCls} w-full font-mono`} placeholder="DummyAppDown" />
            </label>
            <label className="space-y-1">
              <span className="text-muted">Match labels (key=value, one per line)</span>
              <textarea
                value={labels}
                onChange={(e) => setLabels(e.target.value)}
                rows={2}
                className="w-full rounded-lg bg-bg-elevated/60 border border-line px-2.5 py-1.5 font-mono focus:outline-none focus:border-accent/60"
                placeholder="namespace=demo"
              />
            </label>
          </div>
          <fieldset className="space-y-1">
            <legend className="text-muted">Allowed actions</legend>
            <div className="flex flex-wrap gap-3">
              {Array.from(new Set([...WRITE_TOOLS, ...allowed])).map((t) => (
                <label key={t} className="inline-flex items-center gap-1.5 font-mono">
                  <input
                    type="checkbox"
                    checked={allowed.includes(t)}
                    onChange={(e) =>
                      setAllowed((a) => (e.target.checked ? [...a, t] : a.filter((x) => x !== t)))
                    }
                  />
                  {t}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="block space-y-1">
            <span className="text-muted">Body (markdown)</span>
            <textarea
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={16}
              spellCheck={false}
              className="w-full rounded-lg bg-bg-elevated/60 border border-line px-3 py-2 font-mono text-[12.5px] leading-relaxed focus:outline-none focus:border-accent/60 resize-y"
            />
          </label>
        </div>
      ) : (
        <textarea
          value={file}
          onChange={(e) => setFile(e.target.value)}
          rows={24}
          spellCheck={false}
          aria-label="Runbook file"
          className="w-full rounded-lg bg-bg-elevated/60 border border-line px-3 py-2 font-mono text-[12.5px] leading-relaxed focus:outline-none focus:border-accent/60 resize-y"
        />
      )}

      <ErrorNote error={save.error ?? publish.error ?? remove.error} />

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={btnPrimary}
          disabled={busy || (mode === "fields" && !title.trim())}
          onClick={() => save.mutate()}
        >
          {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
          {runbook ? "Save changes" : "Create draft"}
        </button>
        {runbook && runbook.status !== "published" && (
          <button
            type="button"
            className={btnSecondary}
            disabled={busy || !isAdmin}
            title={isAdmin ? undefined : "Only a tenant admin can publish runbooks"}
            onClick={() => publish.mutate()}
          >
            {publish.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
            Publish
          </button>
        )}
        {runbook && (
          <ConfirmButton
            label="Delete"
            confirmLabel="Delete runbook"
            consequence="The AI stops using it for matching alerts."
            onConfirm={() => remove.mutate()}
            pending={remove.isPending}
            icon={<Trash2 className="h-3.5 w-3.5" />}
            tone="danger"
          />
        )}
        <span className="text-[11px] text-muted inline-flex items-center gap-1 ml-auto">
          {mode === "file" ? <FileCode2 className="h-3 w-3" /> : <ListChecks className="h-3 w-3" />}
          {mode === "file" ? "Same format as `daalu runbooks import`." : "Published runbooks are injected into triage for matching alerts."}
        </span>
      </div>
    </div>
  );
}
