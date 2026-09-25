import { paperclipMobileHref } from "../paperclipLink";

describe("paperclipMobileHref", () => {
  it("maps a company-prefixed issue path to the in-app issue screen", () => {
    expect(paperclipMobileHref("/ISOA/issues/ISOA-317")).toEqual({
      pathname: "/(app)/teams/issue/[id]",
      params: { id: "ISOA-317" },
    });
  });

  it("tolerates a dropped company prefix", () => {
    expect(paperclipMobileHref("/issues/ISOA-317")).toEqual({
      pathname: "/(app)/teams/issue/[id]",
      params: { id: "ISOA-317" },
    });
  });

  it("maps a run path to the in-app run screen", () => {
    expect(paperclipMobileHref("/ISOA/agents/cto/runs/run-9")).toEqual({
      pathname: "/(app)/teams/run/[runId]",
      params: { runId: "run-9" },
    });
  });

  it("returns null for kinds with no mobile detail screen", () => {
    expect(paperclipMobileHref("/ISOA/agents/cto")).toBeNull();
    expect(paperclipMobileHref("/ISOA/projects/growth")).toBeNull();
    expect(paperclipMobileHref("/ISOA/goals/g-142")).toBeNull();
    expect(paperclipMobileHref("/ISOA/approvals/ca6ba09d-b558")).toBeNull();
  });

  it("returns null for non-Paperclip hrefs", () => {
    expect(paperclipMobileHref(undefined)).toBeNull();
    expect(paperclipMobileHref("https://example.com")).toBeNull();
    expect(paperclipMobileHref("/settings")).toBeNull();
  });
});
