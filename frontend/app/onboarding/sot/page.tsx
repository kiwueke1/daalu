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
import { HUB } from "@/lib/hub-routes";
import { OnboardingHeader } from "@/components/onboarding/onboarding-header";

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
          description="You'll paste your Nautobot URL + an API token. Your
          Nautobot stays under your control; daalu reads + writes via the
          token's permissions."
          onPick={() => onPicked("byo")}
        />
        <ModeCard
          icon={Cloud}
          title="Use the hosted Nautobot"
          subtitle="Hosted mode"
          disabled={!hostedAvailable}
          disabledReason={
            isLoading
              ? "Checking…"
              : hostedStatus?.detail ||
                "Your operator has not configured hosted Nautobot on this deploy."
          }
          description="We'll provision a Nautobot Tenant scoped to your
          account with one click. You get the same Nautobot UI; nobody else
          can see your data. Recommended if you're new to Nautobot."
          onPick={() => onPicked("hosted")}
        />
      </div>

      <details className="surface p-3 text-[12px] text-muted">
        <summary className="cursor-pointer">What's Nautobot?</summary>
        <p className="mt-2 leading-relaxed">
          Nautobot is the source of truth for your infrastructure — every
          device, IP, VLAN, intended config. The daalu reconciler reads
          intended state from Nautobot and the executor pushes approved
          changes back to your devices. Without it, daalu can observe
          drift but has nothing to compare against. See{" "}
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
      className={`group text-left rounded-2xl border p-4 space-y-2 transition-all ${
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
          <div className="text-[10px] uppercase tracking-wider text-muted mt-0.5">
            {subtitle}
          </div>
        </div>
      </div>
      <p className="text-[12.5px] text-fg/70 leading-relaxed">
        {description}
      </p>
      {disabled ? (
        <p className="text-[11px] text-muted italic">{disabledReason}</p>
      ) : (
        <div className="pt-1 text-xs font-medium text-accent inline-flex items-center gap-1">
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

      <button
        type="button"
        onClick={onBack}
        className="text-xs text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> Pick a different mode
      </button>
    </section>
  );
}

function ByoForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
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
    onError: (e) => setTestResult({ ok: false, message: String(e) }),
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
      onDone();
    },
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
        help="If set, daalu verifies HMAC-SHA512 signatures on incoming Nautobot webhooks. Recommended in prod — paste the same string into Nautobot's webhook config."
      />

      <div className="flex items-center justify-between pt-2">
        <button
          type="button"
          onClick={() => test.mutate()}
          disabled={!canTest || test.isPending}
          className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--text)] disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          <Check className="h-3.5 w-3.5" /> Test connection
        </button>
        {testResult && (
          <span
            className={`text-[11.5px] inline-flex items-center gap-1.5 ${
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
        <button
          type="button"
          onClick={() => save.mutate()}
          disabled={!canSave || save.isPending}
          className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          Save &amp; continue <ArrowRight className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

function HostedProvision({ onDone }: { onDone: () => void }) {
  const provision = useMutation({
    mutationFn: () => api.sot.provisionNautobot(),
    onSuccess: () => {
      // Give the user a beat to read the success state before moving on.
      setTimeout(onDone, 800);
    },
  });

  return (
    <div className="surface p-4 space-y-3">
      <p className="text-[13px] text-fg/80 leading-relaxed">
        Clicking <strong>Provision</strong> creates a Nautobot Tenant
        scoped to your account, attaches an ObjectPermission limited to
        that tenant, mints a fresh API token, and saves all of it into
        your daalu integration row. Safe to re-run — existing Tenant +
        Permission are reused; only the token is rotated.
      </p>
      <p className="text-[12px] text-muted italic">
        Hosted at: this deploy's <code>managed_nautobot_url</code>.
      </p>

      <div className="flex justify-end pt-2 border-t border-line/40">
        <button
          type="button"
          onClick={() => provision.mutate()}
          disabled={provision.isPending || provision.isSuccess}
          className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          <Wand2 className="h-3.5 w-3.5" />
          {provision.isPending
            ? "Provisioning…"
            : provision.isSuccess
            ? "Provisioned"
            : "Provision now"}
        </button>
      </div>

      {provision.isError && (
        <div className="text-[11.5px] text-[color:var(--critical)] inline-flex items-center gap-1.5">
          <X className="h-3 w-3" /> {String(provision.error)}
        </div>
      )}
      {provision.isSuccess && provision.data && (
        <div className="text-[11.5px] text-[color:var(--accent-emerald)] inline-flex items-center gap-1.5">
          <Check className="h-3 w-3" /> {provision.data.message}
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
      {help && <div className="text-[10px] text-muted">{help}</div>}
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
    onSuccess: () => onDone(),
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
        <p className="text-muted text-[12.5px] mt-1">
          One tenant-wide credential covers every Linux server you onboard.
          Per-device overrides (named pools, dedicated keys for high-trust
          hosts) can be added later via /integrations.
        </p>
      </div>

      <div className="surface p-4 space-y-3">
        <Field
          label="SSH user"
          value={user}
          onChange={setUser}
          placeholder="daalu"
          help="The user the executor will SSH in as. Must exist on every managed device with sudo (or root)."
        />
        <Field
          label="Port"
          value={port}
          onChange={setPort}
          placeholder="22"
        />

        <div className="text-xs space-y-1">
          <div className="text-muted uppercase tracking-wider">Authentication</div>
          <div className="flex gap-2">
            {(["key", "password"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setAuthMode(m)}
                className={`text-xs px-3 py-1.5 rounded-lg border ${
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
              className="w-full px-3 py-2 rounded-lg bg-bg-elevated/60 border border-line text-[12px] font-mono"
            />
            <div className="text-[10px] text-muted">
              Stored encrypted at rest. Paired public key must be in
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
          <button
            type="button"
            onClick={onBack}
            className="text-xs h-9 px-3 rounded-lg border border-line text-muted hover:text-[color:var(--text)]"
          >
            Back
          </button>
          <button
            type="button"
            onClick={() => save.mutate()}
            disabled={!canSave || save.isPending}
            className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            Save &amp; finish <ArrowRight className="h-3.5 w-3.5" />
          </button>
        </div>

        {save.isError && (
          <div className="text-[11.5px] text-[color:var(--critical)] inline-flex items-center gap-1.5">
            <X className="h-3 w-3" /> {String(save.error)}
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
      <div className="surface p-6 text-center space-y-3">
        <div
          className="mx-auto h-12 w-12 rounded-full flex items-center justify-center"
          style={{
            background: "color-mix(in srgb, var(--accent-emerald) 16%, transparent)",
          }}
        >
          <CheckCircle2 className="h-6 w-6 text-[color:var(--accent-emerald)]" />
        </div>
        <h2 className="text-lg font-semibold">SoT is ready</h2>
        <p className="text-[13px] text-muted leading-relaxed max-w-md mx-auto">
          Nautobot is wired and the executor can authenticate to your
          Linux servers. Next: add your first device — daalu can't manage
          anything it doesn't know about.
        </p>
        <div className="flex justify-center gap-2 pt-2">
          <Link
            href="/devices"
            className="text-xs h-9 px-4 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base inline-flex items-center gap-1.5"
          >
            <HardDrive className="h-3.5 w-3.5" /> Add my first device
          </Link>
          <Link
            href="/proposals"
            className="text-xs h-9 px-4 rounded-lg border border-line text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
          >
            <GitPullRequest className="h-3.5 w-3.5" /> View proposals
          </Link>
        </div>
      </div>
    </section>
  );
}
