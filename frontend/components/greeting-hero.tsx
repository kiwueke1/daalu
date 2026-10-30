"use client";

import Image from "next/image";
import Link from "next/link";
import { useAuth } from "@/lib/auth";

/**
 * Greeting + operational AI core.
 *
 * Composition, back → front:
 *   1. Surface gradient (the .surface-hero base)
 *   2. Top-left ambient bloom (primary room light)
 *   3. Bottom-right rim wash
 *   4. Deep radial behind the core (the "core is alive" glow)
 *   5. AI-core PNG, positioned to overflow the right edge, masked to fade
 *      into surrounding atmosphere via radial alpha mask, screen-blended
 *      so its light *adds* to the surface rather than sitting on top
 *   6. Particle field — tiny floating emerald dots animated subtly
 *   7. UI content on the left (text)
 *
 * The PNG should never feel "boxed in" — its edges dissolve into the
 * surrounding darkness.
 */
export function GreetingHero({
  critical = 0,
  open = 0,
  loaded = true,
}: {
  /** Open critical alerts. */
  critical?: number;
  /** All open alerts. */
  open?: number;
  /** False while the alert query is still loading: say nothing about status. */
  loaded?: boolean;
}) {
  const { user } = useAuth();
  const firstName = (user?.full_name || user?.email || "there")
    .split(/[\s.@]/)[0]
    .replace(/^./, (c) => c.toUpperCase());
  const greeting = greetingFor(new Date());

  return (
    <section className="surface-hero relative overflow-hidden p-7 md:p-9 md:min-h-[200px]">
      {/* Lighting pass 1 — wide top-left ambient bloom */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(80% 100% at 8% 0%, rgba(var(--accent-rgb),0.16) 0%, transparent 55%)",
        }}
      />
      {/* Lighting pass 2 — bottom-right rim */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 90% at 95% 100%, rgba(var(--accent-rgb),0.10) 0%, transparent 60%)",
        }}
      />
      {/* Lighting pass 3 — deep radial behind the AI core */}
      <div
        aria-hidden
        className="pointer-events-none absolute"
        style={{
          right: "2%",
          top: "18%",
          width: 260,
          height: 200,
          background:
            "radial-gradient(50% 50% at 50% 50%, rgba(var(--accent-rgb),0.30) 0%, transparent 65%)",
          filter: "blur(8px)",
        }}
      />

      {/* The AI core PNG.
       *
       * In dark mode the PNG (bright emerald on near-black) blends with
       * `screen` so the chip's light adds to the surface. In light mode
       * `screen` washes out completely — so we flip the image with
       * `invert(1) hue-rotate(180deg)` (which gives "dark emerald on
       * light"), then blend with `multiply` so the chip burns into the
       * white surface as visible dark-emerald linework.
       *
       * The selectors are placed on the wrapping <div> so the values
       * react to `data-theme` on <html>.
       */}
      <div
        aria-hidden
        className="hero-core pointer-events-none absolute hidden md:block"
        style={{
          right: "-30px",
          top: "10px",
          width: 290,
          height: 220,
          maskImage:
            "radial-gradient(60% 60% at 50% 50%, rgba(0,0,0,1) 38%, rgba(0,0,0,0.7) 62%, rgba(0,0,0,0) 90%)",
          WebkitMaskImage:
            "radial-gradient(60% 60% at 50% 50%, rgba(0,0,0,1) 38%, rgba(0,0,0,0.7) 62%, rgba(0,0,0,0) 90%)",
        }}
      >
        <Image
          src="/assets/hero/ai-core.png"
          alt=""
          fill
          sizes="290px"
          priority
          style={{ objectFit: "contain" }}
        />
      </div>
      <style jsx>{`
        :global(html[data-theme="dark"]) .hero-core {
          opacity: 0.82;
          mix-blend-mode: screen;
          filter: blur(0.2px);
        }
        :global(html[data-theme="light"]) .hero-core {
          opacity: 0.78;
          mix-blend-mode: multiply;
          filter: invert(1) hue-rotate(180deg) saturate(1.2) brightness(0.9);
        }
      `}</style>

      {/* Particle field — subtle floating emerald dots */}
      <ParticleField />

      {/* Foreground content */}
      <div className="relative md:max-w-[70%] z-[1]">
        <div className="text-[10px] uppercase tracking-[0.22em] text-muted mb-3 flex items-center gap-2">
          <span
            className="inline-block h-1.5 w-1.5 rounded-full"
            style={{
              background: "var(--accent)",
              boxShadow: "0 0 8px var(--accent-bloom)",
            }}
          />
          AI operating ·{" "}
          {greeting === "morning"
            ? "Morning summary"
            : greeting === "afternoon"
            ? "Afternoon summary"
            : "Evening summary"}
        </div>
        <h1 className="text-[28px] md:text-[34px] leading-tight font-semibold tracking-tight mb-3">
          Good {greeting}, {firstName}.
        </h1>
        <p className="text-[14px] text-muted max-w-md leading-relaxed">
          <StatusLine critical={critical} open={open} loaded={loaded} />
        </p>
      </div>
    </section>
  );
}

/** Status sentence computed from open alerts, never a static "all good". */
function StatusLine({
  critical,
  open,
  loaded,
}: {
  critical: number;
  open: number;
  loaded: boolean;
}) {
  if (!loaded) return <>Checking open alerts…</>;
  if (open === 0) return <>No open alerts. Everything is running smoothly.</>;
  const n = critical > 0 ? critical : open;
  const label =
    critical > 0
      ? `${n} critical alert${n === 1 ? "" : "s"}`
      : `${n} open alert${n === 1 ? "" : "s"}`;
  return (
    <>
      <Link
        href="/alerts?tab=open"
        className="font-medium hover:underline"
        style={{ color: critical > 0 ? "var(--critical)" : "var(--warning)" }}
      >
        {label} need{n === 1 ? "s" : ""} attention
      </Link>
      {critical > 0 && open > critical && (
        <> ({open} open in total)</>
      )}
      . The briefing below has the details.
    </>
  );
}

function greetingFor(d: Date): "morning" | "afternoon" | "evening" {
  const h = d.getHours();
  if (h < 12) return "morning";
  if (h < 18) return "afternoon";
  return "evening";
}

/**
 * Particle field. 18 dots placed pseudo-randomly across the hero,
 * each on its own animation phase. Pure CSS — no canvas, no JS.
 */
function ParticleField() {
  const particles = Array.from({ length: 18 }, (_, i) => i);
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0">
      {particles.map((i) => {
        // Pseudo-random but deterministic so the layout is stable.
        const seed = (i * 9301 + 49297) % 233280;
        const x = (seed % 100) / 100;
        const y = ((seed * 17) % 100) / 100;
        const size = 1 + ((seed * 3) % 3);
        const delay = ((seed * 7) % 100) / 25;
        const dur = 4 + ((seed * 11) % 6);
        return (
          <span
            key={i}
            className="absolute rounded-full"
            style={{
              left: `${x * 100}%`,
              top: `${y * 100}%`,
              width: size,
              height: size,
              background: "var(--accent)",
              boxShadow: "0 0 6px var(--accent-bloom)",
              opacity: 0.35,
              animation: `particleFloat ${dur}s ease-in-out ${delay}s infinite alternate`,
            }}
          />
        );
      })}
      <style jsx>{`
        @keyframes particleFloat {
          0% {
            transform: translateY(0) translateX(0);
            opacity: 0.2;
          }
          50% {
            opacity: 0.5;
          }
          100% {
            transform: translateY(-12px) translateX(6px);
            opacity: 0.25;
          }
        }
      `}</style>
    </div>
  );
}
