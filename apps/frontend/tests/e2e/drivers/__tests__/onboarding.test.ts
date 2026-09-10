import { expect, it, vi } from "vitest";
import type { Page } from "@playwright/test";
import { acceptWorkspaceInvitation, onboardWorkspace } from "../onboarding";

function pageFixture(
  failRedirect = false,
  taskNeedsCreateAction = false,
  submitLabel = "Create workspace",
) {
  const fill = vi.fn().mockResolvedValue(undefined);
  const submit = vi.fn().mockResolvedValue(undefined);
  const openCreate = vi.fn().mockResolvedValue(undefined);
  const getByRole = vi.fn((role: string, options: { name: string | RegExp }) => {
    if (role === "textbox" && options.name instanceof RegExp && options.name.test("Name")) {
      return {
        fill,
        isVisible: vi.fn().mockResolvedValue(!taskNeedsCreateAction),
        or: vi.fn(() => ({ waitFor: vi.fn().mockResolvedValue(undefined) })),
        waitFor: vi.fn().mockResolvedValue(undefined),
      };
    }
    if (role === "button" && options.name instanceof RegExp) {
      const click = options.name.source === "^create a workspace$"
        ? openCreate
        : options.name.test(submitLabel)
          ? submit
          : undefined;
      if (!click) throw new Error("Unexpected onboarding action: " + options.name);
      return { and: vi.fn(() => ({ click })) };
    }
    throw new Error("Removed onboarding control: " + String(options.name));
  });
  const page = {
    goto: vi.fn().mockResolvedValue(undefined),
    getByRole,
    waitForURL: vi.fn().mockImplementation(async (url: RegExp) => {
      if (failRedirect && url.test("/chat")) throw new Error("Redirect failed");
    }),
    waitForFunction: vi.fn().mockResolvedValue(undefined),
    evaluate: vi.fn().mockResolvedValue("org_created"),
    locator: vi.fn(() => ({})),
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

it("waits for a delayed direct Clerk form instead of clicking its disabled submit", async () => {
  const fill = vi.fn().mockResolvedValue(undefined);
  const submit = vi.fn().mockResolvedValue(undefined);
  const openCreate = vi.fn().mockRejectedValue(new Error("Direct form submit is disabled"));
  let inputVisible = false;
  const nameInput = {
    fill,
    isVisible: vi.fn(() => Promise.resolve(inputVisible)),
    waitFor: vi.fn().mockResolvedValue(undefined),
    or: vi.fn(() => ({
      waitFor: vi.fn().mockImplementation(() => {
        inputVisible = true;
      }),
    })),
  };
  const page = {
    goto: vi.fn().mockResolvedValue(undefined),
    getByRole: vi.fn((role: string, options: { name: string | RegExp }) => {
      if (role === "textbox") return nameInput;
      if (role === "button" && options.name instanceof RegExp) {
        const click = options.name.source === "^create a workspace$" ? openCreate : submit;
        return { and: vi.fn(() => ({ click })) };
      }
      throw new Error("Unexpected onboarding control: " + String(options.name));
    }),
    waitForURL: vi.fn().mockResolvedValue(undefined),
    waitForFunction: vi.fn().mockResolvedValue(undefined),
    evaluate: vi.fn().mockResolvedValue("org_created"),
    locator: vi.fn(() => ({})),
  } as unknown as Page;

  await expect(onboardWorkspace(page, "Test workspace", vi.fn())).resolves.toEqual({
    orgId: "org_created",
  });
  expect(fill).toHaveBeenCalledWith("Test workspace");
  expect(submit).toHaveBeenCalledOnce();
});

it("opens Clerk's native create-workspace form when the session task shows its action", async () => {
  const { page, fill, openCreate, submit } = pageFixture(false, true);

  await onboardWorkspace(page, "Test workspace", vi.fn());

  expect(openCreate).toHaveBeenCalledOnce();
  expect(fill).toHaveBeenCalledWith("Test workspace");
  expect(submit).toHaveBeenCalledOnce();
  expect(page.locator).toHaveBeenCalledWith("button:enabled");
  expect(page.getByRole).toHaveBeenCalledWith("button", {
    name: /^create a workspace$/i,
  });
  expect(page.getByRole).toHaveBeenCalledWith("button", {
    name: /^(continue|create workspace)$/i,
  });
});

it("submits Clerk's native task form through its enabled Continue action", async () => {
  const { page, fill, openCreate, submit } = pageFixture(false, true, "Continue");

  await onboardWorkspace(page, "Test workspace", vi.fn());

  expect(openCreate).toHaveBeenCalledOnce();
  expect(fill).toHaveBeenCalledWith("Test workspace");
  expect(submit).toHaveBeenCalledOnce();
  expect(page.getByRole).toHaveBeenCalledWith("button", {
    name: /^(continue|create workspace)$/i,
  });
});

it("records the cleanup handle even if the final redirect fails", async () => {
  const { page } = pageFixture(true);
  const record = vi.fn();
  await expect(onboardWorkspace(page, "Test workspace", record)).rejects.toThrow("Redirect failed");
  expect(record).toHaveBeenCalledWith("org_created");
});

function invitationFixture(createFirst = false, nativeTask = true) {
  const click = vi.fn().mockResolvedValue(undefined);
  const waitFor = vi.fn().mockResolvedValue(undefined);
  const row = document.createElement("div");
  const accept = document.createElement("button");
  const create = document.createElement("button");
  row.append(...(createFirst ? [create, accept] : [accept, create]));
  const acceptAction = {
    click,
    evaluate: async (fn: (node: HTMLElement, other: HTMLElement) => boolean, other: HTMLElement) => fn(accept, other),
  };
  const createAction = {
    waitFor,
    elementHandle: async () => create,
  };
  const page = {
    goto: vi.fn().mockResolvedValue(undefined),
    url: () => nativeTask ? "/session-tasks/choose-organization" : "/onboarding",
    waitForURL: vi.fn().mockResolvedValue(undefined),
    getByRole: vi.fn((role: string, options: { name: RegExp }) => {
      if (role === "heading") return { isVisible: vi.fn().mockResolvedValue(false) };
      if (role === "button") return { first: () => options.name.test("Accept") ? acceptAction : createAction };
      throw new Error(`Unexpected role: ${role}`);
    }),
    getByText: vi.fn(() => ({ waitFor })),
    waitForFunction: vi.fn().mockResolvedValue(undefined),
    evaluate: vi.fn().mockResolvedValue("org_invited"),
  } as unknown as Page;
  return { page, click };
}

it("accepts the native invitation shown before the create-workspace action", async () => {
  const { page, click } = invitationFixture();
  await expect(
    acceptWorkspaceInvitation(page, "Inviter workspace"),
  ).resolves.toEqual({ orgId: "org_invited" });

  expect(click).toHaveBeenCalledOnce();
  expect(page.evaluate).toHaveBeenCalledOnce();
});

it("rejects native create-first ordering before accepting an invitation", async () => {
  const { page, click } = invitationFixture(true);
  await expect(acceptWorkspaceInvitation(page, "Inviter workspace"))
    .rejects.toThrow("Pending invitation must precede the create-workspace action");
  expect(click).not.toHaveBeenCalled();
});

it("accepts an invitation on onboarding without requiring a create action", async () => {
  const { page, click } = invitationFixture(false, false);
  await expect(acceptWorkspaceInvitation(page, "Inviter workspace"))
    .resolves.toEqual({ orgId: "org_invited" });
  expect(click).toHaveBeenCalledOnce();
});
