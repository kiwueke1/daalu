"use client";

import Link from "next/link";
import { ApiError } from "@/lib/api";
import { buttonClass } from "@/components/ui/button";

/**
 * The SoT endpoints answer 409 "no nautobot integration for tenant …" until
 * Nautobot is connected. That's a setup step, not an error: these helpers
 * turn it into LoadState's neutral "not set up yet" panel.
 */
export function isSotNotSetUp(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    /nautobot integration/i.test(error.detail || error.body || "")
  );
}

export const SOT_NOT_SET_UP = {
  when: isSotNotSetUp,
  title: "Source of Truth isn't connected yet",
  hint: "Devices, drift and proposals come from Nautobot. Connect a hosted or your own Nautobot and they appear here.",
  action: (
    <Link href="/onboarding/sot" className={buttonClass("primary", "sm")}>
      Set up Nautobot
    </Link>
  ),
};
