"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { AlertTriangle, Loader2 } from "lucide-react";
import { DaaluWordmark } from "@/components/brand/daalu-mark";
import { Button } from "@/components/ui/button";
import { api, ApiError, type InvitePreview } from "@/lib/api";
import { errorText } from "@/lib/errors";
import { useAuth } from "@/lib/auth";

/**
 * /accept-invite/[token] — public page the invitee lands on when
 * they click the link in their email.
 *
 * Flow:
 *   1. Load InvitePreview from /auth/invite/{token} (no auth).
 *   2. Show "You're invited to <tenant> as <role>".
 *   3. User picks a display name + password and submits.
 *   4. POST /auth/redeem-invite creates the User + sets the session
 *      cookie + returns the user. Redirect to /.
 *
 * Auth probing is disabled for this path in AuthProvider and the
 * fetch wrapper (see lib/auth.tsx + lib/api.ts) so a 401 from a
 * stale state doesn't bounce the invitee to /login.
 */
export default function AcceptInvitePage() {
  // Next 14: route params are a plain object read via the useParams() hook —
  // the same idiom every other dynamic page here uses (/alerts/[id], etc.).
  // (Do NOT `use(props.params)`; that's the Next 15 API and throws
  // "An unsupported type was passed to use()" on 14.2.5.)
  const params = useParams<{ token: string }>();
  const token = decodeURIComponent(params.token);
  const router = useRouter();
  const { refresh } = useAuth();

  const [preview, setPreview] = useState<InvitePreview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pwShort = password.length > 0 && password.length < 8;
  const pwMismatch = confirm.length > 0 && confirm !== password;

  useEffect(() => {
    let cancelled = false;
    api.auth
      .previewInvite(token)
      .then((p) => {
        if (!cancelled) setPreview(p);
      })
      .catch((err: Error) => {
        if (!cancelled) {
          // We deliberately render a generic message — no need to
          // distinguish "expired" from "revoked" to phishers.
          setLoadError(
            "This invite is invalid or has expired. Ask your admin to send a new one.",
          );
          // eslint-disable-next-line no-console
          console.error("invite.preview", err);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
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
      await api.auth.redeemInvite({
        token,
        full_name: fullName.trim() || null,
        password,
      });
      // The cookie is now set; pull /auth/me so the AuthProvider
      // populates state for the rest of the app.
      await refresh();
      router.replace("/");
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      setError(
        status === 404
          ? "This invite is no longer valid. Ask your admin to send a new one."
          : status === 409
          ? "An account with this email already exists. Sign in instead."
          : errorText(err),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8">
          <DaaluWordmark />
        </div>

        {loadError ? (
          <InviteError message={loadError} />
        ) : preview === null ? (
          <div role="status" className="flex items-center gap-2 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Checking invite…
          </div>
        ) : (
          <div className="motion-enter">
            <h1 className="text-xl font-semibold mb-1">
              Join {preview.tenant_name || "Daalu"}
            </h1>
            <p className="text-sm text-muted mb-2">
              You&apos;re invited as{" "}
              <span className="text-[color:var(--text)] font-medium">
                {preview.role}
              </span>
              {preview.inviter_name ? ` by ${preview.inviter_name}` : ""}.
            </p>
            {preview.message && (
              <blockquote className="text-sm text-muted border-l-2 border-line pl-3 mb-4">
                {preview.message}
              </blockquote>
            )}

            <form onSubmit={onSubmit} className="space-y-3" noValidate>
              <label className="block">
                <span className="text-sm font-medium">Email</span>
                <input
                  readOnly
                  type="email"
                  autoComplete="email"
                  value={preview.email}
                  className="mt-1 w-full h-10 bg-bg-secondary/40 border border-dashed border-line rounded-md px-3 text-sm text-muted"
                />
              </label>
              <label className="block">
                <span className="text-sm font-medium">Display name (optional)</span>
                <input
                  type="text"
                  autoFocus
                  autoComplete="name"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  className={inputCls}
                />
              </label>
              <label className="block">
                <span className="text-sm font-medium">Password</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  minLength={8}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  aria-invalid={pwShort || undefined}
                  aria-describedby="invite-pw-hint"
                  className={inputCls}
                />
                <span
                  id="invite-pw-hint"
                  className={`mt-1 block text-xs ${pwShort ? "text-signal-critical" : password ? "text-accent" : "text-muted"}`}
                >
                  {pwShort
                    ? `At least 8 characters: ${8 - password.length} more to go.`
                    : password
                      ? "Long enough."
                      : "At least 8 characters."}
                </span>
              </label>
              <label className="block">
                <span className="text-sm font-medium">Confirm password</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  minLength={8}
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  aria-invalid={pwMismatch || undefined}
                  aria-describedby="invite-confirm-hint"
                  className={inputCls}
                />
                {confirm && (
                  <span
                    id="invite-confirm-hint"
                    aria-live="polite"
                    className={`mt-1 block text-xs ${pwMismatch ? "text-signal-critical" : "text-accent"}`}
                  >
                    {pwMismatch ? "Passwords don't match yet." : "Passwords match."}
                  </span>
                )}
              </label>
              {error && (
                <div
                  role="alert"
                  className="text-sm text-signal-critical bg-signal-critical/10 border border-signal-critical/30 rounded-md px-3 py-2 flex items-start gap-2 motion-enter"
                >
                  <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" aria-hidden />
                  <span>{error}</span>
                </div>
              )}
              <Button
                type="submit"
                variant="primary"
                pending={busy}
                disabledReason="Accepting invite…"
                className="w-full h-10"
              >
                Accept invite and sign in
              </Button>
              <p className="text-xs text-muted text-center">
                Expires {new Date(preview.expires_at).toLocaleString()}.
              </p>
            </form>
          </div>
        )}
      </div>
    </div>
  );
}

function InviteError({ message }: { message: string }) {
  return (
    <div role="alert" className="rounded-md border border-signal-critical/30 bg-signal-critical/10 p-4 text-sm motion-enter">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0 text-signal-critical" aria-hidden />
        <p>{message}</p>
      </div>
    </div>
  );
}

const inputCls =
  "mt-1 w-full h-10 bg-bg-secondary/60 border border-line rounded-md px-3 text-sm focus:border-accent focus:bg-bg-secondary aria-[invalid=true]:border-signal-critical";
