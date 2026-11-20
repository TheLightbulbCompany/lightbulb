import { describe, expect, it } from "vitest";
import {
  agentLine, agentLiveStatus, agentWorking, floorHolder, liveJoinErrorCode, liveMarker,
  agentHand, agentStateLine, handKey, meetVoiceLimitText, voiceLimit, parseLiveNotice, renamedText, voiceNoteText,
} from "./words";

describe("voice words", () => {
  it("names who is live", () => {
    expect(liveMarker(["Aden"])).toBe("Live — Aden");
    expect(liveMarker(["Aden", "Leo"])).toBe("Live — Aden and Leo");
    expect(liveMarker(["Aden", "Leo", "Sam"])).toBe("Live — Aden and 2 others");
  });

  it("maps join failures", () => {
    expect(liveJoinErrorCode({ name: "NotAllowedError" })).toBe("mic_denied");
    expect(liveJoinErrorCode({ status: 503 })).toBe("voice_unavailable");
    expect(liveJoinErrorCode({ name: "NotFoundError" })).toBe("no_mic");
    expect(liveJoinErrorCode({ status: 404, body: { code: "ROOM_NOT_FOUND" } })).toBe("room_not_found");
    expect(liveJoinErrorCode(new Error("x"))).toBe("connection_lost");
  });
});

const st = (id: string) => `lightbulb.agent.${id}.status`;

describe("Meet voice limit", () => {
  it("reads the attribute, trimmed; empty or absent is null", () => {
    expect(voiceLimit({ "lightbulb.voice_limit": " credits_low " })).toBe("credits_low");
    expect(voiceLimit({ "lightbulb.voice_limit": "" })).toBeNull();
    expect(voiceLimit({})).toBeNull();
    expect(voiceLimit(undefined)).toBeNull();
  });
  it("words each reason for people who cannot tap", () => {
    expect(meetVoiceLimitText("free_minutes_used")).toBe("Today's free voice minutes are used up. The agents can speak again tomorrow.");
    expect(meetVoiceLimitText("credits_low")).toBe("Voice is paused: the workspace's credits are low.");
    expect(meetVoiceLimitText("plan_inactive")).toBe("Voice is off: the workspace's plan is not active.");
    expect(meetVoiceLimitText("whatever")).toBe("The agents cannot speak right now.");
  });
});

describe("per-agent Live readers", () => {
  it("reads status and the floor-holder", () => {
    const a = { "lightbulb.speaking_agent": "g", [st("g")]: "listening" };
    expect(agentLiveStatus(a, "g")).toBe("listening");
    expect(floorHolder(a)).toBe("g");
    expect(agentLiveStatus({ [st("g")]: "bogus" }, "g")).toBe("idle");
    expect(agentLiveStatus(undefined, "g")).toBe("idle");
    expect(floorHolder({ "lightbulb.speaking_agent": "" })).toBeNull();
    expect(agentWorking({ "lightbulb.agent.g.working": `  ${"x".repeat(80)} ` }, "g")).toHaveLength(60);
  });

  it.each([
    ["speaking", {}, "Gauge — speaking"],
    ["joining", {}, "Gauge is joining…"],
    ["listening", { listeningTo: "Aden" }, "Gauge — listening to Aden"],
    ["working", { working: "pulling Q3 revenue" }, "Gauge — working: pulling Q3 revenue"],
    ["has_news", {}, "Gauge has something — tap to hear it"],
    ["idle", {}, "Gauge"],
  ] as const)("agentLine(%s)", (status, x, text) => {
    expect(agentLine("Gauge", status, x)).toBe(text);
  });

  it("parses live notices", () => {
    expect(parseLiveNotice(JSON.stringify({ kind: "renamed", agentId: "a", from: "CEO", to: "Nova", by: "Aden", collides: false })))
      .toEqual({ kind: "renamed", agentId: "a", from: "CEO", to: "Nova", by: "Aden", collides: false });
    const saved = parseLiveNotice(JSON.stringify({ kind: "voice_note_saved", agentId: "a", name: "Nova", text: "x".repeat(1200), by: "Aden" }));
    expect(saved?.kind === "voice_note_saved" && saved.text.length).toBe(1000);
    expect(parseLiveNotice('{"kind":"ambiguous_name","agentId":"a"}')).toEqual({ kind: "ambiguous_name", agentId: "a" });
    expect(parseLiveNotice('{"kind":"voice_failed","agentId":"a"}')).toEqual({ kind: "voice_failed", agentId: "a" });
    expect(parseLiveNotice('{"kind":"renamed","agentId":"a"}')).toBeNull();
    expect(parseLiveNotice('{"kind":"nope","agentId":"a"}')).toBeNull();
    expect(parseLiveNotice('{"kind":"__proto__","agentId":"a"}')).toBeNull();
    expect(parseLiveNotice('{"kind":"voice_failed","agentId":"__proto__"}')).toBeNull();
    expect(parseLiveNotice("not json")).toBeNull();
    expect(parseLiveNotice('{"kind":"voice_failed","agentId":7}')).toBeNull();
  });

  it("words notices", () => {
    expect(renamedText("CEO", "Nova")).toBe("CEO is now Nova");
    expect(voiceNoteText("Nova", "be more blunt")).toBe("Nova will remember: be more blunt");
  });
});

describe("has_news wording", () => {
  it("Live bar keeps its tap wording, with the topic when there is one", () => {
    expect(agentStateLine("has_news")).toBe("Has something — tap to hear");
    expect(agentStateLine("has_news", { hand: "what PRs are open" })).toBe("Ready: what PRs are open — tap to hear");
    expect(agentLine("Gauge", "has_news")).toBe("Gauge has something — tap to hear it");
    expect(agentLine("Gauge", "has_news", { hand: "what PRs are open" })).toBe("Gauge is ready: what PRs are open — tap to hear it");
  });
  it("meeting wording never says tap", () => {
    expect(agentStateLine("has_news", { meeting: true })).toBe("Hand up: has something for you");
    expect(agentStateLine("has_news", { meeting: true, hand: "the Q3 numbers" })).toBe("Hand up: the Q3 numbers");
    expect(agentLine("Gauge", "has_news", { meeting: true, hand: "x" })).toBe("Gauge — hand up: x");
  });
  it("agentHand trims, caps at 60 and reads absent as empty", () => {
    expect(agentHand({ [handKey("a")]: "  hi  " }, "a")).toBe("hi");
    expect(agentHand({ [handKey("a")]: "x".repeat(80) }, "a")).toHaveLength(60);
    expect(agentHand({}, "a")).toBe("");
  });
});

import { usPhone } from "./words";

describe("usPhone", () => {
  it("formats +1 numbers and leaves anything else alone", () => {
    expect(usPhone("+18566995838")).toBe("(856) 699-5838");
    expect(usPhone("+442071234567")).toBe("+442071234567");
    expect(usPhone("(856) 699-5838")).toBe("(856) 699-5838");
  });
});
