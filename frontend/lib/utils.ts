import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.round(ms / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

// Severity helpers — colors come from the theme tokens (--critical, --warning, --info)
// so they render correctly in both light and dark mode.
export const severityColor: Record<string, string> = {
  info: "var(--info)",
  warning: "var(--warning)",
  critical: "var(--critical)",
};

// Legacy class-based variants still consumed by some pages. Keep the
// names stable so we don't have to grep-replace every call site.
export const severityClass: Record<string, string> = {
  info: "text-[color:var(--info)]",
  warning: "text-[color:var(--warning)]",
  critical: "text-[color:var(--critical)]",
};

export const severityBg: Record<string, string> = {
  info: "bg-[color:var(--info)]/10 text-[color:var(--info)]",
  warning: "bg-[color:var(--warning)]/10 text-[color:var(--warning)]",
  critical: "bg-[color:var(--critical)]/10 text-[color:var(--critical)]",
};
