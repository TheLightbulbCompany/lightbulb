// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/ledgerly/checkpoint-1.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect, type Page } from "@playwright/test";
import { signUp, newUser, api, url } from "../_shared/helpers";

async function addAccount(page: Page, code: string, name: string, type: "debit" | "credit") {
  await page.goto(url("/accounts/new"));
  await page.getByTestId("account-form-code").fill(code);
  await page.getByTestId("account-form-name").fill(name);
  await page.getByTestId("account-form-type").selectOption(type);
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("account-form-submit").click()]); // let the write land before navigating on
}

async function fillEntry(page: Page, memo: string, lines: [string, string, string][]) {
  await page.goto(url("/journal/new"));
  await page.getByTestId("entry-form-date").fill("2030-03-31");
  await page.getByTestId("entry-form-memo").fill(memo);
  for (const [i, [label, debit, credit]] of lines.entries()) {
    const row = page.getByTestId("entry-line-row").nth(i);
    await row.getByTestId("line-account").selectOption({ label });
    if (debit) await row.getByTestId("line-debit").fill(debit);
    if (credit) await row.getByTestId("line-credit").fill(credit);
  }
  await page.getByTestId("entry-submit").click();
}

test("accounts are sorted by code and duplicate codes are refused", async ({ page }) => {
  await signUp(page);
  // An app may render both (an empty list container plus the empty note), so take whichever is visible.
  await expect(page.locator('[data-testid="accounts-empty"]:visible, [data-testid="accounts-list"]:visible').first()).toBeVisible();
  await addAccount(page, "4000", "Sales", "credit");
  await addAccount(page, "1000", "Cash", "debit");
  await addAccount(page, "1000", "Cash again", "debit");
  await expect(page.locator('[data-testid="account-form-error"]:visible, [data-testid="account-row"]:visible').first()).toBeVisible();
  const accounts = (await api(page, "GET", "/api/accounts")).body;
  expect(accounts.map((a: any) => a.code)).toEqual(["1000", "4000"]);
  await page.goto(url("/accounts"));
  await expect(page.getByTestId("account-row-code")).toHaveText(["1000", "4000"]);
});

test("a balanced entry saves, exactly, and an unbalanced one does not", async ({ page }) => {
  await signUp(page);
  await addAccount(page, "1000", "Cash", "debit");
  await addAccount(page, "4000", "Sales", "credit");
  await fillEntry(page, "Bad", [["1000 Cash", "10.00", ""], ["4000 Sales", "", "9.00"]]);
  await expect(page.getByTestId("entry-error")).toBeVisible();
  expect((await api(page, "GET", "/api/entries")).body).toEqual([]);
  await fillEntry(page, "Sale", [["1000 Cash", "1250.10", ""], ["4000 Sales", "", "1250.10"]]);
  await expect(page).not.toHaveURL(/\/journal\/new/);
  await page.goto(url("/journal"));
  await expect(page.getByTestId("entry-row")).toHaveCount(1);
  await expect(page.getByTestId("entry-row")).toHaveAttribute("data-entry-date", "2030-03-31");
  const list = (await api(page, "GET", "/api/entries")).body;
  expect(list[0].totalDebitCents).toBe(125010);
  expect(list[0].totalCreditCents).toBe(125010);
  await page.getByTestId("entry-row-link").click();
  await expect(page.getByTestId("entry-detail-date")).toHaveAttribute("data-entry-date", "2030-03-31");
  await expect(page.getByTestId("entry-detail-total-debit")).toContainText(/1,?250\.10/);
});

test("the API rejects an unbalanced entry and other users see nothing", async ({ page, browser }) => {
  await signUp(page);
  const a = (await api(page, "POST", "/api/accounts", { code: "1000", name: "Cash", type: "debit" })).body;
  const b = (await api(page, "POST", "/api/accounts", { code: "4000", name: "Sales", type: "credit" })).body;
  const bad = await api(page, "POST", "/api/entries", { date: "2030-01-01", memo: "x", lines: [{ accountId: a.id, debitCents: 100, creditCents: 0 }, { accountId: b.id, debitCents: 0, creditCents: 90 }] });
  expect(bad.status).toBe(400);
  expect(typeof bad.body.error).toBe("string");
  const good = await api(page, "POST", "/api/entries", { date: "2030-01-01", memo: "ok", lines: [{ accountId: a.id, debitCents: 100, creditCents: 0 }, { accountId: b.id, debitCents: 0, creditCents: 100 }] });
  expect(good.status).toBeLessThan(300);
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/accounts")).body).toEqual([]);
  expect((await api(other.page, "GET", "/api/entries")).body).toEqual([]);
  expect([403, 404]).toContain((await api(other.page, "GET", "/api/entries/" + good.body.id)).status);
  await other.ctx.close();
});
