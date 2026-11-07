// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/ledgerly/checkpoint-2.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, me, url } from "../_shared/helpers";

async function books(page: import("@playwright/test").Page) {
  const a = (await api(page, "POST", "/api/accounts", { code: "1000", name: "Cash", type: "debit" })).body;
  const b = (await api(page, "POST", "/api/accounts", { code: "4000", name: "Sales", type: "credit" })).body;
  const entry = (cents: number) => ({ date: "2030-02-01", memo: "Sale", lines: [{ accountId: a.id, debitCents: cents, creditCents: 0 }, { accountId: b.id, debitCents: 0, creditCents: cents }] });
  return { a, b, entry };
}
const balance = async (page: import("@playwright/test").Page, id: string) => (await api(page, "GET", "/api/accounts")).body.find((x: any) => x.id === id).balanceCents;

test("everyone has exactly one personal book", async ({ page }) => {
  await signUp(page, { name: "Dana" });
  const m = await me(page);
  expect(m.memberships).toHaveLength(1);
  expect(m.memberships[0].bookName).toBe("Dana's Books");
  expect(m.activeBookId).toBe(m.memberships[0].bookId);
  await expect(page.getByTestId("book-switcher")).toContainText("Dana's Books");
  expect((await api(page, "GET", "/api/books")).body).toHaveLength(1);
});

test("drafts do not move balances; posting does, and numbers the entry", async ({ page }) => {
  await signUp(page);
  const { a, entry } = await books(page);
  const e = (await api(page, "POST", "/api/entries", entry(5000))).body;
  expect(e.status).toBe("draft");
  expect(await balance(page, a.id)).toBe(0);
  await page.goto(url("/journal/" + e.id));
  await expect(page.getByTestId("entry-detail-status")).toHaveText("draft");
  await page.getByTestId("entry-post-button").click();
  await expect(page.getByTestId("entry-detail-status")).toHaveText("posted");
  await expect(page.getByTestId("entry-detail-number")).toHaveText("1");
  await expect(page.getByTestId("entry-edit-button")).toHaveCount(0);
  expect(await balance(page, a.id)).toBe(5000);
  await page.goto(url("/accounts"));
  await expect(page.getByTestId("account-row").filter({ hasText: "Cash" }).getByTestId("account-row-balance")).toHaveAttribute("data-cents", "5000");
});

test("a posted entry is immutable; the only fix is one reversal", async ({ page }) => {
  await signUp(page);
  const { a, entry } = await books(page);
  const e = (await api(page, "POST", "/api/entries", { ...entry(700), status: "posted" })).body;
  expect(e.entryNumber).toBe(1);
  for (const r of [await api(page, "PATCH", "/api/entries/" + e.id, { memo: "changed" }), await api(page, "DELETE", "/api/entries/" + e.id)]) {
    expect(r.status).toBe(409);
    expect(typeof r.body.error).toBe("string");
  }
  const rev = await api(page, "POST", "/api/entries/" + e.id + "/reverse");
  expect(rev.status).toBeLessThan(300);
  expect(rev.body.status).toBe("posted");
  expect(rev.body.reversesEntryId).toBe(e.id);
  expect(rev.body.entryNumber).toBe(2);
  expect(await balance(page, a.id)).toBe(0);
  expect((await api(page, "POST", "/api/entries/" + e.id + "/reverse")).status).toBe(409);
  expect((await api(page, "POST", "/api/entries/" + rev.body.id + "/reverse")).status).toBe(409);
  await page.goto(url("/journal/" + e.id));
  await expect(page.getByTestId("entry-reversed-by-link")).toBeVisible();
});

test("books are separate: same code twice, own numbering, no peeking", async ({ page, browser }) => {
  await signUp(page);
  const mine = await books(page);
  const posted = (await api(page, "POST", "/api/entries", { ...mine.entry(100), status: "posted" })).body;
  const other = await newUser(browser);
  const theirs = await books(other.page);
  expect(theirs.a.code).toBe("1000");
  const t = (await api(other.page, "POST", "/api/entries", { ...theirs.entry(100), status: "posted" })).body;
  expect(t.entryNumber).toBe(1);
  expect((await api(other.page, "GET", "/api/entries/" + posted.id)).status).toBeGreaterThanOrEqual(400);
  await other.ctx.close();
});
