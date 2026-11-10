import { describe, expect, it } from "vitest";
import {
  agentA11yName, agentLine, agentLiveStatus, agentWorking, errorText, floorChange, floorHolder,
  liveAgents, liveErrorText, liveHappening, liveHint, liveJoinErrorCode, liveMarker, liveQueue,
  agentHand, agentStateLine, handKey, meetVoiceLimitText, voiceLimit, nameCollides, orderAgentSlots, parseLiveNotice, queuePosition, renamedText, voiceFailedText, voiceNoteText, wakingText,
} from "./words";

describe("voice words", () => {
  it("falls back for unknown and prototype keys", () => {
    expect(errorText("connection_lost", "x")).toBe("The call dropped.");
    expect(errorText("__proto__", "x")).toBe("x");
    expect(errorText(undefined, "x")).toBe("x");
  });

  it("reads voice_limit's reason", () => {
    expect(liveErrorText({ code: "voice_limit", reason: "credits_low" })).toBe("Add credits to keep talking to agents.");
    expect(errorText("free_minutes_used", "x")).toBe("You've used today's free voice minutes. People can keep talking.");
    expect(errorText("agents_absent", "x")).toBe("Agents couldn't join this Live.");
    expect(errorText("lightbulb_unreachable", "x")).toBe("Couldn't reach the agent — try again.");
    expect(liveErrorText({ code: "nope" })).toBe("Live couldn't start.");
  });

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
const names: Record<string, string> = { g: "Gauge", f: "Forge", n: "Nova", gauge: "Gauge", forge: "Forge" };
const nameOf = (id: string) => names[id] ?? id;

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

  it("reads the queue", () => {
    expect(liveQueue({ "lightbulb.queue": "f, g,,f" })).toEqual(["f", "g"]);
    const a = { "lightbulb.queue": "forge,gauge", "lightbulb.speaking_agent": "forge", [st("forge")]: "joining" };
    expect(queuePosition(a, "gauge")).toBe(2);
    expect(queuePosition(a, "forge")).toBeNull();
    expect(queuePosition(a, "nova")).toBeNull();
  });

  it("orders slots", () => {
    const a = { [st("c")]: "speaking", [st("d")]: "joining", "lightbulb.queue": "c,b" };
    expect(orderAgentSlots(["a", "b", "c", "d"], a)).toEqual(["c", "d", "b", "a"]);
  });

  it("tiles every agent whatever its listen mode and drops humans", () => {
    const m = (id: string, kind: string, listenMode: string | null) => ({ id, kind, displayName: id, listenMode });
    const tiles = liveAgents([m("a", "agent", "active"), m("h", "user", null), m("o", "agent", "observe"), m("b", "agent", "mentions")]);
    expect(tiles.map((t) => t.id)).toEqual(["a", "o", "b"]);
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

  it("builds a11y names", () => {
    expect(agentA11yName("Gauge", "speaking")).toBe("Gauge, agent, speaking");
    expect(agentA11yName("Forge", "working", { queuePos: 2, working: "drafting the launch post" }))
      .toBe("Forge, agent, next, working: drafting the launch post");
  });

  it("says what's happening", () => {
    const a = { "lightbulb.queue": "gauge,forge", "lightbulb.speaking_agent": "gauge", [st("gauge")]: "speaking" };
    expect(liveHappening(a, nameOf)).toBe("Gauge is speaking · Next: Forge");
    expect(liveHappening({ ...a, [st("n")]: "working", [st("x")]: "working" }, nameOf)).toBe("Gauge is speaking · Next: Forge · 2 working");
    expect(liveHappening({ [st("g")]: "idle" }, nameOf)).toBe("");
  });

  it("announces floor changes only", () => {
    const prev = { "lightbulb.speaking_agent": "f", [st("f")]: "speaking" };
    const next = { "lightbulb.speaking_agent": "g", [st("g")]: "speaking" };
    expect(floorChange(prev, next, ["f", "g"], ["f", "g"], nameOf)).toBe("Gauge is speaking");
    expect(floorChange(prev, { ...next, [st("g")]: "joining" }, ["f", "g"], ["f", "g"], nameOf)).toBe("Gauge is joining");
    expect(floorChange(prev, { ...prev, "lightbulb.agent.f.working": "x" }, ["f"], ["f"], nameOf)).toBeNull();
    expect(floorChange(prev, prev, ["f", "n"], ["f"], nameOf)).toBe("Nova left");
    const joiningG = { ...next, [st("g")]: "joining" };
    expect(floorChange(joiningG, { ...next, [st("g")]: "listening" }, ["g"], ["g"], nameOf)).toBe("Gauge is listening");
  });

  it("hints how to talk", () => {
    expect(liveHint([{ name: "Gauge", collides: false }])).toBe('Say "Gauge, …" or tap Gauge to talk.');
    expect(liveHint([{ name: "Nova", collides: true }])).toBe('Say "hey Nova, …" or tap Nova to talk.');
    expect(liveHint([{ name: "Gauge", collides: false }, { name: "Forge", collides: false }, { name: "Nova", collides: false }]))
      .toBe('Say an agent\'s name — "Gauge, …" — or tap them. "Lightbulb, …" asks the team.');
    expect(liveHint([])).toBeNull();
    expect(nameCollides("Nova", [{ displayName: "Nova Smith" }])).toBe(true);
    expect(nameCollides("nova", [{ displayName: "Novak" }])).toBe(false);
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
    expect(wakingText("Nova")).toBe("Nova is waking up — this can take a minute the first time.");
    expect(voiceFailedText("Nova")).toBe("Nova couldn't join. Try again.");
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
