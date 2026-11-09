"use client";

import { createContext, useContext, useEffect, useState } from "react";

type Theme = "dark" | "light";
const STORAGE_KEY = "daalu-theme";

const ThemeContext = createContext<{
  theme: Theme;
  toggle: () => void;
  setTheme: (t: Theme) => void;
} | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>("dark");

  // The saved choice wins; with none, follow the OS (and keep following it
  // if the OS switches, e.g. at sunset). The <head> script in app/layout.tsx
  // already applied the same choice before first paint.
  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY);
    } catch {
      /* storage blocked */
    }
    if (stored === "light" || stored === "dark") {
      setThemeState(stored);
      document.documentElement.dataset.theme = stored;
      return;
    }
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const apply = () => {
      const t: Theme = mq.matches ? "light" : "dark";
      setThemeState(t);
      document.documentElement.dataset.theme = t;
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  function setTheme(t: Theme) {
    setThemeState(t);
    document.documentElement.dataset.theme = t;
    try {
      localStorage.setItem(STORAGE_KEY, t);
    } catch {
      /* ignore storage failures (private mode etc.) */
    }
  }

  function toggle() {
    setTheme(theme === "dark" ? "light" : "dark");
  }

  return (
    <ThemeContext.Provider value={{ theme, toggle, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside ThemeProvider");
  return ctx;
}
