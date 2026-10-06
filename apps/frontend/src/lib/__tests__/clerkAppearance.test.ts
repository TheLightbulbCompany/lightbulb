import { describe, expect, it } from "vitest";

import {
  clerkAppearance,
  clerkLocalization,
  clerkTaskUrls,
} from "@/lib/clerkAppearance";

describe("clerkAppearance", () => {
  const variables = clerkAppearance.variables ?? {};

  it("paints Clerk's form on the neutral ladder tokens", () => {
    expect(variables.colorPrimary).toBe("var(--fg, var(--midnight))");
    expect(variables.colorBackground).toBe("var(--plane-pane, var(--cream))");
    expect(variables.colorDanger).toBe("var(--alert, var(--err))");
    // Clerk tints borders, dividers and social-button labels from this; its default is black.
    expect(variables.colorNeutral).toBe("var(--fg, var(--midnight))");
  });

  it("uses the product face", () => {
    expect(variables.fontFamily).toContain("Source Sans");
  });

  it("holds no hex literal (cream or otherwise)", () => {
    const strays = Object.entries(variables)
      .filter(([, v]) => typeof v === "string" && v.startsWith("#"))
      .map(([k, v]) => `${k}: ${v}`);
    expect(strays).toEqual([]);
  });

  it("styles elements with utility classes, never literals", () => {
    const literals = Object.entries(clerkAppearance.elements ?? {})
      .filter(([, v]) => typeof v === "string" && v.includes("#"))
      .map(([k]) => k);
    expect(literals).toEqual([]);
  });
});

describe("Clerk workspace configuration", () => {
  it("routes pending choose-organization sessions to the native task page", () => {
    expect(clerkTaskUrls).toEqual({
      "choose-organization": "/session-tasks/choose-organization",
    });
  });

  it("uses workspace terminology on the native organization task", () => {
    expect(clerkLocalization.taskChooseOrganization?.chooseOrganization?.title).toBe(
      "Choose a workspace",
    );
    expect(clerkLocalization.organizationList?.action__createOrganization).toBe(
      "Create workspace",
    );
  });

  it("names the workspace field in workspace words, not Clerk's organization ones", () => {
    expect(clerkLocalization.formFieldLabel__organizationName).toBe("Workspace name");
    expect(clerkLocalization.formFieldInputPlaceholder__organizationName).toBe("Workspace name");
  });
});
