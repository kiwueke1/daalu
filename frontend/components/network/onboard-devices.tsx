"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  KeyRound,
  Loader2,
  Network,
  ScrollText,
  Waypoints,
  XCircle,
} from "lucide-react";
import { api, type ConfigManagerEnvironment } from "@/lib/api";
import { ImportView } from "@/components/operations/import-view";

/**
 * Device onboarding for an NV-CM environment, inline on the environment
 * detail page (Managed infra → Network & servers → environment).
 *
 * The environment's bundled Nautobot is wired automatically — the
 * controller mirrors its API token onto the environment row and the SoT
 * client dials it through the tunnel — so unlike the BYO wizard there is
 * nothing to paste here. What the operator actually has to do:
 *
 *   1. see that Nautobot answers (auto-wired; this panel just probes it),
 *   2. store the SSH credential the executor/terminal will use,
 *   3. bulk-upload the device inventory,
 *   4. jump to the topology view.
 *
 * Every call below is scoped to `env.slug`: each environment has its own
 * Nautobot and therefore its own inventory, so a lab environment's devices
 * must not land in production's source of truth. The SSH credential is the
 * one exception — it is tenant-wide by design, shared by the executor and
 * the device terminal across every environment.
 */

function SshCredentialForm({ onSaved }: { onSaved: () => void }) {
  const [user, setUser] = useState("");
  const [authMode, setAuthMode] = useState<"password" | "key">("password");
  const [password, setPassword] = useState("");
  const [privateKey, setPrivateKey] = useState("");

  const save = useMutation({
    mutationFn: () => {
      const cfg: Record<string, unknown> = { user, port: 22, sudo: true };
      if (authMode === "key" && privateKey) cfg.private_key = privateKey;
      else if (authMode === "password" && password) cfg.password = password;
      return api.integrations.putConfig("ssh_credentials", {
        name: "Tenant-wide SSH credentials",
        config: cfg,
      });
    },
    onSuccess: onSaved,
  });

  const canSave =
    user.length > 0 &&
    ((authMode === "key" && privateKey.length > 0) ||
      (authMode === "password" && password.length > 0));

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-xs text-muted">SSH user</span>
          <input
            value={user}
            onChange={(e) => setUser(e.target.value)}
            placeholder="cumulus"
            className="w-full rounded-lg border border-line bg-bg-elevated/60 px-3 py-2 text-sm"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">
            {authMode === "password" ? "Password" : "Private key"}
          </span>
          {authMode === "password" ? (
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-line bg-bg-elevated/60 px-3 py-2 text-sm"
            />
          ) : (
            <textarea
              value={privateKey}
              onChange={(e) => setPrivateKey(e.target.value)}
              rows={3}
              placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
              className="w-full rounded-lg border border-line bg-bg-elevated/60 px-3 py-2 font-mono text-xs"
            />
          )}
        </label>
      </div>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() =>
            setAuthMode(authMode === "password" ? "key" : "password")
          }
          className="text-[11px] text-muted underline hover:text-fg"
        >
          Use a {authMode === "password" ? "private key" : "password"} instead
        </button>
        <button
          type="button"
          disabled={!canSave || save.isPending}
          onClick={() => save.mutate()}
          className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-lg border border-line px-3 text-xs hover:bg-bg-elevated/60 disabled:opacity-50"
        >
          {save.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
          Save credential
        </button>
      </div>
      {save.isError && (
        <p className="text-xs text-[color:var(--critical)]">
          {String(save.error)}
        </p>
      )}
    </div>
  );
}

export function OnboardDevices({ env }: { env: ConfigManagerEnvironment }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(true);

  const status = useQuery({
    queryKey: ["sot", "status", env.slug],
    queryFn: () => api.sot.devices.status(env.slug),
    refetchInterval: (q) => (q.state.data?.reachable ? false : 15000),
  });

  const { data: configs } = useQuery({
    queryKey: ["integrations", "config"],
    queryFn: () => api.integrations.listConfig(),
  });
  const sshConfigured = useMemo(
    () => (configs ?? []).some((c) => c.provider === "ssh_credentials"),
    [configs],
  );

  const s = status.data;

  return (
    <section className="rounded-2xl border border-line bg-bg-card">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-5 py-4 text-left"
      >
        {open ? (
          <ChevronDown className="h-4 w-4 text-muted" />
        ) : (
          <ChevronRight className="h-4 w-4 text-muted" />
        )}
        <Network className="h-4 w-4 text-accent-cyan" />
        <span className="text-base font-medium">Onboard your devices</span>
        {/* Name the target environment: with several environments in play,
            "which inventory am I writing to?" is the first question. */}
        <span className="ml-auto text-xs text-muted">
          Into <span className="text-fg">{env.name}</span> · bulk import
        </span>
      </button>

      {open && (
        <div className="space-y-5 border-t border-line px-5 py-4">
          {/* 1 — Nautobot wiring (automatic; just show it) */}
          <div className="flex items-start gap-3">
            {status.isLoading ? (
              <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-muted" />
            ) : s?.reachable ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-accent-emerald" />
            ) : (
              <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-accent-amber" />
            )}
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">
                Nautobot{" "}
                {s?.reachable
                  ? "connected"
                  : s?.wired
                    ? "wired, not answering yet"
                    : "not wired yet"}
              </p>
              <p className="text-xs text-muted">
                {s?.reachable
                  ? s.source === "nvcm"
                    ? "This environment's bundled Nautobot, reached over the tunnel — no setup needed."
                    : "Using your bring-your-own Nautobot integration."
                  : s?.wired
                    ? `The credential is stamped but the probe failed: ${s.detail || "unreachable"}. Usually a mid-install state — this panel keeps retrying.`
                    : "The controller stamps the environment's Nautobot token automatically once the stack is active. If this environment predates that, open its progress once (Managed infra → Network & servers) to trigger it."}
              </p>
            </div>
          </div>

          {/* 2 — SSH credential */}
          <div className="flex items-start gap-3">
            {sshConfigured ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-accent-emerald" />
            ) : (
              <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-accent-amber" />
            )}
            <div className="min-w-0 flex-1 space-y-2">
              <p className="text-sm font-medium">
                Device SSH credentials{" "}
                {sshConfigured ? "configured" : "needed"}
              </p>
              <p className="text-xs text-muted">
                Used for config push, drift checks and the in-browser device
                terminal (Cumulus/NVUE devices: user{" "}
                <code className="font-mono">cumulus</code>).
              </p>
              {!sshConfigured && (
                <SshCredentialForm
                  onSaved={() =>
                    qc.invalidateQueries({
                      queryKey: ["integrations", "config"],
                    })
                  }
                />
              )}
            </div>
          </div>

          {/* 3 — Bulk import */}
          <div className="border-t border-line pt-4">
            <ImportView env={env.slug} />
          </div>

          {/* 4 — device logs & metrics */}
          <div className="border-t border-line pt-4">
            <DeviceMonitoringPanel env={env.slug} />
          </div>

          {/* 5 — where they land */}
          <Link
            href={
              `/network/topology?env=${encodeURIComponent(env.slug)}` +
              `&env_name=${encodeURIComponent(env.name)}`
            }
            className="flex items-center gap-2 rounded-xl border border-line bg-bg-elevated/40 px-3 py-2.5 text-sm transition hover:border-accent-cyan/50"
          >
            <Waypoints className="h-4 w-4 text-accent-cyan" />
            See the imported fabric in the topology view
          </Link>
        </div>
      )}
    </section>
  );
}

/**
 * "Device logs & metrics" — connect the fabric to the tenant's own
 * Prometheus/Loki and jump straight to the network-only views.
 *
 * Status reads the tenant's stores for the `network-devices` scrape job and
 * `network-syslog` streams. Connect pushes the device-side config (rsyslog
 * forward + node_exporter enable, best-effort per device) over the same
 * tunnel-aware SSH the LLDP probe uses. The links land on the observability
 * pages with the Network panel group preselected and already run.
 */
function DeviceMonitoringPanel({ env }: { env?: string }) {
  const [collector, setCollector] = useState("");
  const status = useQuery({
    queryKey: ["sot-monitoring-status", env ?? null],
    queryFn: () => api.sot.devices.monitoringStatus(env),
    refetchInterval: 30_000,
  });
  const connect = useMutation({
    mutationFn: () =>
      api.sot.devices.monitoringConnect(collector.trim(), 5514, env),
    onSuccess: () => status.refetch(),
  });

  const s = status.data;
  const metricsN = s?.metrics_devices.length ?? 0;
  const logsN = s?.logs_devices.length ?? 0;
  const total = s?.devices_total ?? 0;
  const anyFlowing = metricsN > 0 || logsN > 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Activity className="h-4 w-4 text-accent-cyan" />
        <span className="text-sm font-medium">Device logs &amp; metrics</span>
        {s && (
          <span className="ml-auto text-xs text-muted">
            {metricsN}/{total} devices reporting metrics · {logsN} shipping logs
          </span>
        )}
      </div>

      {!anyFlowing && (
        <div className="space-y-2">
          <p className="text-xs text-muted leading-relaxed">
            Point every device at your monitoring stack: enter the syslog
            collector your cluster exposes to the device network (the Alloy
            receiver&apos;s address) and Daalu pushes the rsyslog forward +
            node_exporter config to each device over the tunnel — best-effort,
            per-device results below.
          </p>
          <div className="flex items-center gap-2">
            <input
              value={collector}
              onChange={(e) => setCollector(e.target.value)}
              placeholder="collector host/IP, e.g. 172.20.20.1"
              className="h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-xs font-mono w-64"
            />
            <button
              type="button"
              disabled={!collector.trim() || connect.isPending}
              onClick={() => connect.mutate()}
              className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base disabled:opacity-50 inline-flex items-center gap-1.5"
            >
              {connect.isPending ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Connecting…
                </>
              ) : (
                "Connect monitoring"
              )}
            </button>
          </div>
        </div>
      )}

      {connect.data && (
        <ul className="text-xs space-y-1">
          {connect.data.results.map((r) => (
            <li key={r.device} className="flex items-center gap-2">
              {r.ok ? (
                <CheckCircle2 className="h-3.5 w-3.5 text-accent-emerald" />
              ) : (
                <XCircle className="h-3.5 w-3.5 text-[color:var(--critical)]" />
              )}
              <span className="font-medium">{r.device}</span>
              {r.detail && <span className="text-muted truncate">{r.detail}</span>}
            </li>
          ))}
        </ul>
      )}

      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <Link
          href="/observability/prometheus?group=Network"
          className="flex items-center gap-2 rounded-xl border border-line bg-bg-elevated/40 px-3 py-2.5 text-sm transition hover:border-accent-cyan/50"
        >
          <Activity className="h-4 w-4 text-accent-cyan" />
          View network metrics
          {metricsN > 0 && (
            <span className="ml-auto text-[11px] text-accent-emerald">
              {metricsN} device{metricsN === 1 ? "" : "s"} live
            </span>
          )}
        </Link>
        <Link
          href="/observability/loki?group=Network"
          className="flex items-center gap-2 rounded-xl border border-line bg-bg-elevated/40 px-3 py-2.5 text-sm transition hover:border-accent-cyan/50"
        >
          <ScrollText className="h-4 w-4 text-accent-cyan" />
          View network logs
          {logsN > 0 && (
            <span className="ml-auto text-[11px] text-accent-emerald">
              {logsN} device{logsN === 1 ? "" : "s"} shipping
            </span>
          )}
        </Link>
      </div>
    </div>
  );
}
