import { describe, expect, it } from "vitest";
import { activeTrigger, applyCompletion, linkifyMentions, linkifySessionMentions, parseMentionHref, resolveTypedMentions, resolveTypedSessionMentions, sessionMentionCandidates, sessionMentionHandle } from "./mentions";
import type { LocalSession } from "./types";

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

describe("activeTrigger", () => {
  it("detects a mention query with up to two internal spaces", () => {
    expect(activeTrigger("hi @Aden Kim", 12)).toEqual({ kind: "mention", start: 3, end: 12, query: "Aden Kim" });
    expect(activeTrigger("hi @", 4)).toEqual({ kind: "mention", start: 3, end: 4, query: "" });
  });
  it("detects an emoji query only after two characters", () => {
    expect(activeTrigger("nice :ta", 8)).toEqual({ kind: "emoji", start: 5, end: 8, query: "ta" });
    expect(activeTrigger("nice :t", 7)).toBeNull();
  });
  it("returns null without a caret and for an email-shaped token", () => {
    expect(activeTrigger("hi @friday", null)).toBeNull();
    expect(activeTrigger("mail@friday", 11)).toBeNull();
  });
});

describe("applyCompletion", () => {
  it("replaces the token and leaves exactly one trailing space", () => {
    const trigger = activeTrigger("hi @fri", 7)!;
    expect(applyCompletion("hi @fri", trigger, "@friday")).toEqual({ text: "hi @friday ", caret: 11 });
    const spaced = activeTrigger("hi @fri there", 7)!;
    expect(applyCompletion("hi @fri there", spaced, "@friday")).toEqual({ text: "hi @friday there", caret: 10 });
  });
});

describe("resolveTypedMentions parity", () => {
  it("returns each member once regardless of how many tokens name them", () => {
    expect(resolveTypedMentions("@friday @friday @Aden Kim", participants))
      .toEqual([{ kind: "agent", id: "a1" }, { kind: "human", id: "u1" }]);
  });
  it("returns an empty list when the room has no addressable members", () => {
    expect(resolveTypedMentions("@friday", [])).toEqual([]);
  });
});

function session(changes: Partial<LocalSession> = {}): LocalSession {
  return {
    sessionId: "s1", deviceId: "d1", humanUserId: "u1", provider: "claude", generation: "g1",
    status: "idle", providerVersion: "1.0", controlUnavailableReason: null, activeTurnId: null,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
    latestEventSeq: 0, cwdName: "lightbulb", ...changes,
  };
}

describe("session mentions", () => {
  it("slugifies the cwd and keeps each handle bound to session identity", () => {
    expect(sessionMentionHandle(session({ cwdName: "My Repo" }))).toBe("claude-my-repo");
    expect(sessionMentionHandle(session({ cwdName: null }))).toBe("claude-session");
    const candidates = sessionMentionCandidates([
      session({ sessionId: "a" }), session({ sessionId: "b" }),
      session({ sessionId: "c", cwdName: "lightbulb-2" }),
    ]);
    expect(candidates.map(({ handle }) => handle)).toEqual([
      "claude-lightbulb-a", "claude-lightbulb-b", "claude-lightbulb-2-c",
    ]);
    expect(sessionMentionCandidates([candidates[1].session, candidates[0].session]).map(({ handle }) => handle))
      .toEqual(["claude-lightbulb-b", "claude-lightbulb-a"]);
  });

  it("resolves the longest plain token once and ignores code", () => {
    const sessions = [session(), session({ sessionId: "s2", cwdName: "lightbulb-2" })];
    expect(resolveTypedSessionMentions("`@claude-lightbulb-s1` @claude-lightbulb-2-s2", sessions)).toEqual(["s2"]);
    expect(resolveTypedSessionMentions("@claude-lightbulb-s1 @claude-lightbulb-s1", sessions)).toEqual(["s1"]);
    expect(resolveTypedSessionMentions("email@claude-lightbulb-s1.ai", sessions)).toEqual([]);
    const withEndedFirst = sessionMentionCandidates([session({ sessionId: "ended", status: "ended" }), session({ sessionId: "live" })]);
    expect(resolveTypedSessionMentions("@claude-lightbulb-live", withEndedFirst.slice(1))).toEqual(["live"]);
  });

  it("renders tagged prose as a session chip and leaves code alone", () => {
    expect(linkifySessionMentions("`@claude-lightbulb-s1` @claude-lightbulb-s1", [{ sessionId: "s1", requestId: "r1" }]))
      .toBe("`@claude-lightbulb-s1` [@claude-lightbulb-s1](mention:session:s1)");
    expect(linkifySessionMentions("@claude-lightbulb-s1", [{ sessionId: "s2", requestId: "r2" }]))
      .toBe("@claude-lightbulb-s1");
    expect(parseMentionHref("mention:session:s1")).toEqual({ kind: "session", id: "s1" });
  });
});
