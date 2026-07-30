// extractFailedRuns imports teamsData, which imports expo-router
// (useFocusEffect) at module level; stub it like the other teamsData tests.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));

import { extractFailedRuns } from "../teamsData";

// GET /teams/inbox/runs is forwarded upstream-VERBATIM by the BFF (no
// envelope_list), so all three shapes web's useInboxTriage hedges against
// must extract — a one-shape reader silently renders the band empty forever.
describe("extractFailedRuns", () => {
  const failed = { id: "run_1", status: "failed" };
  const running = { id: "run_2", status: "running" };

  it("reads a {runs: […]} envelope", () => {
    expect(extractFailedRuns({ runs: [failed, running] })).toEqual([failed]);
  });

  it("reads an {items: […]} envelope", () => {
    expect(extractFailedRuns({ items: [failed] })).toEqual([failed]);
  });

  it("reads a bare array", () => {
    expect(extractFailedRuns([failed, running])).toEqual([failed]);
  });

  it("returns [] for junk shapes", () => {
    expect(extractFailedRuns(null)).toEqual([]);
    expect(extractFailedRuns({ nope: true })).toEqual([]);
    expect(extractFailedRuns("string")).toEqual([]);
  });
});
