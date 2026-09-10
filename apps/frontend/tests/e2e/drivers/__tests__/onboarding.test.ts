import { expect, it, vi } from "vitest";
import type { Page } from "@playwright/test";
import { acceptWorkspaceInvitation, onboardWorkspace } from "../onboarding";

function pageFixture(failRedirect = false, taskNeedsCreateAction = false) {
  const fill = vi.fn().mockResolvedValue(undefined);
  const submit = vi.fn().mockResolvedValue(undefined);
  const openCreate = vi.fn().mockResolvedValue(undefined);
  const page = {
    goto: vi.fn().mockResolvedValue(undefined),
    getByRole: (role: string, options: { name: string | RegExp }) => {
      if (role === "textbox" && options.name instanceof RegExp && options.name.test("Name")) {
        return {
          fill,
          isVisible: vi.fn().mockResolvedValue(!taskNeedsCreateAction),
          waitFor: vi.fn().mockResolvedValue(undefined),
        };
      }
      if (role === "button" && options.name instanceof RegExp) {
        const click = options.name.source === "create (a )?workspace"
          ? openCreate
          : submit;
        return { first: () => ({ click }) };
      }
      throw new Error("Removed onboarding control: " + String(options.name));
    },
    waitForURL: vi.fn().mockImplementation(async (url: RegExp) => {
      if (failRedirect && url.test("/chat")) throw new Error("Redirect failed");
    }),
    waitForFunction: vi.fn().mockResolvedValue(undefined),
    evaluate: vi.fn().mockResolvedValue("org_created"),
  };
  return { page: page as unknown as Page, fill, openCreate, submit };
}

it("creates a workspace directly without the retired chooser or invitation skip", async () => {
  const { page, fill, openCreate, submit } = pageFixture();
  const record = vi.fn();
  await expect(onboardWorkspace(page, "Test workspace", record)).resolves.toEqual({ orgId: "org_created" });
  expect(fill).toHaveBeenCalledWith("Test workspace");
  expect(openCreate).not.toHaveBeenCalled();
  expect(submit).toHaveBeenCalledOnce();
  expect(record).toHaveBeenCalledWith("org_created");
});

it("opens Clerk's native create-workspace form when the session task shows its action", async () => {
  const { page, fill, openCreate, submit } = pageFixture(false, true);

  await onboardWorkspace(page, "Test workspace", vi.fn());

  expect(openCreate).toHaveBeenCalledOnce();
  expect(fill).toHaveBeenCalledWith("Test workspace");
  expect(submit).toHaveBeenCalledOnce();
});

it("records the cleanup handle even if the final redirect fails", async () => {
  const { page } = pageFixture(true);
  const record = vi.fn();
  await expect(onboardWorkspace(page, "Test workspace", record)).rejects.toThrow("Redirect failed");
  expect(record).toHaveBeenCalledWith("org_created");
});

it("accepts an invitation before exposing workspace creation", async () => {
  const click = vi.fn().mockResolvedValue(undefined);
  const waitFor = vi.fn().mockResolvedValue(undefined);
  const page = {
    goto: vi.fn().mockResolvedValue(undefined),
    waitForURL: vi.fn().mockResolvedValue(undefined),
    getByRole: vi.fn((role: string) => {
      if (role === "heading") return { isVisible: vi.fn().mockResolvedValue(false) };
      if (role === "button") return { first: () => ({ click }) };
      throw new Error(`Unexpected role: ${role}`);
    }),
    getByText: vi.fn(() => ({ waitFor })),
    waitForFunction: vi.fn().mockResolvedValue(undefined),
    evaluate: vi.fn().mockResolvedValue("org_invited"),
  } as unknown as Page;

  await expect(
    acceptWorkspaceInvitation(page, "Inviter workspace"),
  ).resolves.toEqual({ orgId: "org_invited" });

  expect(click).toHaveBeenCalledOnce();
  expect(page.evaluate).toHaveBeenCalledOnce();
});
