// Small helpers shared by the journeys. Uses only the auth testids every task pins.
import { expect, type Browser, type Page } from "@playwright/test";

export const url = (path: string) => new URL(path, process.env.BASE_URL).toString();
export const uniq = (p = "x") => `${p}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

export type Creds = { name: string; email: string; password: string };

export async function signUp(page: Page, o: Partial<Creds> = {}): Promise<Creds> {
  const c = { name: o.name ?? "Eval User", email: o.email ?? `${uniq("u")}@example.test`, password: o.password ?? "Passw0rd!eval1" };
  await page.goto(url("/auth/sign-up"));
  await page.getByTestId("signup-name").fill(c.name);
  await page.getByTestId("signup-email").fill(c.email);
  await page.getByTestId("signup-password").fill(c.password);
  await page.getByTestId("signup-submit").click();
  await expect(page).not.toHaveURL(/\/auth\//);
  return c;
}

export async function signIn(page: Page, c: Creds) {
  await page.goto(url("/auth/sign-in"));
  await page.getByTestId("signin-email").fill(c.email);
  await page.getByTestId("signin-password").fill(c.password);
  await page.getByTestId("signin-submit").click();
  await expect(page).not.toHaveURL(/\/auth\//);
}

export async function signOut(page: Page) {
  await page.getByTestId("sign-out-button").click();
  await expect(page).toHaveURL(/\/auth\/sign-in/);
}

/** JSON call that shares the page's session cookies. */
export async function api(page: Page, method: string, path: string, data?: unknown) {
  const r = await page.request.fetch(url(path), { method, data, headers: data === undefined ? {} : { "content-type": "application/json" } });
  let body: any = null;
  try { body = await r.json(); } catch { /* empty body */ }
  return { status: r.status(), body };
}

export const me = async (page: Page) => (await api(page, "GET", "/api/me")).body;

/**
 * A fresh signed-in user in their own browser context. `id` comes from `/api/me`, which only some prompts ask for;
 * where the app has none it is undefined (only journeys of tasks whose prompt names `/api/me` read it).
 */
export async function newUser(browser: Browser, o: Partial<Creds> = {}) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const creds = await signUp(page, o);
  return { page, ctx, creds, id: (await me(page))?.id as string };
}
