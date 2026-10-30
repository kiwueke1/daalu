"use client";

import { createContext, useContext, useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Folded detail, one item open at a time. Opening an item closes the one that
 * was open, so a list of steps or evidence never turns into a wall of text.
 *
 *   <Accordion>
 *     <AccordionItem id="logs" title="Logs" meta="2 calls">…</AccordionItem>
 *   </Accordion>
 */
const Ctx = createContext<{
  openId: string | null;
  toggle: (id: string) => void;
} | null>(null);

export function Accordion({
  defaultOpen = null,
  className,
  children,
}: {
  defaultOpen?: string | null;
  className?: string;
  children: React.ReactNode;
}) {
  const [openId, setOpenId] = useState<string | null>(defaultOpen);
  const toggle = (id: string) => setOpenId((cur) => (cur === id ? null : id));
  return (
    <Ctx.Provider value={{ openId, toggle }}>
      <div className={cn("space-y-2", className)}>{children}</div>
    </Ctx.Provider>
  );
}

export function AccordionItem({
  id,
  title,
  leading,
  meta,
  tone,
  children,
}: {
  id: string;
  title: React.ReactNode;
  /** Left of the title: a step number, an icon. */
  leading?: React.ReactNode;
  /** Right-aligned summary shown while folded: a count, a status. */
  meta?: React.ReactNode;
  /** Left accent stripe colour (a CSS colour or var). */
  tone?: string;
  children: React.ReactNode;
}) {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("AccordionItem must be inside <Accordion>");
  const open = ctx.openId === id;
  const panelId = `acc-${id}`;
  return (
    <div
      className="rounded-xl overflow-hidden"
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--line)",
        boxShadow: tone ? `inset 3px 0 0 ${tone}` : undefined,
      }}
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => ctx.toggle(id)}
        className="w-full text-left px-4 py-3 flex items-center gap-3 hover:bg-bg-elevated/40 transition-colors"
      >
        {leading}
        <div className="flex-1 min-w-0 text-[13px] leading-snug">{title}</div>
        {meta && <div className="shrink-0 text-[11px] text-muted">{meta}</div>}
        <ChevronRight
          className={cn(
            "h-4 w-4 shrink-0 text-muted transition-transform",
            open && "rotate-90"
          )}
        />
      </button>
      {open && (
        <div
          id={panelId}
          className="px-4 pb-4 pt-1"
          style={{ borderTop: "1px solid var(--line)" }}
        >
          {children}
        </div>
      )}
    </div>
  );
}
