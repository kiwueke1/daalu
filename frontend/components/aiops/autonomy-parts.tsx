"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, FlaskConical, History, Loader2, Power, RotateCcw, ShieldAlert } from "lucide-react";
import { api, type AutonomyPolicy, type PolicyDecision } from "@/lib/api";
import { LoadState } from "@/components/ui/load-state";
import { ConfirmButton } from "@/components/ui/confirm-button";
import {
  Chip,
  ErrorNote,
  btnPrimary,
  btnSecondary,
  fmtDateTime,
  fmtRate,
  inputCls,
  type Tone,
} from "./ui";
import { WRITE_TOOLS } from "./starter-policies";

const MODE_TONE: Record<string, Tone> = {
  deny: "critical",
  approve: "warning",
  auto: "accent",
  auto_after_trust: "info",
};

export function ModeChip({ mode }: { mode: string }) {
  return <Chip tone={MODE_TONE[mode] ?? "muted"}>{mode.replace(/_/g, " ")}</Chip>;
}

// ── Kill switch ─────────────────────────────────────────────────────────

/**
 * Pause every automatic action. Anyone may turn it ON; only an admin may
 * turn it OFF (the backend answers 403 otherwise, shown inline).
 */
export function KillSwitchCard({ policy }: { policy: AutonomyPolicy }) {
  const qc = useQueryClient();
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.aiops.policy.killSwitch(enabled),
    onSuccess: (p) => {
      qc.setQueryData(["aiops-policy"], p);
      qc.invalidateQueries({ queryKey: ["aiops-trust"] });
    },
  });
  const on = policy.kill_switch;
  const color = on ? "var(--critical)" : "var(--line)";
  return (
    <section
      className="rounded-2xl p-4 lg:p-5 flex flex-wrap items-center gap-4"
      style={{
        background: on
          ? "color-mix(in srgb, var(--critical) 12%, var(--bg-card))"
          : "var(--bg-card)",
        border: `1px solid ${on ? "color-mix(in srgb, var(--critical) 50%, var(--line))" : color}`,
      }}
    >
      <Power className="h-6 w-6 shrink-0" style={{ color: on ? "var(--critical)" : "var(--text-muted)" }} />
      <div className="flex-1 min-w-[14rem]">
        <div className="text-[15px] font-semibold">
          Kill switch {on ? "is ON — all automatic actions are paused" : "is off"}
        </div>
        <div className="text-xs text-muted mt-0.5">
          {on
            ? `Turned on by ${policy.kill_switch_by || "someone"} · ${fmtDateTime(policy.kill_switch_at)}. Every write now waits for approval. Only an admin can turn it off.`
            : "Turn it on to make every write wait for a human, immediately. Anyone can turn it on; only an admin can turn it back off."}
        </div>
        {toggle.error ? (
          <div className="mt-2">
            <ErrorNote error={toggle.error} />
          </div>
        ) : null}
      </div>
      {on ? (
        <ConfirmButton
          label="Turn off"
          confirmLabel="Resume automation"
          consequence="Auto actions resume under the policy."
          onConfirm={() => toggle.mutate(false)}
          pending={toggle.isPending}
          icon={<Power className="h-3.5 w-3.5" />}
        />
      ) : (
        <button
          type="button"
          disabled={toggle.isPending}
          onClick={() => toggle.mutate(true)}
          className="h-10 px-4 rounded-lg text-sm font-semibold inline-flex items-center gap-2 disabled:opacity-50"
          style={{ background: "var(--critical)", color: "var(--bg-base)" }}
        >
          {toggle.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Power className="h-4 w-4" />}
          Pause all automation
        </button>
      )}
    </section>
  );
}

// ── Simulator ───────────────────────────────────────────────────────────

/** "Test a write": what would the policy decide right now? */
export function PolicySimulator({ draftYaml }: { draftYaml: string }) {
  const [tool, setTool] = useState<string>(WRITE_TOOLS[0]);
  const [cluster, setCluster] = useState("");
  const [namespace, setNamespace] = useState("");
  const [severity, setSeverity] = useState("");
  const [external, setExternal] = useState(false);
  const [useDraft, setUseDraft] = useState(true);

  const sim = useMutation({
    mutationFn: () =>
      api.aiops.policy.simulate({
        tool,
        input: namespace ? { namespace } : {},
        cluster,
        severity,
        external,
        yaml: useDraft ? draftYaml : null,
      }),
  });
  const d: PolicyDecision | undefined = sim.data;

  return (
    <section className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5 space-y-3">
      <div className="flex items-center gap-2 text-[15px] font-medium">
        <FlaskConical className="h-4 w-4 text-accent" /> Test a write
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <label className="space-y-1 col-span-2">
          <span className="text-muted">Tool</span>
          <select value={tool} onChange={(e) => setTool(e.target.value)} className={`${inputCls} w-full`}>
            {WRITE_TOOLS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="text-muted">Cluster</span>
          <input value={cluster} onChange={(e) => setCluster(e.target.value)} placeholder="dev-eu-1" className={`${inputCls} w-full`} />
        </label>
        <label className="space-y-1">
          <span className="text-muted">Namespace</span>
          <input value={namespace} onChange={(e) => setNamespace(e.target.value)} placeholder="prod-api" className={`${inputCls} w-full`} />
        </label>
        <label className="space-y-1">
          <span className="text-muted">Alert severity</span>
          <select value={severity} onChange={(e) => setSeverity(e.target.value)} className={`${inputCls} w-full`}>
            <option value="">(any)</option>
            <option value="info">info</option>
            <option value="warning">warning</option>
            <option value="critical">critical</option>
          </select>
        </label>
        <div className="space-y-1.5 pt-1">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={external} onChange={(e) => setExternal(e.target.checked)} />
            <span>From an external agent (MCP)</span>
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={useDraft} onChange={(e) => setUseDraft(e.target.checked)} />
            <span>Test against the draft in the editor</span>
          </label>
        </div>
      </div>
      <button type="button" className={btnPrimary} disabled={sim.isPending} onClick={() => sim.mutate()}>
        {sim.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
        Run test
      </button>
      <ErrorNote error={sim.error} prefix={useDraft ? "The draft doesn't validate:" : "Couldn't simulate:"} />
      {d && !sim.error && (
        <div className="rounded-lg border border-line bg-bg-elevated/40 p-3 space-y-1.5 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-muted">Decision</span>
            <ModeChip mode={d.mode} />
            {d.configured_mode !== d.mode && (
              <span className="text-muted">
                (rule says <span className="font-mono">{d.configured_mode}</span>)
              </span>
            )}
            {d.mode === "approve" && (
              <span className="text-muted">
                {d.approvers} approver{d.approvers === 1 ? "" : "s"}
              </span>
            )}
          </div>
          <div>
            <span className="text-muted">Rule </span>
            <span className="font-mono">{d.rule_id || "defaults"}</span>
          </div>
          <div className="text-fg/80">{d.reason}</div>
          {d.context && (
            <div className="text-muted">
              Kill switch {d.context.kill_switch ? "on" : "off"} · {d.context.auto_actions_last_hour} auto
              actions in the last hour
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ── Trust table ─────────────────────────────────────────────────────────

export function TrustTable({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["aiops-trust"], queryFn: () => api.aiops.trust() });
  const reset = useMutation({
    mutationFn: (ruleId: string) => api.aiops.policy.resetBreaker(ruleId),
    onSuccess: (p) => {
      qc.setQueryData(["aiops-policy"], p);
      qc.invalidateQueries({ queryKey: ["aiops-trust"] });
    },
  });
  const rows = q.data ?? [];
  return (
    <section className="space-y-2">
      <div className="text-[15px] font-medium">Trust by rule</div>
      <p className="text-xs text-muted">
        Each rule&apos;s track record: executed actions whose fix verified (success)
        or didn&apos;t (failure). An <span className="font-mono">auto_after_trust</span>{" "}
        rule runs on its own once it has earned it; a tripped breaker demotes it
        to approve until the cooldown ends.
      </p>
      <ErrorNote error={reset.error} prefix="Couldn't reset the breaker:" />
      <LoadState
        query={q}
        empty={rows.length === 0}
        emptyTitle="No rules in the active policy"
        emptyHint="Every write uses the defaults. Add rules in the editor above to give them a track record."
      >
        <div className="rounded-2xl border border-line bg-bg-card overflow-x-auto">
          <table className="w-full min-w-[720px] text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-muted bg-bg-elevated/50">
              <tr>
                <th className="text-left px-4 py-2">Rule</th>
                <th className="text-left px-4 py-2">Mode</th>
                <th className="text-right px-4 py-2">Successes</th>
                <th className="text-right px-4 py-2">Failures</th>
                <th className="text-right px-4 py-2">Failure rate</th>
                <th className="text-left px-4 py-2">Earned</th>
                <th className="text-left px-4 py-2">Breaker</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const tripped = !!r.breaker_until && new Date(r.breaker_until) > new Date();
                return (
                  <tr key={r.rule_id} className="border-t border-line/60">
                    <td className="px-4 py-2">
                      <div className="font-mono text-[13px]">{r.rule_id}</div>
                      <div className="text-[11px] text-muted">{r.tools.join(", ")}</div>
                    </td>
                    <td className="px-4 py-2">
                      <ModeChip mode={r.mode} />
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {r.successes}
                      {r.min_successes !== null && <span className="text-muted"> / {r.min_successes}</span>}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">{r.failures}</td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      {r.successes + r.failures ? fmtRate(r.failure_rate) : "—"}
                      {r.max_failure_rate !== null && (
                        <span className="text-muted"> ≤ {fmtRate(r.max_failure_rate)}</span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {r.mode === "auto" || r.mode === "auto_after_trust" ? (
                        r.earned ? <Chip tone="accent">earned</Chip> : <Chip>not yet</Chip>
                      ) : (
                        <span className="text-muted text-xs">n/a</span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {tripped ? (
                        <div className="flex flex-wrap items-center gap-2">
                          <Chip tone="critical">
                            <ShieldAlert className="h-3 w-3" /> until {fmtDateTime(r.breaker_until)}
                          </Chip>
                          {isAdmin && (
                            <button
                              type="button"
                              className="text-[11px] text-muted hover:text-fg underline underline-offset-2"
                              disabled={reset.isPending}
                              onClick={() => reset.mutate(r.rule_id)}
                            >
                              Reset
                            </button>
                          )}
                        </div>
                      ) : (
                        <span className="text-muted text-xs">closed</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </LoadState>
    </section>
  );
}

/** Breakers on the defaults (not a named rule) only show on the policy. */
export function DefaultsBreaker({ policy, isAdmin }: { policy: AutonomyPolicy; isAdmin: boolean }) {
  const qc = useQueryClient();
  const reset = useMutation({
    mutationFn: () => api.aiops.policy.resetBreaker(""),
    onSuccess: (p) => qc.setQueryData(["aiops-policy"], p),
  });
  const until = policy.breaker?.["__defaults__"];
  if (!until || new Date(until) <= new Date()) return null;
  return (
    <div className="rounded-lg border border-line bg-bg-card px-3 py-2 text-xs flex flex-wrap items-center gap-2">
      <Chip tone="critical">
        <ShieldAlert className="h-3 w-3" /> breaker tripped
      </Chip>
      Auto actions under the defaults are demoted to approve until {fmtDateTime(until)}.
      {isAdmin && (
        <button type="button" className={btnSecondary} disabled={reset.isPending} onClick={() => reset.mutate()}>
          <RotateCcw className="h-3.5 w-3.5" /> Reset breaker
        </button>
      )}
      <ErrorNote error={reset.error} />
    </div>
  );
}

// ── Version history ─────────────────────────────────────────────────────

export function PolicyHistory({
  activeVersion,
  onRestore,
}: {
  activeVersion: number;
  /** Load an old version into the editor (saving it creates a new version). */
  onRestore: (yaml: string, version: number) => void;
}) {
  const q = useQuery({ queryKey: ["aiops-policy-history"], queryFn: () => api.aiops.policy.history() });
  const [viewing, setViewing] = useState<number | null>(null);
  const rows = q.data ?? [];
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2 text-[15px] font-medium">
        <History className="h-4 w-4 text-accent" /> Version history
      </div>
      <LoadState
        query={q}
        empty={rows.length === 0}
        emptyTitle="No saved versions yet"
        emptyHint="The built-in default (every write needs one approval) is in effect until you save a policy."
      >
        <ul className="rounded-2xl border border-line bg-bg-card divide-y divide-line/60">
          {rows.map((v) => (
            <li key={v.version} className="px-4 py-2.5 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold tabular-nums">v{v.version}</span>
                {v.version === activeVersion && <Chip tone="accent">active</Chip>}
                <span className="text-xs text-muted">
                  {v.updated_by || "—"} · {fmtDateTime(v.updated_at)}
                </span>
                <span className="text-xs text-fg/80 flex-1 min-w-[8rem] truncate">{v.note}</span>
                <button
                  type="button"
                  className={btnSecondary}
                  onClick={() => setViewing(viewing === v.version ? null : v.version)}
                >
                  <Eye className="h-3.5 w-3.5" /> {viewing === v.version ? "Hide" : "View"}
                </button>
                {v.version !== activeVersion && (
                  <button type="button" className={btnSecondary} onClick={() => onRestore(v.yaml, v.version)}>
                    <RotateCcw className="h-3.5 w-3.5" /> Restore into editor
                  </button>
                )}
              </div>
              {viewing === v.version && (
                <pre className="mt-2 text-[12px] font-mono whitespace-pre-wrap bg-bg-elevated/60 border border-line rounded-lg p-3 max-h-[320px] overflow-auto">
                  {v.yaml}
                </pre>
              )}
            </li>
          ))}
        </ul>
      </LoadState>
    </section>
  );
}
