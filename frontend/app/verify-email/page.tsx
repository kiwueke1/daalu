"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Check } from "lucide-react";
import { DaaluWordmark } from "@/components/brand/daalu-mark";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function VerifyEmailPage() {
  return (
    <Suspense fallback={null}>
      <VerifyEmail />
    </Suspense>
  );
}

type State = "verifying" | "success" | "error";

function VerifyEmail() {
  const router = useRouter();
  const search = useSearchParams();
  const { refresh } = useAuth();
  const token = search.get("token") || "";
  const next = search.get("next") || "/";
  const [state, setState] = useState<State>("verifying");
  const [resentEmail, setResentEmail] = useState("");
  const [resend, setResend] = useState<"idle" | "sending" | "sent">("idle");
  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(resentEmail);
  // React 18 StrictMode double-invokes effects in dev; guard so the
  // single-use token isn't consumed twice (second use 404s).
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    if (!token) {
      setState("error");
      return;
    }
    (async () => {
      try {
        await api.auth.verifyEmail(token);
        // The cookie is now set server-side; load the user, then go in.
        await refresh();
        setState("success");
        setTimeout(() => router.replace(next), 1200);
      } catch {
        setState("error");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

        {state === "verifying" && (
          <>
            <h1 className="text-xl font-semibold mb-1">Verifying your email…</h1>
            <p className="text-sm text-muted">One moment while we confirm your link.</p>
          </>
        )}

        {state === "success" && (
          <div className="motion-enter" role="status">
            <Check className="h-6 w-6 text-accent motion-success mb-2" aria-hidden />
            <h1 className="text-xl font-semibold mb-1">You&apos;re all set</h1>
            <p className="text-sm text-muted">
              Your account is verified. Taking you to your workspace…
            </p>
          </div>
        )}

        {state === "error" && (
          <div className="space-y-4 motion-enter">
            <h1 className="text-xl font-semibold">This link didn&apos;t work</h1>
            <p className="text-sm text-muted">
              The verification link is invalid or has expired. Enter your email
              and we&apos;ll send a fresh one.
            </p>
            <form
              className="space-y-1"
              onSubmit={async (e) => {
                e.preventDefault();
                if (!emailOk) return;
                setResend("sending");
                try {
                  await api.auth.resendVerification(resentEmail);
                } finally {
                  setResend("sent");
                }
              }}
            >
              <label htmlFor="resend-email" className="text-sm font-medium">
                Email
              </label>
              <div className="flex gap-2">
                <input
                  id="resend-email"
                  type="email"
                  autoComplete="email"
                  inputMode="email"
                  placeholder="name@example.com"
                  value={resentEmail}
                  onChange={(e) => {
                    setResentEmail(e.target.value);
                    setResend("idle");
                  }}
                  className="flex-1 min-w-0 h-10 bg-bg-secondary/60 border border-line rounded-md px-3 text-sm focus:border-accent"
                />
                <Button
                  type="submit"
                  variant="primary"
                  className="h-10"
                  pending={resend === "sending"}
                  disabled={!emailOk}
                  disabledReason={resend === "sending" ? "Sending…" : "Enter an email like name@example.com"}
                >
                  Send a new link
                </Button>
              </div>
              {resend === "sent" && (
                <p role="status" className="text-sm text-accent motion-enter pt-1">
                  If that email has an account waiting, a new link is on its way.
                </p>
              )}
            </form>
            <a
              href="/login"
              className="inline-block text-sm text-[color:var(--accent)] hover:underline"
            >
              ← Back to sign in
            </a>
          </div>
        )}
      </div>
    </div>
  );
}
