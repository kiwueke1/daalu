"use client";

/**
 * The Bulk import tab — one door for every part of the model.
 *
 * Devices come first because they are the thing everything else references:
 * a cable needs two interfaces, an interface needs a device. The three fabric
 * artifacts follow in the order they must be applied, which is the same order
 * they are listed in. Splitting them into sub-tabs rather than four top-level
 * tabs keeps `/operations` at six, and keeps the four uploads reading as one
 * sequence rather than four unrelated features.
 */

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { HardDrive } from "lucide-react";
import { BulkImportView } from "@/components/operations/bulk-import-view";
import {
  FABRIC_ARTIFACTS,
  FabricImportView,
} from "@/components/operations/fabric-import-view";
import type { SotFabricArtifact } from "@/lib/api";

type ImportTab = "devices" | SotFabricArtifact;

const IMPORT_TABS: { id: ImportTab; label: string }[] = [
  { id: "devices", label: "Devices" },
  ...FABRIC_ARTIFACTS.map((a) => ({ id: a.id as ImportTab, label: a.label })),
];

function isImportTab(value: string | null): value is ImportTab {
  return value !== null && IMPORT_TABS.some((t) => t.id === value);
}

/** `env` is the NV-CM environment slug; omitted means the tenant default.
 *
 *  `urlSync` puts the active sub-tab in `?artifact=` so a step is linkable —
 *  right on /operations, wrong anywhere else, because the rewrite targets
 *  /operations and would navigate away from an embedding page. Off by default
 *  for that reason; the Operations tab opts in. */
export function ImportView({
  env,
  urlSync = false,
}: { env?: string; urlSync?: boolean } = {}) {
  const router = useRouter();
  const sp = useSearchParams();
  const [localTab, setLocalTab] = useState<ImportTab>("devices");
  const raw = sp.get("artifact");
  const active: ImportTab = urlSync
    ? isImportTab(raw)
      ? raw
      : "devices"
    : localTab;

  const select = (id: ImportTab) => {
    if (!urlSync) {
      setLocalTab(id);
      return;
    }
    const next = new URLSearchParams(sp.toString());
    next.set("tab", "bulk-import");
    if (id === "devices") next.delete("artifact");
    else next.set("artifact", id);
    router.replace(`/operations?${next.toString()}`, { scroll: false });
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-1.5">
        {IMPORT_TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => select(t.id)}
            aria-pressed={active === t.id}
            className={
              active === t.id
                ? "text-xs h-8 px-3 rounded-lg border border-line bg-bg-elevated/80 text-[color:var(--text)]"
                : "text-xs h-8 px-3 rounded-lg border border-transparent text-muted hover:text-[color:var(--text)]"
            }
          >
            {t.label}
          </button>
        ))}
      </div>

      {active === "devices" ? (
        <>
          <div className="text-xs text-muted inline-flex items-center gap-1.5">
            <HardDrive className="h-3.5 w-3.5" aria-hidden />
            Start here: the fabric artifacts all reference devices by name.
          </div>
          <BulkImportView env={env} />
        </>
      ) : (
        <FabricImportView artifact={active} env={env} />
      )}
    </div>
  );
}
