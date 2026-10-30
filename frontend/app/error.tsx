"use client";

// Root error boundary. Next.js renders this in place of a route subtree that
// throws during client render, instead of the bare "Application error: a
// client-side exception has occurred" fallback. Keep it dependency-free and
// self-contained — it must render even when app context/providers are the
// thing that failed.
import { useEffect } from "react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surface to the browser console for diagnosis; the digest correlates
    // with the server-side log line in production builds.
    console.error("app-error-boundary", error);
  }, [error]);

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <div className="max-w-md text-center space-y-4">
        <h1 className="text-lg font-semibold">Something went wrong</h1>
        <p className="text-muted text-sm">
          An unexpected error occurred while rendering this page. You can try
          again, or head back to sign in.
        </p>
        <div className="flex items-center justify-center gap-3 pt-2">
          <button
            onClick={reset}
            className="rounded-md border px-4 py-2 text-sm hover:bg-[color:var(--surface-hover)]"
          >
            Try again
          </button>
          <a
            href="/login"
            className="rounded-md px-4 py-2 text-sm text-muted hover:underline"
          >
            Go to sign in
          </a>
        </div>
        {error?.digest && (
          <p className="text-muted text-[11px] pt-2">ref: {error.digest}</p>
        )}
      </div>
    </div>
  );
}
