// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/slotline/checkpoint-2.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, me, url, uniq } from "../_shared/helpers";

const DATE = "2030-01-14"; // a Monday; the clinic is on MST (UTC-7) that day

async function staffUser(browser: import("@playwright/test").Browser) {
  const u = await newUser(browser);
  await u.page.goto(url("/staff/join"));
  await u.page.getByTestId("staff-code-input").fill("slotline-staff-2026");
  await u.page.getByTestId("staff-code-submit").click();
  await expect.poll(async () => (await me(u.page)).role).toBe("staff");
  return u;
}

async function setup(staff: Awaited<ReturnType<typeof staffUser>>) {
  const p = (await api(staff.page, "POST", "/api/practitioners", { name: uniq("Dr "), specialty: "GP" })).body;
  const s = (await api(staff.page, "POST", "/api/services", { name: uniq("Visit "), durationMinutes: 60 })).body;
  const w = await api(staff.page, "POST", "/api/practitioners/" + p.id + "/availability", { weekday: 1, startTime: "09:00", endTime: "12:00" });
  expect(w.status).toBeLessThan(300);
  return { p, s };
}

test("roles: patients are patients, the wrong code is refused, the right one makes staff", async ({ page }) => {
  await signUp(page);
  await expect(page.getByTestId("role-badge")).toHaveText("patient");
  await page.goto(url("/staff/join"));
  await page.getByTestId("staff-code-input").fill("wrong");
  await page.getByTestId("staff-code-submit").click();
  await expect(page.getByTestId("staff-code-error")).toBeVisible();
  expect((await me(page)).role).toBe("patient");
  expect((await api(page, "POST", "/api/staff/claim", { code: "nope" })).status).toBe(403);
  await page.getByTestId("staff-code-input").fill("slotline-staff-2026");
  await page.getByTestId("staff-code-submit").click();
  await expect.poll(async () => (await me(page)).role).toBe("staff");
});

test("patients cannot change the clinic's setup", async ({ browser }) => {
  const staff = await staffUser(browser);
  const { p } = await setup(staff);
  const patient = await newUser(browser);
  expect((await api(patient.page, "POST", "/api/practitioners", { name: "x", specialty: "y" })).status).toBe(403);
  expect((await api(patient.page, "DELETE", "/api/practitioners/" + p.id)).status).toBe(403);
  expect((await api(patient.page, "POST", "/api/practitioners/" + p.id + "/availability", { weekday: 2, startTime: "09:00", endTime: "10:00" })).status).toBe(403);
  await patient.page.goto(url("/practitioners/new"));
  await expect(patient.page.getByTestId("forbidden-message")).toBeVisible();
  await staff.ctx.close(); await patient.ctx.close();
});

test("the slot picker offers the grid and booking removes the slot", async ({ browser }) => {
  const staff = await staffUser(browser);
  const { p, s } = await setup(staff);
  const slots = (await api(staff.page, "GET", "/api/slots?practitionerId=" + p.id + "&serviceId=" + s.id + "&date=" + DATE)).body;
  expect(slots.map((x: any) => x.start)).toEqual(["2030-01-14T16:00:00.000Z", "2030-01-14T17:00:00.000Z", "2030-01-14T18:00:00.000Z"]);
  const patient = await newUser(browser);
  const pg = patient.page;
  await pg.goto(url("/bookings/new"));
  await pg.getByTestId("booking-form-practitioner").selectOption({ label: p.name });
  await pg.getByTestId("booking-form-service").selectOption({ label: s.name });
  await pg.getByTestId("booking-form-date").fill(DATE);
  await expect(pg.getByTestId("slot-option")).toHaveCount(3);
  await pg.getByTestId("slot-option").nth(1).click();
  await Promise.all([pg.waitForResponse((r) => r.request().method() !== "GET"), pg.getByTestId("booking-form-submit").click()]); // let the write land before navigating
  await pg.goto(url("/bookings"));
  await expect(pg.getByTestId("booking-row")).toHaveAttribute("data-start", "2030-01-14T17:00:00.000Z");
  const after = (await api(pg, "GET", "/api/slots?practitionerId=" + p.id + "&serviceId=" + s.id + "&date=" + DATE)).body;
  expect(after.map((x: any) => x.start)).toEqual(["2030-01-14T16:00:00.000Z", "2030-01-14T18:00:00.000Z"]);
  await staff.ctx.close(); await patient.ctx.close();
});

test("a taken or off-grid slot is rejected with 409", async ({ browser }) => {
  const staff = await staffUser(browser);
  const { p, s } = await setup(staff);
  const a = await newUser(browser), b = await newUser(browser);
  const body = { practitionerId: p.id, serviceId: s.id, startAt: "2030-01-14T16:00:00.000Z" };
  expect((await api(a.page, "POST", "/api/bookings", body)).status).toBeLessThan(300);
  const dup = await api(b.page, "POST", "/api/bookings", body);
  expect(dup.status).toBe(409);
  expect(typeof dup.body.error).toBe("string");
  expect((await api(b.page, "POST", "/api/bookings", { ...body, startAt: "2030-01-14T16:30:00.000Z" })).status).toBe(409);
  const race = await Promise.all([a, b].map((u) => api(u.page, "POST", "/api/bookings", { ...body, startAt: "2030-01-14T18:00:00.000Z" })));
  expect(race.filter((r) => r.status < 300)).toHaveLength(1);
  await staff.ctx.close(); await a.ctx.close(); await b.ctx.close();
});
