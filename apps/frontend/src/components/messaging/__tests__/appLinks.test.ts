import { describe, expect, it } from "vitest";
import { taskIdentifiers } from "../MessageTaskCard";
import { appLinkTarget, extractAppLinks } from "../appLinks";

const ORIGIN = "https://thelightbulb.company";

describe("appLinkTarget", () => {
  it("maps each own-app URL to the native card it should render", () => {
    const kind = (url: string) => {
      const link = appLinkTarget(url, ORIGIN);
      return link && { ...link };
    };
    expect(kind("https://company.thelightbulb.company/ISOA/issues/ISOA-400")).toEqual({ kind: "issue", id: "ISOA-400", href: "/chat?panel=teams-issue-detail&detailId=ISOA-400" });
    expect(kind("https://dev.company.thelightbulb.company/ISOA/issues/ISOA-4#document-plan")).toMatchObject({ kind: "document" });
    expect(kind("https://thelightbulb.company/ISOA/approvals/ap-1")).toEqual({ kind: "approval", id: "ap-1", href: "/chat?panel=teams-approval-detail&detailId=ap-1" });
    expect(kind("https://thelightbulb.company/ISOA/agents/cto")).toEqual({ kind: "agent", id: "cto", href: "/chat?panel=agent-inspector&agent=cto" });
    expect(kind("https://dev.thelightbulb.company/chat?panel=chat&room=r1&message=m1")).toMatchObject({ kind: "message", roomId: "r1", messageId: "m1" });
    expect(kind("https://thelightbulb.company/chat?panel=chat&room=r1")).toMatchObject({ kind: "room", roomId: "r1" });
    expect(kind("https://thelightbulb.company/chat?panel=session&session=s1")).toMatchObject({ kind: "session", sessionId: "s1" });
    expect(kind("https://thelightbulb.company/chat?panel=connections&provider=github")).toEqual({ kind: "connect", provider: "github", href: "/chat?panel=connections&provider=github" });
    expect(kind("https://thelightbulb.company/chat?panel=invite")).toEqual({ kind: "invite", href: "/chat?panel=invite" });
    expect(kind("https://thelightbulb.company/pricing")).toEqual({ kind: "other", href: "https://thelightbulb.company/pricing" });
  });

  it("leaves other sites alone", () => {
    expect(appLinkTarget("https://notthelightbulb.company/ISOA/issues/ISOA-1", ORIGIN)).toBeNull();
    expect(appLinkTarget("https://example.com/chat?panel=chat&room=r1", ORIGIN)).toBeNull();
  });
});

describe("own-app links in a message", () => {
  it("dedupes by target and shows a linked task once, even when also named in prose", () => {
    const text = "Board task: ISOA-400 https://company.thelightbulb.company/ISOA/issues/ISOA-400 and https://thelightbulb.company/ISOA/issues/ISOA-400.";
    expect(extractAppLinks(text)).toHaveLength(1);
    expect(taskIdentifiers(text)).toEqual(["ISOA-400"]);
    // A human's prose ids stay text; their issue links still chip.
    expect(taskIdentifiers("COVID-19 see https://thelightbulb.company/ISOA/issues/ISOA-9", false)).toEqual(["ISOA-9"]);
  });
});
