// Two rows are dropped on a migrated workspace, for two reasons, through one flag:
//   - Sessions (M6-R54): the backend's session guard is a PRIVACY rule — a person sees only
//     their own personal sessions — and every Hermes session here is room- or event-scoped,
//     so the tab could only ever open an empty list.
//   - Channels (task 10-D, row 34): channel wiring is M5/LATER on Hermes.
// Both only on a DEFINITE "hidden": `"loading"` means "not known for this workspace yet", and
// hiding on it would shrink the list for an ECS owner mid-poll.
//
// index.tsx imports expo-router + @expo/ui at module scope; stub the router the same way the
// other control suites do. The list is asserted through `visiblePanels` rather than a render
// because @expo/ui's native rows produce no queryable text under jest.
jest.mock("expo-router", () => ({ router: { push: jest.fn() }, Href: {} }));

import type { AgentCoreGate } from "@lightbulb/shared";

import { visiblePanels } from "../index";

const keys = (gate: AgentCoreGate) => visiblePanels(gate).map((p) => p.key);

describe("control panel list", () => {
  it("drops Sessions and Channels on AgentCore, and nothing else", () => {
    expect(keys("hidden")).not.toContain("sessions");
    expect(keys("hidden")).not.toContain("channels");
    expect(keys("hidden")).toEqual(
      keys("shown").filter((k) => k !== "sessions" && k !== "channels"),
    );
  });

  it("keeps both on ECS", () => {
    expect(keys("shown")).toEqual(expect.arrayContaining(["sessions", "channels"]));
  });

  it("keeps both while the runtime is unknown", () => {
    expect(keys("loading")).toEqual(expect.arrayContaining(["sessions", "channels"]));
  });
});
