"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { DaaluWordmark } from "@/components/brand/daalu-mark";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { errorText } from "@/lib/errors";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

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
  const [resending, setResending] = useState(false);
  const [emailTouched, setEmailTouched] = useState(false);

  const emailInvalid = emailTouched && email.length > 0 && !EMAIL_RE.test(email);
  const pwShort = password.length > 0 && password.length < 8;
  const pwMismatch = confirm.length > 0 && confirm !== password;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!EMAIL_RE.test(email)) {
      setEmailTouched(true);
      setError("Enter an email like name@example.com.");
      return;
    }
    if (password.length < 8) {
      setError("Use a password of at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("The two passwords don't match. Retype the confirmation.");
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
      setError(
        err instanceof ApiError && err.status === 404
          ? "Account creation isn't turned on here. Ask your admin for an invite."
          : errorText(err),
      );
    } finally {
      setBusy(false);
    }
  }

  async function resend() {
    setResent(false);
    setResending(true);
    try {
      await api.auth.resendVerification(email);
    } finally {
      setResending(false);
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
        <div className="mb-8">
          <DaaluWordmark />
        </div>

        {done ? (
          <div className="space-y-4 motion-enter">
            <h1 className="text-xl font-semibold">Check your email</h1>
            <p className="text-sm text-muted">
              We&apos;ve sent a verification link to{" "}
              <span className="text-[color:var(--text)]">{email}</span>. Click it
              to activate your account, then sign in.
            </p>
            <div className="text-sm text-muted">
              Didn&apos;t get it? Check spam, then{" "}
              <button
                type="button"
                onClick={resend}
                disabled={resending}
                className="font-medium text-[color:var(--accent)] hover:underline disabled:opacity-60"
              >
                {resending ? "sending…" : "resend the link"}
              </button>
              {resent && (
                <span role="status" className="ml-1 text-accent motion-enter">
                  Sent.
                </span>
              )}
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

            <form onSubmit={onSubmit} className="space-y-3" noValidate>
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
                  inputMode="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  onBlur={() => setEmailTouched(true)}
                  aria-invalid={emailInvalid || undefined}
                  aria-describedby={emailInvalid ? "signup-email-hint" : undefined}
                  className={inputCls}
                />
                {emailInvalid && (
                  <span id="signup-email-hint" className="mt-1 block text-xs text-signal-critical">
                    Enter an email like name@example.com.
                  </span>
                )}
              </Field>
              <Field label="Organisation (optional)">
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
                  aria-invalid={pwShort || undefined}
                  aria-describedby="signup-pw-hint"
                  className={inputCls}
                />
                <span
                  id="signup-pw-hint"
                  className={`mt-1 block text-xs ${pwShort ? "text-signal-critical" : password ? "text-accent" : "text-muted"}`}
                >
                  {pwShort
                    ? `At least 8 characters: ${8 - password.length} more to go.`
                    : password
                      ? "Long enough."
                      : "At least 8 characters."}
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
                  aria-invalid={pwMismatch || undefined}
                  aria-describedby="signup-confirm-hint"
                  className={inputCls}
                />
                {confirm && (
                  <span
                    id="signup-confirm-hint"
                    aria-live="polite"
                    className={`mt-1 block text-xs ${pwMismatch ? "text-signal-critical" : "text-accent"}`}
                  >
                    {pwMismatch ? "Passwords don't match yet." : "Passwords match."}
                  </span>
                )}
              </Field>

              {error && (
                <div
                  role="alert"
                  className="text-sm text-signal-critical bg-signal-critical/10 border border-signal-critical/30 rounded-md px-3 py-2 motion-enter"
                >
                  {error}
                </div>
              )}

              <Button
                type="submit"
                variant="primary"
                pending={busy}
                disabledReason="Creating your account…"
                className="w-full h-10"
              >
                {busy ? "Creating account…" : "Create account"}
              </Button>
            </form>

            <p className="text-sm text-muted text-center pt-4">
              Already have an account?{" "}
              <a href={loginHref} className="font-medium text-[color:var(--accent)] hover:underline">
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
  "mt-1 w-full h-10 bg-bg-secondary/60 border border-line rounded-md px-3 text-sm focus:border-accent focus:bg-bg-secondary aria-[invalid=true]:border-signal-critical";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-sm font-medium">{label}</span>
      {children}
    </label>
  );
}
