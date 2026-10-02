/**
 * These pin the out-to-web deep links. The bug they guard against actually
 * shipped: five Teams screens each carried their own copy of
 * `https://thelightbulb.company/teams`, which 404s — the route tree was removed when
 * Teams became a panel — so the "Manage on thelightbulb.company" button opened a dead
 * page for real users, and nothing in CI noticed.
 */

import { messageUrl, roomUrl, teamsPanelUrl, workspaceUrl } from "../webLinks";

describe("web deep links", () => {
  it("never points at the removed /teams route tree", () => {
    const all = [
      workspaceUrl(),
      teamsPanelUrl("issues"),
      teamsPanelUrl("routines"),
    ];
    for (const url of all) {
      expect(url.startsWith("https://thelightbulb.company/chat")).toBe(true);
      expect(url).not.toContain("thelightbulb.company/teams");
    }
  });

  it("builds list panel urls with the shell's ?panel= grammar", () => {
    expect(teamsPanelUrl("issues")).toBe("https://thelightbulb.company/chat?panel=teams-issues");
    expect(teamsPanelUrl("routines")).toBe("https://thelightbulb.company/chat?panel=teams-routines");
  });

  it("builds a workspace message permalink with the panel contract's keys", () => {
    expect(messageUrl("room-1", "m1")).toBe(
      "https://thelightbulb.company/chat?panel=chat&room=room-1&message=m1",
    );
  });

  it("percent-encodes ids so a crafted id cannot inject query keys", () => {
    expect(messageUrl("a&b=c", "m1")).toContain("room=a%26b%3Dc");
  });

  it("builds a bare room permalink, no message key", () => {
    expect(roomUrl("room-1")).toBe("https://thelightbulb.company/chat?panel=chat&room=room-1");
  });
});
