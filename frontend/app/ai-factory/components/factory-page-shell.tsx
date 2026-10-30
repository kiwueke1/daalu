"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

/**
 * Shared chrome for a factory-operation detail page (diagnostics, benchmark,
 * observability, reliability). Each lives on its own route so results/history
 * stay OFF the AI Factory overview — the overview only launches them.
 */
export function FactoryPageShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-6 max-w-[1200px]">
      <div>
        <Link
          href="/ai-factory"
          className="text-xs text-muted hover:text-[color:var(--text)] inline-flex items-center gap-1.5"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> AI Factory
        </Link>
        <h1 className="text-2xl font-semibold mt-2">{title}</h1>
        <p className="text-muted text-sm mt-1 max-w-[640px]">{subtitle}</p>
      </div>
      {children}
    </div>
  );
}

/** Standard gate notice when a tenant may not see a factory-op page. */
export function FactoryGate({ message }: { message: string }) {
  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-8 text-center text-sm text-muted">
      {message}
    </section>
  );
}
