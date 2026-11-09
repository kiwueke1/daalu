import type { Metadata } from "next";
import { Quicksand } from "next/font/google";
import { AppShell } from "@/components/layout/app-shell";
import { Providers } from "./providers";
import "./globals.css";

// Brand wordmark face. Quicksand is the closest widely-available match to
// the rounded, single-story-`a` geometric letterforms in the original
// daalu logo art. Exposed as a CSS variable so the sidebar wordmark (and
// any future brand text) can opt in without changing the body font, which
// stays Inter. Self-hosted at build by next/font — no runtime fetch, no
// layout shift.
const quicksand = Quicksand({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-brand",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Daalu — AI Operations Command Center",
  description:
    "The live operational nervous system of your company. AI continuously gathers, reasons, and acts.",
};

// Runs before first paint: the saved theme, else the OS preference. Without
// it a light-mode user sees a dark flash on every load (the provider only
// runs after hydration).
const THEME_BOOT = `try{var t=localStorage.getItem("daalu-theme");if(t!=="light"&&t!=="dark"){t=matchMedia("(prefers-color-scheme: light)").matches?"light":"dark"}document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" className={quicksand.variable} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT }} />
      </head>
      <body className="ambient-bg min-h-screen text-[color:var(--text)]">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
