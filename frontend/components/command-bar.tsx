"use client";

import { Search, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ThinkingDots } from "@/components/copilot/thinking-dots";
import { api } from "@/lib/api";
import { CopilotMarkdown } from "@/components/copilot/markdown";

const SUGGESTIONS = [
  "Show failed deployments in the last hour",
  "Which alerts need attention?",
  "Generate infrastructure briefing",
  "Summarise today's events",
];

export function CommandBar() {
  const [focused, setFocused] = useState(false);
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
        setOpen(true);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function ask(query: string) {
    if (!query.trim()) return;
    setPending(true);
    setAnswer(null);
    try {
      const r = await api.copilot.ask(query);
      setAnswer(r.answer);
    } catch (e) {
      setAnswer("Failed to reach the copilot — check the API service.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="relative">
      <div
        className="flex items-center gap-2 h-10 rounded-xl px-3 transition-all"
        style={{
          background: "rgba(255,255,255,0.025)",
          boxShadow: focused
            ? "inset 0 1px 0 rgba(255,255,255,0.06), 0 0 0 1px var(--accent-soft), 0 0 24px var(--accent-glow)"
            : "inset 0 1px 0 rgba(255,255,255,0.04), 0 0 0 1px rgba(255,255,255,0.03)",
        }}
      >
        <Search className="h-4 w-4 text-muted shrink-0" />
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onFocus={() => {
            setFocused(true);
            setOpen(true);
          }}
          onBlur={() => setTimeout(() => setFocused(false), 200)}
          onKeyDown={(e) => {
            if (e.key === "Enter") ask(q);
          }}
          placeholder="Ask anything about operations…"
          className="flex-1 bg-transparent text-sm placeholder:text-muted/70 outline-none"
        />
        <kbd
          className="hidden md:inline text-[10px] text-muted/80 rounded px-1.5 py-0.5"
          style={{ background: "rgba(255,255,255,0.03)" }}
        >
          ⌘K
        </kbd>
      </div>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.18 }}
            className="surface absolute left-0 right-0 mt-2 overflow-hidden"
          >
            {!answer && !pending && (
              <div className="p-2">
                <div className="px-3 py-2 text-[11px] uppercase tracking-wider text-muted">
                  Try
                </div>
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onMouseDown={() => {
                      setQ(s);
                      ask(s);
                    }}
                    className="w-full text-left flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors hover:text-[color:var(--accent)]"
                  >
                    <Sparkles
                      className="h-3.5 w-3.5"
                      style={{ color: "var(--accent)" }}
                    />
                    {s}
                  </button>
                ))}
              </div>
            )}
            {pending && (
              <div className="px-4 py-4 text-sm">
                <ThinkingDots label="Querying your infrastructure…" />
              </div>
            )}
            {answer && (
              <div className="p-4 max-h-[60vh] overflow-y-auto">
                <CopilotMarkdown>{answer}</CopilotMarkdown>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
