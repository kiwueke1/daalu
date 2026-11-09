#!/usr/bin/env node
// Screenshot Daalu pages at desktop + mobile, dark + light, against real data.
//
// The page runs on a local `next dev`; every /api/v1 call is intercepted in the
// browser and answered from one of:
//   --source=prod      GETs forwarded to the live API as you (read-only: every
//                      non-GET is refused locally, nothing reaches the server).
//                      Needs DAALU_UI_TOKEN (a session JWT); DAALU_UI_API
//                      defaults to https://daalu.example.com.
//   --source=fixtures  answered from JSON files in --fixtures=<dir> (see
//                      --record). Unknown endpoints get [] and are listed.
// --record=<dir> saves every GET response while running against prod, so a
// page can be replayed later (screenshot tests, offline review).
//
//   node scripts/ui-shots.mjs --routes=/alerts,/workflows --out=/tmp/shots
//   node scripts/ui-shots.mjs --routes-file=scripts/ui-routes.txt --out=/tmp/shots
//
// Options: --base=http://localhost:3107  --viewports=desktop,mobile
//          --themes=dark,light  --full (full-page shots)  --wait=1500 (ms)
//          --tab=<name> clicks the tab with that name before the shot
// Writes <out>/<route-slug>.<viewport>.<theme>.png and <out>/report.json
// (console errors, page errors, horizontal overflow, missing fixtures).
//
// Playwright: resolved from node_modules, else from $PLAYWRIGHT_MODULE (a path
// to an installed `playwright` package).

import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
function loadPlaywright() {
  try {
    return require("playwright");
  } catch {
    if (process.env.PLAYWRIGHT_MODULE) return require(process.env.PLAYWRIGHT_MODULE);
    throw new Error(
      "playwright not found: `npm install` in frontend/, or set PLAYWRIGHT_MODULE"
    );
  }
}

const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const [k, ...v] = a.replace(/^--/, "").split("=");
    return [k, v.length ? v.join("=") : true];
  })
);

const BASE = args.base || "http://localhost:3107";
const OUT = path.resolve(args.out || "ui-shots");
const SOURCE = args.source || "prod";
const API = process.env.DAALU_UI_API || "https://daalu.example.com";
const TOKEN = process.env.DAALU_UI_TOKEN;
const WAIT = Number(args.wait || 1500);
const VIEWPORTS = {
  desktop: { width: 1440, height: 1000 },
  mobile: { width: 390, height: 844 },
};
const viewports = String(args.viewports || "desktop,mobile").split(",");
const themes = String(args.themes || "dark,light").split(",");

let routes = [];
if (args.routes) routes = String(args.routes).split(",");
if (args["routes-file"])
  routes = fs
    .readFileSync(args["routes-file"], "utf8")
    .split("\n")
    .map((l) => l.replace(/#.*/, "").trim())
    .filter(Boolean);
if (!routes.length) {
  console.error("no routes: pass --routes=/a,/b or --routes-file=<file>");
  process.exit(2);
}
if (SOURCE === "prod" && !TOKEN) {
  console.error("--source=prod needs DAALU_UI_TOKEN");
  process.exit(2);
}

export const fixtureKey = (url) => {
  const u = new URL(url);
  const key = (u.pathname.replace(/^\/api\/v1\//, "") + u.search)
    .replace(/[^a-zA-Z0-9._=-]+/g, "_")
    .slice(0, 180);
  return `${key || "root"}.json`;
};

const slug = (r) => r.replace(/^\//, "").replace(/[^a-zA-Z0-9]+/g, "_") || "home";

async function main() {
  const { chromium } = loadPlaywright();
  fs.mkdirSync(OUT, { recursive: true });
  if (args.record) fs.mkdirSync(args.record, { recursive: true });
  const report = [];
  const missing = new Set();
  const browser = await chromium.launch();

  for (const route of routes) {
    for (const vp of viewports) {
      for (const theme of themes) {
        const ctx = await browser.newContext({ viewport: VIEWPORTS[vp] });
        await ctx.addInitScript((t) => {
          try {
            localStorage.setItem("daalu-theme", t);
          } catch {}
        }, theme);
        const page = await ctx.newPage();
        const errors = [];
        page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
        page.on("console", (m) => m.type() === "error" && errors.push(`console: ${m.text()}`));

        await page.route(/\/(sse|ws)\//, (r) => r.abort());
        await page.route("**/api/v1/**", async (r) => {
          const req = r.request();
          if (req.method() !== "GET") {
            return r.fulfill({
              status: 403,
              contentType: "application/json",
              body: JSON.stringify({ detail: "blocked by ui-shots (read-only)" }),
            });
          }
          const key = fixtureKey(req.url());
          if (SOURCE === "fixtures") {
            const f = path.join(args.fixtures, key);
            if (fs.existsSync(f)) {
              return r.fulfill({ status: 200, contentType: "application/json", body: fs.readFileSync(f) });
            }
            missing.add(key);
            return r.fulfill({ status: 200, contentType: "application/json", body: "[]" });
          }
          const u = new URL(req.url());
          const res = await fetch(API + u.pathname + u.search, {
            headers: { Authorization: `Bearer ${TOKEN}`, Accept: "application/json" },
          }).catch((e) => ({ ok: false, status: 502, text: async () => String(e), headers: new Map() }));
          const body = await res.text();
          if (args.record && res.ok) fs.writeFileSync(path.join(args.record, key), body);
          return r.fulfill({
            status: res.status,
            contentType: res.headers.get?.("content-type") || "application/json",
            body,
          });
        });

        const name = `${slug(route)}.${vp}.${theme}`;
        try {
          await page.goto(BASE + route, { waitUntil: "networkidle", timeout: 120000 });
          // A page may redirect client-side (e.g. /devices → /operations);
          // let that navigation finish before judging the page.
          await page.waitForLoadState("networkidle");
          if (args.tab) await page.getByRole("tab", { name: new RegExp(args.tab, "i") }).click();
          // Don't judge a page by its spinner: wait (up to 20 s) until no
          // loading indicator is on screen. A page stuck loading is still
          // captured, and that is itself a finding.
          await page
            .waitForFunction(
              () =>
                !document.querySelector("main .animate-spin, main [aria-busy='true']") &&
                !/Loading…|Checking .*…/.test(document.querySelector("main")?.innerText ?? ""),
              null,
              { timeout: 20000 }
            )
            .catch(() => {});
          await page.waitForTimeout(WAIT);
          const overflow = await page.evaluate(
            () => document.documentElement.scrollWidth - window.innerWidth
          );
          await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: !!args.full });
          report.push({ route, viewport: vp, theme, file: `${name}.png`, overflowPx: overflow, errors });
          console.log(`ok   ${name}${overflow > 0 ? `  (overflows ${overflow}px)` : ""}${errors.length ? `  (${errors.length} errors)` : ""}`);
        } catch (e) {
          report.push({ route, viewport: vp, theme, failed: String(e), errors });
          console.log(`FAIL ${name}: ${String(e).split("\n")[0]}`);
        }
        await ctx.close();
      }
    }
  }
  await browser.close();
  // Merge with an earlier run into the same --out (e.g. a dark pass then a
  // light pass), replacing entries for shots taken again.
  const reportFile = path.join(OUT, "report.json");
  let prev = { shots: [], missingFixtures: [] };
  try {
    prev = JSON.parse(fs.readFileSync(reportFile, "utf8"));
  } catch {}
  const taken = new Set(report.map((r) => `${r.route}|${r.viewport}|${r.theme}`));
  fs.writeFileSync(
    reportFile,
    JSON.stringify(
      {
        shots: [...prev.shots.filter((r) => !taken.has(`${r.route}|${r.viewport}|${r.theme}`)), ...report],
        missingFixtures: [...new Set([...prev.missingFixtures, ...missing])],
      },
      null,
      2
    )
  );
  if (missing.size) console.log(`missing fixtures (served []): ${[...missing].join(", ")}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
