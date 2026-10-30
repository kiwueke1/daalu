"use client";

import Image from "next/image";

/**
 * The Daalu brand mark — a 44×44 image lockup of the drum + circuit
 * symbol with the wordmark beneath, swapped per theme.
 *
 * Both PNGs are rendered side-by-side and gated by the
 * ``data-theme`` attribute on <html> via the .daalu-mark-{dark,light}
 * classes in globals.css. Doing it in CSS (rather than reading the
 * theme from a React state) avoids the hydration flash where the
 * server-rendered HTML can't know which theme the client will pick.
 *
 * Used by the sidebar header, the login page, and the accept-invite
 * page so the brand reads identically in every entry point.
 */
export function DaaluMark({ size = 44 }: { size?: number }) {
  return (
    <div
      className="relative shrink-0"
      style={{ width: size, height: size }}
      aria-label="Daalu"
    >
      <Image
        src="/assets/brand/logo_dark.png"
        alt=""
        width={size}
        height={size}
        priority
        className="daalu-mark-dark absolute inset-0"
        style={{ objectFit: "contain" }}
      />
      <Image
        src="/assets/brand/logo_light.png"
        alt=""
        width={size}
        height={size}
        priority
        className="daalu-mark-light absolute inset-0"
        style={{ objectFit: "contain" }}
      />
    </div>
  );
}
