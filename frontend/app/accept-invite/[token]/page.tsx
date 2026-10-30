"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { AlertTriangle, Loader2 } from "lucide-react";
import { DaaluMark } from "@/components/brand/daalu-mark";
import { api, type InvitePreview } from "@/lib/api";
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
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match.");
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
      const msg = err instanceof Error ? err.message : String(err);
      setError(
        /404/.test(msg)
          ? "This invite is no longer valid."
          : /409/.test(msg)
          ? "A user with this email already exists. Try signing in instead."
          : "Could not accept invite. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-3 mb-8">
          <DaaluMark />
          <div>
            <div className="text-sm font-semibold tracking-wide">Daalu</div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-muted">
              Operations
            </div>
          </div>
        </div>

        {loadError ? (
          <InviteError message={loadError} />
        ) : preview === null ? (
          <div className="flex items-center gap-2 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> Checking invite…
          </div>
        ) : (
          <>
            <h1 className="text-xl font-semibold mb-1">
              Join {preview.tenant_name || "Daalu"}
            </h1>
            <p className="text-sm text-muted mb-2">
              You're invited as{" "}
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

            <form onSubmit={onSubmit} className="space-y-3">
              <label className="block">
                <span className="text-xs uppercase tracking-wider text-muted">
                  Email
                </span>
                <input
                  readOnly
                  value={preview.email}
                  className="mt-1 w-full bg-bg-secondary/40 border border-line rounded-md px-3 py-2 text-sm text-muted"
                />
              </label>
              <label className="block">
                <span className="text-xs uppercase tracking-wider text-muted">
                  Display name (optional)
                </span>
                <input
                  type="text"
                  autoFocus
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  className="mt-1 w-full bg-bg-secondary/60 border border-line rounded-md px-3 py-2 text-sm focus:outline-none focus:border-accent-blue/60 focus:bg-bg-secondary"
                />
              </label>
              <label className="block">
                <span className="text-xs uppercase tracking-wider text-muted">
                  Password
                </span>
                <input
                  type="password"
                  required
                  minLength={8}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="mt-1 w-full bg-bg-secondary/60 border border-line rounded-md px-3 py-2 text-sm focus:outline-none focus:border-accent-blue/60 focus:bg-bg-secondary"
                />
              </label>
              <label className="block">
                <span className="text-xs uppercase tracking-wider text-muted">
                  Confirm password
                </span>
                <input
                  type="password"
                  required
                  minLength={8}
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  className="mt-1 w-full bg-bg-secondary/60 border border-line rounded-md px-3 py-2 text-sm focus:outline-none focus:border-accent-blue/60 focus:bg-bg-secondary"
                />
              </label>
              {error && (
                <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/30 rounded px-3 py-2 flex items-start gap-2">
                  <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                  <span>{error}</span>
                </div>
              )}
              <button
                type="submit"
                disabled={busy}
                className="w-full rounded-md text-sm font-medium py-2 transition-colors disabled:opacity-60 bg-accent text-black hover:opacity-90 inline-flex items-center justify-center gap-2"
              >
                {busy && <Loader2 className="h-4 w-4 animate-spin" />}
                Accept invite and sign in
              </button>
              <p className="text-xs text-muted text-center">
                Expires {new Date(preview.expires_at).toLocaleString()}.
              </p>
            </form>
          </>
        )}
      </div>
    </div>
  );
}

function InviteError({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-red-500/30 bg-red-500/10 p-4 text-sm">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0 text-red-400" />
        <p>{message}</p>
      </div>
    </div>
  );
}
