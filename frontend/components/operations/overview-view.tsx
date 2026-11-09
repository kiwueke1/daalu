"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  GitPullRequest,
  HardDrive,
} from "lucide-react";
import { api } from "@/lib/api";
import { LoadState } from "@/components/ui/load-state";
import { SOT_NOT_SET_UP, isSotNotSetUp } from "@/components/operations/sot-not-set-up";

interface OverviewViewProps {
  onJumpTo: (tabId: string) => void;
}

export function OverviewView({ onJumpTo }: OverviewViewProps) {
  const devicesQ = useQuery({
    queryKey: ["sot-devices"],
    queryFn: () => api.sot.devices.list(),
  });
  const driftQ = useQuery({
    queryKey: ["change-proposals", "drift"],
    queryFn: () =>
      api.changeProposals.list({ kind: "drift", status: "pending" }),
    refetchInterval: 15_000,
  });
  const pendingQ = useQuery({
    queryKey: ["change-proposals", "pending"],
    queryFn: () => api.changeProposals.list({ status: "pending" }),
    refetchInterval: 15_000,
  });

  const devices = devicesQ.data;
  const openDrift = driftQ.data;
  const pending = pendingQ.data;
  // Never show "0 devices / all clean" while a query is still loading or has
  // failed: a loading fleet must not read as an empty, healthy one.
  const totalDevices = devices?.length;
  const driftCount = openDrift?.length;
  const inSyncCount =
    totalDevices !== undefined && driftCount !== undefined
      ? Math.max(totalDevices - driftCount, 0)
      : undefined;
  const pendingCount = pending?.length;
  const failed = [devicesQ, driftQ, pendingQ].find((q) => q.isError);
  const needsDecision = (driftCount ?? 0) > 0;

  // Without Nautobot there's nothing to count: show the setup step, not a
  // red error above three dashes.
  // Proposals don't depend on Nautobot, so pending ones keep their card.
  if (devicesQ.isError && isSotNotSetUp(devicesQ.error)) {
    return (
      <div className="space-y-6">
        <LoadState query={devicesQ} notSetUp={SOT_NOT_SET_UP}>
          {null}
        </LoadState>
        {(pendingCount ?? 0) > 0 && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <ProposalsCard pendingCount={pendingCount} onOpen={() => onJumpTo("proposals")} />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {failed && <LoadState query={failed}>{null}</LoadState>}

      {/* KPIs */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <KpiCard
          label="Devices in SoT"
          value={totalDevices}
          accent="var(--info)"
          icon={HardDrive}
          onClick={() => onJumpTo("devices")}
        />
        <KpiCard
          label="In sync"
          value={inSyncCount}
          accent="var(--accent)"
          icon={CheckCircle2}
          onClick={() => onJumpTo("devices")}
          help={
            inSyncCount === undefined || !totalDevices
              ? undefined
              : `${Math.round((inSyncCount / totalDevices) * 100)}% of the fleet`
          }
        />
        <KpiCard
          label="Open drift"
          value={driftCount}
          accent="var(--warning)"
          icon={AlertTriangle}
          onClick={() => onJumpTo("drift")}
          attention={needsDecision}
          help={
            driftCount === undefined
              ? undefined
              : driftCount === 0
              ? "all clean"
              : "needs your decision · Review drift →"
          }
        />
      </div>

      {/* Secondary stats + jump-ins */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <ProposalsCard pendingCount={pendingCount} onOpen={() => onJumpTo("proposals")} />

        <div className="surface p-4 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[11px] uppercase tracking-wider text-muted">
              Onboarding
            </span>
            <Link
              href="/onboarding/sot"
              className="text-xs text-muted hover:text-fg"
            >
              SoT wizard →
            </Link>
          </div>
          <div className="flex items-center gap-2">
            <GitPullRequest className="h-4 w-4 text-accent-blue" aria-hidden />
            <span className="text-sm">
              Use the <strong>Bulk import</strong> tab to seed Nautobot from a
              YAML or Excel spreadsheet.
            </span>
          </div>
          <p className="text-xs text-muted leading-relaxed">
            Already have your own Nautobot? Connect it via{" "}
            <Link
              href="/integrations"
              className="underline text-accent-cyan"
            >
              Integrations
            </Link>{" "}
            and skip our hosted instance — same experience either way.
          </p>
        </div>
      </div>

      {/* Vocabulary, folded: useful once, not on every visit. */}
      <details className="surface p-4 group">
        <summary className="cursor-pointer text-sm font-medium">
          How Source of Truth works
        </summary>
        <div className="mt-3 space-y-3">
          <p className="text-sm text-muted leading-relaxed">
            <strong className="text-fg">Source of Truth</strong> (SoT) is your
            authoritative inventory, kept in Nautobot. It says what each device{" "}
            <em>should</em> look like.{" "}
            <strong className="text-fg">Live state</strong> is what the device
            actually looks like, right now, on the box.{" "}
            <strong className="text-fg">Drift</strong> is a difference between
            the two.
          </p>
          <p className="text-sm text-muted leading-relaxed">
            Daalu checks every device every 5 minutes. When it finds drift it
            opens a change proposal and never pushes a fix on its own. You
            decide whether to apply Nautobot&apos;s config to the device or
            update Nautobot to match the device.
          </p>
        </div>
      </details>
    </div>
  );
}

function KpiCard({
  label,
  value,
  accent,
  icon: Icon,
  onClick,
  help,
  attention = false,
}: {
  label: string;
  /** undefined while loading: shows a dash, never a misleading 0. */
  value: number | undefined;
  accent: string;
  icon: typeof HardDrive;
  onClick: () => void;
  help?: string;
  /** Needs the user: the help line turns into an accent call to action. */
  attention?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`${label}: ${value ?? "loading"}${help ? `, ${help}` : ""}`}
      className="surface text-left p-4 hover:border-accent-blue/40 transition-colors"
      style={{ boxShadow: `inset 4px 0 0 ${accent}` }}
    >
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted">
        <Icon className="h-3 w-3" style={{ color: accent }} aria-hidden />
        {label}
      </div>
      <div className="mt-1.5 flex items-baseline gap-2">
        <span className="text-3xl font-semibold">{value ?? "—"}</span>
        {help && (
          <span
            className={
              attention
                ? "motion-attention rounded-full px-2 py-0.5 text-xs font-semibold bg-accent text-bg-base"
                : "text-xs text-muted"
            }
          >
            {help}
          </span>
        )}
      </div>
    </button>
  );
}

function ProposalsCard({
  pendingCount,
  onOpen,
}: {
  pendingCount: number | undefined;
  onOpen: () => void;
}) {
  return (
    <div className="surface p-4 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-wider text-muted">
          Proposals
        </span>
        <button
          type="button"
          onClick={onOpen}
          className="text-xs text-muted hover:text-fg"
        >
          Open queue →
        </button>
      </div>
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-semibold">{pendingCount ?? "—"}</span>
        <span className="text-xs text-muted">pending</span>
      </div>
      <p className="text-xs text-muted leading-relaxed">
        Every proposal (drift, AI-suggested, manual, workflow) goes
        through the same approve/reject queue. Nothing reaches a device
        until someone approves it.
      </p>
    </div>
  );
}
