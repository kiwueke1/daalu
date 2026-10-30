"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ShieldCheck, ShieldAlert, Shield, Camera } from "lucide-react";
import {
  api,
  type AiFactoryReliability,
  type AiFactorySignalLevel,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { LoadState } from "@/components/ui/load-state";

/**
 * Reliability — the NVSentinel auto-remediation posture for the tenant's card,
 * read-only (owner/provider). Surfaces doc 03 in the product:
 *  · DCGM health signals (XID / ECC / thermal) that NVSentinel acts on.
 *  · Whether NVSentinel auto-remediation is active (it watches the same DCGM
 *    stream and cordons/reboots a faulted node) — and that it ships in
 *    observe/dry-run mode first.
 *  · cuda-checkpoint status — gated behind legal sign-off (proprietary EULA),
 *    shown so the capability is visible without being offered until cleared.
 *
 * The hub never drives remediation; it only reads NVSentinel's exported metrics.
 */
export function ReliabilityPanel() {
  const rel = useQuery({
    queryKey: ["aiFactory", "reliability"],
    queryFn: api.aiFactory.reliability,
    refetchInterval: 30_000,
  });

  const data = rel.data;
  return (
    <LoadState
      query={rel}
      empty={!data || data.status === "n/a"}
      loadingText="Reading the fault signals…"
      emptyTitle="No GPU is reporting reliability signals"
      emptyHint="Fault signals (XID, ECC, thermal) show here once a GPU card is onboarded and its metrics reach Daalu."
      emptyAction={
        <Link
          href="/ai-factory"
          className="text-xs h-8 px-3 rounded-lg border border-line inline-flex items-center hover:border-line-strong"
        >
          Go to AI Factory
        </Link>
      }
    >
      {data && <ReliabilityBody data={data} />}
    </LoadState>
  );
}

function ReliabilityBody({ data }: { data: AiFactoryReliability }) {

  const Icon =
    data.status === "crit"
      ? ShieldAlert
      : data.status === "warn"
        ? ShieldAlert
        : ShieldCheck;
  const tone =
    data.status === "crit"
      ? "text-[color:var(--critical)]"
      : data.status === "warn"
        ? "text-[color:var(--warning)]"
        : "text-accent-emerald";

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-sm font-medium flex items-center gap-1.5">
          <Icon className={cn("h-4 w-4", tone)} /> Auto-remediation
          <span className="text-[10px] text-muted font-normal ml-1 uppercase tracking-wide">
            NVSentinel
          </span>
        </h3>
        <NvSentinelBadge
          active={data.nvsentinel.active}
          mode={data.nvsentinel.mode}
          remediations={data.nvsentinel.remediations}
        />
      </div>
      <p className="text-xs text-muted mt-1 max-w-[620px]">
        NVSentinel watches the card&apos;s fault stream and, once it leaves
        observe mode, cordons and recovers a faulted node in seconds.
      </p>

      {data.metrics_available === false ? (
        <div className="mt-4 text-xs text-muted">
          Connect a metrics source to see live reliability signals.
        </div>
      ) : (
        <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
          {data.signals.map((s) => (
            <SignalCard
              key={s.name}
              name={s.name}
              value={s.value}
              unit={s.unit}
              level={s.level}
            />
          ))}
        </div>
      )}

      {/* cuda-checkpoint — legal-gated, informational only. */}
      <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-line bg-bg-base/40 px-3.5 py-3">
        <Camera className="h-4 w-4 mt-0.5 shrink-0 text-muted" />
        <div className="min-w-0">
          <div className="text-xs font-medium flex items-center gap-2">
            cuda-checkpoint
            <span
              className={cn(
                "inline-flex rounded px-1.5 py-0.5 border text-[10px] uppercase tracking-wide",
                data.cuda_checkpoint.status === "enabled"
                  ? "border-accent-emerald/40 text-[color:var(--text)]"
                  : "border-line text-muted"
              )}
            >
              {data.cuda_checkpoint.status === "enabled"
                ? "enabled"
                : "gated — legal sign-off"}
            </span>
          </div>
          <p className="text-[11px] text-muted mt-1">
            {data.cuda_checkpoint.note}
          </p>
        </div>
      </div>

      {!data.nvsentinel.active && (
        <div className="mt-3 text-[11px] text-muted">
          Auto-remediation is not active yet — GPU faults page a human via the
          runbook. NVSentinel is piloted in observe/dry-run mode before it is
          allowed to cordon or reboot a node.
        </div>
      )}
    </section>
  );
}

function NvSentinelBadge({
  active,
  mode,
  remediations,
}: {
  active: boolean;
  mode?: string;
  remediations?: number;
}) {
  if (!active) {
    return (
      <span className="text-[11px] rounded-full px-2.5 py-1 border border-line text-muted inline-flex items-center gap-1.5">
        <Shield className="h-3 w-3" /> Not active
      </span>
    );
  }
  return (
    <span className="text-[11px] rounded-full px-2.5 py-1 border border-accent-emerald/40 bg-accent-emerald/10 inline-flex items-center gap-1.5">
      <ShieldCheck className="h-3 w-3 text-accent-emerald" />
      Active{mode ? ` · ${mode}` : ""}
      {typeof remediations === "number" ? ` · ${remediations} remediations` : ""}
    </span>
  );
}

function SignalCard({
  name,
  value,
  unit,
  level,
}: {
  name: string;
  value: number;
  unit: string;
  level: AiFactorySignalLevel;
}) {
  const tone =
    level === "crit"
      ? "border-signal-critical/40 text-[color:var(--critical)]"
      : level === "warn"
        ? "border-signal-warning/40 text-[color:var(--warning)]"
        : "border-line text-[color:var(--text)]";
  return (
    <div className={cn("rounded-lg border bg-bg-base/40 px-3.5 py-3", tone)}>
      <div className="text-[10px] uppercase tracking-[0.12em] text-muted">
        {name}
      </div>
      <div className="text-lg font-semibold mt-1">
        {value}
        {unit && <span className="text-xs font-normal ml-0.5">{unit}</span>}
      </div>
    </div>
  );
}
