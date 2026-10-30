import type { Config } from "tailwindcss";
import typography from "@tailwindcss/typography";

/**
 * A theme colour that supports Tailwind's opacity modifier (`bg-accent/10`).
 * A bare `var(--x)` can't take an alpha, so Tailwind silently emitted no CSS
 * for every `/NN` class (≈450 of them: untinted badges, invisible status
 * dots, native-grey inputs). color-mix works with any CSS colour the variable
 * holds, hex or rgba, in both themes.
 */
const tok = (v: string) =>
  `color-mix(in srgb, var(${v}) calc(<alpha-value> * 100%), transparent)`;

/** Markdown (`prose`) colours from the theme tokens, so both themes match. */
const proseColors = {
  "--tw-prose-body": "var(--text)",
  "--tw-prose-headings": "var(--text)",
  "--tw-prose-lead": "var(--text-muted)",
  "--tw-prose-links": "var(--accent)",
  "--tw-prose-bold": "var(--text)",
  "--tw-prose-counters": "var(--text-muted)",
  "--tw-prose-bullets": "var(--text-muted)",
  "--tw-prose-hr": "var(--line-strong)",
  "--tw-prose-quotes": "var(--text)",
  "--tw-prose-quote-borders": "var(--line-strong)",
  "--tw-prose-captions": "var(--text-muted)",
  "--tw-prose-code": "var(--text)",
  "--tw-prose-pre-code": "var(--text)",
  "--tw-prose-pre-bg": "var(--bg-base)",
  "--tw-prose-th-borders": "var(--line-strong)",
  "--tw-prose-td-borders": "var(--line)",
};

/**
 * Token mapping. Tailwind classes resolve to CSS variables defined in
 * `globals.css`, so swapping `data-theme` on <html> instantly recolors
 * the entire UI without rebuilding.
 */
const config: Config = {
  darkMode: ["class", '[data-theme="dark"]'],
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: {
          base: tok("--bg-base"),
          surface: tok("--bg-surface"),
          card: tok("--bg-card"),
          elevated: tok("--bg-elevated"),
          // Aliases the older code still uses — left pointing at sensible
          // surfaces so nothing breaks during the migration.
          secondary: tok("--bg-surface"),
        },
        // Body text colour: `text-fg`, `text-fg/80`.
        fg: tok("--text"),
        line: tok("--line"),
        "line-strong": tok("--line-strong"),
        muted: tok("--text-muted"),
        accent: {
          DEFAULT: tok("--accent"),
          soft: tok("--accent-soft"),
          glow: tok("--accent-glow"),
          emerald: tok("--accent"),
          // Legacy aliases — point at the same emerald so any pre-rewrite
          // class using `accent-blue/cyan/violet` still renders in-palette
          // instead of throwing the design off.
          blue: tok("--accent"),
          cyan: tok("--accent"),
          violet: tok("--accent"),
          amber: tok("--warning"),
          red: tok("--critical"),
        },
        signal: {
          critical: tok("--critical"),
          warning: tok("--warning"),
          info: tok("--info"),
        },
        circuit: tok("--circuit"),
        "circuit-strong": tok("--circuit-strong"),
      },
      fontFamily: {
        sans: [
          "Inter",
          "Geist",
          "Satoshi",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
      },
      boxShadow: {
        glow: "0 0 32px -6px var(--accent-glow)",
        glowSoft: "0 0 18px -2px var(--accent-glow)",
        glass: "0 8px 30px rgba(0,0,0,0.35)",
      },
      backgroundImage: {
        "hero-grad":
          "radial-gradient(120% 100% at 0% 0%, rgba(34,211,166,0.18) 0%, rgba(11,22,18,0) 60%), radial-gradient(120% 100% at 100% 100%, rgba(34,211,166,0.10) 0%, rgba(11,22,18,0) 60%)",
        "card-grad":
          "linear-gradient(180deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0) 100%)",
      },
      keyframes: {
        shimmer: {
          "0%, 100%": { opacity: "0.6" },
          "50%": { opacity: "1" },
        },
        pulseSoft: {
          "0%, 100%": { boxShadow: "0 0 0 0 var(--accent-glow)" },
          "50%": { boxShadow: "0 0 0 6px transparent" },
        },
        fadeIn: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        // Typing-indicator bounce. Staggered per dot at the call site so the
        // three read as a travelling wave rather than pulsing together.
        bounceDot: {
          "0%, 80%, 100%": { transform: "translateY(0)", opacity: "0.3" },
          "40%": { transform: "translateY(-5px)", opacity: "1" },
        },
      },
      typography: {
        DEFAULT: {
          css: {
            ...proseColors,
            // Invert (dark:prose-invert) uses the same tokens: the tokens
            // already switch with the theme.
            ...Object.fromEntries(
              Object.entries(proseColors).map(([k, v]) => [
                k.replace("--tw-prose-", "--tw-prose-invert-"),
                v,
              ])
            ),
            "code::before": { content: "none" },
            "code::after": { content: "none" },
          },
        },
      },
      animation: {
        shimmer: "shimmer 3.2s ease-in-out infinite",
        pulseSoft: "pulseSoft 2.8s ease-out infinite",
        fadeIn: "fadeIn 280ms ease-out",
        bounceDot: "bounceDot 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [typography],
};

export default config;
