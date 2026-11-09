import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./mock-api";

/**
 * Screenshot regression tests for the screens that matter most. Baselines
 * live in e2e/__screenshots__ and are made in the Playwright container, so
 * they match CI pixel for pixel. After an intended UI change:
 *   npm run test:visual:update
 * and review the changed PNGs in the diff.
 *
 * The clock is frozen at the time the fixtures were captured, so "fired 3h
 * ago" doesn't drift.
 */
const ALERT = "/alerts/89146ca2-2af3-4e05-9464-51a0058dd89e";
const CAPTURED_AT = new Date("2026-09-28T08:10:00Z");

async function open(page: Page, url: string, theme: "dark" | "light" = "dark") {
  await page.clock.setFixedTime(CAPTURED_AT);
  await mockApi(page, theme);
  await page.goto(url, { waitUntil: "networkidle" });
}

async function settle(page: Page) {
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
}

const snap = { animations: "disabled", maxDiffPixelRatio: 0.01 } as const;

test.describe("alert page", () => {
  test("root cause tab", async ({ page }) => {
    await open(page, ALERT);
    await expect(page.getByRole("tab", { name: /Root cause/ })).toHaveAttribute("aria-selected", "true");
    await settle(page);
    await expect(page).toHaveScreenshot("alert-root-cause.png", snap);
  });

  test("remediation workflow, step 1 open", async ({ page }) => {
    await open(page, `${ALERT}?tab=workflow`);
    await expect(page.getByText(/waiting for your approval/i)).toBeVisible();
    await page.locator("button[aria-expanded]").first().click();
    await settle(page);
    await expect(page).toHaveScreenshot("alert-workflow.png", snap);
  });

  test("evidence, logs open", async ({ page }) => {
    await open(page, `${ALERT}?tab=evidence`);
    await page.getByRole("button", { name: /Logs/ }).click();
    await settle(page);
    await expect(page).toHaveScreenshot("alert-evidence.png", snap);
  });

  test("light theme", async ({ page }) => {
    await open(page, `${ALERT}?tab=workflow`, "light");
    await expect(page.getByText(/waiting for your approval/i)).toBeVisible();
    await settle(page);
    await expect(page).toHaveScreenshot("alert-workflow-light.png", snap);
  });

  test("mobile", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, `${ALERT}?tab=workflow`);
    await expect(page.getByText(/waiting for your approval/i)).toBeAttached();
    await settle(page);
    await expect(page).toHaveScreenshot("alert-workflow-mobile.png", snap);
    // Nothing may push the page wider than the phone.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
});

test.describe("alerts list", () => {
  test("desktop", async ({ page }) => {
    await open(page, "/alerts");
    await settle(page);
    await expect(page).toHaveScreenshot("alerts.png", snap);
  });

  test("mobile", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, "/alerts");
    // Asserted directly: dark on dark, it's within the screenshot's 1% tolerance.
    await expect(page.getByRole("button", { name: /Open Assistant/ })).toBeVisible();
    await settle(page);
    await expect(page).toHaveScreenshot("alerts-mobile.png", snap);
  });

  test("mobile Assistant sheet keeps the conversation and returns focus", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, "/alerts");
    const button = page.getByRole("button", { name: /Open Assistant/ });
    await button.click();
    const sheet = page.getByRole("dialog", { name: "Assistant" });
    // Opening must not throw up the phone keyboard over the sheet.
    await expect(page.getByLabel("Message the assistant")).not.toBeFocused();
    await settle(page);
    await expect(page).toHaveScreenshot("assistant-sheet-mobile.png", snap);

    await page.getByLabel("Message the assistant").fill("What services are degraded?");
    await page.keyboard.press("Enter");
    await expect(sheet.getByText(/What services are degraded/)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(button).toBeFocused();

    await button.click();
    await expect(sheet.getByText(/What services are degraded/)).toBeVisible();
  });

  test("sidebar is grouped and keeps Settings on screen", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await open(page, "/alerts");
    const nav = page.getByRole("navigation", { name: "Main" });
    for (const group of ["Respond", "Automate", "Infrastructure", "Insights"]) {
      await expect(nav.getByRole("group", { name: group })).toBeVisible();
    }
    await expect(nav.getByRole("group", { name: "Respond" }).getByRole("link", { name: /Alerts/ })).toHaveAttribute(
      "aria-current",
      "page"
    );
    await expect(page.getByRole("link", { name: "Settings" })).toBeInViewport();
  });
});
