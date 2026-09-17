// overview.tsx imports expo-router / Clerk at module scope; stub them the
// same way usage.spend.test.tsx does — this suite only exercises the pure
// planLabel helper.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));
jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ getToken: jest.fn() }) }));

import { healthSessionCount, planLabel } from "../overview";

describe("planLabel", () => {
  it("maps known statuses, including the free tier", () => {
    expect(planLabel(null)).toBe("None");
    expect(planLabel("trialing")).toBe("Trial");
    expect(planLabel("trial_local")).toBe("Free");
    expect(planLabel("active")).toBe("Active");
  });

  it("renders unknown statuses verbatim", () => {
    expect(planLabel("past_due")).toBe("past_due");
  });
});

// M6 task 10-E: the Hermes bridge answers `health` with a plain session count,
// and `null` when no profile is restored yet — which is "not known", not zero.
describe("healthSessionCount", () => {
  it("reads both runtimes' shapes and keeps 'unknown' distinct from zero", () => {
    expect(healthSessionCount(4)).toBe(4);
    expect(healthSessionCount({ count: 3 })).toBe(3);
    expect(healthSessionCount(0)).toBe(0);
    expect(healthSessionCount(null)).toBeUndefined();
    expect(healthSessionCount(undefined)).toBeUndefined();
  });
});
