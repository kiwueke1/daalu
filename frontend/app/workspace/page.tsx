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
import { Button, buttonClass } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { TabBar } from "@/components/ui/tabs";
import { useToast } from "@/components/ui/toast";
import {
  ArrowRight,
  Bot,
  Check,
  Code2,
  Copy,
  Cpu,
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
import { api, type WorkspaceView } from "@/lib/api";
import { errorText } from "@/lib/errors";

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
          <Code2 className="h-5 w-5 text-accent-cyan" aria-hidden /> Workspace
        </h1>
        <p className="text-muted text-sm mt-1">
          A browser-hosted VS Code, pre-wired to your inference tier so
          the assistant inside it runs on your GPU (or the Daalu-hosted
          pool) — never the public internet. Code prompts never leave
          your perimeter.
        </p>
      </header>

      {eligibility.isLoading && (
        <div role="status" className="surface p-4 text-sm text-muted flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Checking whether Workspace is available…
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
            <div role="status" className="surface p-4 text-sm text-muted flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Loading workspace…
            </div>
          )}
          {session.isError && <LoadState query={session}>{null}</LoadState>}
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

      {eligibility.data?.eligible !== false && <FeatureHints />}
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
  /** The first option as a real action, when there is one to take here. */
  primary?: { label: string; href: string };
} {
  const r = reason.toLowerCase();
  if (r.includes("no coding model")) {
    return {
      title: "No coding model is running yet",
      sentence:
        "Your organisation can use Workspace, but the coding model it talks to isn't deployed on your cluster yet, so nothing can answer the assistant's prompts.",
      options: [
        <>Ask your operator to deploy the coding model on your GPU cluster.</>,
        <>Then come back here to create your workspace.</>,
      ],
    };
  }
  return {
    title: "Workspace isn't turned on for your organisation",
    sentence:
      "Workspace only runs on an AI tier that keeps your code off the public internet, and your organisation doesn't have one yet. Connecting your own GPU is the quickest way.",
    primary: { label: "Connect a GPU", href: "/ai-factory" },
    options: [
      <>
        Or opt into the <strong>Daalu-hosted</strong> AI tier (talk to your
        account contact).
      </>,
      <>Or ask your Daalu operator to turn Workspace on for your organisation.</>,
    ],
  };
}

function NotEligibleCard({ reason }: { reason: string }) {
  const { title, sentence, options, primary } = explainIneligible(reason || "");
  return (
    <section className="surface p-5 space-y-3">
      <div className="flex items-start gap-3">
        <div
          // Neutral, not a warning: "not turned on" is a setup step, not an error.
          className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0 border border-line bg-bg-elevated/60"
        >
          <Lock className="h-4 w-4 text-muted" aria-hidden />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-base font-semibold">{title}</div>
          <p className="text-sm text-muted mt-1 leading-relaxed">{sentence}</p>
          {primary && (
            <Link
              href={primary.href}
              className={buttonClass("primary") + " mt-3"}
            >
              <Cpu className="h-4 w-4" aria-hidden /> {primary.label}
            </Link>
          )}
          <ul className="text-sm text-muted mt-3 space-y-1.5 list-disc list-inside">
            {options.map((o, i) => (
              <li key={i}>{o}</li>
            ))}
          </ul>
          {reason && (
            <details className="mt-3 text-xs text-muted">
              <summary className="cursor-pointer hover:text-fg">
                Technical details
              </summary>
              <pre className="mt-1.5 whitespace-pre-wrap break-words rounded-lg bg-bg-elevated p-2.5 font-mono text-xs">
                {reason}
              </pre>
            </details>
          )}
        </div>
      </div>
      <div className="pt-2 border-t border-line/40 flex justify-end">
        <Link href="/billing" className={buttonClass("secondary")}>
          Manage AI tier <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </Link>
      </div>
      <FeatureHints collapsed />
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
  const toast = useToast();
  const destroy = useMutation({
    mutationFn: () => api.ide.destroy(),
    onSuccess: () => {
      onDestroyed();
      toast.success("Workspace deleted. Its volume has been released.");
    },
    onError: (err) => toast.error(errorText(err)),
  });
  const isProvisioning = workspace.state === "provisioning";
  const isActive = workspace.state === "active";
  return (
    <section className="surface p-5 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-xs text-muted">
            Your workspace · {workspace.profile} · {workspace.model}
          </div>
          <div className="text-base font-semibold mt-1 flex items-center gap-2">
            <StateBadge state={workspace.state} />
            {isProvisioning && (
              <span className="text-xs text-muted font-normal">
                First boot can take about a minute while the image downloads.
              </span>
            )}
          </div>
          {workspace.git_repo_url && (
            <div className="text-xs text-muted mt-2 flex items-center gap-1.5 flex-wrap">
              <span>
                Cloned from{" "}
                <code className="text-xs">{workspace.git_repo_url}</code> on{" "}
                <code className="text-xs">{workspace.git_branch}</code>
              </span>
              {workspace.git_authenticated && (
                <span className="inline-flex items-center gap-1 text-[11px] uppercase tracking-wider rounded px-1.5 py-0.5 bg-accent/15 text-accent">
                  <ShieldCheck className="h-3 w-3" aria-hidden /> Private
                </span>
              )}
            </div>
          )}
        </div>
        <ConfirmButton
          tone="danger"
          label="Delete"
          confirmLabel="Delete workspace"
          consequence="Your /workspace files are deleted. Push any changes first."
          icon={<Trash2 className="h-3.5 w-3.5" aria-hidden />}
          pending={destroy.isPending}
          onConfirm={() => destroy.mutate()}
          className="shrink-0"
        />
      </div>

      {isActive && (
        <div className="flex flex-wrap gap-2 pt-2 border-t border-line/40">
          <a
            href={workspace.ide_url}
            target="_blank"
            rel="noreferrer"
            className={buttonClass("primary")}
          >
            <Code2 className="h-3.5 w-3.5" aria-hidden /> Open IDE
            <span className="sr-only">(opens in a new tab)</span>
          </a>
          <div className="text-xs text-muted self-center">
            Opens in a new tab. The Daalu Assistant lives in the activity
            bar, or run <code className="text-xs">daalu chat</code>{" "}
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
      cls: "bg-signal-warning/15 text-signal-warning",
    },
    active: {
      label: "Active",
      cls: "bg-accent/15 text-accent",
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
      cls: "bg-signal-critical/15 text-signal-critical",
    },
  };
  const m = map[state];
  return (
    <span
      className={`inline-flex items-center text-[11px] font-semibold uppercase tracking-wider rounded px-1.5 py-0.5 ${m.cls}`}
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
  const toast = useToast();

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
    onSuccess: () => {
      onCreated();
      toast.success("Workspace is starting. It'll be ready in about a minute.");
    },
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
        <p className="text-sm text-muted mt-1 leading-relaxed">
          One workspace per user. Your files live on a persistent volume
          and survive pauses; idle workspaces are reaped after 14 days.
        </p>
      </div>

      <div className="space-y-3">
        <label className="block text-xs space-y-1">
          <span className="text-xs font-medium text-muted">
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
          <span className="block text-xs text-muted">
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
          <span className="text-xs font-medium text-muted">Profile</span>
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
          <span className="block text-xs text-muted">
            Pick small unless you know you need more. Your operator may cap
            the largest size your organisation can use.
          </span>
        </label>

        <label className="block text-xs space-y-1">
          <span className="text-xs font-medium text-muted">
            Git repo URL (optional)
          </span>
          <input
            type="url"
            value={gitRepoUrl}
            onChange={(e) => setGitRepoUrl(e.target.value)}
            placeholder="https://github.com/acme/infra.git"
            autoComplete="url"
            className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
          />
          <span className="block text-xs text-muted">
            HTTPS URL of a GitHub, GitLab, or self-hosted repo. If set, your
            workspace is seeded with a fresh clone on first boot. Leave blank
            to start empty — you can drag-and-drop files into the IDE later,
            or git clone from a terminal once it&apos;s open.
          </span>
        </label>

        {gitRepoUrl.trim() && (
          <label className="block text-xs space-y-1">
            <span className="text-xs font-medium text-muted flex items-center gap-1.5">
              <KeyRound className="h-3 w-3" aria-hidden /> Access token (private repos)
            </span>
            <input
              type="password"
              value={gitToken}
              onChange={(e) => setGitToken(e.target.value)}
              autoComplete="off"
              placeholder="ghp_… / glpat-… (leave blank for public repos)"
              className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm font-mono"
            />
            <span className="block text-xs text-muted">
              A personal-access-token with <code className="text-xs">repo</code>{" "}
              (GitHub) or <code className="text-xs">read_repository</code>{" "}
              (GitLab) scope. Stored{" "}
              <strong className="text-[color:var(--text)]">encrypted</strong>,
              mounted into the clone as an in-memory credential, and never
              written into your repo&apos;s git config. Required only for
              private repos.
            </span>
          </label>
        )}

        <label className="block text-xs space-y-1">
          <span className="text-xs font-medium text-muted">
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
        <Button
          variant="primary"
          onClick={() => create.mutate()}
          pending={create.isPending}
          disabled={noneAvailable || !effectiveModel}
          disabledReason={
            create.isPending
              ? "Creating…"
              : models.isLoading
                ? "Loading models…"
                : "No coding model is available yet"
          }
          icon={<Sparkles className="h-3.5 w-3.5" aria-hidden />}
        >
          {create.isPending ? "Creating…" : "Create workspace"}
        </Button>
      </div>

      {create.isError && (
        <div role="alert" className="text-sm text-signal-critical">
          {errorText(create.error)}
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
      aria-label={copied ? "Copied" : `${label} command`}
      className="motion-press shrink-0 text-xs h-7 px-2 rounded-md border border-line text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1"
    >
      {copied ? (
        <Check className="h-3 w-3 motion-success" aria-hidden />
      ) : (
        <Copy className="h-3 w-3" aria-hidden />
      )}
      {copied ? "Copied" : label}
    </button>
  );
}

function CommandLine({ cmd }: { cmd: string }) {
  return (
    <div className="flex items-center gap-2 bg-bg-elevated/60 border border-line rounded-lg px-3 h-9">
      <code className="text-xs font-mono flex-1 overflow-x-auto whitespace-nowrap">
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
  type InstallId = (typeof INSTALL_TABS)[number]["id"];

  return (
    <section className="surface p-5 space-y-4">
      <div className="flex items-start gap-3">
        <div
          className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0"
          style={{
            background: "color-mix(in srgb, var(--accent) 14%, transparent)",
          }}
        >
          <Terminal className="h-4 w-4 text-accent" aria-hidden />
        </div>
        <div className="flex-1">
          <h2 className="text-base font-semibold">Use Daalu from your terminal</h2>
          <p className="text-sm text-muted mt-1 leading-relaxed">
            Prefer your own editor? Install the <code className="text-xs">daalu</code>{" "}
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
        <TabBar<InstallId>
          tabs={INSTALL_TABS.map((t) => ({ id: t.id, label: t.label }))}
          active={tab}
          onChange={setTab}
          ariaLabel="Install method"
        />
        <div
          role="tabpanel"
          id={`tabpanel-${active.id}`}
          aria-labelledby={`tab-${active.id}`}
          key={active.id}
          className="motion-enter"
        >
          <CommandLine cmd={active.cmd} />
        </div>
      </div>

      {/* Step 2 — log in */}
      <div className="space-y-2">
        <div className="text-[11px] uppercase tracking-wider text-muted">
          2 · Log in
        </div>
        <CommandLine cmd="daalu login" />
        <p className="text-xs text-muted">
          Opens your browser to approve this machine, then you&apos;re signed in.
          Or skip the browser and generate a one-time command tied to your account:
        </p>
        {loginCmd.data ? (
          <div className="space-y-1.5 motion-enter">
            <CommandLine cmd={loginCmd.data.command} />
            <p className="text-xs text-muted">
              One-time, expires in {Math.round(loginCmd.data.expires_in / 60)}{" "}
              minutes. Paste it into your terminal to log in instantly.
            </p>
          </div>
        ) : (
          <Button
            onClick={() => loginCmd.mutate()}
            pending={loginCmd.isPending}
            disabledReason="Generating…"
            icon={<KeyRound className="h-3.5 w-3.5" aria-hidden />}
          >
            {loginCmd.isPending ? "Generating…" : "Generate one-time login command"}
          </Button>
        )}
        {loginCmd.isError && (
          <div role="alert" className="text-sm text-signal-critical">
            {errorText(loginCmd.error)}
          </div>
        )}
      </div>

      {/* Step 3 — use it */}
      <div className="space-y-2 pt-1 border-t border-line/40">
        <div className="text-[11px] uppercase tracking-wider text-muted">
          3 · Code
        </div>
        <CommandLine cmd="cd ~/your/project && daalu" />
        <p className="text-xs text-muted">
          The agent reads files, proposes diffs you approve, and runs commands
          (asking first). <code className="text-xs">daalu --help</code> for
          everything.
        </p>
      </div>
    </section>
  );
}

/**
 * What a workspace includes. On its own it's a card; inside the blocked
 * state (`collapsed`) it folds into a <details> so the next step leads.
 */
function FeatureHints({ collapsed = false }: { collapsed?: boolean }) {
  const body = (
    <>
      <ul className="space-y-1.5 list-disc list-inside leading-relaxed">
        <li>
          <strong className="text-[color:var(--text)]">VS Code</strong>{" "}
          (code-server) with the <em>Daalu Assistant</em> extension
          pre-installed — opens in the activity bar.
        </li>
        <li>
          <strong className="text-[color:var(--text)]">Terminal</strong> with
          <code className="text-xs"> kubectl</code>,{" "}
          <code className="text-xs">helm</code>,{" "}
          <code className="text-xs">git</code>,{" "}
          <code className="text-xs">jq</code> pre-installed.
        </li>
        <li>
          <strong className="text-fg">
            <code className="text-xs">daalu chat</code> CLI
          </strong>{" "}
          — a coding-agent REPL inside the terminal. Reads + writes
          files (asks before each), runs shell commands (asks first).
        </li>
        <li>
          <strong className="text-[color:var(--text)]">Privacy</strong>: prompts
          and code never reach external LLM providers — the router blocks
          all external tiers when{" "}
          <code className="text-xs">purpose=&apos;coding&apos;</code>.
        </li>
      </ul>
      <p className="pt-1 italic">
        <RefreshCw className="h-3 w-3 inline mr-1" aria-hidden /> Idle workspaces
        pause after 24 h (files retained) and are destroyed after 14 days
        of continued inactivity. You&apos;ll get an email warning before
        then.
      </p>
      <div className="flex items-center gap-2 pt-1">
        <HardDrive className="h-3 w-3" aria-hidden />
        <Bot className="h-3 w-3" aria-hidden />
        <span>One workspace per user. Your organisation&apos;s size limits apply.</span>
      </div>
    </>
  );
  if (collapsed) {
    return (
      <details className="pt-3 border-t border-line/40 text-sm text-muted group">
        <summary className="cursor-pointer hover:text-fg font-medium">
          What&apos;s in the box
        </summary>
        <div className="mt-2 space-y-2">{body}</div>
      </details>
    );
  }
  return (
    <section className="surface p-4 space-y-2 text-sm text-muted">
      <h3 className="text-[11px] uppercase tracking-wider">What&apos;s in the box</h3>
      {body}
    </section>
  );
}
