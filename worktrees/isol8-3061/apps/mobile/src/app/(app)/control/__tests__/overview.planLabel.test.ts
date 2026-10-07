// overview.tsx imports expo-router / Clerk at module scope; stub them the
// same way usage.spend.test.tsx does — this suite only exercises the pure
// planLabel helper.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));
jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ getToken: jest.fn() }) }));

import { planLabel } from "../overview";

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
