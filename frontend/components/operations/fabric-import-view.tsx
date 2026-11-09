"use client";

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Cable,
  CheckCircle2,
  CircleSlash,
  FileText,
  Loader2,
  Network,
  Route,
  Settings2,
  Upload,
} from "lucide-react";
import {
  api,
  type SotFabricArtifact,
  type SotFabricImportResult,
  type SotFabricRow,
} from "@/lib/api";
import { errorText } from "@/lib/errors";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { useToast } from "@/components/ui/toast";

/** What each tab is, in the order the artifacts must be applied.
 *
 *  The order is load-bearing, not cosmetic: a cable can only terminate on an
 *  interface that already exists, and an address can only sit on one. Cabling
 *  therefore has to land before addressing. The API rejects the wrong order
 *  with a real error, and `dependsOn` is how the UI says so first. */
export const FABRIC_ARTIFACTS: {
  id: SotFabricArtifact;
  label: string;
  file: string;
  blurb: string;
  dependsOn?: SotFabricArtifact;
}[] = [
  {
    id: "cabling",
    label: "Cabling",
    file: "fabric-cabling.yaml",
    blurb:
      "The fabric ports each box has, and what is supposed to be plugged into what. Creates interfaces, then cables.",
  },
  {
    id: "addressing",
    label: "Addressing",
    file: "fabric-addressing.yaml",
    blurb:
      "The numbers a config contains. Creates prefixes, then the addresses that sit on the ports cabling made.",
    dependsOn: "cabling",
  },
  {
    id: "intent",
    label: "Intent",
    file: "fabric-intent.yaml",
    blurb:
      "What “correct” means for a box, as structured data rather than config. Sets device roles and role-scoped config contexts.",
  },
];

const OP_LABEL: Record<SotFabricRow["op"], string> = {
  interface: "Interface",
  cable: "Cable",
  prefix: "Prefix",
  address: "Address",
  role: "Role",
  config_context: "Config context",
};

/** Plural nouns for the "what formed" summary. */
const OP_PLURAL: Record<SotFabricRow["op"], string> = {
  interface: "interfaces",
  cable: "cables",
  prefix: "prefixes",
  address: "addresses",
  role: "roles",
  config_context: "config contexts",
};

const OP_ICON: Record<SotFabricRow["op"], typeof Cable> = {
  interface: Network,
  cable: Cable,
  prefix: Route,
  address: Route,
  role: Settings2,
  config_context: Settings2,
};

/** The second column's header changes with the artifact, because `detail` is
 *  a different thing per object — the far end of a cable, an address, a scope. */
const DETAIL_HEADER: Record<SotFabricArtifact, string> = {
  cabling: "Type / far end",
  addressing: "Address",
  intent: "Role / scope",
};

/** `env` is the NV-CM environment slug to import into — each environment has
 *  its own Nautobot. Omitted (the standalone Operations view) means the
 *  tenant's default source of truth. */
export function FabricImportView({
  artifact,
  env,
}: {
  artifact: SotFabricArtifact;
  env?: string;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<SotFabricImportResult | null>(null);
  const [applied, setApplied] = useState<SotFabricImportResult | null>(null);

  const spec = FABRIC_ARTIFACTS.find((a) => a.id === artifact)!;

  const dryRun = useMutation({
    mutationFn: (f: File) => api.sot.fabric.import(f, artifact, true, env),
    onSuccess: (r) => {
      setPreview(r);
      setApplied(null);
    },
    onError: () => setPreview(null),
  });

  const apply = useMutation({
    mutationFn: (f: File) => api.sot.fabric.import(f, artifact, false, env),
    onSuccess: (r) => {
      setApplied(r);
      // The fabric graph and the device list both read what just changed.
      qc.invalidateQueries({ queryKey: ["sot-devices"] });
      qc.invalidateQueries({ queryKey: ["topology"] });
      const n = r.summary.created;
      toast.success(`${n} object${n === 1 ? "" : "s"} written to Nautobot`);
    },
    onError: (err) => toast.error(errorText(err)),
  });

  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setFile(f);
    setPreview(null);
    setApplied(null);
    dryRun.mutate(f);
  };

  const display = applied ?? preview;
  const hasUploadableRows = display !== null && display.summary.valid > 0;

  return (
    <div className="space-y-5">
      <p className="text-muted text-sm">{spec.blurb}</p>

      {spec.dependsOn && (
        <div className="text-xs text-muted inline-flex items-center gap-1.5">
          <AlertCircle className="h-3.5 w-3.5" aria-hidden />
          Upload{" "}
          <span className="text-[color:var(--text)]">{spec.dependsOn}</span>{" "}
          first: every row here lands on something that file creates.
        </div>
      )}

      {/* Upload box */}
      <div className="surface p-5 space-y-4">
        <div className="flex items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            // Extensions AND MIME types: Linux file dialogs filter by MIME and
            // .yaml often has no shared-mime-info mapping, so extension-only
            // `accept` hides the very files this tab wants.
            accept=".yaml,.yml,application/yaml,application/x-yaml,text/yaml"
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
        </div>

        <div className="text-[11px] uppercase tracking-wider text-muted">
          Expected: {spec.file}
        </div>

        {dryRun.isError && (
          <div role="alert" className="text-xs text-[color:var(--critical)]">
            {errorText(dryRun.error)}
          </div>
        )}
      </div>

      {/* What formed. The whole point of the upload, so it sits above the
          row table rather than being something you count out of it. */}
      {display && Object.keys(display.counts).length > 0 && (
        <FormedPanel result={display} applied={applied !== null} />
      )}

      {display?.errors && display.errors.length > 0 && (
        <div className="surface p-4 text-xs space-y-1 border-l-2 border-[color:var(--critical)]">
          <div className="font-semibold">
            Nautobot didn&apos;t answer everything we asked for
          </div>
          <p className="text-muted">
            Rows below may report unknown devices or interfaces because the
            model couldn&apos;t be read — not because the file is wrong.
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
                {display.summary.total} objects ·{" "}
                <span className="text-[color:var(--text)]">
                  {display.summary.valid}
                </span>{" "}
                new
                {display.summary.skipped > 0 && (
                  <>
                    {" · "}
                    <span className="text-[color:var(--text)]">
                      {display.summary.skipped}
                    </span>{" "}
                    already there
                  </>
                )}
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
              </span>
            </div>
            {!applied && (
              <ConfirmButton
                label={`Apply ${display.summary.valid} object${display.summary.valid === 1 ? "" : "s"}`}
                confirmLabel="Apply to Nautobot"
                consequence={`Writes ${display.summary.valid} object${display.summary.valid === 1 ? "" : "s"} to Nautobot. There is no undo.`}
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
                  <th className="text-left px-3 py-2 w-12">#</th>
                  <th className="text-left px-3 py-2 w-12">Status</th>
                  <th className="text-left px-3 py-2">Object</th>
                  <th className="text-left px-3 py-2">Target</th>
                  <th className="text-left px-3 py-2">
                    {DETAIL_HEADER[artifact]}
                  </th>
                  <th className="text-left px-3 py-2">Detail</th>
                </tr>
              </thead>
              <tbody>
                {display.rows.map((r) => (
                  <RowCell key={`${r.op}-${r.row}`} row={r} />
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

      <details className="surface p-4">
        <summary className="cursor-pointer text-sm font-medium inline-flex items-center gap-2">
          <FileText className="h-4 w-4 text-accent-cyan" aria-hidden />
          What this file looks like
        </summary>
        <pre className="mt-3 text-xs font-mono text-muted overflow-x-auto whitespace-pre">
          {EXAMPLES[artifact]}
        </pre>
        <p className="mt-3 text-xs text-muted">
          The <span className="font-mono">kind:</span> line is what picks the
          importer. A file dropped on the wrong tab is refused by name rather
          than imported as nothing.
        </p>
      </details>
    </div>
  );
}

/** "71 interfaces · 32 cables" — the shape of the model that just formed.
 *  On preview it reads as what would form; after apply, what did. */
function FormedPanel({
  result,
  applied,
}: {
  result: SotFabricImportResult;
  applied: boolean;
}) {
  const entries = Object.entries(result.counts) as [
    SotFabricRow["op"],
    number,
  ][];
  return (
    <div className="surface p-4 space-y-3">
      <div className="text-sm font-semibold inline-flex items-center gap-2">
        <Network className="h-4 w-4 text-accent-cyan" aria-hidden />
        {applied ? "What formed" : "What this will form"}
      </div>
      <div className="flex flex-wrap gap-2">
        {entries.map(([op, n]) => {
          const Icon = OP_ICON[op];
          return (
            <span
              key={op}
              className="inline-flex items-center gap-1.5 text-xs rounded-lg border border-line bg-bg-elevated/60 px-2.5 py-1.5"
            >
              <Icon className="h-3.5 w-3.5 text-accent-emerald" aria-hidden />
              <span className="font-mono">{n}</span>
              <span className="text-muted">{OP_PLURAL[op]}</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

function RowCell({ row }: { row: SotFabricRow }) {
  const icon =
    row.status === "error" ? (
      <AlertCircle className="h-3.5 w-3.5 text-[color:var(--critical)]" aria-label="Error" />
    ) : row.status === "exists" ? (
      <CircleSlash className="h-3.5 w-3.5 text-muted" aria-label="Already there" />
    ) : (
      <CheckCircle2
        className={
          row.status === "created"
            ? "h-3.5 w-3.5 text-accent-emerald"
            : "h-3.5 w-3.5 text-muted"
        }
        aria-label={row.status === "created" ? "Created" : "New"}
      />
    );

  return (
    <tr className="border-b border-line/40 last:border-b-0 align-top">
      <td className="px-3 py-2 text-muted font-mono text-xs">{row.row}</td>
      <td className="px-3 py-2">{icon}</td>
      <td className="px-3 py-2 text-muted">{OP_LABEL[row.op]}</td>
      <td className="px-3 py-2 font-mono text-xs">{row.target}</td>
      <td className="px-3 py-2 font-mono text-xs text-muted">
        {row.detail}
      </td>
      <td className="px-3 py-2">
        {row.error && (
          <div className="text-[color:var(--critical)]">{row.error}</div>
        )}
        {row.warnings.map((w, i) => (
          <div key={i} className="text-signal-warning">
            {w}
          </div>
        ))}
        {!row.error && row.warnings.length === 0 && row.object_id && (
          <span className="text-muted font-mono text-xs">
            {row.object_id.slice(0, 8)}
          </span>
        )}
      </td>
    </tr>
  );
}

const EXAMPLES: Record<SotFabricArtifact, string> = {
  cabling: `kind: cabling

interfaces:
  leaf1:
    - {name: swp1, type: 10gbase-x-sfpp}
    - {name: lo,   type: virtual}
  gpu1:
    - {name: eth1, type: 10gbase-x-sfpp}

cables:
  - [gpu1:eth1, leaf1:swp1]`,
  addressing: `kind: addressing

prefixes:
  - {prefix: 10.0.0.0/24, description: fabric loopbacks}

loopbacks:
  - {device: leaf1, interface: lo, address: 10.0.0.21/32}

point_to_point:
  - {device: leaf1, interface: swp11, address: 10.1.0.1/31}`,
  intent: `kind: intent

roles:
  leaf1: Leaf-Common

config_contexts:
  - name: leaf-common-intent
    scope: {role: Leaf-Common, location: lab-site}
    data:
      intended-firmware:
        version: '5.13.1'
      fabric-mac: '44:38:39:ff:00:00'`,
};
