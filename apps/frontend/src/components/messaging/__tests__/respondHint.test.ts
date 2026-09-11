import { describe, expect, it } from "vitest";
import { respondHint, type RespondTarget } from "../respondHint";

const friday: RespondTarget = { kind: "agent", id: "agent-friday", displayName: "Friday", listenMode: "active" };
const gauge: RespondTarget = { kind: "agent", id: "agent-gauge", displayName: "Gauge", listenMode: "mentions" };
const atlas: RespondTarget = { kind: "agent", id: "agent-atlas", displayName: "Atlas", listenMode: "observe" };
const person: RespondTarget = { kind: "human", id: "user-aden", displayName: "Aden Kim", listenMode: null };

describe("respondHint", () => {
  it("renders the compact channel form", () => {
    expect(respondHint("channel", [person, friday, gauge, atlas], [], []))
      .toBe("Friday will reply · Gauge when mentioned · Atlas observing");
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
  it("never promises a reply from an observe agent, even mentioned in a DM", () => {
    expect(respondHint("dm", [atlas], [{ kind: "agent", id: "agent-atlas" }], [])).toBe("Atlas observing");
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
