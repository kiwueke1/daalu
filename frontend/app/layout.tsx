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

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" className={quicksand.variable}>
      <body className="ambient-bg min-h-screen text-[color:var(--text)]">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
