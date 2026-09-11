import { describe, expect, it } from "vitest";
import { linkifyMentions, parseMentionHref } from "../mentions";

const participants = [
  { kind: "agent" as const, id: "a1", displayName: "Friday", mentionHandle: "friday" },
  { kind: "human" as const, id: "u1", displayName: "Aden Kim", mentionHandle: null },
  { kind: "human" as const, id: "u2", displayName: "Aden", mentionHandle: null },
];

describe("linkifyMentions", () => {
  it("links agent handles and human display names, longest first", () => {
    expect(linkifyMentions("hey @friday and @Aden Kim, also @aden", [], participants))
      .toBe("hey [@friday](mention:agent:a1) and [@Aden Kim](mention:human:u1), also [@aden](mention:human:u2)");
  });
  it("leaves code spans and fenced blocks alone", () => {
    expect(linkifyMentions("`@friday` stays\n```\n@friday\n```\n@friday goes", [], participants))
      .toBe("`@friday` stays\n```\n@friday\n```\n[@friday](mention:agent:a1) goes");
  });
  it("does not link partial words or unknown handles", () => {
    expect(linkifyMentions("email@friday.ai @nobody @fridays", [], participants)).toBe("email@friday.ai @nobody @fridays");
  });
  it("links a structured mention even when the text token differs in case", () => {
    expect(linkifyMentions("@FRIDAY?", [{ kind: "agent", id: "a1" }], participants)).toBe("[@FRIDAY](mention:agent:a1)?");
  });
});

describe("parseMentionHref", () => {
  it("parses and rejects", () => {
    expect(parseMentionHref("mention:agent:a1")).toEqual({ kind: "agent", id: "a1" });
    expect(parseMentionHref("https://x")).toBeNull();
    expect(parseMentionHref("mention:robot:a1")).toBeNull();
  });
});
