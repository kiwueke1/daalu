"use client";

import { Suspense, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, SearchCheck } from "lucide-react";
import { api } from "@/lib/api";
import { cn, formatRelative, severityColor } from "@/lib/utils";
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
        <label className="inline-flex items-center gap-2 text-xs min-h-[24px]">
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
        {/* Phone: stacked cards, so the verdict and outcome stay visible. */}
        <ul className="md:hidden space-y-2 motion-enter">
          {rows.map((r) => (
            <li key={r.id}>
              <button
                type="button"
                onClick={() => setOpen(r.id)}
                className="w-full text-left rounded-2xl border border-line bg-bg-card p-3.5 hover:bg-bg-elevated/30"
              >
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-sm break-words">{r.alert_title}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
                      <Severity value={r.alert_severity} />
                      <span>{formatRelative(r.started_at)}</span>
                      {r.triaged_at && <span>triaged in {fmtDuration(r.time_to_triage_s)}</span>}
                    </div>
                  </div>
                  <ChevronRight aria-hidden className="h-4 w-4 mt-0.5 shrink-0 text-muted" />
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <VerdictCell r={r} />
                  <Chip tone={RESOLUTION_TONE[r.resolution] ?? "muted"}>
                    {RESOLUTION_LABEL[r.resolution] ?? r.resolution}
                  </Chip>
                </div>
                <div className="mt-2 text-xs text-muted">
                  <ActionCounts r={r} />
                </div>
              </button>
            </li>
          ))}
        </ul>

        <div className="hidden md:block rounded-2xl border border-line bg-bg-card relative overflow-x-auto motion-enter">
          <table className="w-full min-w-[900px] text-sm">
            <thead className="text-[11px] uppercase tracking-wider text-muted bg-bg-elevated/50">
              <tr>
                <th className="text-left px-4 py-2.5">Alert</th>
                <th className="text-left px-4 py-2.5">Started</th>
                <th className="text-right px-4 py-2.5">To triage</th>
                <th className="text-left px-4 py-2.5">Actions</th>
                <th className="text-left px-4 py-2.5">Resolution</th>
                <th className="text-left px-4 py-2.5">Verdict</th>
                <th className="px-2">
                  <span className="sr-only">Open</span>
                </th>
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
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        setOpen(r.id);
                      }}
                      className="block max-w-full text-left font-medium truncate hover:text-accent"
                    >
                      {r.alert_title}
                    </button>
                    <Severity value={r.alert_severity} />
                  </td>
                  <td className="px-4 py-2.5 text-muted whitespace-nowrap">{formatRelative(r.started_at)}</td>
                  <td className="px-4 py-2.5 text-right tabular-nums">
                    {r.triaged_at ? fmtDuration(r.time_to_triage_s) : <span className="text-muted">—</span>}
                  </td>
                  <td className="px-4 py-2.5 text-xs text-muted min-w-[7rem]">
                    <ActionCounts r={r} stacked />
                  </td>
                  <td className="px-4 py-2.5">
                    <Chip tone={RESOLUTION_TONE[r.resolution] ?? "muted"}>
                      {RESOLUTION_LABEL[r.resolution] ?? r.resolution}
                    </Chip>
                  </td>
                  <td className="px-4 py-2.5">
                    <VerdictCell r={r} />
                  </td>
                  <td className="px-2 text-muted">
                    <ChevronRight aria-hidden className="h-4 w-4" />
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

type Row = Awaited<ReturnType<typeof api.aiops.investigations.list>>[number];

/** Severity as a dot + word, quiet enough that a list of criticals isn't a wall of red. */
function Severity({ value }: { value: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted capitalize">
      <span
        aria-hidden
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: severityColor[value] ?? "var(--text-muted)" }}
      />
      {value}
    </span>
  );
}

/**
 * "1 proposed", "1 approved", … hiding zeros. Each count is its own unbroken
 * segment (stacked in the table, wrapping in cards), so no line ever starts
 * with a stray separator.
 */
function ActionCounts({ r, stacked = false }: { r: Row; stacked?: boolean }) {
  const parts: { n: number; label: string; accent?: boolean }[] = [
    { n: r.actions_proposed, label: "proposed" },
    { n: r.actions_approved, label: "approved" },
    { n: r.actions_rejected, label: "rejected" },
    { n: r.actions_auto, label: "automatic", accent: true },
  ];
  if (parts.every((p) => !p.n)) return <span className="whitespace-nowrap">No actions</span>;
  return (
    <span
      className={cn(
        "tabular-nums",
        stacked ? "flex flex-col gap-0.5" : "flex flex-wrap gap-x-3 gap-y-0.5",
      )}
    >
      {parts
        .filter((p) => p.n)
        .map((p) => (
          <span
            key={p.label}
            className={cn("whitespace-nowrap", p.accent && "text-accent font-semibold")}
          >
            {p.n} {p.label}
          </span>
        ))}
    </span>
  );
}

function VerdictCell({ r }: { r: Row }) {
  if (r.verdict) return <VerdictChip verdict={r.verdict} />;
  if (r.triaged_at) return <Chip tone="accent" solid>needs verdict</Chip>;
  return <span className="text-muted text-xs">Not triaged yet</span>;
}
