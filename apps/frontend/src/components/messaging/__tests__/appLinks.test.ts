import { describe, expect, it } from "vitest";
import { appLinkTarget, extractAppLinks } from "../appLinks";

const ORIGIN = "https://thelightbulb.company";

describe("appLinkTarget", () => {
  it("maps each own-app URL to the native card it should render", () => {
    const kind = (url: string) => {
      const link = appLinkTarget(url, ORIGIN);
      return link && { ...link };
    };
    // The Paperclip task and approval pages are gone: their links open Activity.
    expect(kind("https://company.thelightbulb.company/ISOA/issues/ISOA-400")).toEqual({ kind: "other", href: "/chat?panel=activity" });
    expect(kind("https://dev.company.thelightbulb.company/ISOA/issues/ISOA-4#document-plan")).toMatchObject({ kind: "document" });
    expect(kind("https://thelightbulb.company/ISOA/approvals/ap-1")).toEqual({ kind: "other", href: "/chat?panel=activity" });
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
  it("dedupes by target", () => {
    const text = "Board task: ISOA-400 https://company.thelightbulb.company/ISOA/issues/ISOA-400 and https://thelightbulb.company/ISOA/issues/ISOA-400.";
    expect(extractAppLinks(text)).toHaveLength(1);
  });
});
