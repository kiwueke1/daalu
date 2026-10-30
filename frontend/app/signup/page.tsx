"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { DaaluMark } from "@/components/brand/daalu-mark";
import { api } from "@/lib/api";

export default function SignupPage() {
  return (
    <Suspense fallback={null}>
      <SignupForm />
    </Suspense>
  );
}

function SignupForm() {
  const search = useSearchParams();
  const next = search.get("next") || "/";
  const loginHref = `/login${next !== "/" ? `?next=${encodeURIComponent(next)}` : ""}`;

  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [organization, setOrganization] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [resent, setResent] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      await api.auth.signup({
        email,
        password,
        full_name: fullName.trim() || null,
        organization: organization.trim() || null,
      });
      setDone(true);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(
        /404/.test(msg)
          ? "Account creation isn't available on this hub."
          : "Sign-up failed. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function resend() {
    setResent(false);
    try {
      await api.auth.resendVerification(email);
    } finally {
      setResent(true);
    }
  }

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4 overflow-hidden">
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 50% at 50% 30%, rgba(var(--accent-rgb),0.10), transparent 60%), radial-gradient(40% 40% at 80% 80%, rgba(var(--accent-rgb),0.05), transparent 60%)",
        }}
      />
      <div className="relative w-full max-w-sm">
        <div className="flex items-center gap-3 mb-8">
          <DaaluMark />
          <div>
            <div className="text-sm font-semibold tracking-wide">Daalu</div>
            <div className="text-[10px] tracking-[0.18em] text-muted">
              intelligent IT operations
            </div>
          </div>
        </div>

        {done ? (
          <div className="space-y-4">
            <h1 className="text-xl font-semibold">Check your email</h1>
            <p className="text-sm text-muted">
              We&apos;ve sent a verification link to{" "}
              <span className="text-[color:var(--text)]">{email}</span>. Click it
              to activate your account, then sign in.
            </p>
            <div className="text-xs text-muted">
              Didn&apos;t get it?{" "}
              <button
                type="button"
                onClick={resend}
                className="text-[color:var(--accent)] hover:underline"
              >
                Resend the link
              </button>
              {resent && <span className="ml-1 text-accent-emerald">Sent.</span>}
            </div>
            <a
              href={loginHref}
              className="inline-block text-sm text-[color:var(--accent)] hover:underline pt-2"
            >
              ← Back to sign in
            </a>
          </div>
        ) : (
          <>
            <h1 className="text-xl font-semibold mb-1">Create your account</h1>
            <p className="text-sm text-muted mb-6">
              Sign up with your email. You&apos;ll start in your own private
              workspace.
            </p>

            <form onSubmit={onSubmit} className="space-y-3">
              <Field label="Full name">
                <input
                  type="text"
                  autoComplete="name"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  className={inputCls}
                />
              </Field>
              <Field label="Work email">
                <input
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className={inputCls}
                />
              </Field>
              <Field label="Organization (optional)">
                <input
                  type="text"
                  autoComplete="organization"
                  value={organization}
                  onChange={(e) => setOrganization(e.target.value)}
                  className={inputCls}
                />
              </Field>
              <Field label="Password">
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  minLength={8}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className={inputCls}
                />
                <span className="mt-1 block text-[11px] text-muted">
                  At least 8 characters.
                </span>
              </Field>
              <Field label="Confirm password">
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  minLength={8}
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  className={inputCls}
                />
              </Field>

              {error && (
                <div className="text-xs text-signal-critical bg-signal-critical/10 border border-signal-critical/30 rounded px-3 py-2">
                  {error}
                </div>
              )}

              <button
                type="submit"
                disabled={busy}
                className="w-full rounded-md text-sm font-medium py-2 transition-colors disabled:opacity-60"
                style={{
                  background: "var(--accent)",
                  color: "var(--bg-base)",
                  boxShadow: "0 0 22px var(--accent-glow)",
                }}
              >
                {busy ? "Creating account…" : "Create account"}
              </button>
            </form>

            <p className="text-[12px] text-muted text-center pt-4">
              Already have an account?{" "}
              <a href={loginHref} className="text-[color:var(--accent)] hover:underline">
                Sign in
              </a>
            </p>
          </>
        )}
      </div>
    </div>
  );
}

const inputCls =
  "mt-1 w-full bg-bg-secondary/60 border border-line rounded-md px-3 py-2 text-sm focus:outline-none focus:border-accent-blue/60 focus:bg-bg-secondary";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs uppercase tracking-wider text-muted">{label}</span>
      {children}
    </label>
  );
}
