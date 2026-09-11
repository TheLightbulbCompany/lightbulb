import { describe, expect, it } from "vitest";
import { linkifyMentions, parseMentionHref, resolveTypedMentions } from "../mentions";

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
  it("links a human by their handle and still by their display name", () => {
    const withHandle = [{ kind: "human" as const, id: "u1", displayName: "Aden Kim", mentionHandle: "aden-kim" }];
    expect(linkifyMentions("@aden-kim and @Aden Kim", [], withHandle))
      .toBe("[@aden-kim](mention:human:u1) and [@Aden Kim](mention:human:u1)");
    expect(resolveTypedMentions("@aden-kim", withHandle)).toEqual([{ kind: "human", id: "u1" }]);
  });
  it("prefers the handle owner when another member's display name is the same token", () => {
    // The display-name holder is listed first, so only a handle-wins tie-break
    // keeps @aden pointing at the person whose handle it actually is.
    const ambiguous = [
      { kind: "human" as const, id: "u-name", displayName: "Aden", mentionHandle: "zed" },
      { kind: "human" as const, id: "u-handle", displayName: "Aden Kim", mentionHandle: "aden" },
    ];
    expect(linkifyMentions("@aden", [], ambiguous)).toBe("[@aden](mention:human:u-handle)");
    expect(resolveTypedMentions("@aden", ambiguous)).toEqual([{ kind: "human", id: "u-handle" }]);
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

describe("resolveTypedMentions", () => {
  const participants = [
    { kind: "agent" as const, id: "agent-friday", displayName: "Friday", mentionHandle: "friday" },
    { kind: "human" as const, id: "user-aden", displayName: "Aden Kim", mentionHandle: null },
  ];
  it("resolves a typed agent handle and a typed human display name", () => {
    expect(resolveTypedMentions("@friday can you ping @Aden Kim", participants)).toEqual([
      { kind: "agent", id: "agent-friday" },
      { kind: "human", id: "user-aden" },
    ]);
  });
  it("is case-insensitive and de-duplicates", () => {
    expect(resolveTypedMentions("@Friday @friday", participants)).toEqual([{ kind: "agent", id: "agent-friday" }]);
  });
  it("ignores tokens inside code spans and fences, and unknown tokens", () => {
    expect(resolveTypedMentions("`@friday` and ```\n@friday\n``` and @nobody", participants)).toEqual([]);
  });
  it("returns an empty list when the room has no mentionable members", () => {
    expect(resolveTypedMentions("@friday", [])).toEqual([]);
  });
});
