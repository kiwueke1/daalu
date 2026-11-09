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

/**
 * The text wordmark ("daalu" + tagline) used by the sidebar header,
 * rendered as real text in the brand face so it stays legible at any size
 * and takes its colours from the theme tokens (no washed-out raster in
 * light mode). Prefer this over `DaaluMark` wherever the name is shown.
 */
export function DaaluWordmark({
  size = "md",
  tagline = true,
}: {
  size?: "sm" | "md";
  tagline?: boolean;
}) {
  return (
    <div aria-label="Daalu" role="img">
      <div
        aria-hidden
        className={
          "leading-none font-bold tracking-[-0.02em] text-[color:var(--text)] " +
          (size === "sm" ? "text-[26px]" : "text-[34px]")
        }
        style={{ fontFamily: "var(--font-brand)" }}
      >
        daalu
      </div>
      {tagline && (
        <div
          aria-hidden
          className="mt-1.5 text-[12.5px] font-medium tracking-[0.04em] text-[color:var(--accent)]"
          style={{ fontFamily: "var(--font-brand)" }}
        >
          intelligent IT operations
        </div>
      )}
    </div>
  );
}
