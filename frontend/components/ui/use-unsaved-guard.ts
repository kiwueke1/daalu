"use client";

import { useEffect } from "react";

/**
 * Warn before the tab closes or reloads while an editor has unsaved changes.
 * The browser shows its own "Leave site?" prompt; the text can't be custom.
 *
 *   useUnsavedGuard(draft !== saved);
 */
export function useUnsavedGuard(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Older browsers need returnValue set to show the prompt.
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);
}
