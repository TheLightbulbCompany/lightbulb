import { afterEach, describe, expect, it } from "vitest";

import { returnsToApp } from "@/lib/externalAuth";

describe("returnsToApp", () => {
  afterEach(() => {
    sessionStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  it("has no app destination on plain web", () => {
    expect(returnsToApp()).toBeNull();
  });

  it("latches ?from=app for the tab even after the URL changes", () => {
    window.history.replaceState(null, "", "/chat?panel=integrations&from=app");
    expect(returnsToApp()).toBe("ios");
    window.history.replaceState(null, "", "/chat?panel=connections");
    expect(returnsToApp()).toBe("ios");
  });
});
