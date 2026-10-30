"use client";

/**
 * /cli/activate — the device-flow approval screen.
 *
 * `daalu login` opens the browser here with ?code=USER-CODE. We show which
 * machine is asking, then Authorize / Deny. Approving binds the pending device
 * authorization to the logged-in user; the CLI (polling) then redeems a token.
 *
 * If the visitor isn't signed in, the api wrapper 401s and redirects to
 * /login?next=/cli/activate?code=… — they come back here after signing in.
 */

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Loader2, ShieldCheck, Terminal } from "lucide-react";
import { DaaluMark } from "@/components/brand/daalu-mark";
import { api } from "@/lib/api";

export default function CliActivatePage() {
  return (
    <Suspense fallback={null}>
      <CliActivate />
    </Suspense>
  );
}

type State = "loading" | "ready" | "approved" | "denied" | "error";

function CliActivate() {
  const search = useSearchParams();
  const [code, setCode] = useState(search.get("code") || "");
  const [state, setState] = useState<State>("loading");
  const [clientName, setClientName] = useState<string | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    const c = search.get("code");
    if (!c) {
      setState("ready"); // let the user type the code in manually
      return;
    }
    void loadPending(c);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadPending(c: string) {
    setState("loading");
    try {
      const p = await api.cli.devicePending(c.trim());
      setClientName(p.client_name);
      if (p.status === "approved" || p.status === "redeemed") {
        setState("approved");
      } else if (p.status === "denied") {
        setState("denied");
      } else {
        setState("ready");
      }
    } catch (e) {
      setError(String(e));
      setState("error");
    }
  }

  async function approve() {
    setState("loading");
    try {
      await api.cli.deviceApprove(code.trim());
      setState("approved");
    } catch (e) {
      setError(String(e));
      setState("error");
    }
  }

  async function deny() {
    try {
      await api.cli.deviceDeny(code.trim());
    } catch {
      /* best-effort */
    }
    setState("denied");
  }

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4 overflow-hidden">
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 50% at 50% 30%, rgba(var(--accent-rgb),0.10), transparent 60%)",
        }}
      />
      <div className="relative w-full max-w-sm">
        <div className="flex items-center gap-3 mb-8">
          <DaaluMark />
          <div>
            <div className="text-sm font-semibold tracking-wide">Daalu</div>
            <div className="text-[10px] tracking-[0.18em] text-muted">
              command-line sign-in
            </div>
          </div>
        </div>

        {state === "loading" && (
          <p className="text-sm text-muted flex items-center gap-2">
            <Loader2 className="h-4 w-4 animate-spin" /> Checking your login
            request…
          </p>
        )}

        {state === "ready" && (
          <div className="space-y-4">
            <div className="flex items-start gap-3">
              <Terminal className="h-5 w-5 mt-0.5 text-accent" />
              <div>
                <h1 className="text-xl font-semibold">Authorize this terminal?</h1>
                <p className="text-sm text-muted mt-1">
                  {clientName ? (
                    <>
                      <strong className="text-fg">
                        {clientName}
                      </strong>{" "}
                      wants to sign in to Daalu as you.
                    </>
                  ) : (
                    "A terminal wants to sign in to Daalu as you."
                  )}
                </p>
              </div>
            </div>

            {search.get("code") ? (
              <div>
                <div className="text-xs text-muted mb-1.5">
                  Check this matches the code in your terminal
                </div>
                <div
                  className="w-full rounded-lg border border-line bg-bg-elevated px-3 py-3 text-center text-2xl font-mono font-semibold uppercase tracking-[0.25em] select-all"
                  aria-label="Login code"
                >
                  {code}
                </div>
              </div>
            ) : (
              <label className="block">
                <span className="block text-xs text-muted mb-1.5">
                  Code shown in your terminal
                </span>
                <input
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="XXXX-XXXX"
                  autoComplete="off"
                  spellCheck={false}
                  className="w-full h-10 px-3 rounded-lg bg-bg-elevated border border-line text-base font-mono uppercase tracking-widest placeholder:text-muted/50 placeholder:normal-case"
                />
              </label>
            )}

            <p className="text-[11px] text-muted">
              Only approve if you just ran{" "}
              <code className="text-[10.5px]">daalu login</code> on this machine.
              Approving grants it a token that acts as you until you revoke it in
              Settings → API tokens.
            </p>

            <div className="flex gap-2">
              <button
                type="button"
                onClick={approve}
                disabled={!code.trim()}
                className="flex-1 h-9 rounded-lg bg-gradient-to-r from-accent-emerald to-accent-cyan text-bg-base text-sm font-medium disabled:opacity-50 inline-flex items-center justify-center gap-1.5"
              >
                <ShieldCheck className="h-4 w-4" /> Authorize
              </button>
              <button
                type="button"
                onClick={deny}
                className="h-9 px-4 rounded-lg border border-line text-muted hover:text-[color:var(--critical)] text-sm"
              >
                Deny
              </button>
            </div>
          </div>
        )}

        {state === "approved" && (
          <div className="space-y-2">
            <h1 className="text-xl font-semibold">You&apos;re all set ✓</h1>
            <p className="text-sm text-muted">
              Return to your terminal — <code className="text-[11px]">daalu</code>{" "}
              is now signed in. You can close this tab.
            </p>
          </div>
        )}

        {state === "denied" && (
          <div className="space-y-2">
            <h1 className="text-xl font-semibold">Request denied</h1>
            <p className="text-sm text-muted">
              No token was issued. If this was you, run{" "}
              <code className="text-[11px]">daalu login</code> again.
            </p>
          </div>
        )}

        {state === "error" && (
          <div className="space-y-2">
            <h1 className="text-xl font-semibold">That didn&apos;t work</h1>
            <p className="text-sm text-muted">
              {error || "The login request is invalid or has expired."} Run{" "}
              <code className="text-[11px]">daalu login</code> again to get a
              fresh request.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
