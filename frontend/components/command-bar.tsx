"use client";

import { Search, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ThinkingDots } from "@/components/copilot/thinking-dots";
import { api } from "@/lib/api";
import { CopilotMarkdown } from "@/components/copilot/markdown";
import { errorText } from "@/lib/errors";

const SUGGESTIONS = [
  "Show failed deployments in the last hour",
  "Which alerts need attention?",
  "Generate infrastructure briefing",
  "Summarise today's events",
];

/** True below Tailwind's `sm` breakpoint, where the long placeholder truncates. */
function useNarrow() {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 639px)");
    const on = () => setNarrow(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return narrow;
}

export function CommandBar() {
  const narrow = useNarrow();
  const [focused, setFocused] = useState(false);
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
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
    setFailed(false);
    try {
      const r = await api.copilot.ask(query);
      setAnswer(r.answer?.trim() || "The assistant returned an empty answer. Try rephrasing.");
    } catch (e) {
      setFailed(true);
      setAnswer(`The assistant couldn't answer. ${errorText(e)}`);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="relative">
      <div
        className="flex items-center gap-2 h-10 rounded-xl px-3 transition-all"
        style={{
          background: "color-mix(in srgb, var(--text) 3%, transparent)",
          boxShadow: focused
            ? "inset 0 1px 0 var(--hairline-strong), 0 0 0 1px var(--accent-soft), 0 0 24px var(--accent-glow)"
            : "inset 0 1px 0 var(--hairline), 0 0 0 1px var(--line-strong)",
        }}
      >
        <Search className="h-4 w-4 text-muted shrink-0" aria-hidden />
        <input
          ref={inputRef}
          type="search"
          role="combobox"
          aria-autocomplete="none"
          aria-label="Ask Daalu"
          aria-controls="command-bar-results"
          aria-expanded={open}
          enterKeyHint="send"
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
          placeholder={narrow ? "Ask Daalu…" : "Ask anything about operations…"}
          className="flex-1 min-w-0 bg-transparent text-sm placeholder:text-muted outline-none [&::-webkit-search-cancel-button]:hidden"
        />
        <kbd
          aria-hidden
          className="hidden md:inline text-[11px] text-muted rounded px-1.5 py-0.5"
          style={{ background: "color-mix(in srgb, var(--text) 4%, transparent)" }}
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
            id="command-bar-results"
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
                    type="button"
                    onMouseDown={() => {
                      setQ(s);
                      ask(s);
                    }}
                    className="w-full text-left flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors hover:text-[color:var(--accent)]"
                  >
                    <Sparkles
                      aria-hidden
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
            {/* Always mounted so screen readers announce the answer. */}
            <div aria-live="polite" aria-atomic="true">
              {answer && (
                <div
                  key={answer}
                  className="p-4 max-h-[60vh] overflow-y-auto motion-enter"
                  style={failed ? { color: "var(--critical)" } : undefined}
                >
                  <CopilotMarkdown>{answer}</CopilotMarkdown>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
