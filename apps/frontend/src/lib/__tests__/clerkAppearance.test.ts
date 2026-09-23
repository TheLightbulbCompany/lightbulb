import { describe, expect, it } from "vitest";

import {
  clerkAppearance,
  clerkLocalization,
  clerkTaskUrls,
} from "@/lib/clerkAppearance";

/**
 * Clerk's `variables` cannot read CSS custom properties, so clerkAppearance.ts
 * is the one file outside the token layer allowed to hold hexes — and is on the
 * brand-token scanner's skip list because of it. This test is what replaces the
 * scanner there: every hex it holds must still be a token from the manual.
 *
 * The colour-mix steps are the sRGB channel-wise mix of the two token hexes,
 * matching what the browser resolves for the `:root` custom properties.
 */
const TOKEN_HEXES = new Set([
  "#0F1A2B", // --midnight
  "#F5EFE2", // --cream
  "#F0C34E", // --bulb
  "#B68235", // --brass
  "#EFE8D9", // --cream-deep
  "#E7DECB", // --cream-deeper
  "#DED5C2", // --edge
  "#E8E0D0", // --edge-soft
  "#3D4550", // --midnight-soft   = midnight 80% on cream
  "#62676D", // --midnight-muted  = midnight 64% on cream
  "#696D72", // --midnight-label  = midnight 61% on cream
  "#3A6B45", // --ok
  "#96501A", // --warn
  "#96382B", // --err
  "#35566F", // --info
]);

describe("clerkAppearance", () => {
  const variables = clerkAppearance.variables ?? {};

  it("paints Clerk's form on the manual's palette", () => {
    expect(variables.colorPrimary).toBe("#0F1A2B");
    expect(variables.colorBackground).toBe("#F5EFE2");
    expect(variables.colorDanger).toBe("#96382B");
  });

  it("uses the product face", () => {
    expect(variables.fontFamily).toContain("Source Sans");
  });

  it("holds no colour that is not a token", () => {
    const strays = Object.entries(variables)
      .filter(([, v]) => typeof v === "string" && v.startsWith("#"))
      .filter(([, v]) => !TOKEN_HEXES.has(v as string))
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
