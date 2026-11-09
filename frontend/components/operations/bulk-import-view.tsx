"use client";

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  FileSpreadsheet,
  FileText,
  Loader2,
  PlusCircle,
  Upload,
} from "lucide-react";
import {
  api,
  type SotBulkImportResult,
  type SotBulkRow,
  type SotCatalogPlan,
} from "@/lib/api";
import { errorText } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { useToast } from "@/components/ui/toast";

const YAML_EXAMPLE = `devices:
  - name: web01
    primary_ip: 10.0.0.5/24
    transport: linux_ssh
    site: dc1
    device_type: generic-server
    role: server
    platform: linux

  - name: edge-router-3
    primary_ip: 10.10.0.1/24
    transport: junos
    site: dc1
    device_type: mx204
    role: edge-router
`;

const EXCEL_COLUMNS =
  "name | primary_ip | transport | site | device_type | role | platform (optional)";

/** `env` is the NV-CM environment slug to import into — each environment has
 *  its own Nautobot, so the upload has to name one. Omitted (the standalone
 *  Operations view) means the tenant's default source of truth. */
export function BulkImportView({ env }: { env?: string } = {}) {
  const qc = useQueryClient();
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<SotBulkImportResult | null>(null);
  const [applied, setApplied] = useState<SotBulkImportResult | null>(null);
  // On by default: a newly provisioned environment's Nautobot has no
  // locations and only NVIDIA's reference device types, so the first import
  // into one otherwise fails every row on names that don't exist yet. The
  // preview still lists everything that would be created before it is.
  const [createMissing, setCreateMissing] = useState(true);

  const dryRun = useMutation({
    mutationFn: ({ f, create }: { f: File; create: boolean }) =>
      api.sot.devices.bulkImport(f, true, env, create),
    onSuccess: (r) => {
      setPreview(r);
      setApplied(null);
    },
    onError: () => {
      setPreview(null);
    },
  });

  const apply = useMutation({
    mutationFn: (f: File) =>
      api.sot.devices.bulkImport(f, false, env, createMissing),
    onSuccess: (r) => {
      setApplied(r);
      qc.invalidateQueries({ queryKey: ["sot-devices"] });
      const n = r.summary.created;
      toast.success(`${n} device${n === 1 ? "" : "s"} written to Nautobot`);
    },
    onError: (err) => toast.error(errorText(err)),
  });

  const reset = () => {
    setFile(null);
    setPreview(null);
    setApplied(null);
    if (fileRef.current) fileRef.current.value = "";
  };

  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setFile(f);
    setPreview(null);
    setApplied(null);
    dryRun.mutate({ f, create: createMissing });
  };

  const onToggleCreateMissing = (next: boolean) => {
    setCreateMissing(next);
    // Re-validate: the toggle changes which rows are errors and which are
    // merely waiting on a catalog entry, so a stale preview would lie.
    if (file) dryRun.mutate({ f: file, create: next });
  };

  const display = applied ?? preview;
  const hasUploadableRows =
    display !== null &&
    display.summary.valid > 0 &&
    display.summary.errors < display.summary.total;

  return (
    <div className="space-y-5">
      <p className="text-muted text-sm">
        Upload your inventory as a YAML or Excel file. We&apos;ll validate it
        against your Nautobot catalog (sites, device types, roles, platforms)
        and show you a row-by-row preview before anything is created.
      </p>

      {/* Upload box */}
      <div className="surface p-5 space-y-4">
        <div className="flex items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            // Extensions AND MIME types: Linux file dialogs (GTK /
            // xdg-desktop-portal) filter by MIME, and .yaml/.yml often have
            // no shared-mime-info mapping — extension-only `accept` then
            // hides YAML files, making the picker look xlsx-only.
            accept=".yaml,.yml,.xlsx,application/yaml,application/x-yaml,text/yaml,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={onPick}
            className="text-xs file:mr-3 file:rounded-lg file:border file:border-line file:bg-bg-elevated/60 file:px-3 file:py-2 file:text-[color:var(--text)] file:cursor-pointer"
          />
          {file && (
            <span className="text-xs text-muted truncate">
              {file.name} · {(file.size / 1024).toFixed(1)} KB
            </span>
          )}
          {(dryRun.isPending || apply.isPending) && (
            <Loader2 className="h-4 w-4 animate-spin text-muted" aria-label="Working" />
          )}
          {file && (
            <Button variant="ghost" size="sm" onClick={reset} className="ml-auto">
              Choose another file
            </Button>
          )}
        </div>

        <label className="flex items-start gap-2 text-xs text-muted cursor-pointer">
          <input
            type="checkbox"
            checked={createMissing}
            onChange={(e) => onToggleCreateMissing(e.target.checked)}
            className="mt-0.5 accent-[color:var(--accent)]"
          />
          <span>
            <span className="text-[color:var(--text)]">
              Create catalog entries this file needs
            </span>{" "}
            — sites, device types, roles, platforms and parent prefixes that
            aren&apos;t in this environment&apos;s Nautobot yet. You&apos;ll see
            the full list below before anything is created.
          </span>
        </label>

        {dryRun.isError && (
          <div role="alert" className="text-xs text-[color:var(--critical)]">
            {errorText(dryRun.error)}
          </div>
        )}
      </div>

      {/* What the run will add to (or added to) the catalog. Shown before the
          row table because it's the thing to check for typos: a misspelt role
          here becomes a real role in Nautobot. */}
      {display?.planned_catalog && (
        <CatalogPlanPanel
          plan={display.planned_catalog}
          created={display.created_catalog ?? []}
          applied={applied !== null}
        />
      )}

      {/* Run-level problems. These are the difference between "your file names
          things that don't exist" and "we couldn't read the catalog at all",
          which otherwise look identical — both produce unknown-X on every row. */}
      {display?.errors && display.errors.length > 0 && (
        <div className="surface p-4 text-xs space-y-1 border-l-2 border-[color:var(--critical)]">
          <div className="font-semibold">
            Nautobot didn&apos;t answer everything we asked for
          </div>
          <p className="text-muted">
            Rows below may report unknown site / type / role because the catalog
            couldn&apos;t be read — not because the file is wrong.
          </p>
          {display.errors.map((e, i) => (
            <div key={i} className="font-mono text-xs text-muted">
              {e}
            </div>
          ))}
        </div>
      )}

      {/* Preview table */}
      {display && (
        <div className="surface p-0 overflow-hidden motion-enter">
          <div className="px-5 py-3 border-b border-line flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="font-semibold">
                {applied ? "Applied" : "Preview"}
              </span>
              <span className="text-muted">
                {display.summary.total} rows ·{" "}
                <span className="text-[color:var(--text)]">
                  {display.summary.valid}
                </span>{" "}
                valid
                {display.summary.errors > 0 && (
                  <>
                    {" · "}
                    <span className="text-[color:var(--critical)]">
                      {display.summary.errors}
                    </span>{" "}
                    error{display.summary.errors === 1 ? "" : "s"}
                  </>
                )}
                {applied && display.summary.created > 0 && (
                  <>
                    {" · "}
                    <span className="text-accent-emerald">
                      {display.summary.created}
                    </span>{" "}
                    created
                  </>
                )}
                {applied && (display.summary.warnings ?? 0) > 0 && (
                  <>
                    {" · "}
                    <span className="text-signal-warning">
                      {display.summary.warnings}
                    </span>{" "}
                    incomplete
                  </>
                )}
              </span>
            </div>
            {!applied && (
              <ConfirmButton
                label={`Apply ${display.summary.valid} device${display.summary.valid === 1 ? "" : "s"}`}
                confirmLabel="Apply to Nautobot"
                consequence={`Writes ${display.summary.valid + (display.planned_catalog ? Object.values(display.planned_catalog).reduce((n, xs) => n + xs.length, 0) : 0)} objects to Nautobot. There is no undo.`}
                icon={<Upload className="h-3.5 w-3.5" aria-hidden />}
                disabled={!hasUploadableRows || !file}
                pending={apply.isPending}
                onConfirm={() => file && apply.mutate(file)}
              />
            )}
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-[11px] uppercase tracking-wider text-muted border-b border-line">
                  <th className="text-left px-3 py-2 w-12">Row</th>
                  <th className="text-left px-3 py-2 w-12">Status</th>
                  <th className="text-left px-3 py-2">Name</th>
                  <th className="text-left px-3 py-2">Primary IP</th>
                  <th className="text-left px-3 py-2">Transport</th>
                  <th className="text-left px-3 py-2">Site</th>
                  <th className="text-left px-3 py-2">Type</th>
                  <th className="text-left px-3 py-2">Role</th>
                  <th className="text-left px-3 py-2">Detail</th>
                </tr>
              </thead>
              <tbody>
                {display.rows.map((r) => (
                  <RowCell key={r.row} row={r} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {apply.isError && (
        <div className="text-xs text-[color:var(--critical)]">
          Nothing was written: {errorText(apply.error)}
        </div>
      )}

      {/* Format reference */}
      <details className="surface p-4">
        <summary className="cursor-pointer text-sm font-medium inline-flex items-center gap-2">
          <FileText className="h-4 w-4 text-accent-cyan" aria-hidden /> YAML format
        </summary>
        <pre className="mt-3 text-xs font-mono whitespace-pre bg-bg-elevated/60 border border-line rounded-lg p-3 overflow-x-auto">
          {YAML_EXAMPLE}
        </pre>
        <p className="text-xs text-muted mt-2">
          A bare top-level list (no <code>devices:</code> key) also works.
          Names in <code>site</code>, <code>device_type</code>,{" "}
          <code>role</code>, <code>platform</code> are resolved
          case-insensitively against your Nautobot catalog — they don&apos;t
          have to be the UUIDs.
        </p>
      </details>

      <details className="surface p-4">
        <summary className="cursor-pointer text-sm font-medium inline-flex items-center gap-2">
          <FileSpreadsheet className="h-4 w-4 text-accent-cyan" aria-hidden /> Excel format
        </summary>
        <p className="text-xs text-muted mt-2 leading-relaxed">
          First sheet, first row = headers (case-insensitive). Columns:{" "}
          <code>{EXCEL_COLUMNS}</code>. Empty rows are skipped. Transports must
          be one of <code>linux_ssh</code>, <code>redfish</code>,{" "}
          <code>junos</code>, <code>iosxr</code>, <code>eos</code>.
        </p>
      </details>
    </div>
  );
}

/** The catalog objects the run will create (preview) or created (apply).
 *  Empty plan + preview means the environment already has everything the file
 *  references, which is worth saying explicitly — it's the difference between
 *  "nothing to add" and "we didn't look". */
function CatalogPlanPanel({
  plan,
  created,
  applied,
}: {
  plan: SotCatalogPlan;
  created: string[];
  applied: boolean;
}) {
  const groups: [string, string[]][] = [
    ["Sites", plan.sites],
    ["Device types", plan.device_types],
    ["Roles", plan.roles],
    ["Platforms", plan.platforms],
    ["Prefixes", plan.prefixes],
  ];
  const total = groups.reduce((n, [, items]) => n + items.length, 0);

  if (total === 0 && !applied) {
    return (
      <div className="surface p-4 text-xs text-muted flex items-center gap-2">
        <CheckCircle2 className="h-4 w-4 text-accent-emerald" aria-hidden />
        Everything this file references already exists in your Nautobot catalog.
      </div>
    );
  }
  if (total === 0) return null;

  return (
    <div className="surface p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-semibold">
        <PlusCircle className="h-4 w-4 text-accent-cyan" aria-hidden />
        {applied
          ? `Catalog entries created (${created.length}/${total})`
          : `${total} catalog ${total === 1 ? "entry" : "entries"} will be created first`}
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {groups
          .filter(([, items]) => items.length > 0)
          .map(([label, items]) => (
            <div key={label} className="space-y-1">
              <div className="text-[11px] uppercase tracking-wider text-muted">
                {label}
              </div>
              <div className="flex flex-wrap gap-1">
                {items.map((name) => (
                  <span
                    key={name}
                    className="font-mono text-xs rounded-md border border-line bg-bg-elevated/60 px-1.5 py-0.5"
                  >
                    {name}
                  </span>
                ))}
              </div>
            </div>
          ))}
      </div>
      <p className="text-xs text-muted">
        {applied
          ? "These were added to this environment's Nautobot as part of the import."
          : "Check the spelling — these are created in your Nautobot exactly as written. Uncheck the box above to import against the existing catalog only."}
      </p>
    </div>
  );
}

function RowCell({ row }: { row: SotBulkRow }) {
  const accent =
    row.status === "error"
      ? "var(--critical)"
      : row.status === "created"
        ? "var(--accent)"
        : "var(--text-muted)";
  const Icon =
    row.status === "error"
      ? AlertCircle
      : row.status === "created"
        ? CheckCircle2
        : FileText;
  return (
    <tr className="border-b border-line/40 last:border-b-0 align-top">
      <td className="px-3 py-2 text-muted font-mono">{row.row}</td>
      <td className="px-3 py-2">
        <span className="inline-flex items-center gap-1" style={{ color: accent }}>
          <Icon className="h-3 w-3" aria-hidden />
          <span className="uppercase text-[11px] tracking-wider">{row.status}</span>
        </span>
      </td>
      <td className="px-3 py-2 font-medium">{row.name || "—"}</td>
      <td className="px-3 py-2 font-mono text-xs">{row.primary_ip || "—"}</td>
      <td className="px-3 py-2">{row.transport || "—"}</td>
      <td className="px-3 py-2">{row.site || "—"}</td>
      <td className="px-3 py-2">{row.device_type || "—"}</td>
      <td className="px-3 py-2">{row.role || "—"}</td>
      <td className="px-3 py-2 text-muted">
        {row.error ? (
          <span style={{ color: accent }}>{row.error}</span>
        ) : (
          <>
            {row.device_id && (
              <span className="font-mono text-[11px]">
                {row.device_id.slice(0, 8)}
              </span>
            )}
            {/* A created device that didn't get everything it should have —
                usually no primary IP. Saying so beats a bare green tick. */}
            {row.warnings?.map((w, i) => (
              <div
                key={i}
                className="mt-0.5 text-xs text-signal-warning"
              >
                {w}
              </div>
            ))}
            {!row.device_id && !row.warnings?.length && "—"}
          </>
        )}
      </td>
    </tr>
  );
}
