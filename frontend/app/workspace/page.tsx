"use client";

/**
 * /workspace — the browser-IDE landing page.
 *
 * Three states the page renders for, decided by the API responses:
 *
 *  1. Tenant not eligible → an explainer + CTA pointing at the right
 *     onboarding step (request hosted enrolment, or wire a SOVEREIGN
 *     GPU). No create-form rendered.
 *  2. Tenant eligible, no workspace yet → the create form. Profile +
 *     optional git repo URL. Submit POSTs /ide/sessions and redirects.
 *  3. Tenant eligible, workspace exists → state card + "Open IDE"
 *     button + "Delete" button. Polls every 5s while in provisioning
 *     so the user sees the transition to active without a refresh.
 */

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoadState } from "@/components/ui/load-state";
import {
  ArrowRight,
  Bot,
  Check,
  Code2,
  Copy,
  HardDrive,
  KeyRound,
  Loader2,
  Lock,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Terminal,
  Trash2,
} from "lucide-react";
import { api, type WorkspaceView, type CodingModelView } from "@/lib/api";

export default function WorkspacePage() {
  const qc = useQueryClient();

  const eligibility = useQuery({
    queryKey: ["ide-eligibility"],
    queryFn: () => api.ide.eligibility(),
  });
  const session = useQuery({
    queryKey: ["ide-session"],
    queryFn: () => api.ide.me(),
    enabled: eligibility.data?.eligible === true,
    // While the pod is provisioning, the controller flips to "active"
    // only once the Deployment has ready replicas — poll so the UI
    // moves without a refresh.
    refetchInterval: (q) => {
      const d = q.state.data as WorkspaceView | null | undefined;
      return d?.state === "provisioning" ? 5000 : false;
    },
  });

  return (
    <div className="space-y-6 max-w-3xl">
      <header>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <Code2 className="h-5 w-5 text-accent-cyan" /> Workspace
        </h1>
        <p className="text-muted text-sm mt-1">
          A browser-hosted VS Code, pre-wired to your inference tier so
          the assistant inside it runs on your GPU (or the Daalu-hosted
          pool) — never the public internet. Code prompts never leave
          your perimeter.
        </p>
      </header>

      {eligibility.isLoading && (
        <div className="surface p-4 text-sm text-muted flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin" /> Checking eligibility…
        </div>
      )}

      {eligibility.isError && (
        <LoadState query={eligibility}>{null}</LoadState>
      )}

      {eligibility.data && !eligibility.data.eligible && (
        <NotEligibleCard reason={eligibility.data.reason} />
      )}

      {eligibility.data?.eligible && (
        <>
          {session.isLoading && (
            <div className="surface p-4 text-sm text-muted flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading workspace…
            </div>
          )}
          {session.data && (
            <ExistingWorkspaceCard
              workspace={session.data}
              onDestroyed={() => {
                qc.invalidateQueries({ queryKey: ["ide-session"] });
              }}
            />
          )}
          {session.data === null && (
            <CreateWorkspaceForm
              onCreated={() => {
                qc.invalidateQueries({ queryKey: ["ide-session"] });
              }}
            />
          )}
          <TerminalCard />
        </>
      )}

      <FeatureHints />
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Sub-views
// ────────────────────────────────────────────────────────────────────────

/**
 * The eligibility probe returns the backend's error text, which is written
 * for operators (kubectl pipelines, flag names). Turn it into a plain
 * sentence and keep the raw text one click away.
 */
function explainIneligible(reason: string): {
  title: string;
  sentence: string;
  options: React.ReactNode[];
} {
  const r = reason.toLowerCase();
  if (r.includes("no coding model")) {
    return {
      title: "No coding model is running yet",
      sentence:
        "Your tenant can use Workspace, but the coding model it talks to isn't deployed on your cluster, so there is nothing to answer the assistant's prompts.",
      options: [
        <>Ask your operator to deploy the coding model on your GPU cluster.</>,
        <>Then come back here to create your workspace.</>,
      ],
    };
  }
  return {
    title: "Workspace isn't enabled for your tenant",
    sentence:
      "Workspace only runs on an AI tier that keeps your code off the public internet, and your tenant doesn't have one yet.",
    options: [
      <>
        Connect your own <strong>GPU</strong> (sovereign tier) to Daalu, or
      </>,
      <>
        Opt into the <strong>Daalu-hosted</strong> AI tier (talk to your
        account contact), or
      </>,
      <>Ask your Daalu operator to turn Workspace on for your tenant.</>,
    ],
  };
}

function NotEligibleCard({ reason }: { reason: string }) {
  const { title, sentence, options } = explainIneligible(reason || "");
  return (
    <section className="surface p-5 space-y-3">
      <div className="flex items-start gap-3">
        <div
          className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0"
          style={{
            background: "color-mix(in srgb, var(--warning) 14%, transparent)",
          }}
        >
          <Lock className="h-4 w-4" style={{ color: "var(--warning)" }} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-base font-semibold">{title}</div>
          <p className="text-[12.5px] text-muted mt-1 leading-relaxed">{sentence}</p>
          <ul className="text-[12.5px] text-muted mt-3 space-y-1.5 list-disc list-inside">
            {options.map((o, i) => (
              <li key={i}>{o}</li>
            ))}
          </ul>
          {reason && (
            <details className="mt-3 text-[11.5px] text-muted">
              <summary className="cursor-pointer hover:text-fg">
                Technical details
              </summary>
              <pre className="mt-1.5 whitespace-pre-wrap break-words rounded-lg bg-bg-elevated p-2.5 font-mono text-[11px]">
                {reason}
              </pre>
            </details>
          )}
        </div>
      </div>
      <div className="pt-2 border-t border-line/40 flex justify-end">
        <Link
          href="/billing"
          className="text-xs h-9 px-4 rounded-lg border border-line text-muted hover:text-fg inline-flex items-center gap-1.5"
        >
          Manage AI tier <ArrowRight className="h-3.5 w-3.5" />
        </Link>
      </div>
    </section>
  );
}

function ExistingWorkspaceCard({
  workspace,
  onDestroyed,
}: {
  workspace: WorkspaceView;
  onDestroyed: () => void;
}) {
  const destroy = useMutation({
    mutationFn: () => api.ide.destroy(),
    onSuccess: onDestroyed,
  });
  const isProvisioning = workspace.state === "provisioning";
  const isActive = workspace.state === "active";
  return (
    <section className="surface p-5 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-[11px] uppercase tracking-wider text-muted">
            Your workspace · {workspace.profile} · {workspace.model}
          </div>
          <div className="text-base font-semibold mt-1 flex items-center gap-2">
            <StateBadge state={workspace.state} />
            {isProvisioning && (
              <span className="text-[12px] text-muted font-normal">
                first boot can take ~60s while the image pulls
              </span>
            )}
          </div>
          {workspace.git_repo_url && (
            <div className="text-[12px] text-muted mt-2 flex items-center gap-1.5 flex-wrap">
              <span>
                Cloned from{" "}
                <code className="text-[11px]">{workspace.git_repo_url}</code> on{" "}
                <code className="text-[11px]">{workspace.git_branch}</code>
              </span>
              {workspace.git_authenticated && (
                <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider rounded px-1.5 py-0.5 bg-accent-emerald/15 text-[color:var(--accent-emerald)]">
                  <ShieldCheck className="h-3 w-3" /> Private
                </span>
              )}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={() => {
            if (
              confirm(
                "Delete this workspace? Your /workspace folder (and its 5/20/50 GiB PVC) " +
                  "will be released. Push any in-flight changes first.",
              )
            ) {
              destroy.mutate();
            }
          }}
          disabled={destroy.isPending}
          className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--critical)] disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          <Trash2 className="h-3.5 w-3.5" /> Delete
        </button>
      </div>

      {isActive && (
        <div className="flex flex-wrap gap-2 pt-2 border-t border-line/40">
          <a
            href={workspace.ide_url}
            target="_blank"
            rel="noreferrer"
            className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base inline-flex items-center gap-1.5"
          >
            <Code2 className="h-3.5 w-3.5" /> Open IDE
          </a>
          <div className="text-[11px] text-muted self-center">
            Opens in a new tab. The Daalu Assistant lives in the activity
            bar — or run <code className="text-[10.5px]">daalu chat</code>{" "}
            in the terminal for a CLI agent.
          </div>
        </div>
      )}
    </section>
  );
}

function StateBadge({ state }: { state: WorkspaceView["state"] }) {
  const map: Record<WorkspaceView["state"], { label: string; cls: string }> = {
    provisioning: {
      label: "Provisioning",
      cls: "bg-accent-amber/15 text-[color:var(--accent-amber)]",
    },
    active: {
      label: "Active",
      cls: "bg-accent-emerald/15 text-[color:var(--accent-emerald)]",
    },
    paused: {
      label: "Paused (idle)",
      cls: "bg-bg-elevated text-muted",
    },
    destroyed: {
      label: "Destroyed",
      cls: "bg-bg-elevated text-muted",
    },
    error: {
      label: "Error",
      cls: "bg-signal-critical/15 text-[color:var(--critical)]",
    },
  };
  const m = map[state];
  return (
    <span
      className={`inline-flex items-center text-[10px] uppercase tracking-wider rounded px-1.5 py-0.5 ${m.cls}`}
    >
      {m.label}
    </span>
  );
}

function CreateWorkspaceForm({ onCreated }: { onCreated: () => void }) {
  const [profile, setProfile] = useState<"small" | "medium" | "large">("small");
  const [model, setModel] = useState<string>("");
  const [gitRepoUrl, setGitRepoUrl] = useState("");
  const [gitBranch, setGitBranch] = useState("main");
  const [gitToken, setGitToken] = useState("");

  const models = useQuery({
    queryKey: ["ide-models"],
    queryFn: () => api.ide.models(),
  });
  // Default-select the catalog default (or the first available one) once
  // the list loads and the user hasn't picked yet.
  const list = models.data ?? [];
  const effectiveModel =
    model ||
    list.find((m) => m.default && m.available)?.id ||
    list.find((m) => m.available)?.id ||
    "";

  const create = useMutation({
    mutationFn: () =>
      api.ide.create({
        profile,
        model: effectiveModel || null,
        git_repo_url: gitRepoUrl.trim() || null,
        git_branch: gitBranch.trim() || "main",
        // Only send a token when both a repo and a token are present.
        git_token: gitRepoUrl.trim() && gitToken.trim() ? gitToken.trim() : null,
      }),
    onSuccess: () => onCreated(),
  });
  const noneAvailable = list.length > 0 && !list.some((m) => m.available);

  return (
    <section className="surface p-5 space-y-4">
      <div>
        <div className="text-[11px] uppercase tracking-wider text-muted">
          First-time setup
        </div>
        <h2 className="text-base font-semibold mt-1">
          Provision your workspace
        </h2>
        <p className="text-[12.5px] text-muted mt-1 leading-relaxed">
          One workspace per user. Your files live on a persistent volume
          and survive pauses; idle workspaces are reaped after 14 days.
        </p>
      </div>

      <div className="space-y-3">
        <label className="block text-xs space-y-1">
          <span className="text-muted uppercase tracking-wider">
            Coding model
          </span>
          <select
            value={effectiveModel}
            onChange={(e) => setModel(e.target.value)}
            disabled={models.isLoading || noneAvailable}
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm disabled:opacity-50"
          >
            {models.isLoading && <option>Loading models…</option>}
            {list.map((m) => (
              <option key={m.id} value={m.id} disabled={!m.available}>
                {m.label}
                {m.available ? "" : `  — requires RTX 6000 Ada (coming soon)`}
              </option>
            ))}
          </select>
          <span className="text-[10px] text-muted">
            {(() => {
              const sel = list.find((m) => m.id === effectiveModel);
              if (sel) return sel.description;
              if (noneAvailable)
                return "No coding model is deployed yet — ask your operator to bring a GPU online.";
              return "The assistant inside your workspace runs on this model, on your GPU.";
            })()}
          </span>
        </label>

        <label className="block text-xs space-y-1">
          <span className="text-muted uppercase tracking-wider">Profile</span>
          <select
            value={profile}
            onChange={(e) =>
              setProfile(e.target.value as "small" | "medium" | "large")
            }
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          >
            <option value="small">
              Small — 0.5/2 CPU · 1/4 GiB RAM · 5 GiB disk
            </option>
            <option value="medium">
              Medium — 1/4 CPU · 4/8 GiB RAM · 20 GiB disk
            </option>
            <option value="large">
              Large — 2/8 CPU · 8/16 GiB RAM · 50 GiB disk
            </option>
          </select>
          <span className="text-[10px] text-muted">
            Pick small unless you know you need more. Operator may cap your
            tenant's max profile.
          </span>
        </label>

        <label className="block text-xs space-y-1">
          <span className="text-muted uppercase tracking-wider">
            Git repo URL (optional)
          </span>
          <input
            type="url"
            value={gitRepoUrl}
            onChange={(e) => setGitRepoUrl(e.target.value)}
            placeholder="https://github.com/acme/infra.git"
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          />
          <span className="text-[10px] text-muted">
            HTTPS URL of a GitHub, GitLab, or self-hosted repo. If set, your
            workspace is seeded with a fresh clone on first boot. Leave blank
            to start empty — you can drag-and-drop files into the IDE later,
            or git clone from a terminal once it&apos;s open.
          </span>
        </label>

        {gitRepoUrl.trim() && (
          <label className="block text-xs space-y-1">
            <span className="text-muted uppercase tracking-wider flex items-center gap-1.5">
              <KeyRound className="h-3 w-3" /> Access token (private repos)
            </span>
            <input
              type="password"
              value={gitToken}
              onChange={(e) => setGitToken(e.target.value)}
              autoComplete="off"
              placeholder="ghp_… / glpat-… (leave blank for public repos)"
              className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm font-mono"
            />
            <span className="text-[10px] text-muted">
              A personal-access-token with <code className="text-[10px]">repo</code>{" "}
              (GitHub) or <code className="text-[10px]">read_repository</code>{" "}
              (GitLab) scope. Stored{" "}
              <strong className="text-[color:var(--text)]">encrypted</strong>,
              mounted into the clone as an in-memory credential, and never
              written into your repo&apos;s git config. Required only for
              private repos.
            </span>
          </label>
        )}

        <label className="block text-xs space-y-1">
          <span className="text-muted uppercase tracking-wider">
            Git branch
          </span>
          <input
            value={gitBranch}
            onChange={(e) => setGitBranch(e.target.value)}
            placeholder="main"
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          />
        </label>
      </div>

      <div className="flex justify-end pt-2 border-t border-line/40">
        <button
          type="button"
          onClick={() => create.mutate()}
          disabled={create.isPending || noneAvailable || !effectiveModel}
          className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          {create.isPending ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Provisioning…
            </>
          ) : (
            <>
              <Sparkles className="h-3.5 w-3.5" /> Create workspace
            </>
          )}
        </button>
      </div>

      {create.isError && (
        <div className="text-[11.5px] text-[color:var(--critical)]">
          {String(create.error)}
        </div>
      )}
    </section>
  );
}

// ────────────────────────────────────────────────────────────────────────
// "Use Daalu from your terminal" — the CLI surface
// ────────────────────────────────────────────────────────────────────────

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard blocked — the user can still select the text */
        }
      }}
      className="shrink-0 text-[10.5px] h-7 px-2 rounded-md border border-line text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1"
    >
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      {copied ? "Copied" : label}
    </button>
  );
}

function CommandLine({ cmd }: { cmd: string }) {
  return (
    <div className="flex items-center gap-2 bg-bg-elevated/60 border border-line rounded-lg px-3 h-9">
      <code className="text-[11.5px] font-mono flex-1 overflow-x-auto whitespace-nowrap">
        {cmd}
      </code>
      <CopyButton text={cmd} />
    </div>
  );
}

const INSTALL_TABS: { id: string; label: string; cmd: string }[] = [
  { id: "curl", label: "Linux / macOS", cmd: "curl -fsSL https://get.daalu.io/install.sh | sh" },
  { id: "brew", label: "Homebrew", cmd: "brew install daalu-io/tap/daalu" },
  { id: "pipx", label: "pipx", cmd: "pipx install daalu-cli" },
  { id: "ps", label: "Windows", cmd: "irm https://get.daalu.io/install.ps1 | iex" },
];

function TerminalCard() {
  const [tab, setTab] = useState(INSTALL_TABS[0].id);
  const active = INSTALL_TABS.find((t) => t.id === tab) ?? INSTALL_TABS[0];

  const loginCmd = useMutation({
    mutationFn: () => api.cli.loginCommand(),
  });

  return (
    <section className="surface p-5 space-y-4">
      <div className="flex items-start gap-3">
        <div
          className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0"
          style={{
            background: "color-mix(in srgb, var(--accent-cyan) 14%, transparent)",
          }}
        >
          <Terminal className="h-4 w-4 text-[color:var(--accent-cyan)]" />
        </div>
        <div className="flex-1">
          <h2 className="text-base font-semibold">Use Daalu from your terminal</h2>
          <p className="text-[12.5px] text-muted mt-1 leading-relaxed">
            Prefer your own editor? Install the <code className="text-[11px]">daalu</code>{" "}
            CLI and run the same coding agent in any project on your machine —
            it edits the files that are already on disk, across as many repos as
            you like. Same sovereign inference; your code never leaves the
            perimeter.
          </p>
        </div>
      </div>

      {/* Step 1 — install */}
      <div className="space-y-2">
        <div className="text-[11px] uppercase tracking-wider text-muted">
          1 · Install
        </div>
        <div className="flex flex-wrap gap-1.5">
          {INSTALL_TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={`text-[10.5px] h-7 px-2.5 rounded-md border ${
                t.id === tab
                  ? "border-[color:var(--accent-cyan)] text-[color:var(--text)]"
                  : "border-line text-muted hover:text-[color:var(--text)]"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <CommandLine cmd={active.cmd} />
      </div>

      {/* Step 2 — log in */}
      <div className="space-y-2">
        <div className="text-[11px] uppercase tracking-wider text-muted">
          2 · Log in
        </div>
        <CommandLine cmd="daalu login" />
        <p className="text-[11px] text-muted">
          Opens your browser to approve this machine, then you&apos;re signed in.
          Or skip the browser — generate a one-time command tied to your account:
        </p>
        {loginCmd.data ? (
          <div className="space-y-1.5">
            <CommandLine cmd={loginCmd.data.command} />
            <p className="text-[10.5px] text-muted">
              One-time, expires in {Math.round(loginCmd.data.expires_in / 60)}{" "}
              minutes. Paste it into your terminal to log in instantly.
            </p>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => loginCmd.mutate()}
            disabled={loginCmd.isPending}
            className="text-xs h-9 px-4 rounded-lg border border-line text-muted hover:text-[color:var(--text)] disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            {loginCmd.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Generating…
              </>
            ) : (
              <>
                <KeyRound className="h-3.5 w-3.5" /> Generate one-time login command
              </>
            )}
          </button>
        )}
        {loginCmd.isError && (
          <div className="text-[11px] text-[color:var(--critical)]">
            {String(loginCmd.error)}
          </div>
        )}
      </div>

      {/* Step 3 — use it */}
      <div className="space-y-2 pt-1 border-t border-line/40">
        <div className="text-[11px] uppercase tracking-wider text-muted">
          3 · Code
        </div>
        <CommandLine cmd="cd ~/your/project && daalu" />
        <p className="text-[11px] text-muted">
          The agent reads files, proposes diffs you approve, and runs commands
          (asking first). <code className="text-[10.5px]">daalu --help</code> for
          everything.
        </p>
      </div>
    </section>
  );
}

function FeatureHints() {
  return (
    <section className="surface p-4 space-y-2 text-[12.5px] text-muted">
      <h3 className="text-[11px] uppercase tracking-wider">What's in the box</h3>
      <ul className="space-y-1.5 list-disc list-inside leading-relaxed">
        <li>
          <strong className="text-[color:var(--text)]">VS Code</strong>{" "}
          (code-server) with the <em>Daalu Assistant</em> extension
          pre-installed — opens in the activity bar.
        </li>
        <li>
          <strong className="text-[color:var(--text)]">Terminal</strong> with
          <code className="text-[11px]"> kubectl</code>,{" "}
          <code className="text-[11px]">helm</code>,{" "}
          <code className="text-[11px]">git</code>,{" "}
          <code className="text-[11px]">jq</code> pre-installed.
        </li>
        <li>
          <strong className="text-fg">
            <code className="text-[11px]">daalu chat</code> CLI
          </strong>{" "}
          — a coding-agent REPL inside the terminal. Reads + writes
          files (asks before each), runs shell commands (asks first).
        </li>
        <li>
          <strong className="text-[color:var(--text)]">Privacy</strong>: prompts
          and code never reach external LLM providers — the router blocks
          all external tiers when{" "}
          <code className="text-[11px]">purpose=&apos;coding&apos;</code>.
        </li>
      </ul>
      <p className="pt-1 italic">
        <RefreshCw className="h-3 w-3 inline mr-1" /> Idle workspaces
        pause after 24 h (files retained) and are destroyed after 14 d
        of continued inactivity. You'll get an email warning before the
        14-day cliff.
      </p>
      <div className="flex items-center gap-2 pt-1">
        <HardDrive className="h-3 w-3" />
        <Bot className="h-3 w-3" />
        <span>One workspace per user. Per-tenant profile caps apply.</span>
      </div>
    </section>
  );
}
