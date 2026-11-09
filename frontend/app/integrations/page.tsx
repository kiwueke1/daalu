"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { LoadState } from "@/components/ui/load-state";

/**
 * Integrations used to be its own sidebar entry. The view now lives
 * inside /managed-infra as the Integrations tab — wiring external
 * systems (channels, ingest sources, BYO SoT) is managed-infra work.
 * Show a loading state while the client redirect runs, so the page
 * is never blank.
 */
export default function IntegrationsRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/managed-infra?tab=integrations");
  }, [router]);
  return (
    <div className="max-w-[1200px]">
      <LoadState query={{ isLoading: true, isError: false }} loadingText="Opening Integrations…">
        {null}
      </LoadState>
    </div>
  );
}
