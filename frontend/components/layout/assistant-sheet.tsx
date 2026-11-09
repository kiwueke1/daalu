"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { MessageSquare } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Dialog } from "@/components/ui/dialog";
import { TabBar } from "@/components/ui/tabs";
import {
  CopilotBody,
  NotificationsBody,
  SystemHealthBody,
  type Turn,
} from "@/components/layout/right-panel";

type SheetTab = "ask" | "notifications" | "health";

/**
 * The right rail for screens too narrow to dock it (below `xl`): a floating
 * Assistant button that opens a bottom sheet. The conversation lives here, not
 * in the sheet's body, so closing the sheet, switching tabs or changing page
 * doesn't lose it, and an answer still in flight lands when you come back.
 */
export function AssistantSheet() {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<SheetTab>("ask");
  const [history, setHistory] = useState<Turn[]>([]);

  // Same key as the header's status, so this is one fetch, not two.
  const { data: alerts } = useQuery({
    queryKey: ["alerts", "open-summary"],
    queryFn: () => api.alerts.list({ status: "open" }),
    refetchInterval: 60_000,
    enabled: !!user,
  });
  const count = alerts?.length ?? 0;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-label={count ? `Open Assistant, ${count} open alert${count === 1 ? "" : "s"}` : "Open Assistant"}
        className="xl:hidden motion-press fixed z-30 right-4 bottom-[max(1.5rem,env(safe-area-inset-bottom))] h-12 pl-4 pr-[18px] inline-flex items-center gap-2 rounded-full bg-bg-card border border-[color:var(--accent)]/40 text-sm font-semibold shadow-2xl"
      >
        <MessageSquare aria-hidden className="h-[18px] w-[18px] text-[color:var(--accent)]" />
        Assistant
        {count > 0 && (
          <span
            aria-hidden
            className="min-w-5 h-5 px-1.5 inline-flex items-center justify-center rounded-full bg-[color:var(--accent)] text-xs tabular-nums text-[color:var(--bg-base)]"
          >
            {count}
          </span>
        )}
      </button>

      <Dialog open={open} onClose={() => setOpen(false)} title="Assistant" variant="sheet">
        <div className="h-full flex flex-col">
        <div className="-mt-2 mb-3 shrink-0">
          <TabBar<SheetTab>
            ariaLabel="Assistant sections"
            active={tab}
            onChange={setTab}
            tabs={[
              { id: "ask", label: "Ask" },
              { id: "notifications", label: "Notifications", badge: count || undefined, badgeTone: "accent" },
              { id: "health", label: "System health" },
            ]}
          />
        </div>
        {tab === "ask" && <CopilotBody comfortable history={history} onHistoryChange={setHistory} />}
        {tab === "notifications" && <NotificationsBody />}
        {tab === "health" && <SystemHealthBody />}
        </div>
      </Dialog>
    </>
  );
}
