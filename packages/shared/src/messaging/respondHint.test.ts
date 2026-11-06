import { describe, expect, it } from "vitest";
import { canChangeListenMode, respondHint, soleChannelOwner, type RespondTarget } from "./respondHint";

const friday: RespondTarget = { kind: "agent", id: "agent-friday", displayName: "Friday", listenMode: "active" };
const gauge: RespondTarget = { kind: "agent", id: "agent-gauge", displayName: "Gauge", listenMode: "mentions" };
const atlas: RespondTarget = { kind: "agent", id: "agent-atlas", displayName: "Atlas", listenMode: "observe" };
const person: RespondTarget = { kind: "human", id: "user-aden", displayName: "Aden Kim", listenMode: null };

describe("respondHint", () => {
  it("renders the compact channel form", () => {
    expect(respondHint("channel", [person, friday, gauge, atlas], [], []))
      .toBe("Friday may reply · Gauge when mentioned · Atlas is off");
  });
  it("promotes a mentions agent the moment the draft mentions it", () => {
    expect(respondHint("channel", [gauge], [{ kind: "agent", id: "agent-gauge" }], [])).toBe("Gauge will reply");
  });
  it("treats a group DM as a channel, not a DM", () => {
    expect(respondHint("group-dm", [gauge], [], [])).toBe("Gauge when mentioned");
  });
  it("makes every agent reply in a DM regardless of listen mode", () => {
    expect(respondHint("dm", [gauge], [], [])).toBe("Gauge will reply");
  });
  it("never promises a reply from an Off agent, even mentioned in a DM", () => {
    expect(respondHint("dm", [atlas], [{ kind: "agent", id: "agent-atlas" }], [])).toBe("Atlas is off");
  });
  it("only promises a Listening agent's reply when the message addresses it", () => {
    expect(respondHint("channel", [friday], [], [])).toBe("Friday may reply");
    expect(respondHint("channel", [friday], [{ kind: "agent", id: "agent-friday" }], [])).toBe("Friday will reply");
    expect(respondHint("dm", [friday], [], [])).toBe("Friday will reply");
  });
  it("says a live run will be steered", () => {
    expect(respondHint("channel", [friday], [], ["agent-friday"])).toBe("Friday is working — sending now steers that run");
  });
  it("never offers to steer a run this message will not be dispatched to", () => {
    expect(respondHint("channel", [gauge], [], ["agent-gauge"])).toBe("Gauge when mentioned");
  });
  it("is empty when the room has no agents", () => {
    expect(respondHint("channel", [person], [], [])).toBe("");
  });
});

describe("canChangeListenMode", () => {
  const channel = { kind: "channel" as const };
  it.each([
    // [role, room, expected] — every plan: any member may change it
    ["member", channel, true],
    ["admin", channel, true],
    ["owner", channel, true],
    ["member", { kind: "group-dm" as const }, true],
    ["member", { kind: "channel" as const, shared: true }, false],
    ["admin", { kind: "channel" as const, shared: true }, true],
    ["owner", { kind: "dm" as const }, false],
    ["member", { kind: "dm" as const }, false],
  ] as const)("%s, %o -> %s", (role, room, expected) => {
    expect(canChangeListenMode(role, room)).toBe(expected);
  });
  it("keeps the web's room-less gate: any member, never an unknown viewer", () => {
    expect(canChangeListenMode("member")).toBe(true);
    expect(canChangeListenMode(undefined)).toBe(false);
  });
});

describe("soleChannelOwner", () => {
  const me = { kind: "human", id: "me", role: "owner" };
  const agent = { kind: "agent", id: "ceo", role: "member" };
  it("is true only for a channel's owner with no other person in it", () => {
    expect(soleChannelOwner({ kind: "channel", members: [me, agent] }, "me")).toBe(true);
    expect(soleChannelOwner({ kind: "channel", members: [me, { kind: "human", id: "you", role: "member" }] }, "me")).toBe(false);
    expect(soleChannelOwner({ kind: "channel", members: [{ ...me, role: "admin" }] }, "me")).toBe(false);
    expect(soleChannelOwner({ kind: "group-dm", members: [me] }, "me")).toBe(false);
    expect(soleChannelOwner(undefined, "me")).toBe(false);
  });
});

describe("respondHint with the super-agent on (one voice)", () => {
  const agent = (id: string, displayName: string, mentionHandle: string): RespondTarget =>
    ({ kind: "agent", id, displayName, listenMode: "active", mentionHandle });
  const seated = [agent("a1", "Growth", "growth"), agent("lb", "Lightbulb", "lightbulb"), agent("a2", "Finance", "finance")];
  it("only Lightbulb answers in a room where specialists are still seated, said plainly (W14)", () => {
    expect(respondHint("channel", seated, [], [], true)).toBe("Lightbulb reads every message here");
    // The same words in the Lightbulb DM and when @mentioned (W13: "may" in a project, "will" in the DM).
    expect(respondHint("dm", seated, [], [], true)).toBe("Lightbulb reads every message here");
    expect(respondHint("channel", seated, [{ kind: "agent", id: "lb" }], [], true)).toBe("Lightbulb reads every message here");
    expect(respondHint("channel", seated, [], [], false)).toBe("Growth may reply · Lightbulb may reply · Finance may reply");
  });
  it("with no @lightbulb seat on the row, it is still Lightbulb, never the specialists", () => {
    expect(respondHint("channel", [seated[0], seated[2]], [], [], true)).toBe("Lightbulb reads every message here");
    expect(respondHint("dm", [{ kind: "human", id: "h", displayName: "Ana", listenMode: null }], [], [], true)).toBe("");
  });
});
