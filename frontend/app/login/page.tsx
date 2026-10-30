"use client";

import { Suspense, useState, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { DaaluMark } from "@/components/brand/daalu-mark";
import { useAuth } from "@/lib/auth";
import { api, type SSOProvider } from "@/lib/api";

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18Z"
      />
      <path
        fill="#FBBC05"
        d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33Z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.47.89 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58Z"
      />
    </svg>
  );
}

function LoginForm() {
  const router = useRouter();
  const search = useSearchParams();
  const { login, user } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // null = mode not yet known (probing /auth/config). While unknown we render
  // nothing so neither the password form nor the SSO buttons flash early.
  const [mode, setMode] = useState<"password" | "oidc" | null>(null);
  const [ssoPath, setSsoPath] = useState<string>("");
  const [providers, setProviders] = useState<SSOProvider[]>([]);
  // Whether to show the local email+password form and the "Create an account"
  // link. In oidc mode with self-signup on, BOTH SSO and the local form render.
  const [passwordLogin, setPasswordLogin] = useState(false);
  const [selfSignup, setSelfSignup] = useState(false);
  const next = search.get("next") || "/";

  useEffect(() => {
    if (user) router.replace(next);
  }, [user, next, router]);

  useEffect(() => {
    // Ask the hub which login mode is active. In SSO mode we DON'T bounce to
    // Keycloak — we render Daalu's own login screen with a "Continue with
    // Google" button. Daalu brokers Keycloak/Google on the user's behalf
    // (the button starts the OIDC flow with kc_idp_hint=google, so Keycloak's
    // own login page is skipped). Fall back to the password form if the probe
    // fails (e.g. legacy backend).
    let cancelled = false;
    api.auth
      .config()
      .then((cfg) => {
        if (cancelled) return;
        setSsoPath(cfg.sso_login_path);
        setProviders(cfg.sso_providers ?? []);
        setPasswordLogin(cfg.password_login_enabled ?? cfg.mode === "password");
        setSelfSignup(cfg.self_signup_enabled ?? false);
        setMode(cfg.mode);
      })
      .catch(() => {
        if (!cancelled) {
          setPasswordLogin(true);
          setMode("password");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function startSSO(idp?: string) {
    const params = new URLSearchParams({ next });
    if (idp) params.set("idp", idp);
    window.location.href = `${ssoPath}?${params.toString()}`;
  }

  if (mode === null) {
    // Still probing /auth/config — render nothing to avoid a flash.
    return null;
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      router.replace(next);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      // Trim the verbose body off the network-level error so the user
      // sees "Invalid email or password" instead of the full stack.
      const friendly = /401/.test(msg)
        ? "Invalid email or password."
        : /403/.test(msg)
        ? "This account is inactive."
        : "Sign-in failed. Try again.";
      setError(friendly);
    } finally {
      setBusy(false);
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

        <h1 className="text-xl font-semibold mb-1">Sign in</h1>
        <p className="text-sm text-muted mb-6">
          Access your live operational nervous system.
        </p>

        <div className="space-y-4">
          {/* SSO buttons — one per brokered provider (e.g. Google). */}
          {providers.length > 0 && (
            <div className="space-y-3">
              {providers.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => startSSO(p.id)}
                  className="w-full flex items-center justify-center gap-3 rounded-md text-sm font-medium py-2.5 bg-bg-secondary/70 border border-line hover:bg-bg-secondary transition-colors"
                >
                  {p.id === "google" && <GoogleIcon />}
                  Continue with {p.label}
                </button>
              ))}
            </div>
          )}

          {/* OIDC enabled but no specific IdP advertised — generic SSO start. */}
          {mode === "oidc" && providers.length === 0 && (
            <button
              type="button"
              onClick={() => startSSO()}
              className="w-full rounded-md text-sm font-medium py-2.5 transition-colors"
              style={{
                background: "var(--accent)",
                color: "var(--bg-base)",
                boxShadow: "0 0 22px var(--accent-glow)",
              }}
            >
              Continue with SSO
            </button>
          )}

          {/* Divider when both SSO and the local form are shown. */}
          {providers.length > 0 && passwordLogin && (
            <div className="flex items-center gap-3 text-[11px] uppercase tracking-wider text-muted">
              <span className="h-px flex-1 bg-line" />
              or
              <span className="h-px flex-1 bg-line" />
            </div>
          )}

          {/* Local email + password sign-in. */}
          {passwordLogin && (
            <form onSubmit={onSubmit} className="space-y-3">
              <label className="block">
                <span className="text-xs uppercase tracking-wider text-muted">Email</span>
                <input
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="mt-1 w-full bg-bg-secondary/60 border border-line rounded-md px-3 py-2 text-sm focus:outline-none focus:border-accent-blue/60 focus:bg-bg-secondary"
                />
              </label>
              <label className="block">
                <span className="text-xs uppercase tracking-wider text-muted">Password</span>
                <input
                  type="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="mt-1 w-full bg-bg-secondary/60 border border-line rounded-md px-3 py-2 text-sm focus:outline-none focus:border-accent-blue/60 focus:bg-bg-secondary"
                />
                <span className="mt-1 block text-[11px] text-muted">
                  Forgot your password? Ask your workspace admin to reset it.
                </span>
              </label>
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
                {busy ? "Signing in…" : "Sign in"}
              </button>
            </form>
          )}

          {/* Self-service signup entry point. */}
          {selfSignup && (
            <p className="text-[12px] text-muted text-center pt-1">
              New to Daalu?{" "}
              <a
                href={`/signup${next && next !== "/" ? `?next=${encodeURIComponent(next)}` : ""}`}
                className="text-[color:var(--accent)] hover:underline"
              >
                Create an account
              </a>
            </p>
          )}

          {mode === "oidc" && !passwordLogin && (
            <p className="text-[11px] text-muted text-center pt-1">
              Secured by single sign-on.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
