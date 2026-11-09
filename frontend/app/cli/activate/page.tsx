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
import { AlertTriangle, CheckCircle2, Loader2, ShieldCheck, Terminal } from "lucide-react";
import { DaaluWordmark } from "@/components/brand/daalu-mark";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { errorText } from "@/lib/errors";

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
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);

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
      setError(errorText(e));
      setState("error");
    }
  }

  async function approve() {
    setBusy("approve");
    try {
      await api.cli.deviceApprove(code.trim());
      setState("approved");
    } catch (e) {
      setError(errorText(e));
      setState("error");
    } finally {
      setBusy(null);
    }
  }

  async function deny() {
    setBusy("deny");
    try {
      await api.cli.deviceDeny(code.trim());
    } catch {
      /* best-effort */
    }
    setBusy(null);
    setState("denied");
  }

  const cmd = <code className="font-mono text-[0.95em] px-1 rounded bg-bg-elevated">daalu login</code>;

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4 py-10 overflow-hidden">
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 50% at 50% 30%, rgba(var(--accent-rgb),0.10), transparent 60%)",
        }}
      />
      <div className="relative w-full max-w-md">
        <div className="mb-6 flex justify-center">
          <DaaluWordmark size="sm" />
        </div>

        <div className="surface rounded-2xl p-6 sm:p-7">
          {state === "loading" && (
            <p role="status" className="text-sm text-muted flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Checking
              your sign-in request…
            </p>
          )}

          {state === "ready" && (
            <div className="space-y-5 motion-enter">
              <div className="flex items-start gap-3">
                <Terminal className="h-5 w-5 mt-1 text-accent shrink-0" aria-hidden />
                <div>
                  <h1 className="text-xl font-semibold">Authorize this terminal?</h1>
                  <p className="text-sm text-muted mt-1">
                    {clientName ? (
                      <>
                        <strong className="text-fg">{clientName}</strong> wants to
                        sign in to Daalu as you.
                      </>
                    ) : (
                      "A terminal wants to sign in to Daalu as you."
                    )}
                  </p>
                </div>
              </div>

              {search.get("code") ? (
                <div>
                  <div id="code-label" className="text-sm font-medium mb-1.5">
                    Check this matches the code in your terminal
                  </div>
                  <div
                    className="w-full rounded-lg border border-line bg-bg-elevated px-3 py-3 text-center text-2xl font-mono font-semibold uppercase tracking-[0.25em] select-all"
                    aria-labelledby="code-label"
                  >
                    {code}
                  </div>
                </div>
              ) : (
                <label className="block">
                  <span className="block text-sm font-medium mb-1.5">
                    Code shown in your terminal
                  </span>
                  <input
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    placeholder="XXXX-XXXX"
                    autoComplete="one-time-code"
                    spellCheck={false}
                    className="w-full h-10 px-3 rounded-lg bg-bg-elevated border border-line text-base font-mono uppercase tracking-widest placeholder:text-muted placeholder:normal-case"
                  />
                </label>
              )}

              <div
                role="note"
                className="flex items-start gap-2.5 rounded-lg px-3 py-2.5 text-sm"
                style={{
                  background: "color-mix(in srgb, var(--warning) 10%, var(--bg-card))",
                  border: "1px solid color-mix(in srgb, var(--warning) 40%, var(--line))",
                }}
              >
                <AlertTriangle
                  className="h-4 w-4 mt-0.5 shrink-0"
                  style={{ color: "var(--warning)" }}
                  aria-hidden
                />
                <p>
                  <strong className="font-semibold">Only approve if you just ran {cmd} on
                  your own machine.</strong>{" "}
                  It gets a token that acts as you until you revoke it in
                  Settings → API tokens.
                </p>
              </div>

              {/* Side by side only when both fit; otherwise stacked full
                  width with Authorize first. Fixed height, never squashed. */}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="primary"
                  onClick={approve}
                  pending={busy === "approve"}
                  disabled={!code.trim() || busy !== null}
                  disabledReason={
                    busy === "approve"
                      ? "Authorizing…"
                      : !code.trim()
                        ? "Enter the code from your terminal"
                        : undefined
                  }
                  icon={<ShieldCheck className="h-4 w-4" aria-hidden />}
                  className="grow shrink-0 basis-52 min-w-fit h-10"
                >
                  Authorize this terminal
                </Button>
                <Button
                  variant="secondary"
                  onClick={deny}
                  pending={busy === "deny"}
                  disabled={busy !== null}
                  className="grow shrink-0 basis-44 min-w-fit h-10"
                >
                  Deny — don&apos;t sign in
                </Button>
              </div>
            </div>
          )}

          {state === "approved" && (
            <div role="status" className="text-center space-y-2 motion-enter">
              <CheckCircle2
                className="h-10 w-10 mx-auto text-accent motion-success"
                aria-hidden
              />
              <h1 className="text-xl font-semibold">Terminal authorized.</h1>
              <p className="text-sm text-muted">
                You can close this tab. Your terminal is now signed in to Daalu.
              </p>
            </div>
          )}

          {state === "denied" && (
            <div role="status" className="space-y-2 motion-enter">
              <h1 className="text-xl font-semibold">Request denied</h1>
              <p className="text-sm text-muted">
                No token was issued. If this was you, run {cmd} again.
              </p>
            </div>
          )}

          {state === "error" && (
            <div role="alert" className="space-y-2 motion-enter">
              <h1 className="text-xl font-semibold">That didn&apos;t work</h1>
              <p className="text-sm text-muted">
                {error || "The sign-in request is invalid or has expired."} Run{" "}
                {cmd} again to get a fresh request.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
