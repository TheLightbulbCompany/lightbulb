import { expect, it, vi } from "vitest";
import type { Page } from "@playwright/test";
import { onboardWorkspace } from "../onboarding";

function pageFixture(failRedirect = false) {
  const fill = vi.fn().mockResolvedValue(undefined);
  const click = vi.fn().mockResolvedValue(undefined);
  const page = {
    goto: vi.fn().mockResolvedValue(undefined),
    getByRole: (role: string, options: { name: string | RegExp }) => {
      if (role === "textbox" && options.name instanceof RegExp && options.name.test("Name")) return { fill };
      if (role === "button" && options.name instanceof RegExp && options.name.test("Create workspace")) return { first: () => ({ click }) };
      throw new Error("Removed onboarding control: " + String(options.name));
    },
    waitForURL: vi.fn().mockImplementation(async (url: RegExp) => {
      if (failRedirect && url.test("/chat")) throw new Error("Redirect failed");
    }),
    waitForFunction: vi.fn().mockResolvedValue(undefined),
    evaluate: vi.fn().mockResolvedValue("org_created"),
  };
  return { page: page as unknown as Page, fill, click };
}

it("creates a workspace directly without the retired chooser or invitation skip", async () => {
  const { page, fill, click } = pageFixture();
  const record = vi.fn();
  await expect(onboardWorkspace(page, "Test workspace", record)).resolves.toEqual({ orgId: "org_created" });
  expect(fill).toHaveBeenCalledWith("Test workspace");
  expect(click).toHaveBeenCalledOnce();
  expect(record).toHaveBeenCalledWith("org_created");
});

it("records the cleanup handle even if the final redirect fails", async () => {
  const { page } = pageFixture(true);
  const record = vi.fn();
  await expect(onboardWorkspace(page, "Test workspace", record)).rejects.toThrow("Redirect failed");
  expect(record).toHaveBeenCalledWith("org_created");
});
