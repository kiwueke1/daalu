import fs from "node:fs";
import path from "node:path";
import type { Page } from "@playwright/test";

/**
 * Answer every /api/v1 call from JSON fixtures, so pages render real data
 * without a backend. File names match scripts/ui-shots.mjs --record, so a
 * fixture can be captured from prod and dropped in here.
 */
export const FIXTURES = path.join(__dirname, "fixtures");

export function fixtureKey(url: string): string {
  const u = new URL(url);
  const key = (u.pathname.replace(/^\/api\/v1\//, "") + u.search)
    .replace(/[^a-zA-Z0-9._=-]+/g, "_")
    .slice(0, 180);
  return `${key || "root"}.json`;
}

export async function mockApi(page: Page, theme: "dark" | "light" = "dark") {
  await page.addInitScript((t) => {
    try {
      localStorage.setItem("daalu-theme", t);
    } catch {}
  }, theme);
  await page.route(/\/(sse|ws)\//, (r) => r.abort());
  await page.route("**/api/v1/**", (route) => {
    const req = route.request();
    if (req.method() !== "GET") {
      return route.fulfill({ status: 403, json: { detail: "read-only test" } });
    }
    const file = path.join(FIXTURES, fixtureKey(req.url()));
    const body = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "[]";
    return route.fulfill({ status: 200, contentType: "application/json", body });
  });
}
