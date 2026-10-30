"use client";

/**
 * Standalone cluster-tunnel onboarding workflow.
 *
 * Reachable directly at `/onboarding/cluster` for operators who only
 * want to set up the VPN side without walking through the full
 * integrations wizard. Renders the same <ClusterWorkflow /> component
 * the general wizard embeds, so the flow is identical — just framed
 * as its own page here.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Boxes } from "lucide-react";
import { OnboardingHeader } from "@/components/onboarding/onboarding-header";
import { ClusterWorkflow } from "@/components/onboarding/cluster-workflow";
import { HUB } from "@/lib/hub-routes";

export default function ClusterOnboardingPage() {
  const router = useRouter();
  return (
    <div className="space-y-6 max-w-[860px]">
      <OnboardingHeader
        backLink
        icon={Boxes}
        title="Cluster tunnel onboarding"
        description={
          <>
            Set up a secure tunnel to a managed cluster, end to end. Use the{" "}
            <Link href={HUB.onboarding} className="underline">
              full onboarding
            </Link>{" "}
            if you also need notifications and observability.
          </>
        }
      />

      <ClusterWorkflow
        onComplete={() => router.push(HUB.clusters)}
        allowAnother
      />
    </div>
  );
}
