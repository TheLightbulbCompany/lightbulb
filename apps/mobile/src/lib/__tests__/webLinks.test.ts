/**
 * These pin the out-to-web deep links. The bug they guard against actually
 * shipped: five Teams screens each carried their own copy of
 * `https://isol8.co/teams`, which 404s — the route tree was removed when
 * Teams became a panel — so the "Manage on isol8.co" button opened a dead
 * page for real users, and nothing in CI noticed.
 */

import { teamsDetailUrl, teamsPanelUrl, workspaceUrl } from "../webLinks";

describe("web deep links", () => {
  it("never points at the removed /teams route tree", () => {
    const all = [
      workspaceUrl(),
      teamsPanelUrl("issues"),
      teamsPanelUrl("routines"),
      teamsDetailUrl("issue", "iss_1", "issues"),
      teamsDetailUrl("run", "run_1", "issues"),
      teamsDetailUrl("run", undefined, "issues"),
    ];
    for (const url of all) {
      expect(url.startsWith("https://isol8.co/chat")).toBe(true);
      expect(url).not.toContain("isol8.co/teams");
    }
  });

  it("builds list panel urls with the shell's ?panel= grammar", () => {
    expect(teamsPanelUrl("issues")).toBe("https://isol8.co/chat?panel=teams-issues");
    expect(teamsPanelUrl("routines")).toBe("https://isol8.co/chat?panel=teams-routines");
  });

  it("builds detail panel urls carrying detailId", () => {
    expect(teamsDetailUrl("issue", "iss_42", "issues")).toBe(
      "https://isol8.co/chat?panel=teams-issue-detail&detailId=iss_42",
    );
    expect(teamsDetailUrl("run", "run_7", "issues")).toBe(
      "https://isol8.co/chat?panel=teams-run-detail&detailId=run_7",
    );
  });

  it("falls back to the list panel when the id is missing", () => {
    // resolvePanel returns null for a teams-*-detail key with no detailId,
    // so emitting one without an id would produce a link that opens nothing.
    expect(teamsDetailUrl("run", undefined, "issues")).toBe(
      "https://isol8.co/chat?panel=teams-issues",
    );
    expect(teamsDetailUrl("issue", "", "issues")).toBe(
      "https://isol8.co/chat?panel=teams-issues",
    );
  });

  it("percent-encodes the id so it cannot break the query string", () => {
    expect(teamsDetailUrl("issue", "a b&panel=evil", "issues")).toBe(
      "https://isol8.co/chat?panel=teams-issue-detail&detailId=a%20b%26panel%3Devil",
    );
  });
});
