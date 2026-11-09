"use client";

/**
 * SoT-specific onboarding wizard.
 *
 * The generic /onboarding wizard treats Nautobot as one of 14 steps with
 * a single URL+token form. That works for operators who already know what
 * they're doing, but customers new to the SoT model need more hand-holding:
 *
 *   1. Decide whether to bring their own Nautobot or use ours (if hosted
 *      mode is configured on this deploy)
 *   2. Wire the connection — paste URL+token for BYO, or click Provision
 *      for hosted
 *   3. Set up at least one SSH credential row so the executor can push
 *
 * After step 3 the wizard funnels them to /devices to add their first
 * device. That's where they actually start using the feature.
 */

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  Cloud,
  GitPullRequest,
  HardDrive,
  Lock,
  Server,
  Wand2,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import { OnboardingHeader } from "@/components/onboarding/onboarding-header";
import { Button, buttonClass } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

type Mode = "byo" | "hosted";
type Step = "mode" | "connect" | "credentials" | "done";

export default function SotOnboardingPage() {
  const [step, setStep] = useState<Step>("mode");
  const [mode, setMode] = useState<Mode | null>(null);

  return (
    <div className="max-w-3xl space-y-6">
      <Header step={step} />

      {step === "mode" && (
        <ModeStep
          onPicked={(m) => {
            setMode(m);
            setStep("connect");
          }}
        />
      )}

      {step === "connect" && mode && (
        <ConnectStep
          mode={mode}
          onBack={() => setStep("mode")}
          onDone={() => setStep("credentials")}
        />
      )}

      {step === "credentials" && (
        <CredentialsStep
          onBack={() => setStep("connect")}
          onDone={() => setStep("done")}
        />
      )}

      {step === "done" && <DoneStep />}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Step 0 — header / progress
// ────────────────────────────────────────────────────────────────────────

function Header({ step }: { step: Step }) {
  const stepIdx = ["mode", "connect", "credentials", "done"].indexOf(step);
  const done = step === "done";
  return (
    <OnboardingHeader
      backLink
      icon={GitPullRequest}
      title="Source of Truth setup"
      description="Connect Nautobot so Daalu knows the intended state of your servers, BMCs and network gear, and can push approved changes."
      progress={{
        value: done ? 1 : (stepIdx + 1) / 3,
        label: done
          ? "All done"
          : `Step ${stepIdx + 1} of 3: ${["Choose Nautobot", "Connect", "SSH credentials"][stepIdx]}`,
      }}
    />
  );
}

// ────────────────────────────────────────────────────────────────────────
// Step 1 — pick BYO or hosted
// ────────────────────────────────────────────────────────────────────────

function ModeStep({ onPicked }: { onPicked: (m: Mode) => void }) {
  const { data: hostedStatus, isLoading } = useQuery({
    queryKey: ["sot-hosted-status"],
    queryFn: () => api.sot.hostedStatus(),
  });
  const hostedAvailable = hostedStatus?.hosted_available ?? false;

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">
          Bring your own Nautobot, or use ours?
        </h2>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <ModeCard
          icon={Server}
          title="I already run Nautobot"
          subtitle="BYO mode"
          description="You'll paste your Nautobot URL and an API token. Your Nautobot stays under your control; Daalu reads and writes only what the token allows."
          onPick={() => onPicked("byo")}
        />
        <ModeCard
          icon={Cloud}
          title="Use the hosted Nautobot"
          subtitle="Hosted mode"
          disabled={!hostedAvailable}
          disabledReason={
            isLoading
              ? "Checking whether hosted Nautobot is available…"
              : "Hosted Nautobot isn't available on your plan yet. Bring your own Nautobot for now."
          }
          description={`We set up a Nautobot space for your account in one click. You get the same Nautobot UI; nobody else can see your data.${
            hostedAvailable ? " Recommended if you're new to Nautobot." : ""
          }`}
          onPick={() => onPicked("hosted")}
        />
      </div>

      <details className="surface p-3 text-xs text-muted">
        <summary className="cursor-pointer">What's Nautobot?</summary>
        <p className="mt-2 leading-relaxed">
          Nautobot is the source of truth for your infrastructure — every
          device, IP, VLAN and intended config. Daalu reads what each device
          should look like from Nautobot and pushes the changes you approve.
          Without it, Daalu can see what&apos;s running but has nothing to
          compare it against. See{" "}
          <a
            href="https://docs.nautobot.com/"
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            docs.nautobot.com
          </a>
          .
        </p>
      </details>
    </section>
  );
}

function ModeCard({
  icon: Icon,
  title,
  subtitle,
  description,
  onPick,
  disabled = false,
  disabledReason = "",
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  subtitle: string;
  description: string;
  onPick: () => void;
  disabled?: boolean;
  disabledReason?: string;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onPick}
      // A <button> centres its content vertically; pin it to the top so the
      // two cards' headers line up when one card is taller.
      className={`group text-left rounded-2xl border p-4 flex flex-col items-stretch justify-start gap-2 transition-all ${
        disabled
          ? "border-line/60 opacity-50 cursor-not-allowed"
          : "border-line hover:border-accent-blue/60 hover:bg-accent-blue/5 hover:shadow-glow"
      }`}
    >
      <div className="flex items-start gap-3">
        <div
          className="h-9 w-9 rounded-lg flex items-center justify-center shrink-0"
          style={{ background: "color-mix(in srgb, var(--accent) 14%, transparent)" }}
        >
          <Icon className="h-4 w-4 text-accent-cyan" />
        </div>
        <div className="flex-1">
          <div className="text-[15px] font-semibold">{title}</div>
          <div className="text-[11px] uppercase tracking-wider text-muted mt-0.5">
            {subtitle}
          </div>
        </div>
      </div>
      <p className="text-sm text-fg/80 leading-relaxed">
        {description}
      </p>
      {disabled ? (
        <p className="text-xs text-muted">{disabledReason}</p>
      ) : (
        <div className="pt-1 text-xs font-medium text-accent inline-flex self-start items-center gap-1">
          Choose <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
        </div>
      )}
    </button>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Step 2 — connect (BYO form OR hosted provision button)
// ────────────────────────────────────────────────────────────────────────

function ConnectStep({
  mode,
  onBack,
  onDone,
}: {
  mode: Mode;
  onBack: () => void;
  onDone: () => void;
}) {
  return (
    <section className="space-y-4">
      <div>
        <div className="text-[11px] uppercase tracking-wider text-muted mb-1">
          {mode === "byo" ? "Bring your own Nautobot" : "Hosted Nautobot"}
        </div>
        <h2 className="text-base font-semibold">Wire the connection</h2>
      </div>

      {mode === "byo" ? <ByoForm onDone={onDone} /> : <HostedProvision onDone={onDone} />}

      <Button variant="ghost" size="sm" onClick={onBack} icon={<ArrowLeft className="h-3.5 w-3.5" aria-hidden />}>
        Pick a different option
      </Button>
    </section>
  );
}

function ByoForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    message: string;
  } | null>(null);

  const test = useMutation({
    mutationFn: () =>
      api.onboarding.test("nautobot", {
        url,
        token,
        webhook_secret: webhookSecret,
      }),
    onSuccess: (r) => setTestResult({ ok: r.ok, message: r.message }),
    onError: (e) => setTestResult({ ok: false, message: errorText(e) }),
  });

  const save = useMutation({
    mutationFn: () =>
      api.integrations.putConfig("nautobot", {
        name: "Nautobot",
        config: {
          url,
          token,
          ...(webhookSecret ? { webhook_secret: webhookSecret } : {}),
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sot-hosted-status"] });
      toast.success("Nautobot connected.");
      onDone();
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const canTest = url.length > 0 && token.length > 0;
  const canSave = canTest && testResult?.ok === true;

  return (
    <div className="surface p-4 space-y-3">
      <Field
        label="Nautobot base URL"
        value={url}
        onChange={setUrl}
        placeholder="https://nautobot.example.com"
      />
      <Field
        label="API token"
        value={token}
        onChange={setToken}
        type="password"
        help="Write-enabled token from a user with view+add+change+delete on DCIM and IPAM, scoped to your Nautobot tenant."
      />
      <Field
        label="Webhook secret (optional)"
        value={webhookSecret}
        onChange={setWebhookSecret}
        type="password"
        help="If set, Daalu checks that change notifications really come from your Nautobot. Recommended in production; paste the same string into Nautobot's webhook settings."
      />

      <div className="flex flex-wrap items-center justify-between gap-2 pt-2">
        <Button
          onClick={() => test.mutate()}
          disabled={!canTest}
          disabledReason={canTest ? undefined : "Enter the URL and token first"}
          pending={test.isPending}
          icon={<Check className="h-3.5 w-3.5" aria-hidden />}
        >
          Test connection
        </Button>
        {testResult && (
          <span
            role="status"
            className={`text-xs inline-flex items-center gap-1.5 motion-enter ${
              testResult.ok ? "text-[color:var(--accent-emerald)]" : "text-[color:var(--critical)]"
            }`}
          >
            {testResult.ok ? (
              <Check className="h-3 w-3" />
            ) : (
              <X className="h-3 w-3" />
            )}
            {testResult.message}
          </span>
        )}
      </div>

      <div className="flex justify-end pt-2 border-t border-line/40">
        <Button
          variant="primary"
          onClick={() => save.mutate()}
          disabled={!canSave}
          disabledReason={canSave ? undefined : "Run a successful connection test first"}
          pending={save.isPending}
        >
          Save &amp; continue <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </div>
    </div>
  );
}

function HostedProvision({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const provision = useMutation({
    mutationFn: () => api.sot.provisionNautobot(),
    onError: (e) => toast.error(errorText(e)),
    onSuccess: () => {
      toast.success("Hosted Nautobot is ready.");
      // Give the user a beat to read the success state before moving on.
      setTimeout(onDone, 800);
    },
  });

  return (
    <div className="surface p-4 space-y-3">
      <p className="text-sm text-fg/80 leading-relaxed">
        <strong>Provision</strong> sets up a Nautobot space that only your
        account can see, creates an API token for it, and connects it to
        Daalu. It&apos;s safe to run again: your data is kept and only the
        token is replaced.
      </p>

      <div className="flex justify-end pt-2 border-t border-line/40">
        {provision.isSuccess ? (
          <span className="text-xs text-accent inline-flex items-center gap-1.5">
            <Check className="h-3.5 w-3.5 motion-success" aria-hidden /> Provisioned
          </span>
        ) : (
          <ConfirmButton
            label="Provision now"
            confirmLabel="Provision Nautobot"
            consequence="Creates your Nautobot space and a new API token; re-running replaces the token."
            onConfirm={() => provision.mutate()}
            pending={provision.isPending}
            icon={<Wand2 className="h-3.5 w-3.5" aria-hidden />}
          />
        )}
      </div>

      {provision.isError && (
        <div role="alert" className="text-xs text-signal-critical inline-flex items-center gap-1.5">
          <X className="h-3 w-3" aria-hidden /> {errorText(provision.error)}
        </div>
      )}
      {provision.isSuccess && provision.data && (
        <div role="status" className="text-xs text-accent inline-flex items-center gap-1.5 motion-enter">
          <Check className="h-3 w-3" aria-hidden /> {provision.data.message}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
  help,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: "text" | "password";
  placeholder?: string;
  help?: string;
}) {
  return (
    <label className="block text-xs space-y-1">
      <div className="text-muted uppercase tracking-wider">{label}</div>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full h-9 px-3 rounded-lg bg-bg-elevated/60 border border-line text-sm"
      />
      {help && <div className="text-xs text-muted normal-case">{help}</div>}
    </label>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Step 3 — credentials
// ────────────────────────────────────────────────────────────────────────

function CredentialsStep({
  onBack,
  onDone,
}: {
  onBack: () => void;
  onDone: () => void;
}) {
  // The wizard only sets up ssh_credentials in v1 — the most common
  // case. Redfish + network credentials can be added later via the
  // /integrations page once the customer has devices that need them.
  const [user, setUser] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [password, setPassword] = useState("");
  const [authMode, setAuthMode] = useState<"key" | "password">("key");
  const [port, setPort] = useState("22");
  const toast = useToast();

  const save = useMutation({
    mutationFn: () => {
      const cfg: Record<string, unknown> = {
        user,
        port: Number(port) || 22,
        sudo: true,
      };
      if (authMode === "key" && privateKey) {
        // Backend's _load_credentials accepts either ciphertext or
        // plaintext fields; mirror nautobot's dual-shape contract.
        cfg.private_key = privateKey;
      } else if (authMode === "password" && password) {
        cfg.password = password;
      }
      return api.integrations.putConfig("ssh_credentials", {
        name: "Tenant-wide SSH credentials",
        config: cfg,
      });
    },
    onSuccess: () => {
      toast.success("SSH credentials saved.");
      onDone();
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const canSave =
    user.length > 0 &&
    ((authMode === "key" && privateKey.length > 0) ||
      (authMode === "password" && password.length > 0));

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-base font-semibold inline-flex items-center gap-2">
          <Lock className="h-4 w-4 text-accent-cyan" /> Set up SSH credentials
        </h2>
        <p className="text-muted text-sm mt-1">
          One credential covers every Linux server you add. You can add
          per-device credentials later, under Managed infra → Integrations.
        </p>
      </div>

      <div className="surface p-4 space-y-3">
        <Field
          label="SSH user"
          value={user}
          onChange={setUser}
          placeholder="daalu"
          help="The user Daalu signs in as over SSH. It must exist on every managed device, with sudo (or be root)."
        />
        <Field
          label="Port"
          value={port}
          onChange={setPort}
          placeholder="22"
        />

        <div className="text-xs space-y-1">
          <div id="ssh-auth-label" className="text-muted uppercase tracking-wider">Authentication</div>
          <div role="group" aria-labelledby="ssh-auth-label" className="flex gap-2">
            {(["key", "password"] as const).map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={authMode === m}
                onClick={() => setAuthMode(m)}
                className={`motion-press text-xs px-3 py-1.5 rounded-lg border ${
                  authMode === m
                    ? "border-accent-blue/60 bg-accent-blue/15 text-[color:var(--text)]"
                    : "border-line text-muted"
                }`}
              >
                {m === "key" ? "SSH key" : "Password"}
              </button>
            ))}
          </div>
        </div>

        {authMode === "key" ? (
          <label className="block text-xs space-y-1">
            <div className="text-muted uppercase tracking-wider">
              Private key (PEM)
            </div>
            <textarea
              value={privateKey}
              onChange={(e) => setPrivateKey(e.target.value)}
              rows={8}
              placeholder={`-----BEGIN OPENSSH PRIVATE KEY-----\n…`}
              className="w-full px-3 py-2 rounded-lg bg-bg-elevated/60 border border-line text-xs font-mono"
            />
            <div className="text-xs text-muted normal-case">
              Stored encrypted. The matching public key must be in
              authorized_keys on every managed host.
            </div>
          </label>
        ) : (
          <Field
            label="Password"
            value={password}
            onChange={setPassword}
            type="password"
            help="Stored encrypted at rest."
          />
        )}

        <div className="flex justify-between pt-2 border-t border-line/40">
          <Button variant="ghost" onClick={onBack}>
            Back
          </Button>
          <Button
            variant="primary"
            onClick={() => save.mutate()}
            disabled={!canSave}
            disabledReason={canSave ? undefined : "Enter the SSH user and a key or password"}
            pending={save.isPending}
          >
            Save &amp; finish <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Button>
        </div>

        {save.isError && (
          <div role="alert" className="text-xs text-signal-critical inline-flex items-center gap-1.5">
            <X className="h-3 w-3" aria-hidden /> {errorText(save.error)}
          </div>
        )}
      </div>
    </section>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Step 4 — done, funnel to devices
// ────────────────────────────────────────────────────────────────────────

function DoneStep() {
  return (
    <section className="space-y-4">
      <div className="surface p-6 text-center space-y-3 motion-enter">
        <div
          className="mx-auto h-12 w-12 rounded-full flex items-center justify-center"
          style={{
            background: "color-mix(in srgb, var(--accent-emerald) 16%, transparent)",
          }}
        >
          <CheckCircle2 className="h-6 w-6 text-[color:var(--accent-emerald)] motion-success" aria-hidden />
        </div>
        <h2 className="text-lg font-semibold">Source of Truth is ready</h2>
        <p className="text-sm text-muted leading-relaxed max-w-md mx-auto">
          Nautobot is connected and Daalu can sign in to your Linux servers.
          Next, add your first device: Daalu can&apos;t manage anything it
          doesn&apos;t know about.
        </p>
        <div className="flex flex-wrap justify-center gap-2 pt-2">
          <Link href="/devices" className={buttonClass("primary")}>
            <HardDrive className="h-3.5 w-3.5" aria-hidden /> Add my first device
          </Link>
          <Link href="/proposals" className={buttonClass("secondary")}>
            <GitPullRequest className="h-3.5 w-3.5" aria-hidden /> View proposals
          </Link>
        </div>
      </div>
    </section>
  );
}
