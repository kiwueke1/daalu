"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Save, SlidersHorizontal, Undo2 } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { LoadState } from "@/components/ui/load-state";
import {
  ErrorNote,
  PageHeader,
  btnPrimary,
  btnSecondary,
  fmtDateTime,
  inputCls,
  isStatus,
} from "@/components/aiops/ui";
import {
  DefaultsBreaker,
  KillSwitchCard,
  PolicyHistory,
  PolicySimulator,
  TrustTable,
} from "@/components/aiops/autonomy-parts";
import { STARTER_POLICIES } from "@/components/aiops/starter-policies";

/**
 * /autonomy — the autonomy policy as code: which writes the AI may make on
 * its own, which need approval (and from how many people), which are denied,
 * plus the guardrails, the kill switch and each rule's track record.
 */
export default function AutonomyPage() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const isAdmin = !!(user?.is_admin || user?.is_superuser);

  const q = useQuery({ queryKey: ["aiops-policy"], queryFn: () => api.aiops.policy.get() });
  const policy = q.data;

  const [draft, setDraft] = useState("");
  const [note, setNote] = useState("");
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null);
  const [restoredFrom, setRestoredFrom] = useState<number | null>(null);

  // Load the saved YAML into the editor once (and after each save), not on
  // every refetch, so a background refresh never clobbers an edit.
  useEffect(() => {
    if (policy && policy.version !== loadedVersion) {
      setDraft(policy.yaml);
      setLoadedVersion(policy.version);
    }
  }, [policy, loadedVersion]);

  const save = useMutation({
    mutationFn: () => api.aiops.policy.save(draft, note),
    onSuccess: (p) => {
      qc.setQueryData(["aiops-policy"], p);
      qc.invalidateQueries({ queryKey: ["aiops-policy-history"] });
      qc.invalidateQueries({ queryKey: ["aiops-trust"] });
      setNote("");
      setRestoredFrom(null);
    },
  });

  const dirty = !!policy && draft !== policy.yaml;

  return (
    <div className="space-y-6 max-w-[1200px]">
      <PageHeader icon={SlidersHorizontal} title="Autonomy policy">
        Decides, for every change the AI wants to make, whether it is denied,
        waits for approval, or runs on its own. Read-only tools always run.
        First matching rule wins; no match uses the defaults.
      </PageHeader>

      <LoadState query={q} loadingText="Loading the policy…">
        {policy && (
          <>
            <KillSwitchCard policy={policy} />
            <DefaultsBreaker policy={policy} isAdmin={isAdmin} />

            <div className="grid lg:grid-cols-[1.5fr_1fr] gap-4 items-start">
              <section className="rounded-2xl border border-line bg-bg-card p-4 lg:p-5 space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  <div className="text-[15px] font-medium flex-1 min-w-[10rem]">
                    {policy.version === 0 ? (
                      "Built-in default (not saved yet)"
                    ) : (
                      <>
                        Version {policy.version}
                        <span className="text-xs text-muted font-normal">
                          {" "}
                          · saved {fmtDateTime(policy.updated_at)}
                          {policy.note ? ` · ${policy.note}` : ""}
                        </span>
                      </>
                    )}
                  </div>
                  <select
                    aria-label="Starter policies"
                    value=""
                    onChange={(e) => {
                      const s = STARTER_POLICIES.find((p) => p.id === e.target.value);
                      if (s) {
                        setDraft(s.yaml);
                        setRestoredFrom(null);
                        if (!note) setNote(`Starter: ${s.label}`);
                      }
                    }}
                    className={inputCls}
                  >
                    <option value="">Starter policies…</option>
                    {STARTER_POLICIES.map((s) => (
                      <option key={s.id} value={s.id} title={s.description}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </div>
                {restoredFrom !== null && (
                  <div className="text-xs text-muted">
                    Loaded version {restoredFrom} into the editor. Saving it creates a new version.
                  </div>
                )}
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  spellCheck={false}
                  aria-label="Policy YAML"
                  rows={26}
                  className="w-full rounded-lg bg-bg-elevated/60 border border-line px-3 py-2 font-mono text-[12.5px] leading-relaxed focus:outline-none focus:border-accent/60 resize-y"
                />
                {save.error ? (
                  isStatus(save.error, 422) ? (
                    <ErrorNote error={save.error} prefix="The policy doesn't validate:" />
                  ) : (
                    <ErrorNote error={save.error} prefix="Couldn't save:" />
                  )
                ) : null}
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    maxLength={512}
                    placeholder="What changed and why (saved with the version)"
                    className={`${inputCls} flex-1 min-w-[14rem]`}
                  />
                  <button
                    type="button"
                    className={btnSecondary}
                    disabled={!dirty || save.isPending}
                    onClick={() => {
                      setDraft(policy.yaml);
                      setRestoredFrom(null);
                      save.reset();
                    }}
                  >
                    <Undo2 className="h-3.5 w-3.5" /> Discard
                  </button>
                  <button
                    type="button"
                    className={btnPrimary}
                    disabled={!dirty || save.isPending || !isAdmin}
                    title={isAdmin ? undefined : "Only a tenant admin can change the policy"}
                    onClick={() => save.mutate()}
                  >
                    {save.isPending ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Save className="h-3.5 w-3.5" />
                    )}
                    Save new version
                  </button>
                </div>
                {!isAdmin && (
                  <div className="text-xs text-muted">
                    Only a tenant admin can save the policy. You can still edit
                    the draft and test it.
                  </div>
                )}
              </section>

              <PolicySimulator draftYaml={draft} />
            </div>

            <TrustTable isAdmin={isAdmin} />

            <PolicyHistory
              activeVersion={policy.version}
              onRestore={(yaml, version) => {
                setDraft(yaml);
                setRestoredFrom(version);
                setNote(`Restore v${version}`);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            />
          </>
        )}
      </LoadState>
    </div>
  );
}
