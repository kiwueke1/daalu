import { ApiError, UnauthorizedError } from "@/lib/api";

/**
 * One plain sentence for any error, saying what to do next.
 *
 * Shows FastAPI's `detail` when the API gave one (those are written for
 * people). Never shows a raw response body, an HTML error page, a stack or
 * a JSON dump: those become a sentence chosen by status.
 */
export function errorText(error: unknown): string {
  if (error instanceof UnauthorizedError) {
    return "Your session has ended. Sign in again to continue.";
  }
  if (error instanceof ApiError) {
    const detail = readableDetail(error.detail);
    if (error.status === 403) {
      return `Only a tenant admin can do this.${detail ? ` ${detail}` : ""}`;
    }
    if (detail) return detail;
    return byStatus(error.status);
  }
  if (error instanceof TypeError && /fetch|network/i.test(error.message)) {
    return "Couldn't reach Daalu. Check your connection, then retry.";
  }
  if (error instanceof Error && readableDetail(error.message)) return error.message;
  return "Something went wrong. Retry, and if it keeps happening, contact support.";
}

function byStatus(status: number): string {
  if (status === 400 || status === 422) return "Some of the values weren't accepted. Check the form and try again.";
  if (status === 404) return "This no longer exists. It may have been removed; refresh the page.";
  if (status === 409) return "This changed or is busy right now. Refresh, then try again.";
  if (status === 429) return "Too many requests. Wait a moment, then retry.";
  if (status === 502 || status === 503 || status === 504) {
    return "A service Daalu depends on isn't answering. Retry in a minute.";
  }
  if (status >= 500) return "Daalu hit an error on its side. Retry; if it keeps happening, contact support.";
  return "The request failed. Retry, and if it keeps happening, contact support.";
}

/** A detail string worth showing a person: short, not HTML, not JSON. */
function readableDetail(detail: string | undefined): string {
  const d = (detail || "").trim();
  if (!d || d.length > 400) return "";
  if (d.startsWith("<") || d.startsWith("{") || d.startsWith("[")) return "";
  if (/Traceback|^\d{3} [A-Z]/.test(d)) return "";
  return d;
}
