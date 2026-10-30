"use client";

/**
 * Local-GPU onboarding, shared by Usage & Pricing and AI Factory.
 *
 * Deploys an open vLLM stack onto a GPU node in a cluster that is already
 * connected over the tunnel, and registers it as the tenant's SOVEREIGN
 * tier. The form discovers the GPU node, derives its class from the
 * hardware and suggests the model that fits the card, so connecting a GPU
 * is: pick the cluster, confirm what Daalu found, one click.
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  Cpu,
  CloudOff,
  Check,
  Loader2,
  Server,
  Plus,
  Trash2,
  AlertTriangle,
  X,
  Terminal,
  ChevronUp,
  ChevronDown,
} from "lucide-react";
import { api, type GpuCluster, type GpuServing } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ConfirmButton } from "@/components/ui/confirm-button";

/** SRE-agent models Daalu knows how to serve (core/model_catalog.py). */
export const SERVE_MODELS = [
  {
    id: "qwen/qwen3.6-35b-a3b",
    label: "Qwen3.6 35B-A3B (FP8) — open, for 48 GB cards",
    classes: ["ada-48"],
  },
  {
    id: "meta/llama-3.1-8b-instruct",
    label: "Llama 3.1 8B Instruct (AWQ) — fits 16 GB+ cards",
    classes: ["ada-16", "ada-48"],
  },
] as const;

/** The best model for a GPU class: the biggest one that fits. */
export const modelForClass = (gpuClass: string | null | undefined): string =>
  SERVE_MODELS.find((m) => gpuClass && (m.classes as readonly string[]).includes(gpuClass))?.id ??
  "meta/llama-3.1-8b-instruct";

// ── Local-GPU status pill ──────────────────────────────────────────────

export function LocalGpuPill({
  configured,
  healthy,
  model,
}: {
  configured: boolean;
  healthy: boolean;
  model: string;
}) {
  if (!configured) {
    return (
      <div className="text-xs text-muted border border-line rounded-full px-3 py-1.5 flex items-center gap-2">
        <CloudOff className="h-3.5 w-3.5" /> Local GPU not configured
      </div>
    );
  }
  const tone = healthy
    ? "border-accent-blue/40 text-[color:var(--text)] bg-accent-blue/15"
    : "border-signal-critical/40 text-[color:var(--critical)] bg-signal-critical/10";
  return (
    <div
      className={cn(
        "text-xs rounded-full px-3 py-1.5 flex items-center gap-2 border",
        tone
      )}
    >
      <Cpu className="h-3.5 w-3.5" />
      Local GPU {healthy ? "online" : "offline"} · {model || "—"}
    </div>
  );
}

// ── Local-GPU onboarding card ───────────────────────────────────────────
//
// Inline on Usage & Pricing (where the "not configured" banner lives).
// Deploys the vLLM stack onto a joined cluster over the tunnel and
// registers it as the tenant's SOVEREIGN tier. When already configured,
// shows status + a Disconnect control.

export function GpuOnboardingCard({
  defaultOpen = false,
  onDone,
}: {
  /** Open the form straight away (AI Factory's "Add GPU"). */
  defaultOpen?: boolean;
  /** Called after a deploy is accepted (202). */
  onDone?: () => void;
} = {}) {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["gpu", "status"],
    queryFn: api.onboarding.gpu.status,
    // Poll faster while a deploy is in flight.
    refetchInterval: (q) => {
      const s = (q.state.data as { state?: string } | undefined)?.state;
      return s === "provisioning" || s === "pending" ? 5_000 : 30_000;
    },
  });
  const clusters = useQuery({
    queryKey: ["gpu", "clusters"],
    queryFn: api.onboarding.gpu.clusters,
  });

  const [open, setOpen] = useState(defaultOpen);
  const [clusterId, setClusterId] = useState<string>("");
  const [baseUrl, setBaseUrl] = useState("");
  const [hfToken, setHfToken] = useState("");
  const [model, setModel] = useState("meta/llama-3.1-8b-instruct");
  const [gpuClass, setGpuClass] = useState("ada-16");
  const [gpuNode, setGpuNode] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Best-effort GPU node discovery on the selected cluster — pre-fills
  // the gpu-class so the user doesn't have to know their node labels.
  const nodes = useQuery({
    queryKey: ["gpu", "nodes", clusterId],
    queryFn: () => api.onboarding.gpu.nodes(clusterId || null),
    enabled: open,
  });
  const detected = nodes.data ?? [];
  // Discovery answered and found nothing: deploying would leave a pod
  // Pending on FailedScheduling (e.g. the laptop kind cluster from S1-02).
  const noGpu = nodes.isSuccess && !nodes.isFetching && detected.length === 0;

  const onPickCluster = (id: string) => {
    setClusterId(id);
    setGpuNode(null);
    const c = clusters.data?.find((x) => x.cluster_tunnel_id === id);
    setBaseUrl(c ? c.suggested_base_url : "");
  };

  // Use what Daalu found: a node + the class it derived from the card's
  // memory, and the model that fits that class.
  const pickNode = (n: { name: string; gpu_class?: string | null }) => {
    setGpuNode(n.name);
    if (n.gpu_class) {
      setGpuClass(n.gpu_class);
      setModel(modelForClass(n.gpu_class));
    }
  };

  // Exactly one GPU node on the cluster: pre-select it.
  useEffect(() => {
    if (detected.length === 1 && gpuNode === null) pickNode(detected[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detected.length, clusterId]);

  const provision = useMutation({
    mutationFn: () =>
      api.onboarding.gpu.provision({
        target_cluster_tunnel_id: clusterId || null,
        base_url: baseUrl || null,
        hf_token: hfToken || null,
        model_classifier: model,
        gpu_class: gpuClass,
        gpu_node: gpuNode,
      }),
    onMutate: () => setErr(null),
    onSuccess: () => {
      setOpen(false);
      setHfToken("");
      onDone?.();
      qc.invalidateQueries({ queryKey: ["gpu"] });
      qc.invalidateQueries({ queryKey: ["billing", "localGpu"] });
    },
    onError: (e: unknown) =>
      setErr(e instanceof Error ? e.message : "provisioning failed"),
  });

  const remove = useMutation({
    mutationFn: api.onboarding.gpu.remove,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["gpu"] });
      qc.invalidateQueries({ queryKey: ["billing", "localGpu"] });
      // AI Factory polls every 15–30 s; without this the removed card
      // lingers on screen after a successful Disconnect.
      qc.invalidateQueries({ queryKey: ["aiFactory"] });
    },
  });

  const s = status.data;
  const configured = s?.configured ?? false;
  const deploying =
    provision.isPending || s?.state === "provisioning" || s?.state === "pending";

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="flex items-start gap-3">
          <span className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-accent-blue/15 text-accent-blue shrink-0">
            <Server className="h-4 w-4" />
          </span>
          <div>
            <div className="text-sm font-semibold">Local GPU</div>
            <p className="text-xs text-muted mt-0.5 max-w-[560px]">
              Run inference on your own NVIDIA GPU over the secure tunnel.
              Daalu deploys an open model
              {s?.model_classifier ? (
                <>
                  {" "}(currently{" "}
                  <span className="font-mono text-fg/80 break-all">
                    {s.model_classifier}
                  </span>
                  )
                </>
              ) : null}{" "}
              onto a connected cluster and routes the hot path there: the
              cheapest, most private tier.
            </p>
          </div>
        </div>
        {configured ? (
          <ConfirmButton
            label="Disconnect GPU…"
            confirmLabel="Disconnect GPU"
            consequence="Inference falls back to external models, billed at external rates."
            onConfirm={() => remove.mutate()}
            pending={remove.isPending}
            icon={<Trash2 className="h-3.5 w-3.5" />}
            tone="danger"
          />
        ) : (
          !open && (
            <button
              type="button"
              onClick={() => setOpen(true)}
              className="text-xs h-8 px-3 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base inline-flex items-center gap-1.5"
            >
              <Plus className="h-3.5 w-3.5" /> Connect your GPU
            </button>
          )
        )}
      </div>

      {/* Configured status line */}
      {configured && (
        <div className="mt-4 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <span
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 border",
              s?.healthy
                ? "border-accent-blue/40 text-[color:var(--text)] bg-accent-blue/15"
                : "border-signal-critical/40 text-[color:var(--critical)] bg-signal-critical/10"
            )}
          >
            <Cpu className="h-3.5 w-3.5" />
            {deploying
              ? "Deploying…"
              : s?.healthy
                ? "Online"
                : "Offline"}
          </span>
          <span className="min-w-0 break-all text-muted font-mono">{s?.base_url}</span>
          <span className="min-w-0 break-all text-muted">· {s?.model_classifier}</span>
        </div>
      )}
      {configured && s?.last_error && !s.healthy && (
        <div className="mt-2 text-[11px] text-[color:var(--critical)] flex items-center gap-1.5">
          <AlertTriangle className="h-3 w-3" /> {s.last_error}
        </div>
      )}

      {/* Live deploy progress. Offered whenever the stack isn't healthy —
          during a first boot (the long case) and also afterwards, when the
          events/logs are how you find out why it went unhealthy. */}
      {configured && (deploying || !s?.healthy) && <GpuProgressPanel />}

      {/* Onboarding form */}
      {open && !configured && (
        <div className="mt-4 rounded-lg border border-line bg-bg-base/40 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">
              Connect a GPU
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-muted hover:text-[color:var(--text)]"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>

          <label className="block text-xs">
            <span className="text-muted">Cluster (joined over WireGuard)</span>
            <select
              value={clusterId}
              onChange={(e) => onPickCluster(e.target.value)}
              className="mt-1 w-full rounded-md border border-line bg-bg-base px-2 py-1.5 text-xs"
            >
              <option value="">Operator cluster (default)</option>
              {(clusters.data ?? []).map((c: GpuCluster) => (
                <option key={c.cluster_tunnel_id} value={c.cluster_tunnel_id}>
                  {c.name} ({c.tunnel_ip})
                </option>
              ))}
            </select>
            {clusters.data && clusters.data.length === 0 && (
              <span className="text-[11px] text-muted">
                No connected clusters — join one under Managed infra, or
                enter a reachable vLLM URL below.
              </span>
            )}
          </label>

          {noGpu && (
            <div className="flex items-start gap-1.5 text-[11px] text-accent-amber">
              <AlertTriangle className="h-3 w-3 mt-0.5 shrink-0" />
              No GPU node found on this cluster, so there is nowhere to run the
              model. Pick the cluster your GPU node is in.
            </div>
          )}

          {/* Detected GPU nodes on the selected cluster. */}
          {detected.length > 0 && (
            <div className="text-[11px] text-muted">
              Detected GPU{detected.length === 1 ? "" : "s"}:{" "}
              {detected.map((n) => (
                <button
                  key={n.name}
                  type="button"
                  onClick={() => pickNode(n)}
                  className={cn(
                    "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 mr-1.5 font-mono",
                    n.name === gpuNode
                      ? "border-accent-blue/50 text-[color:var(--text)]"
                      : "border-line"
                  )}
                  title="Deploy onto this node (its class is derived from the GPU's memory)"
                >
                  <Cpu className="h-3 w-3" /> {n.name}
                  {n.gpu_class ? ` · ${n.gpu_class}` : ""} ·{" "}
                  {n.allocatable_gpus}× {n.ready ? "" : "(not ready)"}
                </button>
              ))}
            </div>
          )}

          <label className="block text-xs">
            <span className="text-muted">GPU class (node label)</span>
            <input
              value={gpuClass}
              onChange={(e) => setGpuClass(e.target.value)}
              className="mt-1 w-full rounded-md border border-line bg-bg-base px-2 py-1.5 text-xs font-mono"
            />
          </label>

          <label className="block text-xs">
            <span className="text-muted">vLLM endpoint URL</span>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://10.200.0.2:8000/v1"
              className="mt-1 w-full rounded-md border border-line bg-bg-base px-2 py-1.5 text-xs font-mono"
            />
          </label>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label className="block text-xs">
              <span className="text-muted">Hugging Face token (only for gated models)</span>
              <input
                value={hfToken}
                onChange={(e) => setHfToken(e.target.value)}
                type="password"
                placeholder="hf_… (optional if cached)"
                className="mt-1 w-full rounded-md border border-line bg-bg-base px-2 py-1.5 text-xs"
              />
            </label>
            <label className="block text-xs">
              <span className="text-muted">Model to serve</span>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="mt-1 w-full rounded-md border border-line bg-bg-base px-2 py-1.5 text-xs"
              >
                {SERVE_MODELS.map((m) => (
                  <option
                    key={m.id}
                    value={m.id}
                    disabled={!(m.classes as readonly string[]).includes(gpuClass)}
                  >
                    {m.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {err && (
            <div className="text-[11px] text-[color:var(--critical)] flex items-center gap-1.5">
              <AlertTriangle className="h-3 w-3" /> {err}
            </div>
          )}

          <div className="flex items-center gap-2 pt-1">
            <button
              type="button"
              disabled={provision.isPending || noGpu}
              title={noGpu ? "No GPU node on this cluster" : undefined}
              onClick={() => provision.mutate()}
              className="text-xs h-8 px-3 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base inline-flex items-center gap-1.5 disabled:opacity-60"
            >
              {provision.isPending ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Deploying
                  vLLM… (this can take a few minutes)
                </>
              ) : (
                <>
                  <Check className="h-3.5 w-3.5" /> Deploy &amp; connect
                </>
              )}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

// ── Deploy progress panel ───────────────────────────────────────────────
//
// "Deploying…" covers three sequential multi-minute waits that look identical
// from outside: pulling the ~10 GB vLLM image, pulling ~5 GB of weights, then
// compiling CUDA graphs. Before a container starts there are no logs at all —
// kubelet events are the only signal — so this shows events first and folds in
// vLLM's own logs once they exist.

export function GpuProgressPanel() {
  const [open, setOpen] = useState(false);

  const progress = useQuery({
    queryKey: ["billing", "gpuProgress"],
    queryFn: api.onboarding.gpu.progress,
    // Only poll while the panel is open — this reaches through the tunnel to
    // the customer's apiserver, so it shouldn't run behind a collapsed panel.
    enabled: open,
    refetchInterval: open ? 5_000 : false,
  });

  const p = progress.data;
  const pod = p?.pods?.[0];
  const events = p?.events ?? [];
  const logs = pod?.logs?.trim();
  // The reason a container isn't up yet (ImagePullBackOff, CrashLoopBackOff…)
  // is the single most useful line here, so promote it out of the pod blob.
  const blocked = pod?.containers?.find((c) => !c.ready && c.reason);

  return (
    <div className="mt-3 rounded-lg border border-line bg-bg-base/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs"
      >
        <span className="flex items-center gap-2">
          <Terminal className="h-3.5 w-3.5 text-muted" />
          <span className="font-semibold uppercase tracking-wider text-muted">
            Deploy progress
          </span>
          {pod?.phase && (
            <span className="font-mono text-[11px] text-muted">
              {pod.phase}
              {blocked?.reason ? ` · ${blocked.reason}` : ""}
            </span>
          )}
        </span>
        {open ? (
          <ChevronUp className="h-3.5 w-3.5 text-muted" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 text-muted" />
        )}
      </button>

      {open && (
        <div className="space-y-3 border-t border-line px-3 py-3">
          {progress.isLoading && (
            <div className="flex items-center gap-2 text-[11px] text-muted">
              <Loader2 className="h-3 w-3 animate-spin" /> Reading cluster state…
            </div>
          )}

          {!progress.isLoading && !pod && events.length === 0 && (
            <div className="text-[11px] text-muted">
              No pod yet — the controller applies the stack on its next
              reconcile tick (~30 s).
            </div>
          )}

          {pod && (
            <div className="text-[11px] text-muted">
              <span className="font-mono">{pod.name}</span>
              {pod.node ? ` on ${pod.node}` : ""}
              {pod.containers?.map((c) => (
                <span key={c.name} className="ml-2">
                  · {c.name} {c.ready ? "ready" : (c.reason ?? "starting")}
                  {c.restarts > 0 ? ` (${c.restarts} restarts)` : ""}
                </span>
              ))}
            </div>
          )}

          {blocked?.message && (
            <div className="rounded border border-signal-critical/40 bg-signal-critical/10 px-2 py-1.5 text-[11px] text-[color:var(--critical)]">
              {blocked.message}
            </div>
          )}

          {events.length > 0 && (
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">
                Events
              </div>
              <div className="max-h-48 overflow-y-auto rounded border border-line bg-bg-elevated/40">
                {events.map((e, i) => (
                  <div
                    key={`${e.at}-${i}`}
                    className="flex gap-2 border-b border-line/50 px-2 py-1 font-mono text-[11px] last:border-0"
                  >
                    <span
                      className={cn(
                        "shrink-0",
                        e.type === "Warning"
                          ? "text-[color:var(--critical)]"
                          : "text-muted"
                      )}
                    >
                      {e.reason}
                    </span>
                    <span className="text-[color:var(--text)] break-all">
                      {e.message}
                      {e.count > 1 ? ` (×${e.count})` : ""}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {logs && (
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">
                vLLM logs
              </div>
              <pre className="max-h-64 overflow-auto rounded border border-line bg-bg-elevated/40 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-[color:var(--text)]">
                {logs}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Disconnect, on the GPU's own page in AI Factory. Shown only on the card
 * the tenant's model was deployed onto (status.gpu_node == the card's host);
 * a record from before gpu_node was sent matches any card.
 */
export function DisconnectGpuButton({ hostname }: { hostname: string }) {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["gpu", "status"],
    queryFn: api.onboarding.gpu.status,
  });
  const remove = useMutation({
    mutationFn: api.onboarding.gpu.remove,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["gpu"] });
      qc.invalidateQueries({ queryKey: ["billing", "localGpu"] });
      qc.invalidateQueries({ queryKey: ["aiFactory"] });
    },
  });
  const s = status.data;
  if (!s?.configured) return null;
  if (s.gpu_node && s.gpu_node !== hostname) return null;
  return (
    <ConfirmButton
      label="Disconnect GPU…"
      confirmLabel="Disconnect GPU"
      consequence={`Removes ${s.model_classifier ?? "the model"} from this GPU. Inference falls back to external models, billed at external rates.`}
      onConfirm={() => remove.mutate()}
      pending={remove.isPending}
      icon={<Trash2 className="h-3.5 w-3.5" />}
      tone="danger"
    />
  );
}

/**
 * "Serving on this GPU": what Daalu installed on the card, on the card's own
 * page in AI Factory — model, runtime, where it runs, since when — with
 * Disconnect beside it. Shown only on the card the model was deployed onto
 * (a record without gpu_node, from older forms, matches any card).
 */
export function GpuServingPanel({
  hostname,
  canManage,
}: {
  hostname: string;
  canManage: boolean;
}) {
  const serving = useQuery({
    queryKey: ["gpu", "serving"],
    queryFn: api.onboarding.gpu.serving,
    refetchInterval: (q) =>
      q.state.data && q.state.data.state !== "active" ? 5_000 : 30_000,
  });
  const s = serving.data;
  if (!s || (s.node && s.node !== hostname)) return null;

  const online = s.state === "active" && s.healthy;
  const rows: { label: string; value: React.ReactNode; mono?: boolean }[] = [
    {
      label: "Model",
      value: (
        <>
          {s.hf_model}
          {s.open_weights && (
            <span className="ml-2 rounded-full border border-accent-emerald/40 px-1.5 py-px text-[10px] text-accent-emerald">
              open weights
            </span>
          )}
        </>
      ),
      mono: true,
    },
    { label: "Served as", value: s.model, mono: true },
    {
      label: "Runtime",
      value: `${s.engine === "vllm" ? "vLLM" : s.engine} ${s.image.split(":")[1] ?? ""} · ${s.quantization.toUpperCase()}`,
    },
    {
      label: "Resources",
      value: `${Math.round(s.gpu_memory_utilization * 100)}% of GPU memory · ${Math.round(s.context_len / 1024)}k context${s.tool_calling ? " · tool calling" : ""}`,
    },
    {
      label: "Runs in",
      value: [s.cluster_name ?? "operator cluster", s.node, `namespace ${s.namespace}`]
        .filter(Boolean)
        .join(" · "),
    },
    {
      label: "Node label",
      value: `gpu-class=${s.gpu_class} (stamped by Daalu)`,
      mono: true,
    },
    ...(s.endpoint ? [{ label: "Endpoint", value: s.endpoint, mono: true }] : []),
  ];

  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm font-semibold flex items-center gap-2">
            <Server className="h-4 w-4 text-accent-emerald" /> Serving on this GPU
          </div>
          <div className="text-[11px] text-muted mt-0.5">
            Deployed by Daalu. Your AI requests are answered by this model, on
            this card.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "text-[11px] rounded-full px-2.5 py-1 border inline-flex items-center gap-1.5",
              online
                ? "border-accent-emerald/40 text-accent-emerald"
                : "border-line text-muted"
            )}
          >
            {online ? (
              <Check className="h-3 w-3" />
            ) : (
              <Loader2 className="h-3 w-3 animate-spin" />
            )}
            {online
              ? `Online${s.online_since ? ` since ${new Date(s.online_since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}`
              : "Deploying…"}
          </span>
          {canManage && <DisconnectGpuButton hostname={hostname} />}
        </div>
      </div>
      <dl className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2.5">
        {rows.map((r) => (
          <div key={r.label} className="flex gap-3 text-xs min-w-0">
            <dt className="w-24 shrink-0 text-muted">{r.label}</dt>
            <dd className={cn("min-w-0 break-all", r.mono && "font-mono text-[11px]")}>
              {r.value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
