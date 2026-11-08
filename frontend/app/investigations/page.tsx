"use client";

import { Suspense, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, SearchCheck } from "lucide-react";
import { api } from "@/lib/api";
import { formatRelative, severityColor } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";
import {
  Chip,
  PageHeader,
  Segmented,
  WINDOW_OPTIONS,
  fmtDuration,
  inputCls,
} from "@/components/aiops/ui";
import { VerdictChip } from "@/components/aiops/verdict";
import { RESOLUTION_LABEL, RESOLUTION_TONE } from "@/components/aiops/investigation-panel";
import { InvestigationDrawer } from "@/components/aiops/investigation-drawer";

/**
 * /investigations — one row per alert the AI investigated: how fast it found
 * the cause, what it proposed, how it ended, and whether the team agreed.
 * `?open=<id>` opens the detail drawer.
 */
export default function InvestigationsPage() {
  return (
    <Suspense fallback={null}>
      <InvestigationsInner />
    </Suspense>
  );
}

function InvestigationsInner() {
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const openId = search.get("open");

  const [days, setDays] = useState(30);
  const [resolution, setResolution] = useState("");
  const [needsVerdict, setNeedsVerdict] = useState(false);

  const q = useQuery({
    queryKey: ["aiops-investigations", days, resolution, needsVerdict],
    queryFn: () =>
      api.aiops.investigations.list({ days, resolution, needs_verdict: needsVerdict }),
    refetchInterval: 30_000,
  });
  const rows = q.data ?? [];

  const setOpen = (id: string | null) => {
    const p = new URLSearchParams(search.toString());
    if (id) p.set("open", id);
    else p.delete("open");
    const s = p.toString();
    router.replace(`${pathname}${s ? `?${s}` : ""}`, { scroll: false });
  };

  return (
    <div className="space-y-6 max-w-[1200px]">
      <PageHeader icon={SearchCheck} title="Investigations">
        Every alert the AI SRE investigated. Grade its root cause — correct,
        partly right or wrong — so the agreement rate is measured, not guessed.
      </PageHeader>

      <div className="flex flex-wrap items-center gap-2">
        <Segmented ariaLabel="Window" options={WINDOW_OPTIONS} value={days} onChange={setDays} />
        <select
          aria-label="Resolution"
          value={resolution}
          onChange={(e) => setResolution(e.target.value)}
          className={inputCls}
        >
          <option value="">Any resolution</option>
          {Object.entries(RESOLUTION_LABEL).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
        <label className="inline-flex items-center gap-2 text-xs">
          <input type="checkbox" checked={needsVerdict} onChange={(e) => setNeedsVerdict(e.target.checked)} />
          Needs a verdict
        </label>
      </div>

      <LoadState
        query={q}
        loadingText="Loading investigations…"
        empty={rows.length === 0}
        emptyTitle={needsVerdict ? "Nothing waiting for a verdict" : "No investigations in this window"}
        emptyHint="An investigation starts when the AI triages an alert — open an alert, or turn on investigate_automatically in the autonomy policy."
      >
        <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
          <table className="w-full min-w-[900px] text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-muted bg-bg-elevated/50">
              <tr>
                <th className="text-left px-4 py-2.5">Alert</th>
                <th className="text-left px-4 py-2.5">Started</th>
                <th className="text-right px-4 py-2.5">To diagnosis</th>
                <th className="text-left px-4 py-2.5" title="proposed / approved / rejected / auto">
                  Actions P / A / R / Auto
                </th>
                <th className="text-left px-4 py-2.5">Resolution</th>
                <th className="text-left px-4 py-2.5">Verdict</th>
                <th className="px-2" />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => setOpen(r.id)}
                  className="border-t border-line/60 hover:bg-bg-elevated/30 cursor-pointer"
                >
                  <td className="px-4 py-2.5 max-w-[22rem]">
                    <div className="font-medium truncate">{r.alert_title}</div>
                    <div
                      className="text-[10px] uppercase tracking-wider font-semibold"
                      style={{ color: severityColor[r.alert_severity] }}
                    >
                      {r.alert_severity}
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-muted whitespace-nowrap">{formatRelative(r.started_at)}</td>
                  <td className="px-4 py-2.5 text-right tabular-nums">
                    {r.diagnosed_at ? fmtDuration(r.time_to_diagnosis_s) : <span className="text-muted">—</span>}
                  </td>
                  <td className="px-4 py-2.5 tabular-nums text-muted whitespace-nowrap">
                    {r.actions_proposed} / {r.actions_approved} / {r.actions_rejected} /{" "}
                    <span className={r.actions_auto ? "text-accent font-semibold" : undefined}>{r.actions_auto}</span>
                  </td>
                  <td className="px-4 py-2.5">
                    <Chip tone={RESOLUTION_TONE[r.resolution] ?? "muted"}>
                      {RESOLUTION_LABEL[r.resolution] ?? r.resolution}
                    </Chip>
                  </td>
                  <td className="px-4 py-2.5">
                    {r.verdict ? (
                      <VerdictChip verdict={r.verdict} />
                    ) : r.diagnosed_at ? (
                      <Chip tone="accent" solid>needs verdict</Chip>
                    ) : (
                      <span className="text-muted text-xs">—</span>
                    )}
                  </td>
                  <td className="px-2 text-muted">
                    <ChevronRight className="h-4 w-4" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </LoadState>

      {openId && <InvestigationDrawer id={openId} onClose={() => setOpen(null)} />}
    </div>
  );
}
