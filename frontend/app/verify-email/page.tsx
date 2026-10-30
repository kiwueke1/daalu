"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { DaaluMark } from "@/components/brand/daalu-mark";
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
        <div className="flex items-center gap-3 mb-8">
          <DaaluMark />
          <div>
            <div className="text-sm font-semibold tracking-wide">Daalu</div>
            <div className="text-[10px] tracking-[0.18em] text-muted">
              intelligent IT operations
            </div>
          </div>
        </div>

        {state === "verifying" && (
          <>
            <h1 className="text-xl font-semibold mb-1">Verifying your email…</h1>
            <p className="text-sm text-muted">One moment while we confirm your link.</p>
          </>
        )}

        {state === "success" && (
          <>
            <h1 className="text-xl font-semibold mb-1">You&apos;re all set</h1>
            <p className="text-sm text-muted">
              Your account is verified. Taking you to your workspace…
            </p>
          </>
        )}

        {state === "error" && (
          <div className="space-y-4">
            <h1 className="text-xl font-semibold">This link didn&apos;t work</h1>
            <p className="text-sm text-muted">
              The verification link is invalid or has expired. Enter your email
              and we&apos;ll send a fresh one.
            </p>
            <div className="flex gap-2">
              <input
                type="email"
                placeholder="you@example.com"
                value={resentEmail}
                onChange={(e) => setResentEmail(e.target.value)}
                className="flex-1 bg-bg-secondary/60 border border-line rounded-md px-3 py-2 text-sm focus:outline-none focus:border-accent-blue/60"
              />
              <button
                type="button"
                onClick={() => resentEmail && api.auth.resendVerification(resentEmail)}
                className="rounded-md text-sm font-medium px-3 transition-colors"
                style={{ background: "var(--accent)", color: "#031814" }}
              >
                Resend
              </button>
            </div>
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
